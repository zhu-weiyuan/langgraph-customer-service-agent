# -*- coding: utf-8 -*-
"""
Hybrid RAG — 双路召回 + RRF 融合 + 重排 + Parent-Child 分块（RAG 升级模块）

设计原则（与现有代码零耦合，只新增不修改）：
  * 纯注入式：dense 路（vector_search_fn）与 sparse 路（keyword_search_fn）
    均为可注入函数；默认 sparse 路适配现有 agent/rag.py 的 BM25/TF-IDF 检索器。
  * 全部三方依赖守卫导入（jieba / sentence_transformers 等），
    核心逻辑（RRF、分块、规则重排、查询改写降级路径）为纯 stdlib，可独立单测。
  * 分层召回：recall_top_k=50（融合后粗召回）→ rerank_top_n=8（重排截断）
    → context_top_n=5（最终注入上下文）。
  * Parent-Child：child（~300 字）检索命中 → 映射回 parent（~1200 字）输出，
    同 parent 多 child 命中去重、分数取最大。
  * 输出统一结构 [{title, content, score, source, parent_id}]，
    并附带 text=content 别名以兼容 nodes/context_assembler 既有消费字段。

用法（见 RAG_UPGRADE.md）：
    from agent.hybrid_rag import HybridRetriever
    retriever = HybridRetriever(vector_search_fn=..., keyword_search_fn=...)
    hits = retriever.search("咋连WiFi", tenant_id="acme")
"""

from __future__ import annotations

import inspect
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

RRF_K = 60                 # 标准 RRF 常数（本模块默认；rag.py 内部用 40，互不影响）
RECALL_TOP_K = 50          # 双路融合后粗召回条数
RERANK_TOP_N = 8           # 重排后保留条数
CONTEXT_TOP_N = 5          # 最终注入上下文条数

CHILD_SIZE = 300           # child 块目标字符数
PARENT_SIZE = 1200         # parent 块目标字符数

_SYNONYMS_PATH = Path(__file__).parent / "synonyms.json"

# 疑问词/口语填充词 —— 规则改写时剥离（按长度降序整词替换，避免误伤单字）
_QUESTION_WORDS = [
    "请问一下", "能不能", "有没有", "是不是", "怎么办", "怎么样",
    "请问", "怎么", "如何", "为什么", "为啥", "什么", "哪里", "哪个",
    "咋整", "帮我", "我想", "我要", "一下", "可以", "能够",
    "吗", "呢", "吧", "啊", "呀", "哦", "？", "?",
]


# ---------------------------------------------------------------------------
# 纯函数：RRF 融合
# ---------------------------------------------------------------------------

def _result_key(result: Dict[str, Any]) -> Any:
    """结果去重主键：优先显式 id / parent_id+title，退化为 (title, source)。"""
    if result.get("id") is not None:
        return ("id", result["id"])
    return ("ts", result.get("title", ""), result.get("source", ""))


def rrf_fuse(result_lists: Sequence[Sequence[Dict[str, Any]]],
             k: int = RRF_K) -> List[Dict[str, Any]]:
    """Reciprocal Rank Fusion（纯函数，可独测）。

    score(d) = Σ_lists 1 / (k + rank_d)，rank 从 1 开始。

    Args:
        result_lists: 多个已排序结果列表（每条为 dict，键见 _result_key）
        k: RRF 常数（默认 60）

    Returns:
        融合排序后的结果列表；每条附带 "rrf_score"，并保留首次出现的原始字段，
        原始 "score" 保留各路最大值于 "orig_score"。
    """
    fused: Dict[Any, Dict[str, Any]] = {}
    for results in result_lists:
        for rank, result in enumerate(results, start=1):
            key = _result_key(result)
            contribution = 1.0 / (k + rank)
            if key not in fused:
                entry = dict(result)
                entry["rrf_score"] = contribution
                entry["orig_score"] = float(result.get("score", 0.0) or 0.0)
                fused[key] = entry
            else:
                fused[key]["rrf_score"] += contribution
                fused[key]["orig_score"] = max(
                    fused[key]["orig_score"], float(result.get("score", 0.0) or 0.0))
    ordered = sorted(fused.values(), key=lambda x: x["rrf_score"], reverse=True)
    for entry in ordered:
        entry["score"] = round(entry["rrf_score"], 6)
    return ordered


