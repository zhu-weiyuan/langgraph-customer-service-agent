#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/eval_real.py — 真实端到端评测系统（检索真实 embedding/hybrid + 生成真实 LLM + LLM-as-Judge）

与离线纯规则评测(eval/harness.py)互补:本脚本在**用户机器**上跑真实链路——

  检索层  : 调 agent/rag_backend.retrieve(真实 hybrid/pgvector,会调 embedding),
            对每条 query 算 HitRate@K / Recall@K / MRR / Precision@K;
            相关性**同时**在两个粒度判定并分开报告:
              · 文件级(golden_context_ids)—— 命中 hit["source"](文件 stem)
              · 小节级(golden_section)   —— 命中 hit["title"](小节标题) ← 测精排/rerank 价值
            全部按 tier(正常/边缘/对抗/高权重)分组。

  生成层  : 用真实 LLM 生成答案(plain RAG 与 Agentic RAG 各一份),
            再用**另一个/另一次 LLM 调用当 Judge**(可 --judge-model 指定不同裁判模型)
            结构化 JSON 打分:Faithfulness(逐句是否被上下文支撑)、Answer Relevance、
            Completeness(reference_answer 要点覆盖)、幻觉(无依据断言)、拒答正确性。
            位置偏差治理:plain vs agentic 成对比较时交换顺序评两次,一致才判胜负。
            "被评"用 gen 模型、"Judge"用 judge 模型,调用与 token 分开记录。

  对比    : 同一批题,plain RAG vs Agentic RAG 都跑真实检索+真实生成+真实 Judge,
            分层列出 检索质量 / 生成质量 / 成本(真实 token、LLM 调用数、延迟)。

设计:全部依赖注入(retriever / gen-LLM / judge-LLM 可替换)。--mock 注入假实现,
容器无 LLM/embedding 也能跑通验证逻辑并产出分层报告;真实模式会产生费用。

用法(Windows cmd,详见 eval/EVAL_REAL_README.md):
    :: 1) 容器/无依赖先验证逻辑(不产生费用)
    python scripts\\eval_real.py --mock --mode both --limit 5

    :: 2) 真实小跑看费用(先 5-10 条!)
    set RAG_BACKEND=hybrid
    python scripts\\eval_real.py --mode both --limit 8

    :: 3) 只看检索层 / 只某层 / 指定裁判模型 / 导出 CSV
    python scripts\\eval_real.py --mode retrieval --backend pgvector
    python scripts\\eval_real.py --mode generation --tier high --judge-model gpt-4o-mini --csv out.csv
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parent.parent
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))

try:  # .env 加载(守卫,容器无 dotenv 也不崩)
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

DATASET = "eval/rag_eval_hard.jsonl"


# ════════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════════
@dataclass
class LLMResult:
    text: str = ""
    in_tok: int = 0
    out_tok: int = 0
    latency_ms: float = 0.0


@dataclass
class Counters:
    """一个 pipeline 的成本累加器(检索 + LLM 分开计)。"""
    retrievals: int = 0
    llm_calls: int = 0
    in_tok: int = 0
    out_tok: int = 0
    llm_latency_ms: float = 0.0
    retr_latency_ms: float = 0.0

    def add_llm(self, r: LLMResult) -> None:
        self.llm_calls += 1
        self.in_tok += int(r.in_tok)
        self.out_tok += int(r.out_tok)
        self.llm_latency_ms += float(r.latency_ms)


# ════════════════════════════════════════════════════════════════════
# 纯函数指标(rank 化相关性 → 检索指标;手算可验证)
# ════════════════════════════════════════════════════════════════════
def hit_rate_at_k(flags: Sequence[bool], k: int) -> float:
    return 1.0 if any(flags[:k]) else 0.0


def precision_at_k(flags: Sequence[bool], k: int) -> float:
    topk = list(flags[:k])
    return (sum(1 for f in topk if f) / len(topk)) if topk else 0.0


def recall_at_k(matched: int, total_rel: int) -> float:
    if total_rel <= 0:
        return 1.0
    return min(1.0, matched / total_rel)


def mrr(flags: Sequence[bool]) -> float:
    for i, f in enumerate(flags, start=1):
        if f:
            return 1.0 / i
    return 0.0


def mean(xs: Sequence[float]) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


# ── 相关性归一(文件 stem / 小节标题) ──
def _norm_file(s: Any) -> str:
    return re.sub(r"\s+", "", str(s or "").lower()).replace(".md", "")


def _norm_sec(s: Any) -> str:
    """\u5f52\u4e00\u5316\u5c0f\u8282\u6807\u9898\uff1a\u79fb\u9664\u7a7a\u683c\u3001\u5168\u89d2\u7a7a\u683c\u3001\u659c\u6760\u7b49\u5206\u9694\u7b26\u3002"""
    s = str(s or "")
    # \u79fb\u9664\u5404\u79cd\u7a7a\u767d\u5b57\u7b26
    s = re.sub(r"[\s\u3000]+", "", s)
    # \u79fb\u9664\u5e38\u89c1\u7684\u6807\u9898\u5206\u9694\u7b26\uff08\u659c\u6760\u3001\u7834\u6298\u53f7\u7b49\uff09
    s = re.sub(r"[/\\\-\u2013\u2014\uff5e]+", "", s)
    return s.lower()


def _match(a: str, b: str) -> bool:
    return bool(a) and bool(b) and (a in b or b in a)


def file_flags_and_recall(hits: List[Dict[str, Any]], golden: List[str],
                          k: int) -> Tuple[List[bool], int, int]:
    """返回 (topk 每条是否命中相关文件, 命中的 golden 文件数, golden 文件总数)。"""
    gold = [_norm_file(g) for g in golden]
    topk = hits[:k]
    flags = [any(_match(g, _norm_file(h.get("source"))) for g in gold) for h in topk]
    matched = sum(1 for g in gold
                  if any(_match(g, _norm_file(h.get("source"))) for h in topk))
    return flags, matched, len(gold)


def section_flags_and_recall(hits: List[Dict[str, Any]], golden: List[str],
                             k: int) -> Tuple[List[bool], int, int]:
    """小节级:命中 hit['title'] == golden_section(归一后子串匹配)。"""
    gold = [_norm_sec(g) for g in golden]
    topk = hits[:k]
    flags = [any(_match(g, _norm_sec(h.get("title"))) for g in gold) for h in topk]
    matched = sum(1 for g in gold
                  if any(_match(g, _norm_sec(h.get("title"))) for h in topk))
    return flags, matched, len(gold)


# ════════════════════════════════════════════════════════════════════
# LLM 封装(可注入:真实 / mock),complete() 统一计数 + 打印进度
# ════════════════════════════════════════════════════════════════════
CallFn = Callable[[str, str, int, float], LLMResult]  # (system,user,max_tokens,temp)->LLMResult


