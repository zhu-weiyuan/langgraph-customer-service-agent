# -*- coding: utf-8 -*-
"""
纯 stdlib 单测：每个分层指标至少一个手算验证 case + EvalRunner 端到端四层。

运行：
    python -m unittest tests.test_eval_metrics_pure -v
    # 或
    python tests/test_eval_metrics_pure.py
无任何三方依赖；LLM/embedding 全部用注入 mock。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import metrics as M                     # noqa: E402
from eval.harness import (                        # noqa: E402
    EvalCase, EvalRunner, ResultStore, build_mock_agent_fn, load_golden_set,
    dataset_hash, meets_target,
)


def close(a, b, eps=1e-6):
    return abs(a - b) < eps


# ══════════════════════════ 1) 检索层 ══════════════════════════
class TestRetrievalMetrics(unittest.TestCase):
    def test_recall_at_k(self):
        # relevant={B,D}, top-3=[A,B,C] → 命中 B → 1/2
        self.assertTrue(close(M.recall_at_k(["A", "B", "C", "D"], ["B", "D"], 3), 0.5))
        self.assertEqual(M.recall_at_k([], ["X"], 3), 0.0)
        self.assertEqual(M.recall_at_k(["A"], [], 3), 1.0)  # 无相关 → 满分

    def test_hit_rate_at_k(self):
        self.assertEqual(M.hit_rate_at_k(["A", "B"], ["B"], 2), 1.0)
        self.assertEqual(M.hit_rate_at_k(["A", "B"], ["Z"], 2), 0.0)
        self.assertEqual(M.hit_rate_at_k(["A", "B"], ["B"], 1), 0.0)  # B 在第2位，k=1 不命中

    def test_mrr(self):
        # 第一个相关在第 2 位 → 1/2
        self.assertTrue(close(M.mrr(["A", "B", "C"], ["B", "C"]), 0.5))
        # 第一个相关在第 1 位 → 1
        self.assertTrue(close(M.mrr(["B", "A"], ["B"]), 1.0))
        self.assertEqual(M.mrr(["A", "B"], ["Z"]), 0.0)

    def test_precision_at_k(self):
        # top-3=[A,B,C], relevant={B,C} → 2/3
        self.assertTrue(close(M.precision_at_k(["A", "B", "C"], ["B", "C"], 3), 2.0 / 3.0))
        self.assertEqual(M.precision_at_k(["A"], ["A"], 0), 0.0)

    def test_context_precision(self):
        # retrieved=[R,N,R], relevant={R1?}  用具体 id
        # [R1, N, R2], relevant={R1,R2}: 命中1@pos1 →1/1; 命中2@pos3 →2/3; AP=(1+0.667)/2=0.8333
        val = M.context_precision(["R1", "N", "R2"], ["R1", "R2"])
        self.assertTrue(close(val, (1.0 + 2.0 / 3.0) / 2.0))
        # 相关全部靠前 → 1.0
        self.assertTrue(close(M.context_precision(["R1", "R2", "N"], ["R1", "R2"]), 1.0))
        self.assertEqual(M.context_precision(["N1", "N2"], ["R"]), 0.0)

    def test_context_recall(self):
        pts = ["退款", "运费", "缺失点"]
        ctx = ["支持退款并说明运费规则"]
        self.assertTrue(close(M.context_recall(pts, ctx), 2.0 / 3.0))
        # 注入 judge：全部覆盖
        self.assertEqual(M.context_recall(pts, ctx, judge_fn=lambda p, c: True), 1.0)


# ══════════════════════════ 2) 生成层 ══════════════════════════
class TestGenerationMetrics(unittest.TestCase):
    def test_faithfulness_sentence_support(self):
        # 两句：第一句被 context 覆盖，第二句是编造 → 1/2
        answer = "保修一年需要凭证。赠送终身免费上门。"
        contexts = ["本产品保修一年，申请保修需要购买凭证。"]
        val = M.faithfulness(answer, contexts, support_threshold=0.6)
        self.assertTrue(close(val, 0.5), f"got {val}")
        # 注入 judge：全支撑
        self.assertEqual(
            M.faithfulness(answer, contexts, judge_fn=lambda s, c: True), 1.0)
        # 无句子 → 1.0
        self.assertEqual(M.faithfulness("", contexts), 1.0)

    def test_answer_relevance_rule(self):
        # 内容词 退货 在答案里 → 高相关
        self.assertTrue(M.answer_relevance("怎么退货", "您可以在订单页申请退货") >= 0.99)
        # 答非所问
        self.assertTrue(M.answer_relevance("怎么退货", "今天天气不错") < 0.5)
        # judge 注入
        self.assertTrue(close(
            M.answer_relevance("q", "a", judge_fn=lambda q, a: 1.0), 1.0))
        # embed 注入（正交向量 → 0）
        emb = {"q": [1.0, 0.0], "a": [0.0, 1.0]}
        self.assertEqual(
            M.answer_relevance("q", "a", embed_fn=lambda t: emb[t]), 0.0)

    def test_completeness(self):
        self.assertTrue(close(
            M.completeness("已说明A和B", ["A", "B", "C"]), 2.0 / 3.0))
        self.assertEqual(M.completeness("x", []), 1.0)

    def test_context_usage(self):
        # answer 全部 token 都来自 context → 1.0
        self.assertEqual(M.context_usage("退货 退款", ["退货 退款 流程"]), 1.0)
        self.assertEqual(M.context_usage("", ["x"]), 0.0)
        # 一半来自 context
        v = M.context_usage("abc xyz", ["abc only"])
        self.assertTrue(close(v, 0.5))

    def test_noise_sensitivity(self):
        # 干净答案覆盖 2/2，注噪只覆盖 1/2 → sensitivity=1.0-0.5=0.5
        kp = ["退款", "运费"]
        val = M.noise_sensitivity("退款运费都说明", "只说了退款", kp)
        self.assertTrue(close(val, 0.5), f"got {val}")
        # 无退化 → 0
        self.assertEqual(M.noise_sensitivity("退款运费", "退款运费", kp), 0.0)

    def test_pairwise_judge_debias(self):
        # judge 恒偏向“位置A” → 交换后结论矛盾 → tie（位置偏差被治理）
        biased = lambda s, u: "A"
        self.assertEqual(
            M.pairwise_judge_debiased(biased, "sys", "q", "cand", "base"), "tie")
        # judge 恒偏 candidate（无论位置）：A 在前答A、candidate 在后答B
        def fair(system, user):
            # candidate 文本含 'GOOD'
            a_part = user.split("回答A：")[1].split("回答B：")[0]
            return "A" if "GOOD" in a_part else "B"
        self.assertEqual(
            M.pairwise_judge_debiased(fair, "sys", "q", "GOOD", "bad"), "a")


# ══════════════════════════ 3) Agent 层 ══════════════════════════
class TestAgentMetrics(unittest.TestCase):
    def test_tool_selection_accuracy(self):
        expected = [{"tool": "lookup"}, {"tool": "refund"}]
        actual = [{"tool": "lookup"}, {"tool": "extra"}]
        self.assertTrue(close(M.tool_selection_accuracy(actual, expected), 0.5))
        self.assertEqual(M.tool_selection_accuracy([], []), 1.0)

    def test_parameter_accuracy(self):
        expected = [{"tool": "lookup", "args": {"id": "1"}},
                    {"tool": "refund", "args": {"id": "1"}}]
        actual = [{"tool": "lookup", "args": {"id": "1"}},        # 参数对
                  {"tool": "refund", "args": {"id": "WRONG"}}]    # 参数错
        self.assertTrue(close(M.parameter_accuracy(actual, expected), 0.5))

    def test_unnecessary_call_rate(self):
        expected = [{"tool": "lookup"}, {"tool": "refund"}]
        actual = [{"tool": "lookup"}, {"tool": "refund"}, {"tool": "extra"}]
        self.assertTrue(close(M.unnecessary_call_rate(actual, expected), 1.0 / 3.0))
        self.assertEqual(M.unnecessary_call_rate([], []), 0.0)

    def test_task_completion_rate(self):
        self.assertTrue(close(M.task_completion_rate([True, False, True, True]), 0.75))

    def test_error_recovery_rate(self):
        recs = [
            {"trajectory": [{"ok": False}, {"ok": True}], "success": True},   # 失败后恢复
            {"trajectory": [{"ok": False}], "success": False},                # 失败未恢复
            {"trajectory": [{"ok": True}], "success": True},                  # 无失败，不计
        ]
        self.assertTrue(close(M.error_recovery_rate(recs), 0.5))
        self.assertEqual(M.error_recovery_rate([{"trajectory": [{"ok": True}], "success": True}]), 1.0)

    def test_avg_turns_and_calls(self):
        recs = [{"turns": 2, "trajectory": [1, 2]}, {"turns": 4, "trajectory": [1]}]
        self.assertTrue(close(M.avg_turns(recs), 3.0))
        self.assertTrue(close(M.avg_tool_calls(recs), 1.5))

    def test_stability_and_consecutive(self):
        self.assertEqual(M.agent_stability([False, True, False]), 1.0)   # 至少1成
        self.assertEqual(M.agent_stability([False, False]), 0.0)
        self.assertEqual(M.consecutive_success_rate([True, True, True]), 1.0)
        self.assertEqual(M.consecutive_success_rate([True, False, True]), 0.0)


# ══════════════════════════ 4) 工程层 ══════════════════════════
class TestEngineeringMetrics(unittest.TestCase):
    def test_json_validity_rate(self):
        outs = ['{"a":1}', 'not json', '{"b":2}', '{bad}']
        self.assertTrue(close(M.json_validity_rate(outs), 0.5))

    def test_schema_pass_rate(self):
        objs = [{"intent": "x", "reply": "y"}, {"intent": "x"}, '{"intent":"z","reply":"w"}']
        self.assertTrue(close(M.schema_pass_rate(objs, ["intent", "reply"]), 2.0 / 3.0))

    def test_enum_accuracy(self):
        self.assertTrue(close(
            M.enum_accuracy(["a", "b", "zzz"], ["a", "b", "c"]), 2.0 / 3.0))

    def test_latency_stats(self):
        st = M.latency_stats([100, 200, 300, 400])
        self.assertEqual(st["count"], 4)
        self.assertTrue(close(st["mean"], 250.0))
        self.assertTrue(close(st["p50"], 250.0))   # 线性插值中位
        self.assertEqual(st["max"], 400.0)

    def test_token_stats(self):
        st = M.token_stats([100, 200], [10, 30])
        self.assertEqual(st["input_total"], 300)
        self.assertEqual(st["output_total"], 40)
        self.assertEqual(st["total"], 340)
        self.assertTrue(close(st["input_mean"], 150.0))

    def test_retry_rate(self):
        recs = [{"retries": 0}, {"retries": 2}, {"retries": 1}, {"retries": 0}]
        self.assertTrue(close(M.retry_rate(recs), 0.5))

    def test_refusal_rate(self):
        ans = ["好的没问题", "抱歉我无法提供", "这是答案", "很抱歉无法协助"]
        self.assertTrue(close(M.refusal_rate(ans), 0.5))

    def test_hallucination_rate(self):
        # 一条全支撑(幻觉0)，一条 2 句里 1 句无支撑(幻觉0.5) → mean=0.25
        recs = [
            {"answer": "保修一年。", "contexts": ["保修一年"]},
            {"answer": "保修一年。赠送终身上门。", "contexts": ["保修一年"]},
        ]
        self.assertTrue(close(M.hallucination_rate(recs), 0.25), M.hallucination_rate(recs))

    def test_format_following_rate(self):
        outs = ["ORD-100200", "ORD-99", "ORD-123456"]
        self.assertTrue(close(M.format_following_rate(outs, r"ORD-\d{6}"), 2.0 / 3.0))


# ═══════════════ 辅助 & 注册表 ═══════════════
class TestHelpersAndRegistry(unittest.TestCase):
    def test_tokenize_deterministic(self):
        self.assertEqual(M.tokenize("Hello 世界"), ["hello", "世", "界"])

    def test_percentile(self):
        self.assertTrue(close(M.percentile([1, 2, 3, 4], 50), 2.5))
        self.assertEqual(M.percentile([], 50), 0.0)

    def test_cosine(self):
        self.assertTrue(close(M.cosine_similarity([1, 0], [1, 0]), 1.0))
        self.assertEqual(M.cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_metric_count_and_groups(self):
        self.assertEqual(len(M.METRIC_GROUPS), 4)
        self.assertEqual(M.metric_count(), sum(len(v) for v in M.METRIC_GROUPS.values()))
        self.assertGreaterEqual(M.metric_count(), 26)

    def test_meets_target_direction(self):
        self.assertTrue(meets_target("recall_at_k", 0.9))          # higher
        self.assertFalse(meets_target("recall_at_k", 0.1))
        self.assertTrue(meets_target("hallucination_rate", 0.05))  # lower
        self.assertFalse(meets_target("hallucination_rate", 0.5))


# ═══════════════ EvalRunner 端到端（四层，mock agent）═══════════════
class TestEvalRunnerEndToEnd(unittest.TestCase):
    def test_four_layer_report_with_mock_agent(self):
        golden = str(ROOT / "eval" / "golden_set.jsonl")
        cases = load_golden_set(golden)
        self.assertGreaterEqual(len(cases), 60)

        runner = EvalRunner(build_mock_agent_fn(degrade=False), top_k=3)
        report = runner.run_all(cases)

        # 四层都有报告且非空
        for layer in ("retrieval", "generation", "agent", "engineering"):
            self.assertIn(layer, report)
            self.assertGreater(report[layer]["num_cases"], 0, layer)
            self.assertTrue(report[layer]["metrics"], layer)

        # 理想 mock：检索/Agent 层核心指标应达标
        self.assertEqual(report["retrieval"]["metrics"]["recall_at_k"], 1.0)
        self.assertEqual(report["agent"]["metrics"]["tool_selection_accuracy"], 1.0)
        self.assertEqual(report["generation"]["metrics"]["faithfulness"], 1.0)
        self.assertEqual(report["engineering"]["metrics"]["schema_pass_rate"], 1.0)

    def test_degraded_mock_produces_failures(self):
        cases = load_golden_set(str(ROOT / "eval" / "golden_set.jsonl"))
        runner = EvalRunner(build_mock_agent_fn(degrade=True), top_k=3)
        rep = runner.run_layer(cases, "agent")
        # 退化 mock 会引入多余调用 → unnecessary_call_rate 上升
        self.assertGreater(rep["metrics"]["unnecessary_call_rate"], 0.0)

    def test_judge_injection_end_to_end(self):
        cases = load_golden_set(str(ROOT / "eval" / "golden_set.jsonl"))

        def mock_judge(system, user):
            return "YES" if ("核查" in system or "支撑" in user) else "4"

        runner = EvalRunner(build_mock_agent_fn(), judge_fn=mock_judge, use_judge=True)
        rep = runner.run_layer(cases, "generation")
        # judge 判定 answer_relevance=4/5=0.8
        self.assertTrue(close(rep["metrics"]["answer_relevance"], 0.8), rep["metrics"])

    def test_persistence_and_baseline_delta(self):
        cases = load_golden_set(str(ROOT / "eval" / "golden_set.jsonl"))
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "eval_results.db")
            store = ResultStore(db)
            runner = EvalRunner(build_mock_agent_fn(), store=store, top_k=3)
            r1 = runner.run_and_persist(cases, "retrieval", prompt_version="v1",
                                        model_id="mock", dataset_version="t1")
            self.assertIn("run_id", r1)
            # 第二次 run → 应出 baseline_delta
            r2 = runner.run_and_persist(cases, "retrieval", prompt_version="v2")
            self.assertIn("baseline_delta", r2)
            # 落库校验：eval_runs 与 case 明细都有记录
            import sqlite3
            conn = sqlite3.connect(db)
            runs = conn.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0]
            crows = conn.execute("SELECT COUNT(*) FROM eval_case_results").fetchone()[0]
            # 归因字段落库
            row = conn.execute(
                "SELECT prompt_version, model_id, dataset_version, input_hash "
                "FROM eval_runs ORDER BY run_id LIMIT 1").fetchone()
            conn.close()
            self.assertEqual(runs, 2)
            self.assertGreater(crows, 0)
            self.assertEqual(row[0], "v1")
            self.assertEqual(row[1], "mock")
            self.assertTrue(row[3])  # input_hash 非空

    def test_dataset_hash_stable(self):
        cases = load_golden_set(str(ROOT / "eval" / "golden_set.jsonl"))
        self.assertEqual(dataset_hash(cases), dataset_hash(cases))
        self.assertEqual(len(dataset_hash(cases)), 16)


if __name__ == "__main__":
    unittest.main(verbosity=2)