# ---------------------------------------------------------------------------
# 纯函数：Parent-Child 分块
# ---------------------------------------------------------------------------

def chunk_document(text: str,
                   child_size: int = CHILD_SIZE,
                   parent_size: int = PARENT_SIZE,
                   doc_id: str = "doc") -> Dict[str, Any]:
    """将文档切成 parent 与 child 两级块。

    **小节边界为主，parent 扩展相邻小节**：
      * child：按 markdown 小节切（≤child_size 字，超长首尾切），
        section 唯一 = 所属小节标题（与知识库 heading 一致，检索/引用/评测口径统一）。
      * parent：= 该小节 + 紧邻下一小节（上下文扩展，供 LLM 补充背景），
        主 section = 首个小节标题。不跨多个小节 → 上下文不污染。
      * 无 heading → 整篇一个小节正常切。

    纯函数。长度切分优先在段落/句子边界（\n\n > \n > 。！？!? > 硬切）。

    Returns:
        {
          "parents": {parent_id: {"parent_id", "text", "start", "section"}},
          "children": [{"child_id", "parent_id", "text", "start", "section"}],
        }
    """
    if child_size <= 0 or parent_size <= 0:
        raise ValueError("child_size/parent_size must be positive")
    if parent_size < child_size:
        parent_size = child_size

    parents: Dict[str, Dict[str, Any]] = {}
    children: List[Dict[str, Any]] = []

    secs = _markdown_sections(text)
    p_idx = 0
    for idx, sec in enumerate(secs):
        sec_text = sec.get("text", "") or ""
        sec_title = sec.get("title", "") or ""
        if not sec_text.strip():
            continue

        # parent = 该小节 + 紧邻下一非空小节（上下文扩展，不跨太多小节）
        parent_id = f"{doc_id}:p{p_idx}"
        cur = sec_text
        nxt = ""
        for nxt_sec in secs[idx + 1:]:
            nxt_t = nxt_sec.get("text", "") or ""
            if nxt_t.strip():
                nxt = nxt_t
                break
        p_text = f"{cur}\n\n{nxt}" if nxt else cur
        parents[parent_id] = {
            "parent_id": parent_id, "text": p_text,
            "start": sec.get("start", 0),
            "section": sec_title,
        }
        for c_idx, (c_start, c_text) in enumerate(_split_spans(sec_text, child_size)):
            children.append({
                "child_id": f"{parent_id}:c{c_idx}",
                "parent_id": parent_id,
                "text": c_text,
                "start": sec.get("start", 0) + c_start,
                "section": sec_title,
            })
        p_idx += 1
    return {"parents": parents, "children": children}


def _markdown_sections(text: str) -> List[Dict[str, Any]]:
    """按 markdown heading 切小节（复用 knowledge_sections 的纯解析）。

    无 heading 时整篇作为一个小节。返回 [{title, text, start}]，title 为
    heading 文本（去井号）；heading 前的引言归 "（文档引言）"。
    """
    try:
        from .knowledge_sections import parse_markdown_sections
        parsed = parse_markdown_sections(text)
        if parsed:
            out = []
            for s in parsed:
                title = s.get("title", "") or ""
                body = s.get("text", "") or ""
                # heading 行本身不在 body 里：拼回标题，让小节标题文本也参与检索
                if title and title != "（文档引言）" and not body.startswith(title):
                    body = f"{title}\n\n{body}"
                out.append({"title": title, "text": body,
                            "start": int(s.get("start", 0))})
            return out
    except Exception:  # noqa: BLE001 — 解析失败降级为整篇一个小节
        pass
    stripped = text.strip()
    return [{"title": "（文档引言）", "text": stripped, "start": 0}] if stripped else []


def _split_spans(text: str, size: int) -> List[tuple]:
    """按目标长度切分文本，尽量落在段落/句子边界。返回 [(start, chunk_text)]。"""
    spans: List[tuple] = []
    pos = 0
    n = len(text)
    while pos < n:
        remaining = n - pos
        if remaining <= size:
            chunk = text[pos:]
            if chunk.strip():
                spans.append((pos, chunk))
            break
        window = text[pos:pos + size]
        cut = _best_cut(window)
        chunk = text[pos:pos + cut]
        if chunk.strip():
            spans.append((pos, chunk))
        pos += cut
    return spans