class LLM:
    def __init__(self, call_fn: CallFn, label: str, verbose: bool = True):
        self.call = call_fn
        self.label = label            # 例 "gen:mimo-v2.5" / "judge:gpt-4o-mini"
        self.verbose = verbose

    def complete(self, system: str, user: str, *, tag: str, counters: Counters,
                 max_tokens: int = 512, temperature: float = 0.2) -> LLMResult:
        r = self.call(system, user, max_tokens, temperature)
        counters.add_llm(r)
        if self.verbose:
            # 打印真实调用记录,便于用户在 LLM 后台对得上
            print(f"  · [{self.label}] {tag}  in≈{r.in_tok} out≈{r.out_tok} "
                  f"{r.latency_ms:.0f}ms", flush=True)
        return r


# ── 关闭思考链(reasoning)的各家方言 ─────────────────────────────
# 评分/裁判任务不需要思维链:开着不但慢、贵,还会把 max_tokens 吃光导致
# content 为空。但"怎么关"没有统一标准,各家网关参数名不同,而且传错参数
# 有的服务器直接 400。所以这里做一次**自动协商**:按顺序试,第一个既
# 不报错、又真的让 reasoning_content 消失的方案就固定下来复用。
NO_THINK_DIALECTS: List[Tuple[str, Dict[str, Any]]] = [
    ("chat_template_kwargs", {"chat_template_kwargs": {"enable_thinking": False}}),
    ("enable_thinking",      {"enable_thinking": False}),
    ("reasoning_effort",     {"reasoning_effort": "none"}),
    ("thinking.disabled",    {"thinking": {"type": "disabled"}}),
    ("extra_body",           {"extra_body": {"enable_thinking": False}}),
]


def make_real_call(model: Optional[str], no_think: bool = True) -> Tuple[CallFn, str]:
    """真实 OpenAI 兼容 /chat/completions 调用,捕获 usage(真实 token)。

    复用 agent.llm_client.LLMClient 解析 base_url/api_key/model(含 .env 加载),
    但自行 POST 以读取 response.usage(LLMClient.chat 只回文本、拿不到 token 数)。
    """
    from agent.llm_client import LLMClient
    import requests

    base = LLMClient(model=model)          # 解析配置(会加载 .env)
    url = f"{base.base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {base.api_key}",
               "Content-Type": "application/json"}
    mdl = base.model

    # 允许用环境变量直接指定方言,跳过协商:NO_THINK_PARAMS='{"enable_thinking":false}'
    _override = os.environ.get("NO_THINK_PARAMS", "").strip()
    # nt["extra"] = None 表示还没协商;{} 表示协商失败/不需要,按原样发
    nt: Dict[str, Any] = {"extra": None, "name": ""}
    if not no_think:
        nt["extra"], nt["name"] = {}, "off(未启用)"
    elif _override:
        try:
            nt["extra"], nt["name"] = json.loads(_override), "env:NO_THINK_PARAMS"
        except Exception:
            print(f"    [warn] NO_THINK_PARAMS 不是合法 JSON,忽略", flush=True)

    def _post(payload: Dict[str, Any]):
        return requests.post(url, headers=headers, json=payload, timeout=180)

    def _negotiate(base_payload: Dict[str, Any]):
        """返回 (resp, elapsed_ms)。副作用:确定 nt['extra']。"""
        for name, extra in NO_THINK_DIALECTS:
            t = time.perf_counter()
            try:
                r = _post({**base_payload, **extra})
            except Exception:
                continue
            if r.status_code >= 400:
                continue                      # 服务器不认这个参数,换下一个
            try:
                m = r.json()["choices"][0]["message"]
            except Exception:
                continue
            if (m.get("reasoning_content") or "").strip():
                continue                      # 参数被接受了但思考没关掉
            nt["extra"], nt["name"] = extra, name
            print(f"    [no-think] 已用 `{name}` 关闭思考链", flush=True)
            return r, (time.perf_counter() - t) * 1000.0
        nt["extra"], nt["name"] = {}, "unsupported"
        print("    [no-think] 该服务不支持关闭思考链,回退为加大 max_tokens",
              flush=True)
        t = time.perf_counter()
        return _post(base_payload), (time.perf_counter() - t) * 1000.0

    def _call(system: str, user: str, max_tokens: int, temperature: float) -> LLMResult:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": user})
        payload = {"model": mdl, "messages": msgs,
                   "temperature": temperature, "max_tokens": max_tokens}
        if nt["extra"] is None:
            resp, dt = _negotiate(payload)
        else:
            t0 = time.perf_counter()
            resp = _post({**payload, **nt["extra"]})
            dt = (time.perf_counter() - t0) * 1000.0
            # 万一之前协商成功的参数后来被拒(换模型/换网关),降级重试一次
            if resp.status_code >= 400 and nt["extra"]:
                print(f"    [no-think] `{nt['name']}` 被拒({resp.status_code}),"
                      f"改为不带该参数重试", flush=True)
                nt["extra"], nt["name"] = {}, "unsupported"
                t0 = time.perf_counter()
                resp = _post(payload)
                dt = (time.perf_counter() - t0) * 1000.0
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        msg = choice["message"]
        content = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()
        text = content or reasoning
        finish = choice.get("finish_reason") or ""
        # 推理模型:content 空而 reasoning 有内容,且被 max_tokens 截断 ——
        # 说明预算全花在思维链上,正文没来得及输出。此处必须显式告警,
        # 否则下游 JSON 解析失败会被静默当成 0 分(=幻觉率 1.0)。
        if not content and reasoning and finish == "length":
            print(f"    [warn] 推理模型正文为空(reasoning {len(reasoning)} 字符被 "
                  f"max_tokens={max_tokens} 截断)。建议关闭思考链"
                  f"(去掉 --think / 设 NO_THINK_PARAMS)或加大 --judge-max-tokens",
                  flush=True)
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or _est_tok(system + user))
        out_tok = int(usage.get("completion_tokens") or _est_tok(text))
        return LLMResult(text=text, in_tok=in_tok, out_tok=out_tok, latency_ms=dt)

    return _call, mdl


def _est_tok(s: str) -> int:
    """usage 缺失时的兜底估算(中文约 1.5 char/token)。"""
    return max(1, int(len(s or "") / 1.5))


# ════════════════════════════════════════════════════════════════════
# 检索管线(plain / agentic),retrieve_fn 注入
# ════════════════════════════════════════════════════════════════════
CS_SYSTEM = """你是智联科技的专业智能客服助手。严格遵守:
1. 只依据下方“参考资料”回答,不要编造资料里没有的信息;
2. 若问题超出智联科技产品/服务范围(如竞品推荐、天气闲聊、医疗法律、代写创作),
   礼貌拒答并引导用户回到产品相关咨询,不要强行作答;
3. 回答简洁、专业、友好,涉及数字/条件时保持准确。"""


