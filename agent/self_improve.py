# -*- coding: utf-8 -*-
"""
Self-Improvement Loop — Agent 自我优化框架

核心理念：一个优秀的 Agent 应该能自动发现自身弱点并改进。

功能模块：
1. BadCaseCollector — 失败案例收集器（JSONL 持久化）
2. PromptOptimizer — 基于 bad case 的 Prompt 自动优化
3. KnowledgeGapDetector — 知识库缺口检测
4. SelfImprovementPipeline — 完整自我优化流水线
5. ExperimentTracker — 实验对比追踪

面试演示流程：
    pipeline = SelfImprovementPipeline()
    report = pipeline.run_improvement_cycle(dataset)
    # 输出：失败模式分析 + 知识缺口 + 改进建议
"""

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


# ── 1. Bad Case Collector ───────────────────────────────────────

@dataclass
class BadCase:
    """单个失败案例"""
    timestamp: str
    query: str
    difficulty: str
    category: str
    ground_truth: List[str]
    retrieved_sources: List[str]
    metrics: Dict[str, float]
    failure_type: str = ""  # retrieval_miss / hallucination / irrelevant / other


class BadCaseCollector:
    """收集评估失败的案例，用于后续分析。

    使用 JSONL 格式存储（每行一个 JSON），方便增量追加和流式处理。
    """

    def __init__(self, storage_path: str = "bad_cases.jsonl"):
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, case: BadCase) -> None:
        """记录一个 bad case（追加写入 JSONL）。"""
        data = {
            "timestamp": case.timestamp,
            "query": case.query,
            "difficulty": case.difficulty,
            "category": case.category,
            "ground_truth": case.ground_truth,
            "retrieved_sources": case.retrieved_sources,
            "metrics": case.metrics,
            "failure_type": case.failure_type,
        }
        with open(self.storage_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    def get_all_cases(self) -> List[Dict]:
        """读取所有 bad cases。"""
        if not self.storage_path.exists():
            return []
        cases = []
        with open(self.storage_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        cases.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return cases

    def get_bad_cases(self, metric_name: str = "faithfulness",
                      threshold: float = 0.5, limit: int = 50) -> List[Dict]:
        """获取某个指标低于阈值的案例。"""
        all_cases = self.get_all_cases()
        result = []
        for case in all_cases:
            metrics = case.get("metrics", {})
            if metric_name in metrics:
                score = metrics[metric_name]
                if score is not None and score < threshold:
                    result.append(case)
                    if len(result) >= limit:
                        break
        return result

    def failure_pattern_analysis(self) -> Dict[str, Any]:
        """分析失败模式。

        Returns:
            {
                "total_bad_cases": int,
                "failure_type_distribution": {...},
                "difficulty_distribution": {...},
                "top_failed_keywords": [...],
                "weakest_knowledge_sources": [...],
                "common_failure_patterns": [...]
            }
        """
        cases = self.get_all_cases()
        if not cases:
            return {"total_bad_cases": 0, "message": "No bad cases recorded yet"}

        # Failure type distribution
        type_counter = Counter(c.get("failure_type", "unknown") for c in cases)

        # Difficulty distribution
        diff_counter = Counter(c.get("difficulty", "unknown") for c in cases)

        # Top failed keywords (from queries)
        all_keywords = []
        for case in cases:
            query = case.get("query", "")
            # Simple keyword extraction: common Chinese words
            words = re.findall(r'[\u4e00-\u9fff]{2,4}', query)
            all_keywords.extend(words)
        keyword_counter = Counter(all_keywords).most_common(10)

        # Weakest knowledge sources (sources that should have been retrieved but weren't)
        source_misses = Counter()
        for case in cases:
            gt = set(case.get("ground_truth", []))
            retrieved = set(case.get("retrieved_sources", []))
            missed = gt - retrieved
            source_misses.update(missed)

        # Category distribution
        cat_counter = Counter(c.get("category", "unknown") for c in cases)

        return {
            "total_bad_cases": len(cases),
            "failure_type_distribution": dict(type_counter.most_common(10)),
            "difficulty_distribution": dict(diff_counter.most_common()),
            "category_distribution": dict(cat_counter.most_common()),
            "top_failed_keywords": [{"keyword": k, "count": v} for k, v in keyword_counter],
            "weakest_knowledge_sources": source_misses.most_common(5),
        }

    def clear(self) -> None:
        """清空 bad case 记录。"""
        if self.storage_path.exists():
            self.storage_path.unlink()


# ── 2. Prompt Optimizer ───────────────────────────────────────

class PromptOptimizer:
    """基于 bad case 分析，自动优化 system prompt。"""

    # Bad case pattern → prompt improvement mapping
    IMPROVEMENT_RULES = {
        "low_faithfulness": {
            "condition": lambda m: m.get("faithfulness", 1.0) is not None and m["faithfulness"] < 0.7,
            "suggestion": "\n\n【重要】回答时必须严格基于参考资料中的信息。如果参考资料中没有相关信息，诚实说明你不确定，绝对不要编造信息。",
        },
        "low_relevancy": {
            "condition": lambda m: m.get("answer_relevancy", 1.0) is not None and m["answer_relevancy"] < 0.6,
            "suggestion": "\n\n【重要】请紧扣用户的问题回答，不要偏离主题。如果问题涉及多个方面，逐一回答每个方面。",
        },
        "high_miss_rate_edge": {
            "condition": lambda stats: stats.get("difficulty_distribution", {}).get("L4", 0) > 3,
            "suggestion": "\n\n【重要】用户可能会用模糊、口语化或极短的方式提问。请尽量理解用户的真实意图，不要拘泥于字面意思。",
        },
        "adversarial_failures": {
            "condition": lambda stats: stats.get("category_distribution", {}).get("adversarial", 0) > 2,
            "suggestion": "\n\n【重要】如果遇到挑衅、诱导或完全离题的问题，保持专业态度，礼貌地引导回产品话题。不要与用户争论。",
        },
    }

    def __init__(self, base_prompt: str):
        self.base_prompt = base_prompt
        self.version_history: List[Dict] = []

    def analyze_and_suggest(self, bad_cases: List[Dict],
                            pattern_stats: Dict[str, Any]) -> Dict[str, Any]:
        """分析 bad cases，给出 prompt 优化建议。

        Args:
            bad_cases: 失败案例列表
            pattern_stats: failure_pattern_analysis 的结果

        Returns:
            {"suggestions": [...], "avg_metrics": {...}}
        """
        suggestions = []

        # Compute average metrics from bad cases
        metrics_sum = defaultdict(list)
        for case in bad_cases:
            for key, val in case.get("metrics", {}).items():
                if val is not None:
                    metrics_sum[key].append(val)

        avg_metrics = {k: sum(v) / len(v) for k, v in metrics_sum.items()}

        # Check each improvement rule
        for rule_name, rule in self.IMPROVEMENT_RULES.items():
            if rule["condition"](avg_metrics):
                suggestions.append({
                    "rule": rule_name,
                    "suggestion": rule["suggestion"],
                })

        # Check knowledge source weaknesses
        weak_sources = pattern_stats.get("weakest_knowledge_sources", [])
        if weak_sources:
            source_names = [s[0] for s in weak_sources[:3]]
            suggestions.append({
                "rule": "weak_knowledge_source",
                "suggestion": f"\n\n【注意】以下知识源的检索效果较差：{', '.join(source_names)}。回答时如果涉及这些主题，请格外谨慎。",
            })

        return {
            "suggestions": suggestions,
            "avg_metrics": avg_metrics,
        }

    def generate_improved_prompt(self, suggestions: List[Dict]) -> str:
        """根据建议生成改进版 prompt。"""
        if not suggestions:
            return self.base_prompt

        improved = self.base_prompt
        for s in suggestions:
            suggestion_text = s.get("suggestion", "")
            if suggestion_text and suggestion_text not in improved:
                improved += suggestion_text

        # Save to version history
        version = len(self.version_history) + 1
        self.version_history.append({
            "version": version,
            "timestamp": datetime.now().isoformat(),
            "suggestions_count": len(suggestions),
            "prompt_length": len(improved),
        })

        return improved


# ── 3. Knowledge Gap Detector ────────────────────────────────

class KnowledgeGapDetector:
    """检测知识库中缺失的内容。"""

    def __init__(self, kb_dir: Path):
        self.kb_dir = Path(kb_dir)
        self.kb_topics = self._extract_topics()

    def _extract_topics(self) -> Set[str]:
        """从现有知识库提取主题关键词。"""
        topics = set()
        if not self.kb_dir.exists():
            return topics

        for md_file in self.kb_dir.glob("*.md"):
            try:
                text = md_file.read_text(encoding="utf-8")
                # Extract headings as topics
                headings = re.findall(r'^#{1,3}\s+(.+)$', text, re.MULTILINE)
                topics.update(h.strip() for h in headings)
                # Extract key Chinese terms
                terms = re.findall(r'[\u4e00-\u9fff]{2,8}', text)
                term_counter = Counter(terms)
                topics.update(t for t, _ in term_counter.most_common(20))
            except Exception:
                continue

        return topics

    def detect_gaps(self, bad_cases: List[Dict]) -> List[Dict]:
        """分析 bad cases，发现知识缺口。

        Returns:
            [{"gap_type": str, "topic": str, "evidence_queries": [...], "severity": int}, ...]
        """
        gaps = []

        # Group failures by missing knowledge sources
        source_failures = defaultdict(list)
        for case in bad_cases:
            gt = set(case.get("ground_truth", []))
            retrieved = set(case.get("retrieved_sources", []))
            missed = gt - retrieved
            for src in missed:
                source_failures[src].append(case.get("query", ""))

        # Identify knowledge sources with high failure rates
        for source, queries in source_failures.items():
            if len(queries) >= 2:  # At least 2 failures to flag as gap
                gaps.append({
                    "gap_type": "retrieval_weakness",
                    "topic": source,
                    "evidence_queries": queries[:5],
                    "severity": min(5, len(queries)),
                    "description": f"知识源 '{source}' 在 {len(queries)} 次查询中未被正确检索到",
                })

        # Detect completely missing topics (no ground truth match at all)
        no_match_cases = [c for c in bad_cases if not c.get("ground_truth")]
        if no_match_cases:
            gap_queries = [c.get("query", "") for c in no_match_cases[:5]]
            gaps.append({
                "gap_type": "missing_topic",
                "topic": "unknown",
                "evidence_queries": gap_queries,
                "severity": 3,
                "description": f"{len(no_match_cases)} 条查询没有匹配任何知识源，可能需要补充知识库",
            })

        # Check for topic coverage gaps
        queried_topics = set()
        for case in bad_cases:
            query = case.get("query", "")
            terms = re.findall(r'[\u4e00-\u9fff]{2,6}', query)
            queried_topics.update(terms)

        uncovered = queried_topics - self.kb_topics
        if uncovered:
            gaps.append({
                "gap_type": "topic_coverage",
                "topic": "general",
                "evidence_queries": list(uncovered)[:10],
                "severity": 2,
                "description": f"发现 {len(uncovered)} 个查询中的关键词在知识库中未覆盖",
            })

        return gaps

    def generate_kb_suggestions(self) -> str:
        """生成知识库补充建议报告。"""
        lines = [
            "# Knowledge Base Gap Analysis",
            "",
            f"**Knowledge sources covered:** {', '.join(sorted(self.kb_topics)[:10])}",
            "",
        ]
        return "\n".join(lines)


# ── 4. Self Improvement Pipeline ───────────────────────────────

class SelfImprovementPipeline:
    """完整的自我优化流程。

    面试演示入口：
        pipeline = SelfImprovementPipeline()
        report = pipeline.run_improvement_cycle(dataset)
    """

    def __init__(self, evaluator=None, collector=None,
                 prompt_optimizer=None, gap_detector=None,
                 base_prompt: str = "",
                 kb_dir: Optional[Path] = None):
        from pathlib import Path as _Path

        project_root = _Path(__file__).parent.parent

        self.collector = collector or BadCaseCollector(
            storage_path=str(project_root / "bad_cases.jsonl")
        )
        self.evaluator = evaluator
        self.gap_detector = gap_detector or KnowledgeGapDetector(
            kb_dir=kb_dir or project_root / "knowledge"
        )
        self.prompt_optimizer = prompt_optimizer or PromptOptimizer(base_prompt)

    def run_improvement_cycle(self, dataset: List[Dict]) -> Dict[str, Any]:
        """执行一轮完整的自我优化。

        流程：
        1. evaluate → 用 benchmark 评估当前 Agent
        2. collect_bad_cases → 收集低于阈值的案例
        3. analyze_patterns → 分析失败模式
        4. detect_gaps → 检测知识缺口
        5. generate_suggestions → 生成改进建议
        6. report → 输出改进报告

        Args:
            dataset: Benchmark dataset (from eval.benchmark_dataset)

        Returns:
            Complete improvement report dict
        """
        print("=" * 70)
        print("🔄 Self-Improvement Pipeline")
        print("=" * 70)

        # Step 1: Evaluate
        print("\n[Step 1/6] Evaluating current agent...")
        metrics_before = {}
        if self.evaluator:
            from agent.rag import retrieve
            results = []
            for item in dataset:
                hits = retrieve(item["query"], top_k=5)
                results.append({
                    "query": item["query"],
                    "difficulty": item["difficulty"],
                    "category": item["category"],
                    "ground_truth": item["ground_truth"],
                    "retrieved_sources": [h["source"] for h in hits],
                    "retrieved_texts": [h["text"][:200] for h in hits],
                })

            report = self.evaluator.evaluate_retrieval_only(results)
            metrics_before = {
                "hit_rate_1": report.hit_rate_1_avg,
                "hit_rate_3": report.hit_rate_3_avg,
                "recall_at_3": report.recall_at_3_avg,
                "context_recall": report.context_recall_avg,
            }
            print(f"  HitRate@3: {report.hit_rate_3_avg:.2%}")
            print(f"  Context Recall: {report.context_recall_avg:.2%}")

            # Step 2: Collect bad cases
            print("\n[Step 2/6] Collecting bad cases...")
            for r in results:
                gt_set = set(r.get("ground_truth", []))
                retrieved = r.get("retrieved_sources", [])
                hit_3 = any(s in gt_set for s in retrieved[:3]) if retrieved else False

                if not hit_3 and gt_set:  # Only record retrieval misses
                    failure_type = "retrieval_miss"
                    case = BadCase(
                        timestamp=datetime.now().isoformat(),
                        query=r["query"],
                        difficulty=r.get("difficulty", ""),
                        category=r.get("category", ""),
                        ground_truth=r["ground_truth"],
                        retrieved_sources=retrieved,
                        metrics={"hit_rate_3": 0.0},
                        failure_type=failure_type,
                    )
                    self.collector.record(case)

            bad_count = len(self.collector.get_all_cases())
            print(f"  Total bad cases recorded: {bad_count}")
        else:
            print("  ⚠️  No evaluator provided, skipping evaluation")

        # Step 3: Analyze patterns
        print("\n[Step 3/6] Analyzing failure patterns...")
        pattern_stats = self.collector.failure_pattern_analysis()
        total_bad = pattern_stats.get("total_bad_cases", 0)
        print(f"  Total bad cases: {total_bad}")

        # Step 4: Detect knowledge gaps
        print("\n[Step 4/6] Detecting knowledge gaps...")
        knowledge_gaps = self.gap_detector.detect_gaps(self.collector.get_all_cases())
        print(f"  Knowledge gaps found: {len(knowledge_gaps)}")

        # Step 5: Generate suggestions
        print("\n[Step 5/6] Generating improvement suggestions...")
        all_bad_cases = self.collector.get_all_cases()
        suggestion_result = self.prompt_optimizer.analyze_and_suggest(
            all_bad_cases, pattern_stats
        )
        suggestions = suggestion_result.get("suggestions", [])

        improved_prompt = ""
        if suggestions:
            improved_prompt = self.prompt_optimizer.generate_improved_prompt(suggestions)
            print(f"  Prompt suggestions: {len(suggestions)}")

        # Step 6: Compile report
        print("\n[Step 6/6] Compiling improvement report...")

        failure_patterns = []
        if pattern_stats.get("failure_type_distribution"):
            for ftype, count in pattern_stats["failure_type_distribution"].items():
                failure_patterns.append(f"{ftype}: {count} cases")

        report = {
            "timestamp": datetime.now().isoformat(),
            "metrics_before": metrics_before,
            "bad_case_count": total_bad,
            "failure_patterns": failure_patterns,
            "knowledge_gaps": knowledge_gaps,
            "suggestions": {
                "prompt_changes": [s.get("suggestion", "")[:100] for s in suggestions],
                "kb_additions": [g.get("description", "") for g in knowledge_gaps],
                "retrieval_tuning": self._get_retrieval_suggestions(pattern_stats),
            },
            "improved_prompt": improved_prompt,
        }

        print("\n" + "=" * 70)
        print("✅ Improvement cycle complete")
        print("=" * 70)

        return report

    def _get_retrieval_suggestions(self, pattern_stats: Dict) -> List[str]:
        """基于失败模式生成检索优化建议。"""
        suggestions = []
        weak_sources = pattern_stats.get("weakest_knowledge_sources", [])
        if weak_sources:
            for source, count in weak_sources[:3]:
                suggestions.append(
                    f"知识源 '{source}' 有 {count} 次检索失败，建议增加相关关键词或调整 BM25 权重"
                )

        top_keywords = pattern_stats.get("top_failed_keywords", [])
        if top_keywords:
            keywords = [k["keyword"] for k in top_keywords[:5]]
            suggestions.append(
                f"高频失败关键词：{', '.join(keywords)}，建议加入 jieba 自定义词典"
            )

        return suggestions


# ── 5. Experiment Tracker ────────────────────────────────

class ExperimentTracker:
    """追踪多次优化实验的效果变化。"""

    def __init__(self, history_path: str = "experiment_history.json"):
        self.history_path = Path(history_path)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_file()

    def _ensure_file(self):
        if not self.history_path.exists():
            self.history_path.write_text("[]", encoding="utf-8")

    def log_experiment(self, name: str, metrics: Dict[str, float],
                       changes_made: Optional[Dict] = None) -> None:
        """记录一次实验。"""
        self._ensure_file()
        history = json.loads(self.history_path.read_text(encoding="utf-8"))

        entry = {
            "experiment": name,
            "timestamp": datetime.now().isoformat(),
            "metrics": metrics,
            "changes_made": changes_made or {},
        }
        history.append(entry)
        self.history_path.write_text(
            json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get_history(self) -> List[Dict]:
        """获取实验历史。"""
        self._ensure_file()
        return json.loads(self.history_path.read_text(encoding="utf-8"))

    def trend_report(self) -> str:
        """生成趋势报告：指标随时间的变化。"""
        history = self.get_history()
        if not history:
            return "No experiments recorded yet."

        lines = [
            "# Experiment Trend Report",
            "",
            "| # | Experiment | Date | HitRate@3 | Context Recall |",
            "|---|-----------|------|-----------|----------------|",
        ]

        for i, entry in enumerate(history, 1):
            metrics = entry.get("metrics", {})
            hr3 = metrics.get("hit_rate_3", 0)
            cr = metrics.get("context_recall", 0)
            lines.append(
                f"| {i} | {entry['experiment']} | {entry['timestamp'][:10]} "
                f"| {hr3:.2%} | {cr:.2%} |"
            )

        return "\n".join(lines)

    def best_config(self) -> Optional[Dict]:
        """返回历史最优配置（按 HitRate@3 排序）。"""
        history = self.get_history()
        if not history:
            return None

        best = max(history, key=lambda e: e.get("metrics", {}).get("hit_rate_3", 0))
        return best


if __name__ == "__main__":
    # Demo: run improvement cycle
    print("Self-Improvement Pipeline Demo")
    print("=" * 60)

    from eval.ragas_eval_v2 import RagasEvaluatorV2
    from eval.benchmark_dataset import BENCHMARK_DATASET, get_with_ground_truth

    evaluator = RagasEvaluatorV2()
    pipeline = SelfImprovementPipeline(evaluator=evaluator)

    # Use only queries with ground truth for meaningful evaluation
    dataset = get_with_ground_truth()
    report = pipeline.run_improvement_cycle(dataset)

    print("\n" + json.dumps(report, ensure_ascii=False, indent=2)[:2000])
