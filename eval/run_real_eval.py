# -*- coding: utf-8 -*-
"""
Real RAG Evaluation — 真实链路评估（pgvector 检索 + 远程 rerank + 本地 LLM 生成 + LLM-as-Judge）

指标（用户要求的 7 项）：
  1. Hit Rate@K      召回      正确证据是否出现在前 K 个结果里
  2. MRR             排序      第一个正确证据排得有多靠前
  3. Context Recall  召回完整性 回答所需证据是否被找全（对照 golden_answer 要点）
  4. Context Precision 上下文纯度 放入上下文的内容有多少真的相关
  5. Faithfulness    生成忠实度 答案是否能被上下文支撑（逐句核对）
  6. Answer Relevancy 回答相关性 答案是否真正回应用户问题
  7. Citation Accuracy 引用准确性 引用位置是否支撑对应结论（答案带 [n] 引用时）

用法：
    python eval/run_real_eval.py --limit 5            # 小跑 5 条（含 generation 层）
    python eval/run_real_eval.py --layer generation   # 只跑 generation 层
    python eval/run_real_eval.py --all                # 全量 63 条
    python eval/run_real_eval.py --k 5                # 默认 top-k=5

真实链路：
  - 检索：pgvector hybrid（EmbeddingClient → SiliconFlow Qwen3-Embedding-8B）+ RRF + 远程 rerank
    （RemoteReranker → SiliconFlow Qwen3-Reranker-8B），复用 agent.rag_backend.retrieve_with_backend
  - 生成：本地 LLM（.env OPENAI_BASE_URL=127.0.0.1:8080 llama.cpp），max_tokens 足够容纳 reasoning
  - Judge：同一本地 LLM 二次调用，结构化 JSON 打分

输出：eval/reports/real_eval_{timestamp}.md + .json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Windows console UTF-8
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from agent.llm_client import LLMClient  # noqa: E402
from agent.rag_backend import retrieve_with_backend  # noqa: E402

GOLDEN_SET = PROJECT_ROOT / "eval" / "golden_set.jsonl"
REPORTS_DIR = PROJECT_ROOT / "eval" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_K = 5
LLM_MAX_TOKENS = 2048  # reasoning 模型需要足够 token 才输出正式回答
LLM_TIMEOUT = 240

# ── LLM prompts ──────────────────────────────────────────────

SYSTEM_JUDGE = "你是一个严格的 RAG 评估专家。只输出合法 JSON，不要输出其他文字。"

GENERATE_PROMPT = """你是智能客服助手。请根据【参考资料】回答用户问题。

【参考资料】
{context}

【用户问题】
{query}

要求：
1. 只依据参考资料回答；资料中没有的，明确说明"资料中未提及"。
2. 回答中必须标注引用来源编号 [n]（n 对应参考资料条目编号），每个结论后紧跟引用。
3. 引用编号必须指向实际包含该信息的条目；不得凭空指定或使用无关条目编号。
4. 用中文回答，简洁准确，不要复述"根据参考资料"这类话。

直接输出回答正文："""

CITATION_CHECK_PROMPT = """你是引用准确性评审。下面是一个回答及其参考资料，回答中标注了 [n] 引用。

【参考资料】
{context}

【回答（含引用标注）】
{answer}

请对回答中的每个 [n] 引用判断：该位置的结论是否确实由编号为 n 的资料支撑。

返回 JSON（只输出 JSON，不要 markdown）：
{{"citations": [{{"n": 1, "supported": true, "reason": "简述"}}], "citation_accuracy": 0.75}}
其中 citation_accuracy = 被支撑的引用数 / 总引用数；回答中没有任何 [n] 引用时返回 citation_accuracy=0。"""

FAITHFULNESS_PROMPT = """你是事实核查专家。判断回答中的每个陈述是否都能在参考资料中找到依据。

【参考资料】
{context}

【回答】
{answer}