def _build_context(hits: List[Dict[str, Any]], max_chars: int = 500) -> List[str]:
    ctx = []
    for h in hits:
        title = str(h.get("title", ""))
        text = str(h.get("text", "") or h.get("content", ""))
        if len(text) > max_chars:
            text = text[:max_chars] + "..."
        ctx.append(f"【{title}】{text}")
    return ctx


def run_plain(query: str, retrieve_fn: Callable, gen_llm: LLM, k: int,
              counters: Counters, cid: str, do_generate: bool) -> Dict[str, Any]:
    """普通 RAG:单次检索 → (可选)单次生成。"""
    t0 = time.perf_counter()
    hits = list(retrieve_fn(query, top_k=k) or [])[:k]
    counters.retrievals += 1
    counters.retr_latency_ms += (time.perf_counter() - t0) * 1000.0
    out: Dict[str, Any] = {"hits": hits, "contexts": _build_context(hits)}
    if do_generate:
        out["answer"] = _generate(query, out["contexts"], gen_llm, counters,
                                  tag=f"gen/plain/{cid}")
    return out


def run_agentic(query: str, retrieve_fn: Callable, rewrite_fn: Callable,
                eval_fn: Callable, gen_llm: LLM, k: int, rounds: int,
                min_hits: int, counters: Counters, cid: str,
                do_generate: bool) -> Dict[str, Any]:
    """Agentic RAG:先检索原问题，确认不足后才改写和扩展召回。

    ``rounds`` 的语义是“最多改写次数”，不包含初始 query：最佳情况是 0 次
    改写，最差情况是改写 ``rounds`` 次。每一轮只有在上一轮评估为不足时才会
    继续，因此不会无条件执行改写。
    """
    def retrieve_many(queries: Sequence[str]) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        seen = set()
        for q in queries:
            t0 = time.perf_counter()
            hits = retrieve_fn(q, top_k=k) or []
            counters.retrievals += 1
            counters.retr_latency_ms += (time.perf_counter() - t0) * 1000.0
            for h in hits:
                key = (h.get("title"), h.get("source"))
                if key not in seen:
                    seen.add(key)
                    merged.append(h)
        merged.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return merged[:k]

    # 第一轮只查原始 query：优秀原问题不应为“多写几个 query”付费。
    best = retrieve_many([query])
    if best:
        initial_eval = eval_fn(query, best, counters, cid, 0)
        if initial_eval.get("sufficient", False):
            out: Dict[str, Any] = {"hits": best, "contexts": _build_context(best)}
            if do_generate:
                out["answer"] = _generate(query, out["contexts"], gen_llm,
                                          counters, tag=f"gen/agentic/{cid}")
            return out
        suggested = [str(q).strip() for q in (initial_eval.get("new_queries") or [])
                     if str(q).strip()]
    else:
        suggested = []

    # 最多允许 rounds 次改写；每次改写后都重新评估，只有不足才进入下一次。
    for rewrite_round in range(1, max(0, rounds) + 1):
        # 原始检索不足：优先使用评估器给出的针对性查询；没有时才调用改写模型。
        queries = suggested[:3] or (rewrite_fn(query, counters, cid, rewrite_round) or [query])
        rewritten_hits = retrieve_many(queries)
        if rewritten_hits:
            best = rewritten_hits

        if not rewritten_hits:
            break

        # 改写后的结果如果足够，立即停止；否则下一轮最多再改写一次。
        if rewrite_round >= max(0, rounds):
            break
        followup_eval = eval_fn(query, rewritten_hits, counters, cid, rewrite_round)
        if followup_eval.get("sufficient", False):
            break
        suggested = [str(q).strip() for q in (followup_eval.get("new_queries") or [])
                     if str(q).strip()]

    out = {"hits": best, "contexts": _build_context(best)}
    if do_generate:
        out["answer"] = _generate(query, out["contexts"], gen_llm, counters,
                                  tag=f"gen/agentic/{cid}")
    return out


def _generate(query: str, contexts: List[str], gen_llm: LLM,
              counters: Counters, tag: str) -> str:
    ctx = "\n".join(contexts) if contexts else "(无检索结果)"
    user = f"问题:{query}\n\n参考资料:\n{ctx}"
    return gen_llm.complete(CS_SYSTEM, user, tag=tag, counters=counters,
                            max_tokens=512, temperature=0.3).text


# ════════════════════════════════════════════════════════════════════
# Judge(结构化 JSON 打分 + 位置偏差治理),judge_llm 与 gen_llm 分开
# ════════════════════════════════════════════════════════════════════
POINTWISE_SYS = """你是严格的 RAG 答案评审员。只输出 JSON,不要任何多余文字。
基于给定的“检索上下文”和“参考要点”评估“待评回答”,逐句核查后按下述字段打分。"""