def _best_cut(window: str) -> int:
    """在窗口内找最佳切点（优先后半段的段落/换行/句末标点），无则硬切。"""
    half = len(window) // 2
    for pattern in ("\n\n", "\n"):
        idx = window.rfind(pattern)
        if idx >= half:
            return idx + len(pattern)
    best = -1
    for mark in "。！？!?；;":
        idx = window.rfind(mark)
        if idx > best:
            best = idx
    if best >= half:
        return best + 1
    return len(window)


def map_children_to_parents(child_hits: Sequence[Dict[str, Any]],
                            parent_map: Dict[str, Dict[str, Any]],
                            top_n: Optional[int] = None) -> List[Dict[str, Any]]:
    """child 命中映射回 parent 输出，同 parent 去重（分数取最大，保序）。纯函数。

    child_hits 每条需含 parent_id；无 parent_id 或 parent 缺失则原样透传。

    输出条目额外携带 child_ids：该 parent 下所有命中 child 的 id（保序去重，
    首个为 id 字段）。这样 parent 合并后，检索层判定（ChunkHit）仍能识别
    同 parent 下被"代表"掉的兄弟 child——内容确实被召回，只是 id 被合并。
    """
    seen: Dict[Any, int] = {}
    out: List[Dict[str, Any]] = []
    for hit in child_hits:
        pid = hit.get("parent_id")
        parent = parent_map.get(pid) if pid else None
        if parent is None:
            key = ("orphan", _result_key(hit))
            if key in seen:
                idx = seen[key]
                out[idx]["score"] = max(out[idx].get("score", 0.0), hit.get("score", 0.0))
                continue
            seen[key] = len(out)
            merged = dict(hit)
            merged["child_ids"] = [hit.get("id")] if hit.get("id") else []
            out.append(merged)
            continue
        if pid in seen:
            idx = seen[pid]
            cid = hit.get("id")
            if cid and cid not in out[idx].setdefault("child_ids", []):
                out[idx]["child_ids"].append(cid)
            out[idx]["score"] = max(out[idx].get("score", 0.0),
                                    float(hit.get("score", 0.0) or 0.0))
            continue
        merged = dict(hit)
        merged["content"] = parent.get("text", parent.get("content", ""))
        merged["text"] = merged["content"]
        merged["parent_id"] = pid
        merged["title"] = hit.get("title") or parent.get("title", pid)
        merged["section"] = hit.get("section") or parent.get("section", "")
        merged["child_ids"] = [hit.get("id")] if hit.get("id") else []
        seen[pid] = len(out)
        out.append(merged)
    if top_n is not None:
        out = out[:top_n]
    return out


# ---------------------------------------------------------------------------
# QueryRewriter — 多变体查询改写（LLM 注入，规则降级）
# ---------------------------------------------------------------------------

_DEFAULT_SYNONYMS = {
    "咋整": ["怎么办"], "咋连": ["怎么连接"], "咋退": ["怎么退货"],
    "WiFi": ["无线网络"], "Wi-Fi": ["无线网络"],
    "没声": ["没声音"], "断网": ["网络连接失败"], "离线": ["设备离线"],
    "退货": ["退换货"], "退款": ["退换货"], "保修": ["保修服务"],
    "发票": ["开具发票"], "开票": ["开具发票"], "配对": ["设备配对"],
    "连不上": ["连接失败"], "死机": ["无法启动"], "音箱": ["智能音箱"],
    "固件": ["固件升级"],
}


def _load_synonyms(path: Optional[Path] = None) -> Dict[str, List[str]]:
    """加载同义词表（agent/synonyms.json），失败降级到内置表。"""
    path = path or _SYNONYMS_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): ([v] if isinstance(v, str) else [str(x) for x in v])
                    for k, v in data.items()}
    except Exception:
        pass
    return dict(_DEFAULT_SYNONYMS)


