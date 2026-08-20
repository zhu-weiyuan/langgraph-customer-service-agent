#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""综合评测：意图、情绪、PostgreSQL + pgvector RAG。

默认串行运行，不压测、不写入业务数据库，只读取当前知识库和 PostgreSQL RAG 索引。

示例：
    python scripts/run_intent_emotion_rag_eval.py
    python scripts/run_intent_emotion_rag_eval.py --emotion-mode keyword
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

DATASET = ROOT / "eval" / "intent_emotion_rag_dataset.jsonl"
REPORT_DIR = ROOT / "eval" / "reports"
REPORT_JSON = REPORT_DIR / "intent_emotion_rag_report.json"
REPORT_MD = REPORT_DIR / "intent_emotion_rag_report.md"
TOP_K = 5

# 评测日志保留警告，便于报告中发现 embedding/数据库降级；屏蔽模型库的普通噪声。
logging.basicConfig(level=os.getenv("EVAL_LOG_LEVEL", "WARNING"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


def load_cases(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def normalize_label(value: Any) -> str:
    return str(value or "").strip().lower()


def f1_by_label(y_true: Iterable[str], y_pred: Iterable[str]) -> Tuple[float, Dict[str, Dict[str, float]]]:
    true = list(y_true)
    pred = list(y_pred)
    labels = sorted(set(true) | set(pred))
    details: Dict[str, Dict[str, float]] = {}
    f1s: List[float] = []
    for label in labels:
        tp = sum(a == label and b == label for a, b in zip(true, pred))
        fp = sum(a != label and b == label for a, b in zip(true, pred))
        fn = sum(a == label and b != label for a, b in zip(true, pred))
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        details[label] = {"support": sum(a == label for a in true),
                          "precision": round(precision, 4),
                          "recall": round(recall, 4),
                          "f1": round(f1, 4)}
        f1s.append(f1)
    return (sum(f1s) / len(f1s) if f1s else 0.0), details


def metric_summary(expected: List[str], predicted: List[str]) -> Dict[str, Any]:
    n = len(expected)
    correct = sum(a == b for a, b in zip(expected, predicted))
    macro_f1, by_label = f1_by_label(expected, predicted)
    matrix: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for a, b in zip(expected, predicted):
        matrix[a][b] += 1
    return {
        "n": n,
        "accuracy": round(correct / n, 4) if n else 0.0,
        "correct": correct,
        "macro_f1": round(macro_f1, 4),
        "by_label": by_label,
        "confusion_matrix": {k: dict(v) for k, v in sorted(matrix.items())},
    }


def keyword_rank(results: List[Dict[str, Any]], keywords: List[str]) -> int:
    """返回 top-k 中包含任一标注关键词的首个排名；0 代表未命中。"""
    lowered = [str(k).lower() for k in keywords if str(k).strip()]
    if not lowered:
        return 0
    for rank, result in enumerate(results[:TOP_K], start=1):
        haystack = (str(result.get("title", "")) + " "
                    + str(result.get("content", result.get("text", "")))).lower()
        if any(k in haystack for k in lowered):
            return rank
    return 0


def compact_result(result: Dict[str, Any]) -> Dict[str, Any]:
    content = str(result.get("content", result.get("text", "")))
    return {
        "title": str(result.get("title", "")),
        "source": str(result.get("source", "")),
        "score": result.get("score"),
        "snippet": content.replace("\n", " ")[:240],
    }


def predict_intent(text: str) -> str:
    """Predict routing intent without triggering a generation call.

    The restored production node classifies intent through the local LLM and
    does not expose the old private ``_fast_intent`` helper. This evaluator is
    intentionally cheap and deterministic, so it measures the fast routing
    layer with the same four public labels instead of issuing one LLM call per
    sample.
    """
    value = normalize_label(text)
    if not value:
        return "consult"

    ending = (
        value in {"\u8c22\u8c22", "\u611f\u8c22", "\u518d\u89c1", "\u62dc\u62dc", "bye", "thanks", "thank you"}
        or value.endswith(("\u518d\u89c1", "\u62dc\u62dc"))
        or value in {"\u597d\u4e86", "\u6ca1\u95ee\u9898\u4e86", "\u6ca1\u6709\u95ee\u9898\u4e86", "\u5c31\u8fd9\u6837"}
        or ("\u8c22\u8c22" in value and not any(mark in value for mark in ("\u600e\u4e48\u529e", "\u600e\u4e48", "\u5982\u4f55", "\u4f46\u662f", "\u4e0d\u8fc7", "\u8fd8")))
        or (value.startswith("\u597d\u4e86") and not any(mark in value for mark in ("\u600e\u4e48\u529e", "\u600e\u4e48", "\u5982\u4f55")))
    )
    if ending:
        return "ending"

    chat_markers = (
        "\u4f60\u597d", "\u60a8\u597d", "\u55e8", "hi", "hello", "\u5728\u5417", "\u4f60\u662f\u8c01", "\u8bb2\u4e2a\u7b11\u8bdd",
        "\u5199\u4e00\u9996\u8bd7", "\u5929\u6c14", "\u95f2\u804a", "\u5e72\u561b", "\u505a\u4ec0\u4e48",
    )
    if any(marker in value for marker in chat_markers):
        product_markers = (
            "\u8ba2\u5355", "\u53d1\u8d27", "\u7269\u6d41", "\u9000\u6b3e", "\u9000\u8d27", "\u4f1a\u5458", "\u8bbe\u5907", "\u4ea7\u54c1",
            "\u8d26\u53f7", "\u767b\u5f55", "wifi", "\u7f51\u5173", "\u6545\u969c", "\u8d28\u91cf", "\u552e\u540e",
        )
        if not any(marker in value for marker in product_markers):
            return "chat"

    complaint_markers = (
        "\u6295\u8bc9", "\u5751\u4eba", "\u592a\u5dee", "\u5783\u573e", "\u6c14\u6b7b", "\u751f\u6c14", "\u6124\u6012", "\u5931\u671b",
        "\u540e\u6094", "\u574f\u4e86", "\u4e0d\u80fd\u7528", "\u4e0d\u9000\u6b3e", "\u6ca1\u9000\u6b3e", "\u62d6\u4e86\u8fd9\u4e48\u4e45",
        "\u8fdf\u8fdf", "\u9a97", "\u6b3a\u9a97", "\u5dee\u8bc4", "\u8d54\u507f",
    )
    if any(marker in value for marker in complaint_markers) and not any(mark in value for mark in ("\u600e\u4e48\u529e", "\u600e\u4e48", "\u5982\u4f55")):
        return "complaint"
    return "consult"

def predict_emotion(text: str, case_id: str, mode: str) -> Tuple[str, int, str, str | None]:
    from agent import sentiment
    if mode == "keyword":
        result = sentiment._keyword_sentiment(text) or {"emotion": "neutral", "intensity": 1}
        source = "keyword" if sentiment._keyword_sentiment(text) else "neutral-fallback"
        return normalize_label(result.get("emotion", "neutral")), int(result.get("intensity", 1)), source, None
    try:
        # 每条样本使用独立 key，避免全局缓存影响评测结果。
        result = sentiment.analyze(text, cache_key=f"eval:{case_id}")
        source = "keyword" if sentiment._keyword_sentiment(text) else "llm"
        return normalize_label(result.get("emotion", "neutral")), int(result.get("intensity", 1)), source, None
    except Exception as exc:
        # 评测不能因为单条情绪 LLM 失败而中断；把失败保留在样本明细中。
        return "__error__", 0, "error", str(exc)


def build_retriever(backend: str):
    from agent.rag_backend import retrieve_with_backend
    def retrieve(query: str) -> List[Dict[str, Any]]:
        return retrieve_with_backend(query, TOP_K, backend)
    return retrieve


def evaluate(cases: List[Dict[str, Any]], backend: str, emotion_mode: str) -> Dict[str, Any]:
    intent_true: List[str] = []
    intent_pred: List[str] = []
    emotion_true: List[str] = []
    emotion_pred: List[str] = []
    details: List[Dict[str, Any]] = []
    retrieve = build_retriever(backend)

    positive_n = positive_hits = 0
    negative_n = false_positives = 0
    rank_sum = 0.0
    hit_at = {1: 0, 3: 0, 5: 0}
    rag_errors = 0
    source_counts: Counter[str] = Counter()

    for case in cases:
        case_id = str(case["id"])
        query = str(case["query"])
        expected_intent = normalize_label(case.get("expected_intent"))
        expected_emotion = normalize_label(case.get("expected_emotion"))
        predicted_intent = predict_intent(query)
        predicted_emotion, intensity, emotion_source, emotion_error = predict_emotion(
            query, case_id, emotion_mode)
        intent_true.append(expected_intent)
        intent_pred.append(predicted_intent)
        emotion_true.append(expected_emotion)
        emotion_pred.append(predicted_emotion)
        source_counts[emotion_source] += 1

        results: List[Dict[str, Any]] = []
        rag_error = None
        started = time.perf_counter()
        try:
            results = retrieve(query) or []
        except Exception as exc:
            rag_errors += 1
            rag_error = str(exc)
        rag_ms = round((time.perf_counter() - started) * 1000, 1)

        expected_rag = bool(case.get("rag_expected"))
        rank = keyword_rank(results, case.get("expected_doc_keywords", []))
        if expected_rag:
            positive_n += 1
            if rank:
                positive_hits += 1
                rank_sum += 1.0 / rank
                for cutoff in hit_at:
                    if rank <= cutoff:
                        hit_at[cutoff] += 1
        else:
            negative_n += 1
            # 无关问题在当前检索配置下应返回空结果；非空即记为误命中。
            if results:
                false_positives += 1

        details.append({
            "id": case_id,
            "query": query,
            "category": case.get("category", ""),
            "expected_intent": expected_intent,
            "predicted_intent": predicted_intent,
            "intent_correct": expected_intent == predicted_intent,
            "expected_emotion": expected_emotion,
            "predicted_emotion": predicted_emotion,
            "emotion_intensity": intensity,
            "emotion_source": emotion_source,
            "emotion_correct": expected_emotion == predicted_emotion,
            "emotion_error": emotion_error,
            "rag_expected": expected_rag,
            "expected_doc_keywords": case.get("expected_doc_keywords", []),
            "rag_rank": rank,
            "rag_hit": bool(rank),
            "rag_result_count": len(results),
            "rag_ms": rag_ms,
            "rag_error": rag_error,
            "top_results": [compact_result(r) for r in results[:TOP_K]],
        })

    rag_report = {
        "n": len(cases),
        "positive_cases": positive_n,
        "positive_hit_rate_at_1": round(hit_at[1] / positive_n, 4) if positive_n else 0.0,
        "positive_hit_rate_at_3": round(hit_at[3] / positive_n, 4) if positive_n else 0.0,
        "positive_hit_rate_at_5": round(hit_at[5] / positive_n, 4) if positive_n else 0.0,
        "mrr_on_positive_cases": round(rank_sum / positive_n, 4) if positive_n else 0.0,
        "negative_cases": negative_n,
        "false_positive_rate": round(false_positives / negative_n, 4) if negative_n else 0.0,
        "false_positives": false_positives,
        "binary_accuracy": round((positive_hits + negative_n - false_positives) / (positive_n + negative_n), 4)
        if positive_n + negative_n else 0.0,
        "retrieval_errors": rag_errors,
        "avg_latency_ms": round(sum(float(d["rag_ms"]) for d in details) / len(details), 1) if details else 0.0,
    }

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dataset": str(DATASET),
        "dataset_size": len(cases),
        "backend_requested": backend,
        "emotion_mode": emotion_mode,
        "emotion_source_counts": dict(source_counts),
        "intent": metric_summary(intent_true, intent_pred),
        "emotion": metric_summary(emotion_true, emotion_pred),
        "rag": rag_report,
        "details": details,
    }
    # 运行时状态说明：pgvector store 在 embedding 失败时会走 PostgreSQL 关键词降级。
    try:
        from agent import rag_backend
        store = rag_backend._cache.get("pg_store")
        if store is not None:
            report["rag_runtime"] = {
                "backend": "pgvector",
                "embedding_fallback": bool(getattr(store, "_embedding_failure_logged", False)),
                "note": "embedding unavailable 时使用 PostgreSQL tsvector/关键词检索"
                if getattr(store, "_embedding_failure_logged", False)
                else "未观察到 embedding 降级",
            }
    except Exception as exc:
        report["rag_runtime"] = {"inspection_error": str(exc)}
    return report


def render_markdown(report: Dict[str, Any]) -> str:
    i = report["intent"]
    e = report["emotion"]
    r = report["rag"]
    lines = [
        "# 意图 / 情绪 / RAG 综合评测报告", "",
        f"- 生成时间：`{report['generated_at']}`",
        f"- 数据集：`{report['dataset']}`（{report['dataset_size']} 条）",
        f"- RAG 后端：`{report['backend_requested']}`",
        f"- 情绪评测模式：`{report['emotion_mode']}`",
        "", "## 总结", "",
        f"- 意图准确率：**{i['accuracy']:.2%}**；Macro-F1：**{i['macro_f1']:.2%}**",
        f"- 情绪准确率：**{e['accuracy']:.2%}**；Macro-F1：**{e['macro_f1']:.2%}**",
        f"- RAG 正样本 Hit@1 / Hit@3 / Hit@5：**{r['positive_hit_rate_at_1']:.2%} / {r['positive_hit_rate_at_3']:.2%} / {r['positive_hit_rate_at_5']:.2%}**",
        f"- RAG 正样本 MRR：**{r['mrr_on_positive_cases']:.4f}**；无关问题误命中率：**{r['false_positive_rate']:.2%}**",
        f"- RAG 二分类准确率：**{r['binary_accuracy']:.2%}**；平均检索耗时：**{r['avg_latency_ms']:.1f} ms**",
        "", "## 意图分类", "", "| 标签 | 支持数 | Precision | Recall | F1 |", "|---|---:|---:|---:|---:|",
    ]
    for label, s in i["by_label"].items():
        lines.append(f"| {label} | {s['support']} | {s['precision']:.2%} | {s['recall']:.2%} | {s['f1']:.2%} |")
    lines += ["", "## 情绪分类", "", "| 标签 | 支持数 | Precision | Recall | F1 |", "|---|---:|---:|---:|---:|"]
    for label, s in e["by_label"].items():
        lines.append(f"| {label} | {s['support']} | {s['precision']:.2%} | {s['recall']:.2%} | {s['f1']:.2%} |")
    lines += ["", "## 运行说明", ""]
    runtime = report.get("rag_runtime", {})
    if runtime.get("embedding_fallback"):
        lines.append("- 本轮 embedding 请求未成功，RAG 已按设计降级为 **PostgreSQL 关键词检索**；因此本报告的 Hit 率不能等同于纯向量召回效果。")
    else:
        lines.append("- 本轮未观察到 embedding 降级。")
    lines.append(f"- 情绪来源统计：`{json.dumps(report.get('emotion_source_counts', {}), ensure_ascii=False)}`")
    lines += ["", "## 失败样本", "", "### 意图错误", ""]
    wrong_intent = [d for d in report["details"] if not d["intent_correct"]]
    if wrong_intent:
        lines += ["| ID | 输入 | 期望 | 预测 |", "|---|---|---|---|"]
        lines += [f"| {d['id']} | {d['query']} | {d['expected_intent']} | {d['predicted_intent']} |" for d in wrong_intent]
    else:
        lines.append("无")
    lines += ["", "### 情绪错误", ""]
    wrong_emotion = [d for d in report["details"] if not d["emotion_correct"]]
    if wrong_emotion:
        lines += ["| ID | 输入 | 期望 | 预测 | 来源 |", "|---|---|---|---|---|"]
        lines += [f"| {d['id']} | {d['query']} | {d['expected_emotion']} | {d['predicted_emotion']} | {d['emotion_source']} |" for d in wrong_emotion]
    else:
        lines.append("无")
    lines += ["", "### RAG 未命中 / 误命中", ""]
    rag_bad = [d for d in report["details"] if (d["rag_expected"] and not d["rag_hit"]) or (not d["rag_expected"] and d["rag_result_count"] > 0)]
    if rag_bad:
        lines += ["| ID | 输入 | 期望 | rank | 返回数 |", "|---|---|---|---:|---:|"]
        for d in rag_bad:
            expected = "应命中" if d["rag_expected"] else "不应命中"
            lines.append(f"| {d['id']} | {d['query']} | {expected} | {d['rag_rank'] or '-'} | {d['rag_result_count']} |")
    else:
        lines.append("无")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--backend", default=os.getenv("RAG_BACKEND", "pgvector"), choices=["tfidf", "hybrid", "pgvector"])
    parser.add_argument("--emotion-mode", default="production", choices=["production", "keyword"], help="production 调用当前 sentiment.analyze；keyword 只测关键词快速路径")
    parser.add_argument("--json-out", default=str(REPORT_JSON))
    parser.add_argument("--md-out", default=str(REPORT_MD))
    args = parser.parse_args()

    cases = load_cases(Path(args.dataset))
    report = evaluate(cases, args.backend, args.emotion_mode)
    json_path = Path(args.json_out)
    md_path = Path(args.md_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")

    print(json.dumps({
        "dataset_size": report["dataset_size"],
        "intent_accuracy": report["intent"]["accuracy"],
        "intent_macro_f1": report["intent"]["macro_f1"],
        "emotion_accuracy": report["emotion"]["accuracy"],
        "emotion_macro_f1": report["emotion"]["macro_f1"],
        "rag": report["rag"],
        "emotion_source_counts": report["emotion_source_counts"],
        "rag_runtime": report.get("rag_runtime", {}),
        "json_report": str(json_path),
        "markdown_report": str(md_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