POINTWISE_USER_TMPL = """用户问题:
{query}

检索上下文(回答只应依据它):
{context}

参考答案要点(用于判断完整性,不是标准答案原文):
{reference}

待评回答:
{answer}

请逐句核查“待评回答”,输出严格 JSON:
{{
  "faithfulness": 0~1,          // 被上下文支撑的句子比例(逐句判)
  "unsupported_claims": [],     // 无上下文依据的断言(幻觉),逐条列出
  "answer_relevance": 0~1,      // 是否切题回答了用户问题
  "completeness": 0~1,          // 覆盖了多少参考要点
  "reason": "一句话理由"
}}"""


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _extract_json(text: str) -> Dict[str, Any]:
    if not text:
        return {}
    # 推理模型常把思维链包在 <think></think> 里,里面可能含花括号,先剥掉
    text = _THINK_RE.sub(" ", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _clip01(v: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except Exception:
        return default


class Judge:
    """LLM-as-Judge:结构化 pointwise 打分 + debiased pairwise 比较。"""

    def __init__(self, judge_llm: LLM, max_tokens: int = 1600):
        self.llm = judge_llm
        # 推理模型会先输出思维链再给正文,预算过小 → content 为空 → 解析失败。
        # 默认给足 1600,可用 --judge-max-tokens 调整。
        self.max_tokens = max(64, int(max_tokens))

    def pointwise(self, query: str, contexts: List[str], reference: str,
                  answer: str, counters: Counters, cid: str,
                  pipe: str) -> Dict[str, Any]:
        ctx = "\n".join(contexts) if contexts else "(无)"
        user = POINTWISE_USER_TMPL.format(query=query, context=ctx,
                                         reference=reference, answer=answer)
        r = self.llm.complete(POINTWISE_SYS, user,
                              tag=f"judge/pointwise/{pipe}/{cid}",
                              counters=counters, max_tokens=self.max_tokens,
                              temperature=0.0)
        obj = _extract_json(r.text)
        if not obj:
            # 关键:解析失败 ≠ 0 分。返回 None,聚合层会自动跳过,
            # 避免把"裁判没说话"记成"答案完全没有依据"。
            return {"faithfulness": None, "answer_relevance": None,
                    "completeness": None, "hallucination": None,
                    "unsupported_claims": [], "parsed_ok": False,
                    "reason": "judge_parse_failed"}
        faith = _clip01(obj.get("faithfulness"), 0.0)
        return {
            "faithfulness": faith,
            "answer_relevance": _clip01(obj.get("answer_relevance"), 0.0),
            "completeness": _clip01(obj.get("completeness"), 0.0),
            "hallucination": round(1.0 - faith, 4),
            "unsupported_claims": obj.get("unsupported_claims") or [],
            "parsed_ok": True,
            "reason": str(obj.get("reason", ""))[:200],
        }

    def pairwise_debiased(self, query: str, ans_a: str, ans_b: str,
                          counters: Counters, cid: str) -> Optional[str]:
        """交换顺序评两次,一致(非平局)才判胜负,否则 tie。返回 'a'|'b'|'tie'。"""
        sys = ("你是严格的答案对比评审员。给定问题与两个回答,只回答哪个更好。"
               "只输出单个字母:A / B / T(平局)。")

        def _one(x: str, y: str, tag: str) -> Optional[str]:
            user = f"问题:{query}\n\n回答A:\n{x}\n\n回答B:\n{y}\n\n哪个更好? 只答 A/B/T。"
            # 原来是 max_tokens=8:对推理模型等于"只准思考 8 个 token",
            # 正文永远为空 → 全部退化成平局。这里同样给足预算。
            r = self.llm.complete(sys, user, tag=tag, counters=counters,
                                  max_tokens=self.max_tokens, temperature=0.0)
            txt = _THINK_RE.sub(" ", r.text or "").strip()
            # 取最后一个独立字母(思维链里可能出现 "回答A"、"选 B" 等干扰)
            ms = re.findall(r"\b([ABTabt])\b", txt) or re.findall(r"[ABTabt]", txt[-40:])
            return ms[-1].upper() if ms else None

        r1 = _one(ans_a, ans_b, f"judge/pair1/{cid}")   # A=a
        r2 = _one(ans_b, ans_a, f"judge/pair2/{cid}")   # A=b(交换)
        if r1 is None or r2 is None:
            return None                                  # 裁判失效 → 不计入
        first = {"A": "a", "B": "b", "T": "tie"}[r1]
        second = {"A": "b", "B": "a", "T": "tie"}[r2]
        return first if (first == second and first != "tie") else "tie"


# 拒答判定(确定性,不花 Judge 调用)——与 eval/metrics.is_refusal 对齐
# ── 拒答判定 ────────────────────────────────────────────────────────
# 旧实现是"命中任一关键词就算拒答",而 "无法/不能/建议咨询" 在**正常**回答里
# 极常见("如果仍无法解决,建议联系人工客服"),导致误拒答率虚高到 1.000。
# 现在改成:整句级正则 + 只看开头(拒答一定开门见山)+ 长答案有实质内容则免判。
_REFUSAL_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"(抱歉|很遗憾|不好意思)[^。!!\n]{0,30}(无法|不能|不提供|没有找到|没有相关|不清楚)",
    r"无法(回答|提供|解答|查询到|找到|获取)",
    r"不能(回答|提供|解答)",
    r"(没有|未能?)(找到|查到|检索到)[^。\n]{0,8}(信息|内容|资料|记录|依据)",
    r"(知识库|资料|文档)(中|里)?(没有|未)(相关|提及)",
    r"不属于[^。\n]{0,15}(范围|领域|业务|服务)",
    r"(不在|超出)[^。\n]{0,15}(范围|能力)",
    r"(只能|仅能|只负责)(回答|处理|提供|解答)",
    r"(让我们|我们)?回到[^。\n]{0,10}(话题|问题|正题)",
    r"i\s+(cannot|can'?t|am unable to|don'?t have)",
    r"sorry,?\s+i",
)]
# 实质内容信号:分步骤、排查动作等。有这些说明答案在真正解决问题。
_SUBSTANCE_RE = re.compile(
    r"(^|\n)\s*(\d+[.、)]|[-*•])|步骤|请检查|请确认|重启|重新启动|长按|按下|"
    r"设置为|切换到|拔掉|插好|恢复出厂|升级固件|第[一二三四五六七八]步")
_REFUSAL_HEAD = 120        # 拒答一定出现在开头
_SUBSTANCE_MIN_LEN = 200   # 超过这个长度且有实质内容,开头的客套不算拒答


def _substance_score(a: str) -> int:
    """实质内容信号个数(分步骤、排查动作等)。>=2 视为真的在解决问题。"""
    return len(_SUBSTANCE_RE.findall(a))


def is_refusal(answer: str) -> bool:
    """是否为拒答。空答案算拒答;开头命中拒答句式算拒答,
    但"给出了多条实质排查步骤"视为正常回答(开头可能只是礼貌性铺垫)。"""
    a = (answer or "").strip()
    if not a:
        return True
    if any(p.search(a[:_REFUSAL_HEAD]) for p in _REFUSAL_PATTERNS):
        # 有 2 个以上实质动作信号 → 是"先说声抱歉再认真解决",不算拒答
        return not (_substance_score(a) >= 2)
    # 开头没命中:只有短答案才整体再扫一遍(长答案里的"无法"多是条件从句)
    if len(a) < _SUBSTANCE_MIN_LEN and any(p.search(a) for p in _REFUSAL_PATTERNS):
        return _substance_score(a) < 2
    return False


# ════════════════════════════════════════════════════════════════════
# 真实后端接线
# ════════════════════════════════════════════════════════════════════
def build_real_backend(judge_model: Optional[str], verbose: bool,
                       judge_max_tokens: int = 1600,
                       no_think: bool = True,
                       backend_name: str = "pgvector"):
    """构建评测后端。

    Args:
        backend_name: 后端名称 (tfidf/hybrid/pgvector)
    """
    from agent.rag_backend import retrieve_with_backend

    def retrieve_fn(q, top_k=5):
        # 保留命令行选择的后端；pgvector 会执行 DB 双路召回和 Cross-Encoder 重排。
        # top_k 仅控制最终返回/评测窗口，pgvector 内部仍按 max(top_k * 4, 20)
        # 取候选、按 max(top_k * 2, 8) 重排。
        return retrieve_with_backend(q, top_k=top_k, backend=backend_name)

    gen_call, gen_model = make_real_call(None, no_think=no_think)
    gen_llm = LLM(gen_call, f"gen:{gen_model}", verbose)
    judge_call, judge_m = make_real_call(judge_model, no_think=no_think)
    judge_llm = LLM(judge_call, f"judge:{judge_m}", verbose)

    def rewrite_fn(query, counters, cid, rnd):
        sys = ("你是检索专家。为用户问题生成 2-3 个不同措辞的搜索查询词(同义词/相关概念),"
               "每个 3-8 字,只输出 JSON 数组,如 [\"查询1\",\"查询2\"]。")
        r = gen_llm.complete(sys, f"用户问题:{query}", tag=f"gen/rewrite/{cid}#{rnd}",
                             counters=counters, max_tokens=96, temperature=0.5)
        m = re.search(r"\[.*\]", r.text, re.DOTALL)
        if m:
            try:
                arr = json.loads(m.group(0))
                if isinstance(arr, list) and arr:
                    return [str(x).strip() for x in arr[:3]]
            except Exception:
                pass
        return [query]

    def eval_fn(query, hits, counters, cid, rnd):
        preview = "\n".join(f"[{i+1}] {h.get('title')}: {str(h.get('text',''))[:120]}"
                            for i, h in enumerate(hits[:5]))
        sys = "你是检索质量评估助手,只返回 JSON。"
        user = (f"用户问题:{query}\n检索结果:\n{preview}\n\n"
                '返回 {"sufficient": true/false, "new_queries": ["...","..."]}')
        r = gen_llm.complete(sys, user, tag=f"gen/eval/{cid}#{rnd}",
                             counters=counters, max_tokens=128, temperature=0.2)
        return _extract_json(r.text) or {"sufficient": True, "new_queries": []}

    return (retrieve_fn, rewrite_fn, eval_fn, gen_llm,
            Judge(judge_llm, max_tokens=judge_max_tokens))