class QueryRewriter:
    """查询多变体生成。

    * llm_fn 注入：llm_fn(query) -> List[str] 或 JSON 数组字符串；异常/为空则降级。
    * 规则降级：同义词表展开 + 疑问词/口语词剥离。
    * 约定：rewrite() 返回列表首元素恒为原始 query（原始与改写变体同时召回）。
    """

    def __init__(self,
                 llm_fn: Optional[Callable[[str], Any]] = None,
                 synonyms: Optional[Dict[str, List[str]]] = None,
                 max_variants: int = 4):
        self.llm_fn = llm_fn
        self.synonyms = synonyms if synonyms is not None else _load_synonyms()
        self.max_variants = max(1, max_variants)

    def rewrite(self, query: str) -> List[str]:
        variants: List[str] = [query]  # 原始 query 恒在首位
        llm_variants = self._llm_variants(query)
        variants.extend(llm_variants)
        if not llm_variants:
            variants.extend(self._rule_variants(query))
        # 去重保序
        seen, out = set(), []
        for v in variants:
            v = (v or "").strip()
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        return out[: self.max_variants]

    # -- internal --

    def _llm_variants(self, query: str) -> List[str]:
        if self.llm_fn is None:
            return []
        try:
            raw = self.llm_fn(query)
            if isinstance(raw, str):
                match = re.search(r"\[.*\]", raw, re.DOTALL)
                raw = json.loads(match.group()) if match else []
            if isinstance(raw, (list, tuple)):
                return [str(v).strip() for v in raw if str(v).strip()][:3]
        except Exception:
            pass
        return []

    def _rule_variants(self, query: str) -> List[str]:
        variants: List[str] = []
        # 1) 同义词展开：命中词后追加规范词
        expanded = query
        for slang in sorted(self.synonyms, key=len, reverse=True):
            if slang in expanded:
                extra = " ".join(self.synonyms[slang])
                expanded = expanded.replace(slang, f"{slang} {extra}")
        if expanded != query:
            variants.append(expanded)
        # 2) 疑问词剥离（提取核心关键词）
        core = query
        for word in sorted(_QUESTION_WORDS, key=len, reverse=True):
            core = core.replace(word, " ")
        core = "".join(t for t in re.split(r"\s+", core) if t.strip())
        if core and core != query:
            variants.append(core)
        return variants


# ---------------------------------------------------------------------------
# Reranker — 注入式重排接口
# ---------------------------------------------------------------------------

class Reranker:
    """重排接口。实现 rerank(query, results, top_n) -> List[dict]。"""

    def rerank(self, query: str, results: List[Dict[str, Any]],
               top_n: int = RERANK_TOP_N) -> List[Dict[str, Any]]:
        raise NotImplementedError


def _char_ngrams(text: str, n: int = 2) -> set:
    text = re.sub(r"\s+", "", text.lower())
    if len(text) < n:
        return {text} if text else set()
    return {text[i:i + n] for i in range(len(text) - n + 1)}


