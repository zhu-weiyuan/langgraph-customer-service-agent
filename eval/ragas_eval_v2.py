# -*- coding: utf-8 -*-
"""
RAGAS-style RAG Evaluation — 增强版评估体系

核心指标（参考 RAGAS/TruLens/LangSmith）：
1. Faithfulness - 回答是否忠实于检索到的上下文（不自编）
2. Answer Relevancy - 回答是否切题
3. Context Precision - 检索结果中真正相关的占比
4. Context Recall - 有没有漏掉该找到的相关信息

使用方式：
    from eval.ragas_eval_v2 import RagasEvaluatorV2
    evaluator = RagasEvaluatorV2()
    report = evaluator.evaluate_dataset(dataset)
    print(report.to_markdown())

与 v1 的区别：
- v1: 只有 HitRate@K / Recall@K / MRR / Coverage（纯检索指标）
- v2: 增加 Faithfulness / Answer Relevancy / Context Precision（含 LLM-as-Judge）
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.json_parsing import parse_json_object

# Windows PowerShell GBK 编码兼容
if sys.platform == "win32":
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')


# ── LLM-as-Judge Prompts (中文) ───────────────────────────────

FAITHFULNESS_PROMPT = """你是一个事实核查专家。请判断以下回答中的每一个声明是否都能在参考资料中找到依据。

参考资料：
{context}

回答：
{answer}

请逐句分析回答中的声明，判断是否有无根据的内容（即参考资料中没有提及或矛盾的信息）。

返回严格的 JSON 格式（不要其他文字）：
{{"faithful": true/false, "unfounded_claims": ["无根据的声明1", ...], "reason": "简短理由"}}

规则：
- 如果回答中的所有声明都能在参考资料中找到依据 → faithful: true
- 如果回答包含了参考资料中没有的信息 → faithful: false，列出无根据的声明
- 如果回答只是说"我不确定"或"我没有相关信息" → faithful: true（诚实比编造好）"""

ANSWER_RELEVANCY_PROMPT = """你是一个问答质量评估专家。请评估以下回答与问题的相关程度。

问题：{query}
回答：{answer}

评分标准（1-5分）：
1 = 完全不相关，答非所问
2 = 部分相关，但大部分内容偏离主题
3 = 基本相关，回答了部分问题
4 = 高度相关，回答了主要问题
5 = 完美切题，完全回答了用户的问题

返回严格的 JSON 格式（不要其他文字）：
{{"score": 评分数字, "reason": "简短理由"}}"""

CONTEXT_PRECISION_PROMPT = """你是一个信息相关性判断专家。请判断以下每条参考资料是否与用户问题相关。

用户问题：{query}

参考资料：
{contexts}

对每条资料判断相关性，返回严格的 JSON 格式（不要其他文字）：
{{"relevance": [{{"index": 1, "relevant": true/false}}, {{"index": 2, "relevant": true/false}}, ...]}}