# ════════════════════════════════════════════════════════════════════
# Mock 后端(容器可跑通,验证逻辑)——注入假 retriever/llm/judge
# ════════════════════════════════════════════════════════════════════
def build_mock_backend(cases: List[Dict[str, Any]], verbose: bool):
    """确定性 mock。构造伪知识库:每条 case 造一个“相关片段”(正确 source+section 标题,
    正文含关键词/参考答案)+ 若干干扰片段。plain 单次检索易被干扰顶上来;agentic 改写
    拆词走“广召回”把相关片段排前,体现 rerank/多路召回价值。假 LLM/Judge 确定性打分。
    """
    corpus: List[Dict[str, Any]] = []
    for c in cases:
        gid = (c.get("golden_context_ids") or ["kb"])[0]
        sec = (c.get("golden_section") or [c["query"][:10]])[0]
        body = (c.get("reference_answer", "") + " " +
                " ".join(c.get("expected_keywords", [])))
        corpus.append({"title": sec, "source": gid, "text": body,
                       "_kws": [w.lower() for w in c.get("expected_keywords", [])]
                               + [c["query"].lower()]})
    for i in range(6):   # 噪声片段
        corpus.append({"title": f"无关小节{i}", "source": "distractor",
                       "text": "无关内容 " * 3, "_kws": [f"noise{i}"]})

    def _score(seg, terms, broaden):
        blob = (seg["title"] + " " + seg["text"] + " " +
                " ".join(seg.get("_kws", []))).lower()
        hit = sum(1 for t in terms if t and t in blob)
        return hit if broaden else (1 if hit > 0 else 0)

    def _retrieve(q, top_k, broaden):
        terms = [w for w in re.split(r"\s+", q.lower()) if w]
        scored = [(s, _score(s, terms, broaden)) for s in corpus]
        scored = [(s, sc) for s, sc in scored if sc > 0] or \
                 [(s, 0.1) for s in corpus[:top_k]]
        scored.sort(key=lambda x: (x[1], x[0]["source"] == "distractor"), reverse=True)
        return [{"title": s["title"], "source": s["source"], "text": s["text"],
                 "content": s["text"], "score": float(sc)} for s, sc in scored[:top_k]]

    def retrieve_fn(q, top_k=5):
        broaden = "\x00m" in q
        return _retrieve(q.replace("\x00m", "").strip(), top_k, broaden)

    def rewrite_fn(query, counters, cid, rnd):
        counters.add_llm(LLMResult("[]", _est_tok(query), 12, 5.0))
        if verbose:
            print(f"  · [gen:mock] gen/rewrite/{cid}#{rnd}  in≈{_est_tok(query)} out≈12 5ms")
        words = [w for w in re.split(r"[\s?？,，]", query) if len(w) >= 2][:3]
        return ["\x00m" + query] + ["\x00m" + w for w in words]

    def eval_fn(query, hits, counters, cid, rnd):
        counters.add_llm(LLMResult("{}", 40, 10, 4.0))
        return {"sufficient": len(hits) >= 2, "new_queries": []}

    # 假 gen-LLM:非拒答题回参考答案(高忠实/完整);拒答题回拒答语
    case_by_q = {c["query"]: c for c in cases}

    def gen_call(system, user, max_tokens, temperature):
        q = ""
        mm = re.search(r"问题:(.*?)\n", user)
        if mm:
            q = mm.group(1).strip()
        c = case_by_q.get(q)
        if c and c.get("should_refuse"):
            ans = "抱歉,这个问题超出智联产品咨询范围,请回到产品相关问题。"
        elif c:
            kws = "、".join(c.get("expected_keywords", []))
            ans = f"{c.get('reference_answer','')}(要点:{kws})"
        else:
            ans = "根据参考资料,建议按对应步骤处理。"
        return LLMResult(ans, _est_tok(user), _est_tok(ans), 30.0)

    gen_llm = LLM(gen_call, "gen:mock", verbose)

    # 假 judge:按“答案与参考要点关键词重叠”确定性打分,返回结构化 JSON
    def judge_call(system, user, max_tokens, temperature):
        if "只答 A/B/T" in user:      # pairwise
            a = re.search(r"回答A:\n(.*?)\n\n回答B", user, re.DOTALL)
            b = re.search(r"回答B:\n(.*?)\n\n哪个", user, re.DOTALL)
            la = len(a.group(1)) if a else 0
            lb = len(b.group(1)) if b else 0
            verdict = "A" if la >= lb else "B"
            return LLMResult(verdict, _est_tok(user), 1, 8.0)
        # pointwise:用参考要点在待评回答里的覆盖率估分
        ref = re.search(r"参考答案要点.*?:\n(.*?)\n\n待评回答", user, re.DOTALL)
        ans = re.search(r"待评回答:\n(.*)$", user, re.DOTALL)
        ref_t = ref.group(1) if ref else ""
        ans_t = ans.group(1) if ans else ""
        toks = [w for w in re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]+", ref_t)][:8]
        cov = (sum(1 for t in toks if t in ans_t) / len(toks)) if toks else 1.0
        obj = {"faithfulness": round(0.6 + 0.4 * cov, 3),
               "unsupported_claims": [],
               "answer_relevance": round(0.7 + 0.3 * cov, 3),
               "completeness": round(cov, 3),
               "reason": "mock 确定性打分(关键词覆盖)"}
        s = json.dumps(obj, ensure_ascii=False)
        return LLMResult(s, _est_tok(user), _est_tok(s), 12.0)

    judge_llm = LLM(judge_call, "judge:mock", verbose)
    return retrieve_fn, rewrite_fn, eval_fn, gen_llm, Judge(judge_llm)