class RuleReranker(Reranker):
    """纯 stdlib 规则重排：关键词重叠度 + metadata 时间新鲜度 + 来源权重。

    final = overlap * 1.0 + recency * recency_weight + source_w * source_weight
    （保底叠加原召回分的微小 tie-breaker，保证稳定序）
    """

    def __init__(self,
                 source_weights: Optional[Dict[str, float]] = None,
                 recency_weight: float = 0.2,
                 source_weight: float = 0.3):
        self.source_weights = source_weights or {}
        self.recency_weight = recency_weight
        self.source_weight = source_weight

    @staticmethod
    def lexical_overlap(query: str, result: Dict[str, Any]) -> float:
        """Return lexical overlap independently from recency/source tie breakers."""
        q_grams = _char_ngrams(query)
        content = str(result.get("content") or result.get("text") or "")
        title = str(result.get("title") or "")
        d_grams = _char_ngrams(title) | _char_ngrams(content[:800])
        return (len(q_grams & d_grams) / len(q_grams)) if q_grams else 0.0

    def score(self, query: str, result: Dict[str, Any]) -> float:
        # Freshness and source priority may sort relevant documents, but must not
        # make an unrelated document look relevant.  The backend applies the
        # lexical overlap as a separate relevance gate after reranking.
        overlap = self.lexical_overlap(query, result)

        meta = result.get("metadata") or {}
        recency = self._recency(meta.get("created_at") or result.get("created_at"))
        src = str(result.get("source", ""))
        src_w = float(self.source_weights.get(src, 0.0))

        base = float(result.get("score", 0.0) or 0.0)
        # Preserve the retrieval signal.  The old 1e-6 tie-breaker effectively
        # discarded pgvector/RRF relevance and let generic question-word
        # overlap（例如“怎么”“是否”）排到真正相关的知识段之前。
        # 可配置权重使规则重排同时适用于 pgvector/RRF 分数（约 0.01–0.05）
        # 和直接向量检索分数（约 0–1）。
        try:
            base_weight = max(0.0, float(os.getenv("RAG_RERANK_BASE_WEIGHT", "10")))
        except (TypeError, ValueError):
            base_weight = 10.0
        return (overlap
                + self.recency_weight * recency
                + self.source_weight * src_w
                + base_weight * base)

    def rerank(self, query, results, top_n=RERANK_TOP_N):
        scored = [(self.score(query, r), i, r) for i, r in enumerate(results)]
        scored.sort(key=lambda x: (-x[0], x[1]))  # 分数降序、原序稳定
        out = []
        for s, _, r in scored[:top_n]:
            entry = dict(r)
            entry["rerank_score"] = round(s, 6)
            entry["lexical_overlap"] = round(self.lexical_overlap(query, r), 6)
            out.append(entry)
        return out

    @staticmethod
    def _recency(created_at: Any) -> float:
        """created_at（ISO 字符串或 epoch 秒）→ [0,1] 新鲜度；解析失败为 0。"""
        import datetime as _dt
        ts = None
        try:
            if isinstance(created_at, (int, float)):
                ts = float(created_at)
            elif isinstance(created_at, str) and created_at:
                ts = _dt.datetime.fromisoformat(
                    created_at.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0
        if ts is None:
            return 0.0
        now = _dt.datetime.now(_dt.timezone.utc).timestamp()
        age_days = max(0.0, (now - ts) / 86400.0)
        return math.exp(-age_days / 365.0)  # 一年衰减 ~63%


class CrossEncoderReranker(Reranker):
    """CrossEncoder 重排（sentence_transformers 守卫导入）。

    模型名可配（默认 BAAI/bge-reranker-base，env RAG_RERANKER_MODEL 覆盖）。
    库不可用/加载失败 → 自动降级 RuleReranker。
    """

    def __init__(self, model_name: Optional[str] = None,
                 fallback: Optional[Reranker] = None):
        self.model_name = model_name or os.environ.get(
            "RAG_RERANKER_MODEL", "BAAI/bge-reranker-base")
        self.fallback = fallback or RuleReranker()
        self._model = None
        self._available = False
        try:  # 三方守卫
            from sentence_transformers import CrossEncoder  # noqa: F401
            self._cross_encoder_cls = CrossEncoder
            self._available = True
        except Exception:
            self._cross_encoder_cls = None

    @property
    def available(self) -> bool:
        return self._available

    def _ensure_model(self) -> bool:
        if self._model is not None:
            return True
        if not self._available:
            return False
        try:
            self._model = self._cross_encoder_cls(self.model_name)
            return True
        except Exception:
            self._available = False
            return False

    def rerank(self, query, results, top_n=RERANK_TOP_N):
        if not results:
            return []
        if not self._ensure_model():
            return self.fallback.rerank(query, results, top_n)
        try:
            pairs = [(query, str(r.get("content") or r.get("text") or "")[:1000])
                     for r in results]
            scores = self._model.predict(pairs)
            scored = sorted(zip(scores, range(len(results)), results),
                            key=lambda x: (-float(x[0]), x[1]))
            out = []
            for s, _, r in scored[:top_n]:
                entry = dict(r)
                entry["rerank_score"] = round(float(s), 6)
                # Keep the same relevance metadata as RuleReranker.  The
                # backend uses lexical_overlap as a safety gate, and the
                # observability layer needs it to explain why a result was
                # admitted or filtered.
                entry["lexical_overlap"] = round(
                    RuleReranker.lexical_overlap(query, r), 6
                )
                out.append(entry)
            return out
        except Exception:
            return self.fallback.rerank(query, results, top_n)


# ---------------------------------------------------------------------------
# 默认 sparse 路：适配现有 agent/rag.py 的 TF-IDF/BM25 检索器（守卫导入）
# ---------------------------------------------------------------------------

def default_tfidf_search(query: str, top_k: int = 10, *,
                         tenant_id: Optional[str] = None,
                         tags: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """适配 agent/rag.py 的 retrieve()（jieba 守卫；不可用返回 []）。

    rag.py 无租户概念 → tenant_id/tags 在本层做后过滤（文件级 KB 无 metadata，
    通常直接透传结果；带 metadata 时按其过滤）。
    """
    try:
        from .rag import retrieve as _retrieve  # 顶层 import jieba，需守卫
    except Exception:
        try:
            from agent.rag import retrieve as _retrieve  # 脚本方式运行
        except Exception:
            return []
    try:
        hits = _retrieve(query, top_k=top_k, use_vector=False)
    except Exception:
        return []
    out = []
    for h in hits:
        entry = dict(h)
        entry.setdefault("content", entry.get("text", ""))
        out.append(entry)
    return filter_by_metadata(out, tenant_id=tenant_id, tags=tags)


def filter_by_metadata(results: List[Dict[str, Any]],
                       tenant_id: Optional[str] = None,
                       tags: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """按 tenant_id/tags 过滤（纯函数）。结果无对应 metadata 字段时视为公共文档保留。"""
    out = []
    want_tags = set(tags or [])
    for r in results:
        meta = r.get("metadata") or {}
        r_tenant = r.get("tenant_id", meta.get("tenant_id"))
        if tenant_id is not None and r_tenant is not None and r_tenant != tenant_id:
            continue
        r_tags = r.get("tags", meta.get("tags"))
        if want_tags and r_tags is not None and not (want_tags & set(r_tags)):
            continue
        out.append(r)
    return out


def _call_search(fn: Callable, query: str, top_k: int,
                 tenant_id: Optional[str], tags: Optional[List[str]]):
    """调用注入的检索函数，按其签名透传 tenant_id/tags/top_k（不支持则省略）。"""
    kwargs: Dict[str, Any] = {}
    try:
        params = inspect.signature(fn).parameters
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD
                         for p in params.values())
        if "top_k" in params or has_var_kw:
            kwargs["top_k"] = top_k
        if tenant_id is not None and ("tenant_id" in params or has_var_kw):
            kwargs["tenant_id"] = tenant_id
        if tags is not None and ("tags" in params or has_var_kw):
            kwargs["tags"] = tags
    except (TypeError, ValueError):
        kwargs = {"top_k": top_k}
    try:
        return fn(query, **kwargs) or []
    except TypeError:
        try:
            return fn(query) or []
        except Exception:
            return []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# HybridRetriever — 主入口
# ---------------------------------------------------------------------------

class HybridRetriever:
    """双路召回（dense + sparse）→ RRF 融合 → 重排 → Parent-Child 映射。

    Args:
        vector_search_fn: dense 路，fn(query, top_k=?, tenant_id=?, tags=?) -> [dict]
        keyword_search_fn: sparse 路，同签名；默认适配 agent/rag.py TF-IDF
        reranker: Reranker 实例（默认 RuleReranker）
        rewriter: QueryRewriter（默认规则降级版）
        parent_map: {parent_id: {"text"/"content", ...}}，child→parent 映射用
        recall_top_k / rerank_top_n / context_top_n: 分层召回参数
        rrf_k: RRF 常数（默认 60）
    """

    def __init__(self,
                 vector_search_fn: Optional[Callable] = None,
                 keyword_search_fn: Optional[Callable] = None,
                 reranker: Optional[Reranker] = None,
                 rewriter: Optional[QueryRewriter] = None,
                 parent_map: Optional[Dict[str, Dict[str, Any]]] = None,
                 recall_top_k: int = RECALL_TOP_K,
                 rerank_top_n: int = RERANK_TOP_N,
                 context_top_n: int = CONTEXT_TOP_N,
                 rrf_k: int = RRF_K):
        self.vector_search_fn = vector_search_fn
        self.keyword_search_fn = keyword_search_fn or default_tfidf_search
        self.reranker = reranker or RuleReranker()
        self.rewriter = rewriter or QueryRewriter()
        self.parent_map = parent_map or {}
        self.recall_top_k = recall_top_k
        self.rerank_top_n = rerank_top_n
        self.context_top_n = context_top_n
        self.rrf_k = rrf_k

    # -- public --

    def search(self, query: str,
               tenant_id: Optional[str] = None,
               tags: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """执行混合检索，返回统一结构：

        [{title, content, score, source, parent_id, text(=content 兼容别名)}]
        """
        variants = self.rewriter.rewrite(query)  # 首元素恒为原始 query
        per_variant_k = max(10, self.recall_top_k // max(1, len(variants)))

        result_lists: List[List[Dict[str, Any]]] = []
        for variant in variants:
            if self.vector_search_fn is not None:
                dense = _call_search(self.vector_search_fn, variant,
                                     per_variant_k, tenant_id, tags)
                if dense:
                    result_lists.append(list(dense))
            sparse = _call_search(self.keyword_search_fn, variant,
                                  per_variant_k, tenant_id, tags)
            if sparse:
                result_lists.append(list(sparse))

        # 融合 → 粗召回截断 → 防御性 metadata 过滤
        fused = rrf_fuse(result_lists, k=self.rrf_k)[: self.recall_top_k]
        fused = filter_by_metadata(fused, tenant_id=tenant_id, tags=tags)

        # 重排（用原始 query，而非变体）
        reranked = self.reranker.rerank(query, fused, top_n=self.rerank_top_n)

        # Parent-Child 映射 + 去重 → 最终 context_top_n
        mapped = map_children_to_parents(reranked, self.parent_map)
        return [self._normalize(r) for r in mapped[: self.context_top_n]]

    # -- internal --

    @staticmethod
    def _normalize(r: Dict[str, Any]) -> Dict[str, Any]:
        content = str(r.get("content") or r.get("text") or "")
        return {
            "id": str(r.get("id", "")),
            "title": str(r.get("title", "")),
            "section": str(r.get("section", "")),
            "content": content,
            "text": content,  # 兼容 nodes/context_assembler 消费的 text 字段
            "score": round(float(r.get("rerank_score",
                                       r.get("score", 0.0)) or 0.0), 6),
            "source": str(r.get("source", "")),
            "parent_id": r.get("parent_id"),
            # 同 parent 合并后，被代表掉的兄弟 child id（检索判定用）
            "child_ids": list(r.get("child_ids") or []),
            # Preserve ranking diagnostics for the API/metrics dashboard.
            "rrf_score": round(float(r.get("rrf_score", 0.0) or 0.0), 6),
            "orig_score": round(float(r.get("orig_score", 0.0) or 0.0), 6),
            "rerank_score": round(float(r.get("rerank_score", 0.0) or 0.0), 6),
            "lexical_overlap": round(
                float(r.get("lexical_overlap", 0.0) or 0.0), 6
            ),
            "reranker_provider": str(r.get("reranker_provider", "")),
            "reranker_model": str(r.get("reranker_model", "")),
        }


# ---------------------------------------------------------------------------
# 便捷构造：按 env RAG_BACKEND 选择后端（tfidf | hybrid | pgvector）
# ---------------------------------------------------------------------------

def build_retriever_from_env() -> HybridRetriever:
    """根据 RAG_BACKEND 组装 HybridRetriever（接线细节见 RAG_UPGRADE.md）。

    重排默认用 Cross-Encoder（BAAI/bge-reranker-base；env RAG_RERANKER_MODEL 覆盖），
    sentence_transformers 缺失/加载失败时其内部自动降级 RuleReranker。
    """
    reranker = CrossEncoderReranker()
    backend = os.environ.get("RAG_BACKEND", "tfidf").lower()
    if backend == "pgvector":
        try:
            from .pgvector_hybrid import PgHybridStore
            store = PgHybridStore.from_env()
            return HybridRetriever(
                vector_search_fn=store.vector_search,
                keyword_search_fn=store.keyword_search,
                reranker=reranker,
            )
        except Exception:
            backend = "hybrid"  # 降级
    if backend == "hybrid":
        vector_fn = None
        try:
            from .vector_rag import vector_retrieve

            def vector_fn(q, top_k=10, **_):  # noqa: F811
                return vector_retrieve(q, top_k=top_k)
        except Exception:
            pass
        return HybridRetriever(vector_search_fn=vector_fn, reranker=reranker)
    # tfidf：单 sparse 路（仍走融合/重排管线）
    return HybridRetriever(vector_search_fn=None, reranker=reranker)