规则：
- relevant: true = 这条资料包含回答用户问题所需的信息
- relevant: false = 这条资料与用户问题无关或信息不足"""


# ── Data Classes ───────────────────────────────────────────────

@dataclass
class SingleEvalResult:
    """单条查询的评估结果"""
    query: str
    difficulty: str = ""
    category: str = ""
    ground_truth: List[str] = field(default_factory=list)
    retrieved_sources: List[str] = field(default_factory=list)
    retrieved_texts: List[str] = field(default_factory=list)

    # Retrieval metrics (v1 compatible)
    hit_rate_1: float = 0.0
    hit_rate_3: float = 0.0
    recall_at_3: float = 0.0
    context_recall: float = 0.0

    # RAGAS-style metrics (LLM-based, optional)
    faithfulness: Optional[float] = None
    answer_relevancy: Optional[float] = None
    context_precision: Optional[float] = None

    # Details
    faithfulness_reason: str = ""
    relevancy_reason: str = ""


@dataclass
class RagasEvalReport:
    """综合评估报告"""
    total_queries: int = 0

    # Retrieval metrics (avg)
    hit_rate_1_avg: float = 0.0
    hit_rate_3_avg: float = 0.0
    recall_at_3_avg: float = 0.0
    context_recall_avg: float = 0.0

    # RAGAS metrics (avg, may be None if LLM not available)
    faithfulness_avg: Optional[float] = None
    answer_relevancy_avg: Optional[float] = None
    context_precision_avg: Optional[float] = None

    # Per-difficulty breakdown
    by_difficulty: Dict[str, Dict[str, float]] = field(default_factory=dict)

    # Details
    results: List[SingleEvalResult] = field(default_factory=list)
    llm_used: bool = False

    def to_markdown(self) -> str:
        lines = [
            "# RAG Evaluation Report",
            "",
            f"**Total Queries:** {self.total_queries}",
            f"**LLM-as-Judge:** {'✅ Yes' if self.llm_used else '❌ No (retrieval only)'}",
            "",
            "## Retrieval Metrics",
            "",
            "| Metric | Score |",
            "|--------|-------|",
            f"| HitRate@1 | {self.hit_rate_1_avg:.2%} |",
            f"| HitRate@3 | {self.hit_rate_3_avg:.2%} |",
            f"| Recall@3 | {self.recall_at_3_avg:.2%} |",
            f"| Context Recall | {self.context_recall_avg:.2%} |",
        ]

        if self.llm_used and self.faithfulness_avg is not None:
            lines.extend([
                "",
                "## RAGAS Metrics (LLM-as-Judge)",
                "",
                "| Metric | Score | Description |",
                "|--------|-------|-------------|",
                f"| Faithfulness | {self.faithfulness_avg:.2%} | 回答是否忠实于参考资料 |",
                f"| Answer Relevancy | {self.answer_relevancy_avg:.2%} | 回答是否切题 |",
                f"| Context Precision | {self.context_precision_avg:.2%} | 检索结果的相关性 |",
            ])

        # Replace emoji with text for Windows GBK compatibility
        md_text = "\n".join(lines)
        md_text = md_text.replace("✅", "[YES]").replace("❌", "[NO]")
        return md_text

        if self.by_difficulty:
            lines.extend([
                "",
                "## By Difficulty Level",
                "",
                "| Difficulty | Count | HitRate@3 | Context Recall |",
                "|-----------|-------|-----------|----------------|",
            ])
            for diff, metrics in sorted(self.by_difficulty.items()):
                count = metrics.get("count", 0)
                hr3 = metrics.get("hit_rate_3", 0)
                cr = metrics.get("context_recall", 0)
                lines.append(f"| {diff} | {count} | {hr3:.2%} | {cr:.2%} |")

        lines.extend(["", "---", "*Generated by RAGAS-style Evaluator v2*"])
        return "\n".join(lines)

    def to_json(self) -> Dict[str, Any]:
        result = {
            "total_queries": self.total_queries,
            "llm_used": self.llm_used,
            "retrieval_metrics": {
                "hit_rate_1": self.hit_rate_1_avg,
                "hit_rate_3": self.hit_rate_3_avg,
                "recall_at_3": self.recall_at_3_avg,
                "context_recall": self.context_recall_avg,
            },
        }
        if self.llm_used:
            result["ragas_metrics"] = {
                "faithfulness": self.faithfulness_avg,
                "answer_relevancy": self.answer_relevancy_avg,
                "context_precision": self.context_precision_avg,
            }
        if self.by_difficulty:
            result["by_difficulty"] = self.by_difficulty
        return result


# ── Evaluator ───────────────────────────────────────────────

class RagasEvaluatorV2:
    """增强版 RAG 评估器 — 支持检索指标 + LLM-as-Judge 质量指标。"""

    def __init__(self, llm_client=None):
        """
        Args:
            llm_client: LLM 客户端实例（可选）。传入后启用 Faithfulness/Answer Relevancy 等 LLM 评估指标。
                        不传则只计算纯检索指标（HitRate/Recall/MRR/Coverage）。
        """
        self.llm = llm_client

    # ── Retrieval-only evaluation (no LLM needed) ────────────

    def evaluate_retrieval_only(self, results: List[Dict]) -> RagasEvalReport:
        """仅评估检索指标，不需要 LLM。

        Args:
            results: 每条包含 query/ground_truth/retrieved_sources/retrieved_texts/difficulty/category

        Returns:
            RagasEvalReport with retrieval metrics only
        """
        if not results:
            return RagasEvalReport()

        n = len(results)
        single_results = []
        by_difficulty = {}

        for r in results:
            gt_set = set(r.get("ground_truth", []))
            retrieved = r.get("retrieved_sources", [])

            sr = SingleEvalResult(
                query=r.get("query", ""),
                difficulty=r.get("difficulty", ""),
                category=r.get("category", ""),
                ground_truth=r.get("ground_truth", []),
                retrieved_sources=retrieved,
                retrieved_texts=r.get("retrieved_texts", []),
            )

            # HitRate@1
            sr.hit_rate_1 = 1.0 if (retrieved and retrieved[0] in gt_set) else 0.0

            # HitRate@3
            sr.hit_rate_3 = 1.0 if any(s in gt_set for s in retrieved[:3]) else 0.0

            # Recall@3
            if gt_set:
                retrieved_3 = set(retrieved[:3])
                sr.recall_at_3 = len(retrieved_3 & gt_set) / len(gt_set)
            else:
                sr.recall_at_3 = 1.0 if not retrieved else 0.0

            # Context Recall (same as recall@3 for our use case)
            sr.context_recall = sr.recall_at_3

            single_results.append(sr)

            # Per-difficulty tracking
            diff = r.get("difficulty", "unknown")
            if diff not in by_difficulty:
                by_difficulty[diff] = {"count": 0, "hit_rate_1": 0, "hit_rate_3": 0, "recall_at_3": 0, "context_recall": 0}
            by_difficulty[diff]["count"] += 1
            by_difficulty[diff]["hit_rate_1"] += sr.hit_rate_1
            by_difficulty[diff]["hit_rate_3"] += sr.hit_rate_3
            by_difficulty[diff]["recall_at_3"] += sr.recall_at_3
            by_difficulty[diff]["context_recall"] += sr.context_recall

        # Average per-difficulty
        for diff in by_difficulty:
            count = by_difficulty[diff]["count"]
            for key in ["hit_rate_1", "hit_rate_3", "recall_at_3", "context_recall"]:
                by_difficulty[diff][key] /= count

        report = RagasEvalReport(
            total_queries=n,
            hit_rate_1_avg=sum(sr.hit_rate_1 for sr in single_results) / n,
            hit_rate_3_avg=sum(sr.hit_rate_3 for sr in single_results) / n,
            recall_at_3_avg=sum(sr.recall_at_3 for sr in single_results) / n,
            context_recall_avg=sum(sr.context_recall for sr in single_results) / n,
            by_difficulty=by_difficulty,
            results=single_results,
        )
        return report

    # ── Full evaluation with LLM-as-Judge ────────────────────

    def evaluate_with_llm(self, results: List[Dict], answers: Optional[List[str]] = None) -> RagasEvalReport:
        """完整评估，包含 LLM-as-Judge 指标。

        Args:
            results: 检索结果列表（同 evaluate_retrieval_only）
            answers: Agent 生成的回答列表（可选，用于 Faithfulness/Answer Relevancy）

        Returns:
            RagasEvalReport with all metrics including RAGAS-style
        """
        if not self.llm:
            print("⚠️  No LLM client provided, falling back to retrieval-only evaluation")
            return self.evaluate_retrieval_only(results)

        # Start with retrieval metrics
        report = self.evaluate_retrieval_only(results)
        report.llm_used = True

        if not results:
            return report

        faithfulness_scores = []
        relevancy_scores = []
        precision_scores = []

        for i, r in enumerate(results):
            sr = report.results[i]
            retrieved_texts = r.get("retrieved_texts", [])
            answer = (answers[i] if answers and i < len(answers) else "")

            # Faithfulness
            if answer and retrieved_texts:
                context_str = "\n\n".join(retrieved_texts[:3])
                f_score = self._compute_faithfulness(context_str, answer)
                sr.faithfulness = f_score["score"]
                sr.faithfulness_reason = f_score.get("reason", "")
                faithfulness_scores.append(f_score["score"])

            # Answer Relevancy
            if answer:
                rel_score = self._compute_answer_relevancy(r.get("query", ""), answer)
                sr.answer_relevancy = rel_score["score"]
                sr.relevancy_reason = rel_score.get("reason", "")
                relevancy_scores.append(rel_score["score"])

            # Context Precision
            if retrieved_texts:
                cp_score = self._compute_context_precision(r.get("query", ""), retrieved_texts)
                sr.context_precision = cp_score["score"]
                precision_scores.append(cp_score["score"])

        # Update averages
        report.faithfulness_avg = sum(faithfulness_scores) / len(faithfulness_scores) if faithfulness_scores else None
        report.answer_relevancy_avg = sum(relevancy_scores) / len(relevancy_scores) if relevancy_scores else None
        report.context_precision_avg = sum(precision_scores) / len(precision_scores) if precision_scores else None

        return report

    # ── LLM-as-Judge methods ─────────────────────────────────

    def _compute_faithfulness(self, context: str, answer: str) -> Dict[str, Any]:
        """计算 Faithfulness — 回答是否忠实于参考资料。"""
        prompt = FAITHFULNESS_PROMPT.format(context=context, answer=answer)
        try:
            result = self._call_llm_json(prompt)
            faithful = result.get("faithful", False)
            return {"score": 1.0 if faithful else 0.0, "reason": result.get("reason", "")}
        except Exception as e:
            print(f"  [Faithfulness error] {e}")
            return {"score": None, "reason": f"LLM error: {e}"}

    def _compute_answer_relevancy(self, query: str, answer: str) -> Dict[str, Any]:
        """计算 Answer Relevancy — 回答与问题的相关程度（1-5分，归一化到0-1）。"""
        prompt = ANSWER_RELEVANCY_PROMPT.format(query=query, answer=answer)
        try:
            result = self._call_llm_json(prompt)
            raw_score = int(result.get("score", 3))
            return {"score": raw_score / 5.0, "reason": result.get("reason", "")}
        except Exception as e:
            print(f"  [Relevancy error] {e}")
            return {"score": None, "reason": f"LLM error: {e}"}

    def _compute_context_precision(self, query: str, contexts: List[str]) -> Dict[str, Any]:
        """计算 Context Precision — 检索结果中真正相关的占比。"""
        context_list = "\n".join(f"[{i+1}] {ctx[:200]}" for i, ctx in enumerate(contexts))
        prompt = CONTEXT_PRECISION_PROMPT.format(query=query, contexts=context_list)
        try:
            result = self._call_llm_json(prompt)
            relevance = result.get("relevance", [])
            if not relevance:
                return {"score": 0.5, "reason": "No relevance data"}

            relevant_count = sum(1 for r in relevance if r.get("relevant", False))
            score = relevant_count / len(relevance) if relevance else 0.0
            return {"score": score, "reason": f"{relevant_count}/{len(relevance)} contexts relevant"}
        except Exception as e:
            print(f"  [Context Precision error] {e}")
            return {"score": None, "reason": f"LLM error: {e}"}

    def _call_llm_json(self, prompt: str) -> Dict[str, Any]:
        """调用 LLM 并解析 JSON 响应。"""
        try:
            text = self.llm.chat(
                [{"role": "user", "content": prompt}],
                "你是一个评估助手，只返回 JSON。",
                max_tokens=256,
            )
            return parse_json_object(text) or {}
        except Exception as e:
            print(f"  [LLM call error] {e}")
            raise

    # ── Experiment comparison ────────────────────────────────

    def compare_experiments(self, exp_a: RagasEvalReport, exp_b: RagasEvalReport) -> str:
        """对比两组实验结果，生成差异分析报告。"""
        lines = [
            "# Experiment Comparison Report",
            "",
            "| Metric | Exp A | Exp B | Delta |",
            "|--------|-------|-------|-------|",
        ]

        metrics = [
            ("HitRate@1", "hit_rate_1_avg"),
            ("HitRate@3", "hit_rate_3_avg"),
            ("Recall@3", "recall_at_3_avg"),
            ("Context Recall", "context_recall_avg"),
        ]

        if exp_a.llm_used and exp_b.llm_used:
            metrics.extend([
                ("Faithfulness", "faithfulness_avg"),
                ("Answer Relevancy", "answer_relevancy_avg"),
                ("Context Precision", "context_precision_avg"),
            ])

        for name, attr in metrics:
            a_val = getattr(exp_a, attr, None)
            b_val = getattr(exp_b, attr, None)
            if a_val is not None and b_val is not None:
                delta = b_val - a_val
                sign = "+" if delta >= 0 else ""
                lines.append(f"| {name} | {a_val:.2%} | {b_val:.2%} | {sign}{delta:.2%} |")

        lines.extend(["", "---"])
        return "\n".join(lines)


if __name__ == "__main__":
    # Quick test: retrieval-only evaluation
    from agent.rag import retrieve
    from eval.benchmark_dataset import BENCHMARK_DATASET, SUMMARY_STATS

    print("=" * 60)
    print("RAGAS-style Evaluation (Retrieval Only)")
    print("=" * 60)
    print(f"Dataset: {SUMMARY_STATS['total']} queries")
    print()

    results = []
    for item in BENCHMARK_DATASET:
        query = item["query"]
        hits = retrieve(query, top_k=5)
        results.append({
            "query": query,
            "difficulty": item["difficulty"],
            "category": item["category"],
            "ground_truth": item["ground_truth"],
            "retrieved_sources": [h["source"] for h in hits],
            "retrieved_texts": [h["text"][:200] for h in hits],
        })

    evaluator = RagasEvaluatorV2()
    report = evaluator.evaluate_retrieval_only(results)

    print(report.to_markdown())

    # Save
    import sys
    from pathlib import Path
    report_path = Path(__file__).parent / "report_ragas_v2.md"
    report_path.write_text(report.to_markdown(), encoding="utf-8")
    print(f"\nReport saved to {report_path}")