# ════════════════════════════════════════════════════════════════════
# 单条评测(检索 + 生成 + Judge)
# ════════════════════════════════════════════════════════════════════
def eval_one(case: Dict[str, Any], backend, k: int, rounds: int, min_hits: int,
             mode: str, cost: Dict[str, Counters]) -> Dict[str, Any]:
    retrieve_fn, rewrite_fn, eval_fn, gen_llm, judge = backend
    cid = case["id"]
    q = case["query"]
    do_gen = mode in ("generation", "both")

    plain = run_plain(q, retrieve_fn, gen_llm, k, cost["plain"], cid, do_gen)
    ag = run_agentic(q, retrieve_fn, rewrite_fn, eval_fn, gen_llm, k, rounds,
                     min_hits, cost["agentic"], cid, do_gen)

    row: Dict[str, Any] = {"id": cid, "tier": case["tier"],
                           "category": case.get("category", ""),
                           "sg": case.get("sg", False),
                           "multi_hop": case.get("multi_hop", False),
                           "should_refuse": case.get("should_refuse", False)}

    # ── 检索层指标(文件级 + 小节级),两 pipeline ──
    for name, res in (("plain", plain), ("agentic", ag)):
        hits = res["hits"]
        ff, fm, ft = file_flags_and_recall(hits, case["golden_context_ids"], k)
        sf, sm, st = section_flags_and_recall(hits, case["golden_section"], k)
        row[f"{name}_file_hit"] = hit_rate_at_k(ff, k)
        row[f"{name}_file_recall"] = recall_at_k(fm, ft)
        row[f"{name}_file_mrr"] = mrr(ff)
        row[f"{name}_file_prec"] = precision_at_k(ff, k)
        row[f"{name}_sec_hit"] = hit_rate_at_k(sf, k)
        row[f"{name}_sec_recall"] = recall_at_k(sm, st)
        row[f"{name}_sec_mrr"] = mrr(sf)
        row[f"{name}_sec_prec"] = precision_at_k(sf, k)

    # ── 生成层(LLM-as-Judge) ──
    if do_gen:
        for name, res in (("plain", plain), ("agentic", ag)):
            ans = res.get("answer", "")
            row[f"{name}_answer"] = ans
            if case.get("should_refuse"):
                # 拒答题:只看拒答正确性,不进质量聚合
                row[f"{name}_refusal_correct"] = 1.0 if is_refusal(ans) else 0.0
            else:
                jc = cost[f"judge_{name}"]
                sc = judge.pointwise(q, res["contexts"], case.get("reference_answer", ""),
                                     ans, jc, cid, name)
                row[f"{name}_faithfulness"] = sc["faithfulness"]
                row[f"{name}_relevance"] = sc["answer_relevance"]
                row[f"{name}_completeness"] = sc["completeness"]
                row[f"{name}_hallucination"] = sc["hallucination"]
                row[f"{name}_judge_ok"] = sc["parsed_ok"]
                # 非预期拒答(正常题却拒答)= 生成层失败信号
                _wr = is_refusal(ans)
                row[f"{name}_wrong_refusal"] = 1.0 if _wr else 0.0
                if _wr:
                    # 打出来给人核对:拒答判定是启发式的,误判过多要调 is_refusal
                    head = (ans or "").strip().replace("\n", " ")[:70]
                    print(f"    [误拒答?] {name}/{cid}: {head}…", flush=True)

        # ── 位置偏差治理的成对比较(plain vs agentic) ──
        if not case.get("should_refuse"):
            winner = judge.pairwise_debiased(
                q, plain.get("answer", ""), ag.get("answer", ""),
                cost["judge_pair"], cid)
            if winner is not None:
                row["pairwise_winner"] = winner
    return row


# ════════════════════════════════════════════════════════════════════
# 聚合 & 报告
# ════════════════════════════════════════════════════════════════════
def _mean_key(rows, key):
    vals = [r[key] for r in rows if key in r and isinstance(r[key], (int, float))]
    return round(mean(vals), 4) if vals else None


def aggregate(rows: List[Dict[str, Any]], mode: str) -> Dict[str, Any]:
    tiers = ["normal", "edge", "adversarial", "high"]
    out: Dict[str, Any] = {"overall": {}, "by_tier": {}}

    def block(subset):
        b: Dict[str, Any] = {"n": len(subset)}
        # 检索指标只在“有 golden 目标”的题上聚合(拒答题无 golden,不计检索)
        retr = [r for r in subset if not r.get("should_refuse")]
        b["n_retrieval"] = len(retr)
        # 检索(文件级 / 小节级)
        for name in ("plain", "agentic"):
            for lvl in ("file", "sec"):
                b[f"{name}_{lvl}_hit"] = _mean_key(retr, f"{name}_{lvl}_hit")
                b[f"{name}_{lvl}_recall"] = _mean_key(retr, f"{name}_{lvl}_recall")
                b[f"{name}_{lvl}_mrr"] = _mean_key(retr, f"{name}_{lvl}_mrr")
                b[f"{name}_{lvl}_prec"] = _mean_key(retr, f"{name}_{lvl}_prec")
        if mode in ("generation", "both"):
            quality = [r for r in subset if not r.get("should_refuse")]
            refuse = [r for r in subset if r.get("should_refuse")]
            for name in ("plain", "agentic"):
                b[f"{name}_faithfulness"] = _mean_key(quality, f"{name}_faithfulness")
                b[f"{name}_relevance"] = _mean_key(quality, f"{name}_relevance")
                b[f"{name}_completeness"] = _mean_key(quality, f"{name}_completeness")
                b[f"{name}_hallucination"] = _mean_key(quality, f"{name}_hallucination")
                b[f"{name}_wrong_refusal_rate"] = _mean_key(quality, f"{name}_wrong_refusal")
                oks = [1.0 if r.get(f"{name}_judge_ok") else 0.0
                       for r in quality if f"{name}_judge_ok" in r]
                b[f"{name}_judge_parse_rate"] = (round(sum(oks) / len(oks), 4)
                                                 if oks else None)
                if refuse:
                    b[f"{name}_refusal_correctness"] = _mean_key(refuse, f"{name}_refusal_correct")
            wins = [r.get("pairwise_winner") for r in quality if "pairwise_winner" in r]
            if wins:
                b["pairwise"] = {
                    "n": len(wins),
                    "agentic_win": round(wins.count("agentic") / len(wins), 4),
                    "plain_win": round(wins.count("plain") / len(wins), 4),
                    "tie": round(wins.count("tie") / len(wins), 4),
                }
        return b

    out["overall"] = block(rows)
    for t in tiers:
        sub = [r for r in rows if r["tier"] == t]
        if sub:
            out["by_tier"][t] = block(sub)
    return out


TIER_LABEL = {"normal": "正常", "edge": "边缘", "adversarial": "对抗", "high": "高权重"}