请逐句分析回答中的陈述，判断是否有无根据的内容（参考资料中未提及或与之矛盾的信息）。

返回 JSON（只输出 JSON）：
{{"faithful": true/false, "unfounded_claims": ["无依据的陈述1", ...], "faithfulness": 0.8, "reason": "简述"}}
其中 faithfulness = 有依据的陈述占比（0~1）"""

ANSWER_RELEVANCY_PROMPT = """你是回答相关性评审。判断回答是否真正回应了用户的问题。

【用户问题】
{query}

【回答】
{answer}

评分标准（1~5 分）：
- 5：直接、完整地回答了问题核心
- 4：回答了问题但略有冗余或遗漏次要部分
- 3：部分相关，但偏离或缺失关键信息
- 2：勉强相关，大部分内容答非所问
- 1：完全不相关或答非所问

返回 JSON（只输出 JSON）：
{{"score": 4, "reason": "简述"}}"""

CONTEXT_PRECISION_PROMPT = """你是上下文质量评审。判断以下每个参考资料条目是否与用户问题相关（有用信息）。

【用户问题】
{query}

【参考资料条目】
{context}

对每个条目判断 relevance：
- 包含回答问题所需的信息 → relevant=true
- 与问题无关或只是噪声 → relevant=false

返回 JSON（只输出 JSON）：
{{"relevance": [{{"n": 1, "relevant": true, "reason": "简述"}}, ...]}}"""

CONTEXT_RECALL_PROMPT = """你是上下文召回完整性评审。判断参考资料是否覆盖了回答该问题所需的全部关键信息。

【用户问题】
{query}

【理想答案要点】（golden answer 或关键点）
{key_points}

【参考资料】
{context}

请判断：参考资料是否包含了上述每个要点所需的信息？

返回 JSON（只输出 JSON）：
{{"points": [{{"point": "要点简述", "covered": true/false, "reason": "简述"}}], "context_recall": 0.75}}
其中 context_recall = 被覆盖的要点占比"""


# ── LLM helpers ──────────────────────────────────────────────

def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 输出中提取第一个 JSON 对象。"""
    if not text:
        return None
    # 剥离 markdown 代码块围栏
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        # 尝试修复截断的 JSON：补全括号
        raw = match.group()
        try:
            return json.loads(raw + "}")
        except json.JSONDecodeError:
            return None


class RealEvaluator:
    def __init__(self, k: int = DEFAULT_K, max_items: Optional[int] = None,
                 layer_filter: Optional[str] = None, ids: Optional[List[str]] = None,
                 dataset: Optional[Path] = None, verbose: bool = True,
                 multi_turn: bool = False):
        self.k = k
        self.max_items = max_items
        self.layer_filter = layer_filter
        self.ids = ids
        self.dataset = dataset or GOLDEN_SET
        self.verbose = verbose
        self.multi_turn = bool(multi_turn)
        self.llm = self._build_llm()
        self._llm_warm = False

    def _build_llm(self) -> LLMClient:
        """显式传参走 direct HTTP（不走 gateway，避免 fallback chain 干扰评估）。"""
        base_url = os.getenv("OPENAI_BASE_URL", "").rstrip("/")
        api_key = os.getenv("OPENAI_API_KEY", "") or "sk-local"
        # llama.cpp 的模型 id：优先 /v1/models 探测，否则用 env OPENAI_MODEL 或默认
        model = os.getenv("OPENAI_MODEL", "") or "Ternary-Bonsai-27B-Q2_0.gguf"
        return LLMClient(base_url=base_url, api_key=api_key, model=model,
                         max_tokens=LLM_MAX_TOKENS)

    # ── data loading ─────────────────────────────────────────

    def load_dataset(self) -> List[Dict[str, Any]]:
        items = []
        with open(self.dataset, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                items.append(json.loads(line))
        if self.ids:
            id_set = set(self.ids)
            items = [it for it in items if it["id"] in id_set]
        elif self.layer_filter:
            items = [it for it in items if it["layer"] == self.layer_filter]
        if self.max_items:
            items = items[: self.max_items]
        return items

    # ── LLM call ─────────────────────────────────────────────

    def _chat(self, prompt: str, system: str = SYSTEM_JUDGE) -> str:
        return self.llm.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=LLM_MAX_TOKENS,
        )

    def _chat_json(self, prompt: str) -> Dict[str, Any]:
        text = self._chat(prompt)
        result = extract_json(text)
        if result is None and self.verbose:
            print(f"    [warn] LLM JSON parse failed, raw: {text[:120]!r}")
        return result or {}

    # ── retrieval ────────────────────────────────────────────

    def retrieve(self, query: str) -> List[Dict[str, Any]]:
        hits = retrieve_with_backend(query, top_k=self.k, backend="pgvector")
        return hits or []

    # ── generation ───────────────────────────────────────────

    def generate(self, query: str, contexts: List[Dict[str, Any]],
                 item: Optional[Dict[str, Any]] = None) -> str:
        ctx_parts = []
        for i, h in enumerate(contexts, 1):
            content = h.get("content") or h.get("text") or ""
            src = h.get("source", "")
            ctx_parts.append(f"[{i}] (来源: {src})\n{content}")
        context_str = "\n\n".join(ctx_parts)
        # 多轮模式：把 conversation 前缀轮次作为历史注入
        history_str = ""
        if self.multi_turn and item and item.get("conversation"):
            turns = []
            for m in item["conversation"]:
                role = "用户" if m.get("role") == "user" else "客服"
                turns.append(f"{role}: {m.get('content', '')}")
            history_str = "\n".join(turns)
        prompt = GENERATE_PROMPT.format(context=context_str, query=query)
        if history_str:
            prompt = (f"以下是本次对话的历史记录（按时间顺序）：\n{history_str}\n\n"
                      f"请结合历史记录回答用户的最新问题。\n\n{prompt}")
        answer = self._chat(prompt, system="你是智能客服助手，只输出回答正文。")
        return answer.strip()

    # ── LLM-as-Judge metrics ─────────────────────────────────

    def judge_citation_accuracy(self, contexts: List[Dict[str, Any]], answer: str) -> Dict[str, Any]:
        if not answer:
            return {"citation_accuracy": 0.0, "citations": [], "error": "empty answer"}
        ctx_parts = []
        for i, h in enumerate(contexts, 1):
            content = (h.get("content") or h.get("text") or "")[:2000]
            src = h.get("source", "")
            ctx_parts.append(f"[{i}] (来源: {src})\n{content}")
        prompt = CITATION_CHECK_PROMPT.format(
            context="\n\n".join(ctx_parts), answer=answer[:1200])
        data = self._chat_json(prompt)
        acc = data.get("citation_accuracy")
        if acc is None:
            citations = data.get("citations") or []
            if citations:
                supported = sum(1 for c in citations if c.get("supported"))
                acc = supported / len(citations)
            else:
                acc = 0.0
        return {"citation_accuracy": float(acc), "citations": data.get("citations", []),
                "reason": data.get("reason", "")}

    def judge_faithfulness(self, contexts: List[Dict[str, Any]], answer: str) -> Dict[str, Any]:
        if not answer:
            return {"faithfulness": 0.0, "reason": "empty answer"}
        context_str = "\n\n".join(
            (h.get("content") or h.get("text") or "")[:2000] for h in contexts)
        data = self._chat_json(FAITHFULNESS_PROMPT.format(context=context_str, answer=answer[:1500]))
        score = data.get("faithfulness")
        if score is None:
            score = 1.0 if data.get("faithful") else 0.0
        return {"faithfulness": float(score), "reason": data.get("reason", ""),
                "unfounded": data.get("unfounded_claims", [])}

    def judge_answer_relevancy(self, query: str, answer: str) -> Dict[str, Any]:
        if not answer:
            return {"answer_relevancy": 0.0, "reason": "empty answer"}
        data = self._chat_json(ANSWER_RELEVANCY_PROMPT.format(query=query, answer=answer[:1000]))
        raw = data.get("score")
        try:
            raw = float(raw)
        except (TypeError, ValueError):
            raw = 3.0
        return {"answer_relevancy": raw / 5.0, "raw_score": raw, "reason": data.get("reason", "")}

    def judge_context_precision(self, query: str, contexts: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not contexts:
            return {"context_precision": 0.0, "reason": "no contexts"}
        ctx_parts = []
        for i, h in enumerate(contexts, 1):
            content = (h.get("content") or h.get("text") or "")[:2000]
            ctx_parts.append(f"[{i}]\n{content}")
        data = self._chat_json(CONTEXT_PRECISION_PROMPT.format(
            query=query, context="\n\n".join(ctx_parts)))
        relevance = data.get("relevance") or []
        if not relevance:
            return {"context_precision": 0.0, "reason": "no relevance data"}
        relevant = sum(1 for r in relevance if r.get("relevant"))
        return {"context_precision": relevant / len(relevance),
                "relevant_count": relevant, "total": len(relevance)}

    def judge_context_recall(self, query: str, item: Dict[str, Any],
                             contexts: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not contexts:
            return {"context_recall": 0.0, "reason": "no contexts"}
        key_points = item.get("key_points") or item.get("reference_points") or []
        if not key_points:
            return {"context_recall": None, "reason": "no key_points in dataset"}
        context_str = "\n\n".join(
            (h.get("content") or h.get("text") or "")[:2000] for h in contexts)
        points_str = "\n".join(f"- {p}" for p in key_points)
        data = self._chat_json(CONTEXT_RECALL_PROMPT.format(
            query=query, key_points=points_str, context=context_str))
        score = data.get("context_recall")
        if score is None:
            points = data.get("points") or []
            if points:
                score = sum(1 for p in points if p.get("covered")) / len(points)
            else:
                score = 0.0
        return {"context_recall": float(score), "points": data.get("points", []),
                "reason": data.get("reason", "")}

    # ── rule-based retrieval metrics ─────────────────────────

    @staticmethod
    def _golden_sources(item: Dict[str, Any]) -> set:
        return set(item.get("golden_context_ids") or [])

    def hit_rate_at_k(self, retrieved: List[Dict[str, Any]], golden: set) -> float:
        if not golden:
            return 1.0  # 无 golden（如拒答）视为命中
        top = retrieved[: self.k]
        return 1.0 if any(h.get("source") in golden for h in top) else 0.0

    def mrr(self, retrieved: List[Dict[str, Any]], golden: set) -> float:
        if not golden:
            return 1.0
        for i, h in enumerate(retrieved[: self.k], 1):
            if h.get("source") in golden:
                return 1.0 / i
        return 0.0

    # ── per-item evaluation ──────────────────────────────────

    def evaluate_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        query = item["query"]
        item_id = item["id"]
        layer = item["layer"]
        golden = self._golden_sources(item)
        start = time.time()

        # 1. 真实检索（pgvector + rerank）
        retrieved = self.retrieve(query)

        result: Dict[str, Any] = {
            "id": item_id, "layer": layer, "category": item.get("category", ""),
            "difficulty": item.get("difficulty", ""), "query": query,
            "golden_sources": sorted(golden),
            "retrieved_sources": [h.get("source", "") for h in retrieved],
            "retrieved_titles": [h.get("title", "") for h in retrieved],
            "rerank_scores": [round(float(h.get("rerank_score", 0) or 0), 4) for h in retrieved],
            "reranker_provider": (retrieved[0].get("reranker_provider", "") if retrieved else ""),
        }

        # 2. 检索层指标（规则计算）
        result["hit_rate_at_k"] = self.hit_rate_at_k(retrieved, golden)
        result["mrr"] = self.mrr(retrieved, golden)

        # 3. generation 层指标（需 LLM）
        if layer == "generation":
            result["generation"] = {}
            gen = result["generation"]

            # 生成回答（带引用）
            answer = self.generate(query, retrieved, item)
            gen["answer"] = answer

            # Citation Accuracy（检查回答中的引用）
            cit = self.judge_citation_accuracy(retrieved, answer)
            gen["citation_accuracy"] = cit["citation_accuracy"]
            gen["citation_detail"] = cit.get("citations", [])
            if cit.get("reason"):
                gen["citation_reason"] = cit["reason"]

            # Faithfulness
            fai = self.judge_faithfulness(retrieved, answer)
            gen["faithfulness"] = fai["faithfulness"]
            if fai.get("reason"):
                gen["faithfulness_reason"] = fai["reason"]

            # Answer Relevancy
            rel = self.judge_answer_relevancy(query, answer)
            gen["answer_relevancy"] = rel["answer_relevancy"]
            gen["answer_relevancy_raw"] = rel.get("raw_score")
            if rel.get("reason"):
                gen["answer_relevancy_reason"] = rel["reason"]

            # Context Precision
            cpr = self.judge_context_precision(query, retrieved)
            gen["context_precision"] = cpr["context_precision"]

            # Context Recall
            cre = self.judge_context_recall(query, item, retrieved)
            gen["context_recall"] = cre["context_recall"]
            if cre.get("reason"):
                gen["context_recall_reason"] = cre["reason"]

        result["elapsed_s"] = round(time.time() - start, 2)
        return result

    # ── runner ───────────────────────────────────────────────

    def run(self) -> Dict[str, Any]:
        items = self.load_dataset()
        print(f"评估数据集: {len(items)} 条 (k={self.k})")
        results = []
        for idx, item in enumerate(items, 1):
            print(f"[{idx}/{len(items)}] {item['id']} ({item['layer']}/{item['difficulty']}) {item['query'][:40]} ...")
            try:
                r = self.evaluate_item(item)
                results.append(r)
                self._print_item_summary(r)
            except Exception as exc:  # noqa: BLE001
                print(f"    [ERROR] {exc}")
                results.append({"id": item["id"], "error": str(exc),
                                "query": item["query"]})
        return self._aggregate(results)

    def _print_item_summary(self, r: Dict[str, Any]) -> None:
        if self.verbose:
            print(f"    Hit@{self.k}={r.get('hit_rate_at_k')} MRR={r.get('mrr'):.3f}"
                  + (f" | gen: cit={r.get('generation',{}).get('citation_accuracy'):.2f}"
                     f" faith={r.get('generation',{}).get('faithfulness'):.2f}"
                     f" rel={r.get('generation',{}).get('answer_relevancy'):.2f}"
                     f" cprec={r.get('generation',{}).get('context_precision'):.2f}"
                     f" crec={r.get('generation',{}).get('context_recall')}"
                     if "generation" in r else "")
                  + f" [{r.get('elapsed_s')}s]")

    # ── aggregation ──────────────────────────────────────────

    def _aggregate(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        ok = [r for r in results if "error" not in r]
        gen = [r for r in ok if "generation" in r]
        retrieval = [r for r in ok if "generation" not in r]

        def mean(seq):
            vals = [v for v in seq if v is not None]
            return sum(vals) / len(vals) if vals else None

        agg: Dict[str, Any] = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "k": self.k,
            "total": len(results), "ok": len(ok),
            "errors": [r for r in results if "error" in r],
            "metrics": {
                "hit_rate_at_k": mean([r["hit_rate_at_k"] for r in ok]),
                "mrr": mean([r["mrr"] for r in ok]),
                "context_recall": mean([r["generation"]["context_recall"] for r in gen]),
                "context_precision": mean([r["generation"]["context_precision"] for r in gen]),
                "faithfulness": mean([r["generation"]["faithfulness"] for r in gen]),
                "answer_relevancy": mean([r["generation"]["answer_relevancy"] for r in gen]),
                "citation_accuracy": mean([r["generation"]["citation_accuracy"] for r in gen]),
            },
            "counts": {
                "retrieval_layer": len(retrieval),
                "generation_layer": len(gen),
            },
            "by_difficulty": {},
            "by_category": {},
            "items": results,
        }

        for key in ("by_difficulty", "by_category"):
            groups = defaultdict(list)
            for r in ok:
                groups[r.get(key.replace("by_", ""), "unknown")].append(r)
            for gname, grp in groups.items():
                agg[key][gname] = {
                    "count": len(grp),
                    "hit_rate_at_k": mean([r["hit_rate_at_k"] for r in grp]),
                    "mrr": mean([r["mrr"] for r in grp]),
                }
                ggen = [r for r in grp if "generation" in r]
                if ggen:
                    agg[key][gname].update({
                        "context_recall": mean([r["generation"]["context_recall"] for r in ggen]),
                        "context_precision": mean([r["generation"]["context_precision"] for r in ggen]),
                        "faithfulness": mean([r["generation"]["faithfulness"] for r in ggen]),
                        "answer_relevancy": mean([r["generation"]["answer_relevancy"] for r in ggen]),
                        "citation_accuracy": mean([r["generation"]["citation_accuracy"] for r in ggen]),
                    })
        return agg

    # ── report ───────────────────────────────────────────────

    @staticmethod
    @staticmethod
    def to_markdown(agg: Dict[str, Any]) -> str:
        m = agg["metrics"]

        def pct(v):
            return "N/A" if v is None else f"{v:.2%}"

        lines = [
            "# 真实链路 RAG 评估报告",
            "",
            f"- 时间: {agg['timestamp']}",
            f"- Top-K: {agg['k']}",
            f"- 有效样本: {agg['ok']}/{agg['total']}"
            + (f" (失败: {len(agg['errors'])}) " if agg["errors"] else ""),
            "",
            "## 总体指标",
            "",
            "| 指标 | 含义 | 得分 |",
            "|------|------|------|",
            f"| Hit Rate@{agg['k']} | 正确证据是否出现在前 K 个结果里 | **{pct(m['hit_rate_at_k'])}** |",
            f"| MRR | 第一个正确证据排得有多靠前 | **{('N/A' if m['mrr'] is None else format(m['mrr'], '.3f'))}** |",
            f"| Context Recall | 回答所需证据是否被找全 | **{pct(m['context_recall'])}** |",
            f"| Context Precision | 放入上下文的内容有多少真的相关 | **{pct(m['context_precision'])}** |",
            f"| Faithfulness | 答案能否被上下文支撑 | **{pct(m['faithfulness'])}** |",
            f"| Answer Relevancy | 答案是否真正回应用户问题 | **{pct(m['answer_relevancy'])}** |",
            f"| Citation Accuracy | 引用位置是否支撑对应结论 | **{pct(m['citation_accuracy'])}** |",
            "",
        ]
        if agg["by_difficulty"]:
            lines += ["## 按难度分组", "",
                      "| 难度 | N | Hit Rate | MRR | C.Recall | C.Precision | Faith. | Rel. | Cit.Acc |",
                      "|------|---|---------|-----|----------|-------------|--------|------|---------|"]
            for diff, g in sorted(agg["by_difficulty"].items()):
                def fmt(v, pct=True):
                    return f"{v:.2%}" if v is not None and pct else ("N/A" if v is None else f"{v:.2f}")
                lines.append(
                    f"| {diff} | {g['count']} | {fmt(g.get('hit_rate_at_k'))} | {fmt(g.get('mrr'), False)} | "
                    f"{fmt(g.get('context_recall'))} | {fmt(g.get('context_precision'))} | "
                    f"{fmt(g.get('faithfulness'))} | {fmt(g.get('answer_relevancy'))} | "
                    f"{fmt(g.get('citation_accuracy'))} |")
            lines.append("")

        if agg["by_category"]:
            lines += ["## 按类别分组", "",
                      "| 类别 | N | Hit Rate | MRR | C.Recall | C.Precision | Faith. | Rel. | Cit.Acc |",
                      "|------|---|---------|-----|----------|-------------|--------|------|---------|"]
            for cat, g in sorted(agg["by_category"].items()):
                def fmt(v, pct=True):
                    return f"{v:.2%}" if v is not None and pct else ("N/A" if v is None else f"{v:.2f}")
                lines.append(
                    f"| {cat} | {g['count']} | {fmt(g.get('hit_rate_at_k'))} | {fmt(g.get('mrr'), False)} | "
                    f"{fmt(g.get('context_recall'))} | {fmt(g.get('context_precision'))} | "
                    f"{fmt(g.get('faithfulness'))} | {fmt(g.get('answer_relevancy'))} | "
                    f"{fmt(g.get('citation_accuracy'))} |")
            lines.append("")

        lines += ["## 逐条明细", "", "| ID | 难度 | 问题 | Hit | MRR | 引用 | 忠实 | 相关 | 召回 | 精度 |",
                  "|----|------|------|-----|-----|------|------|------|------|------|"]
        for r in agg["items"]:
            if "error" in r:
                lines.append(f"| {r['id']} | - | {r.get('query','')[:30]} | ERROR | | | | | | |")
                continue
            g = r.get("generation", {})
            fmt = lambda v: ("N/A" if v is None else f"{v:.2%}")  # noqa: E731
            lines.append(
                f"| {r['id']} | {r['difficulty']} | {r['query'][:28]} | "
                f"{r['hit_rate_at_k']:.0%} | {r['mrr']:.2f} | {fmt(g.get('citation_accuracy'))} | "
                f"{fmt(g.get('faithfulness'))} | {fmt(g.get('answer_relevancy'))} | "
                f"{fmt(g.get('context_recall'))} | {fmt(g.get('context_precision'))} |")
        lines.append("")
        return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="真实链路 RAG 评估（pgvector + rerank + LLM + Judge）")
    ap.add_argument("--dataset", default=None, help="数据集文件路径（默认 eval/golden_set.jsonl）")
    ap.add_argument("--k", type=int, default=DEFAULT_K, help="Top-K")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 条")
    ap.add_argument("--layer", choices=["retrieval", "generation", "agent", "engineering"],
                    default=None, help="只跑某一层")
    ap.add_argument("--ids", default=None, help="只跑指定 ID 列表（逗号分隔），如 ret-01,gen-02")
    ap.add_argument("--all", action="store_true", help="全量 63 条")
    ap.add_argument("--quiet", action="store_true", help="不打印逐条明细")
    ap.add_argument("--multi-turn", action="store_true",
                    help="多轮模式：注入数据集中 conversation 前缀轮次作为历史")
    args = ap.parse_args()

    if args.all:
        limit, layer, ids = None, None, None
    else:
        limit, layer = args.limit, args.layer
        ids = [x.strip() for x in args.ids.split(",")] if args.ids else None

    ev = RealEvaluator(k=args.k, max_items=limit, layer_filter=layer, ids=ids,
                       dataset=Path(args.dataset) if args.dataset else None,
                       verbose=not args.quiet, multi_turn=args.multi_turn)
    agg = ev.run()

    md = RealEvaluator.to_markdown(agg)
    print()
    print(md)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = REPORTS_DIR / f"real_eval_{ts}.md"
    json_path = REPORTS_DIR / f"real_eval_{ts}.json"
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存: {md_path}")
    print(f"数据已保存: {json_path}")


if __name__ == "__main__":
    main()