def _delta(p, a):
    if p is None or a is None:
        return ""
    d = a - p
    arrow = "↑" if d > 1e-9 else ("↓" if d < -1e-9 else "＝")
    return f"Δ{d:+.3f}{arrow}"


def print_report(agg: Dict[str, Any], mode: str, k: int) -> None:
    def retr_lines(b):
        L = []
        L.append("    检索·文件级(golden_context_ids):")
        for m, lab in (("hit", f"HitRate@{k}"), ("recall", f"Recall@{k}"),
                       ("mrr", "MRR"), ("prec", f"Prec@{k}")):
            p, a = b.get(f"plain_file_{m}"), b.get(f"agentic_file_{m}")
            L.append(f"      {lab:<11} plain={_f(p)}  agentic={_f(a)}  {_delta(p,a)}")
        L.append("    检索·小节级(golden_section,测精排价值):")
        for m, lab in (("hit", f"HitRate@{k}"), ("recall", f"Recall@{k}"),
                       ("mrr", "MRR"), ("prec", f"Prec@{k}")):
            p, a = b.get(f"plain_sec_{m}"), b.get(f"agentic_sec_{m}")
            L.append(f"      {lab:<11} plain={_f(p)}  agentic={_f(a)}  {_delta(p,a)}")
        return L

    def gen_lines(b):
        L = ["    生成质量(LLM-as-Judge):"]
        for m, lab in (("faithfulness", "Faithfulness"), ("relevance", "AnswerRel"),
                       ("completeness", "Completeness"), ("hallucination", "幻觉率↓")):
            p, a = b.get(f"plain_{m}"), b.get(f"agentic_{m}")
            L.append(f"      {lab:<13} plain={_f(p)}  agentic={_f(a)}  {_delta(p,a)}")
        if b.get("plain_refusal_correctness") is not None or \
           b.get("agentic_refusal_correctness") is not None:
            L.append(f"      拒答正确性     plain={_f(b.get('plain_refusal_correctness'))}"
                     f"  agentic={_f(b.get('agentic_refusal_correctness'))}")
        if b.get("plain_wrong_refusal_rate") is not None:
            L.append(f"      误拒答率↓      plain={_f(b.get('plain_wrong_refusal_rate'))}"
                     f"  agentic={_f(b.get('agentic_wrong_refusal_rate'))}")
        if "pairwise" in b:
            pw = b["pairwise"]
            L.append(f"      成对偏好(去偏): agentic胜={pw['agentic_win']:.2f} "
                     f"plain胜={pw['plain_win']:.2f} 平={pw['tie']:.2f}"
                     f"  (有效裁判 {pw.get('n', '?')} 条)")
        pr_p, pr_a = b.get("plain_judge_parse_rate"), b.get("agentic_judge_parse_rate")
        if pr_p is not None or pr_a is not None:
            L.append(f"      Judge解析率     plain={_f(pr_p)}  agentic={_f(pr_a)}")
            worst = min([x for x in (pr_p, pr_a) if x is not None] or [1.0])
            if worst < 0.8:
                L.append("      ⚠ Judge 解析率过低,以上生成层数字不可信 —— "
                         "多半是裁判模型是推理模型且 --judge-max-tokens 太小,"
                         "或改用非推理模型做裁判(--judge-model)")
        return L

    print("\n" + "═" * 68)
    print(f"总体(n={agg['overall']['n']})")
    print("═" * 68)
    for line in retr_lines(agg["overall"]):
        print(line)
    if mode in ("generation", "both"):
        for line in gen_lines(agg["overall"]):
            print(line)

    print("\n" + "─" * 68)
    print("分层报告")
    print("─" * 68)
    for t, b in agg["by_tier"].items():
        print(f"\n【{TIER_LABEL.get(t,t)}】 n={b['n']}")
        for line in retr_lines(b):
            print(line)
        if mode in ("generation", "both"):
            for line in gen_lines(b):
                print(line)


def _f(v):
    return f"{v:6.3f}" if isinstance(v, (int, float)) else "  n/a "


def print_cost(cost: Dict[str, Counters], n: int, mode: str) -> None:
    print("\n" + "═" * 68)
    print("成本(真实 token / LLM 调用数 / 延迟)")
    print("═" * 68)

    def show(label, c: Counters, judge: Optional[Counters] = None):
        print(f"  {label}:")
        print(f"    检索次数 {c.retrievals:>4}  检索延迟 {c.retr_latency_ms:8.0f}ms "
              f"(均 {c.retr_latency_ms/max(1,n):.0f}ms/条)")
        print(f"    生成 LLM 调用 {c.llm_calls:>4}  token in={c.in_tok} out={c.out_tok} "
              f"总={c.in_tok+c.out_tok}  延迟 {c.llm_latency_ms:8.0f}ms")
        if judge is not None:
            print(f"    Judge 调用   {judge.llm_calls:>4}  token in={judge.in_tok} "
                  f"out={judge.out_tok} 总={judge.in_tok+judge.out_tok}  "
                  f"延迟 {judge.llm_latency_ms:8.0f}ms")

    jp = cost.get("judge_plain")
    ja = cost.get("judge_agentic")
    show("plain RAG", cost["plain"], jp if mode != "retrieval" else None)
    show("agentic RAG", cost["agentic"], ja if mode != "retrieval" else None)
    if mode != "retrieval":
        pair = cost.get("judge_pair")
        tot_judge = sum((cost[k].llm_calls for k in
                        ("judge_plain", "judge_agentic", "judge_pair") if k in cost))
        tot_judge_tok = sum((cost[k].in_tok + cost[k].out_tok) for k in
                            ("judge_plain", "judge_agentic", "judge_pair") if k in cost)
        print(f"  成对比较 Judge 调用 {pair.llm_calls if pair else 0} "
              f"(每条 2 次,交换顺序)")
        gen_tok = cost["plain"].in_tok + cost["plain"].out_tok + \
            cost["agentic"].in_tok + cost["agentic"].out_tok
        print(f"  合计:被评(gen) token={gen_tok}  Judge 调用={tot_judge} "
              f"token={tot_judge_tok}")


# ════════════════════════════════════════════════════════════════════
# main
# ════════════════════════════════════════════════════════════════════
def load_cases(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            rows.append(json.loads(line))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(
        description="真实端到端评测(检索真实 embedding/hybrid + 真实 LLM 生成 + LLM-as-Judge)")
    ap.add_argument("--dataset", default=DATASET)
    ap.add_argument("--mode", choices=["retrieval", "generation", "both"],
                    default="both", help="评测层:检索/生成/两者")
    ap.add_argument("--backend", default="", help="覆盖 RAG_BACKEND(tfidf|hybrid|pgvector)")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条(省钱,先小跑!)")
    ap.add_argument("--tier", default="", help="只跑某层:normal|edge|adversarial|high")
    ap.add_argument("--k", type=int, default=5, help="Top-K(默认 5)")
    ap.add_argument("--rounds", type=int, default=2, help="Agentic 最大轮数")
    # 默认 min_hits=2，降低门控阈值让 Agentic 更容易触发改写
    ap.add_argument("--min-hits", type=int, default=2, help="Agentic 门控命中阈值")
    ap.add_argument("--judge-model", default="", help="指定裁判模型(不填=与生成同模型)")
    ap.add_argument("--think", action="store_true",
                    help="保留模型思考链(默认自动关闭:评分任务不需要思维链,"
                         "开着会慢、贵,且吃光 max_tokens 导致正文为空)")
    ap.add_argument("--skip-judge-preflight", action="store_true",
                    help="跳过裁判预检(不建议)")
    ap.add_argument("--judge-max-tokens", type=int, default=1600,
                    help="Judge 单次输出预算。推理模型(会先输出思维链)务必给足,"
                         "否则 content 为空 → JSON 解析失败 → 打分全为默认值(默认 1600)")
    ap.add_argument("--mock", action="store_true", help="注入假 retriever/llm/judge(容器验证逻辑)")
    ap.add_argument("--csv", default="", help="导出逐条结果 CSV")
    ap.add_argument("--quiet", action="store_true", help="不打印每次 LLM 调用")
    args = ap.parse_args()

    if args.backend:
        os.environ["RAG_BACKEND"] = args.backend

    cases = load_cases(args.dataset)
    if args.tier:
        cases = [c for c in cases if c["tier"] == args.tier]
    if args.limit > 0:
        cases = cases[:args.limit]
    if not cases:
        print("[错误] 无用例(检查 --tier/--limit/--dataset)", file=sys.stderr)
        return 1

    backend_name = "mock" if args.mock else \
        (getattr(args, 'backend', '') or os.environ.get("RAG_BACKEND", "") or "tfidf")
    # 把"后端名"翻译成"这条链路到底做了什么",避免看到 tfidf 却以为用了重排
    BACKEND_DESC = {
        "tfidf": "TF-IDF/BM25 + 向量混合,**无 Cross-Encoder 重排**",
        "hybrid": "jieba+BM25 + 向量 + RRF 融合 + Cross-Encoder 重排",
        "pgvector": "pgvector 向量 + BM25 + RRF 融合 + Cross-Encoder 重排 + 父子分块",
        "mock": "假 retriever(仅验证逻辑)",
    }
    print("═" * 68)
    print(f"真实端到端评测 · 数据集 {Path(args.dataset).name} · {len(cases)} 条 · "
          f"Top-{args.k} · mode={args.mode}")
    print(f"后端 RAG_BACKEND={backend_name} → "
          f"{BACKEND_DESC.get(backend_name, '未知')}")
    print(f"Agentic≤{args.rounds}轮/门控{args.min_hits} · "
          f"{'MOCK(无费用)' if args.mock else '真实模式(会产生 LLM/embedding 费用)'}")
    if not args.mock and args.mode != "retrieval":
        print(f"生成模型=默认(.env OPENAI_MODEL)  裁判模型="
              f"{args.judge_model or '同生成模型'}")
    print("═" * 68)

    verbose = not args.quiet
    if args.mock:
        backend = build_mock_backend(cases, verbose)
    else:
        try:
            # 从 --backend 参数获取后端名称，默认 pgvector
            backend_name = getattr(args, 'backend', 'pgvector')
            backend = build_real_backend(args.judge_model or None, verbose,
                                         judge_max_tokens=args.judge_max_tokens,
                                         no_think=not args.think,
                                         backend_name=backend_name)
        except Exception as exc:
            print(f"[错误] 真实后端不可用({type(exc).__name__}: {exc})。"
                  f"可先 --mock 验证逻辑,或检查依赖/.env。", file=sys.stderr)
            return 1

    cost = {k: Counters() for k in
            ("plain", "agentic", "judge_plain", "judge_agentic", "judge_pair")}

    # ── 预检:先花 1 次调用确认裁判能吐出可解析 JSON ──
    # 92 条 × 4 次 Judge 要跑两三个小时,不能等跑完才发现裁判一直在返回空正文。
    if (not args.mock and not args.skip_judge_preflight
            and args.mode in ("generation", "both")):
        _judge = backend[4]
        probe = _judge.pointwise("退货运费谁承担?", ["七天无理由退货,运费由买家承担。"],
                                 "运费由买家承担", "运费由买家承担。",
                                 Counters(), "preflight", "probe")
        if not probe.get("parsed_ok"):
            print("\n[预检失败] 裁判模型没有返回可解析的 JSON。", file=sys.stderr)
            print("  常见原因:裁判是推理模型,输出预算被思维链吃光,content 为空。",
                  file=sys.stderr)
            print(f"  当前 --judge-max-tokens={args.judge_max_tokens},"
                  f"可加大到 3000,或用 --judge-model 换非推理模型。", file=sys.stderr)
            print("  若上面没出现 [no-think] 已用 ... 关闭思考链,说明本服务不认"
                  "自动探测的几种参数,可手动指定,例如:", file=sys.stderr)
            print('    set NO_THINK_PARAMS={"chat_template_kwargs":'
                  '{"enable_thinking":false}}', file=sys.stderr)
            print("  确认要继续(结果将不可信)请加 --skip-judge-preflight。", file=sys.stderr)
            return 2
        print("  [preflight] Judge JSON parse OK / 裁判 JSON 解析正常", flush=True)
    rows: List[Dict[str, Any]] = []
    t0 = time.perf_counter()
    for i, case in enumerate(cases, 1):
        print(f"\n[{i}/{len(cases)}] {case['id']} ({TIER_LABEL.get(case['tier'])}"
              f"{'·语义鸿沟' if case.get('sg') else ''}"
              f"{'·多跳' if case.get('multi_hop') else ''}) {case['query'][:40]}")
        try:
            rows.append(eval_one(case, backend, args.k, args.rounds,
                                 args.min_hits, args.mode, cost))
        except Exception as exc:
            print(f"  [跳过] {case['id']} 评测异常: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
    dt = time.perf_counter() - t0

    agg = aggregate(rows, args.mode)
    print_report(agg, args.mode, args.k)
    print_cost(cost, len(rows), args.mode)
    print(f"\n耗时 {dt:.1f}s · 每条约 {dt/max(1,len(rows)):.2f}s")

    # 结论:agentic 相对 plain 的检索/生成增益
    ov = agg["overall"]
    sec_gain = (ov.get("agentic_sec_hit") or 0) - (ov.get("plain_sec_hit") or 0)
    print("─" * 68)
    print(f"结论:小节级 HitRate@{args.k} agentic 相对 plain {sec_gain:+.3f};"
          f" 语义鸿沟题最能拉开检索差距(见 EVAL_REAL_README.md)。")

    if args.csv:
        import csv
        keys = sorted({k for r in rows for k in r.keys()})
        # 答案文本单列到末尾,分数在前
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"逐条结果已导出: {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
