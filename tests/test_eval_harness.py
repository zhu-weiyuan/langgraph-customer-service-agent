"""Unit tests for evaluation harness components."""
import pytest
import json
from pathlib import Path
from tests.eval_harness import (
    GoldenCase, EvalResult, CustomerServiceScorer, 
    LLMJudgeScorer, EvalRunner
)
from tests.eval_persistence import EvalResultStore


class TestGoldenCase:
    """Test GoldenCase dataclass."""
    
    def test_golden_case_minimal(self):
        """Test minimal golden case creation."""
        case = GoldenCase(
            id="test-1",
            input="你好",
            expected_output_schema={"required": ["intent", "reply"]}
        )
        assert case.id == "test-1"
        assert case.input == "你好"
        assert case.context is None
        assert case.expected_tools == []
        assert case.tags == []
    
    def test_golden_case_full(self):
        """Test full golden case with all fields."""
        case = GoldenCase(
            id="test-2",
            input="订单查询",
            expected_output_schema={"required": ["intent", "reply", "order_id"]},
            context={"order_id": "12345"},
            expected_tools=["order_lookup"],
            tags=["intent_classification", "order_inquiry"],
            expected_intent="consult",
            expected_reply_keywords=["订单", "查询"]
        )
        assert case.expected_intent == "consult"
        assert len(case.expected_tools) == 1
        assert len(case.tags) == 2


class TestEvalResult:
    """Test EvalResult dataclass."""
    
    def test_eval_result_creation(self):
        """Test eval result creation."""
        result = EvalResult(
            case_id="test-1",
            actual_output={"intent": "consult", "reply": "您好"},
            scores={"format": 1.0, "intent": 1.0},
            judge_reasoning="Good response",
            passed=True
        )
        assert result.case_id == "test-1"
        assert result.passed is True
        assert len(result.scores) == 2


class TestCustomerServiceScorer:
    """Test rule-based scorers."""
    
    @pytest.fixture
    def scorer(self):
        return CustomerServiceScorer()
    
    def test_intent_accuracy_exact_match(self, scorer):
        """Test intent accuracy with exact match."""
        assert scorer.score_intent_accuracy("consult", "consult") == 1.0
        assert scorer.score_intent_accuracy("complaint", "complaint") == 1.0
    
    def test_intent_accuracy_mismatch(self, scorer):
        """Test intent accuracy with mismatch."""
        assert scorer.score_intent_accuracy("consult", "complaint") == 0.0
        assert scorer.score_intent_accuracy("chat", "ending") == 0.0
    
    def test_satisfaction_response_with_keywords(self, scorer):
        """Test satisfaction response scoring with keywords."""
        response = "很高兴您满意我们的服务"
        keywords = ["满意", "感谢", "高兴"]
        score = scorer.score_satisfaction_response(response, keywords)
        assert score > 0.0  # Should match at least "高兴" and "满意"
    
    def test_satisfaction_response_no_keywords(self, scorer):
        """Test satisfaction response with no matching keywords."""
        response = "我会继续处理"
        keywords = ["满意", "感谢"]
        assert scorer.score_satisfaction_response(response, keywords) == 0.0
    
    def test_pii_redaction_clean_text(self, scorer):
        """Test PII redaction with clean text."""
        assert scorer.score_pii_redaction("您好，有什么可以帮助您？") == 1.0
        assert scorer.score_pii_redaction("电话是 138****8000") == 1.0
    
    def test_pii_redaction_phone_number(self, scorer):
        """Test PII redaction detects phone numbers."""
        assert scorer.score_pii_redaction("我的电话是 13800138000") == 0.0
        assert scorer.score_pii_redaction("联系 13912345678") == 0.0
    
    def test_pii_redaction_id_card(self, scorer):
        """Test PII redaction detects ID cards."""
        assert scorer.score_pii_redaction("身份证 110101199001011234") == 0.0
    
    def test_response_format_valid_json(self, scorer):
        """Test response format with valid JSON."""
        schema = {"required": ["intent", "reply"]}
        output = '{"intent": "consult", "reply": "您好"}'
        assert scorer.score_response_format(output, schema) == 1.0
    
    def test_response_format_missing_keys(self, scorer):
        """Test response format with missing required keys."""
        schema = {"required": ["intent", "reply", "order_id"]}
        output = '{"intent": "consult", "reply": "您好"}'
        score = scorer.score_response_format(output, schema)
        assert 0.0 < score < 1.0  # Partial match
    
    def test_response_format_invalid_json(self, scorer):
        """Test response format with invalid JSON."""
        schema = {"required": ["intent", "reply"]}
        assert scorer.score_response_format("not json", schema) == 0.0
        assert scorer.score_response_format('{"intent": "consult"}', schema) == 0.5
    
    def test_response_format_dict_input(self, scorer):
        """Test response format with dict input."""
        schema = {"required": ["intent", "reply"]}
        output = {"intent": "consult", "reply": "您好"}
        assert scorer.score_response_format(output, schema) == 1.0
    
    def test_escalation_correctness_should_escalate(self, scorer):
        """Test escalation correctness when escalation needed."""
        assert scorer.score_escalation_correctness(True, True) == 1.0
        assert scorer.score_escalation_correctness(True, False) == 0.0
    
    def test_escalation_correctness_no_escalation(self, scorer):
        """Test escalation correctness when no escalation needed."""
        assert scorer.score_escalation_correctness(False, False) == 1.0
        assert scorer.score_escalation_correctness(False, True) == 0.0
    
    def test_recall_full_match(self, scorer):
        """Test recall with full match."""
        expected = ["订单", "查询", "物流"]
        response = "我帮您查询订单物流信息"
        assert scorer.score_recall(expected, response) == 1.0
    
    def test_recall_partial_match(self, scorer):
        """Test recall with partial match."""
        expected = ["订单", "查询", "物流", "配送"]
        response = "我帮您查询订单"
        score = scorer.score_recall(expected, response)
        assert score == 0.5  # 2 out of 4
    
    def test_recall_no_match(self, scorer):
        """Test recall with no match."""
        expected = ["订单", "查询"]
        response = "今天天气不错"
        assert scorer.score_recall(expected, response) == 0.0
    
    def test_faithfulness_with_context(self, scorer):
        """Test faithfulness with context."""
        context = {"product": "iPhone", "price": "5999", "color": "black"}
        response = "iPhone 的价格是 5999 元，有 black 颜色"
        score = scorer.score_faithfulness(context, response)
        assert score > 0.8  # Should have high overlap
    
    def test_faithfulness_no_context(self, scorer):
        """Test faithfulness with no context."""
        assert scorer.score_faithfulness({}, "any response") == 1.0
    
    def test_tool_usage_full_match(self, scorer):
        """Test tool usage with full match."""
        expected = ["order_lookup", "refund_processor"]
        actual = ["order_lookup", "refund_processor"]
        assert scorer.score_tool_usage(expected, actual) == 1.0
    
    def test_tool_usage_partial_match(self, scorer):
        """Test tool usage with partial match."""
        expected = ["order_lookup", "refund_processor", "escalation"]
        actual = ["order_lookup"]
        assert scorer.score_tool_usage(expected, actual) == pytest.approx(0.33, rel=0.1)
    
    def test_tool_usage_no_expected(self, scorer):
        """Test tool usage with no expected tools."""
        assert scorer.score_tool_usage([], ["any_tool"]) == 1.0


class TestLLMJudgeScorer:
    """Test LLM-as-Judge scorer."""
    
    @pytest.fixture
    def judge(self):
        return LLMJudgeScorer()
    
    def test_judge_initialization(self, judge):
        """Test LLM judge initialization."""
        assert judge.judge_model == "qwen3.6-35b"
        assert judge._client is None
    
    def test_judge_evaluate_fallback(self, judge):
        """Test judge evaluate with fallback (no LLM client)."""
        result = judge.evaluate(
            question="你好",
            answer="您好，有什么可以帮助您？",
            criteria={"relevance": "Is answer relevant?"}
        )
        assert "score" in result
        assert "reasoning" in result
        assert 0 <= result["score"] <= 5
    
    def test_judge_evaluate_short_answer(self, judge):
        """Test judge with very short answer."""
        result = judge.evaluate(
            question="请详细说明",
            answer="好",
            criteria={"completeness": "Is answer complete?"}
        )
        assert result["score"] <= 2.0  # Should be penalized
    
    def test_judge_evaluate_responsive_answer(self, judge):
        """Test judge with responsive answer."""
        result = judge.evaluate(
            question="What is the answer?",
            answer="The answer is...",
            criteria={"relevance": "Is answer relevant?"}
        )
        assert result["score"] >= 3.0  # Should be decent


class TestEvalRunner:
    """Test evaluation runner."""
    
    @pytest.fixture
    def runner(self, tmp_path):
        return EvalRunner(
            task_name="test_task",
            export_path=tmp_path / "eval_results.json"
        )
    
    @pytest.fixture
    def sample_golden_set(self):
        return [
            GoldenCase(
                id="test-1",
                input="你好",
                expected_output_schema={"required": ["intent", "reply"]},
                expected_intent="chat",
                expected_reply_keywords=["你好", "您好"]
            ),
            GoldenCase(
                id="test-2",
                input="订单查询",
                expected_output_schema={"required": ["intent", "reply"]},
                expected_intent="consult",
                context={"order_id": "12345"}
            )
        ]
    
    def test_load_golden_set_from_file(self, runner, tmp_path):
        """Test loading golden set from JSON file."""
        golden_data = [
            {"id": "case-1", "input": "test", "expected_output_schema": {}}
        ]
        golden_file = tmp_path / "golden.json"
        golden_file.write_text(json.dumps(golden_data), encoding="utf-8")
        
        cases = runner.load_golden_set(golden_file)
        assert len(cases) == 1
        assert cases[0].id == "case-1"
    
    def test_run_suite_without_graph(self, runner, sample_golden_set):
        """Test running suite without graph (uses mock outputs)."""
        # Add mock actual output to context
        for case in sample_golden_set:
            if case.context is None:
                case.context = {}
            case.context["actual"] = {
                "intent": case.expected_intent,
                "reply": "测试回复"
            }
        
        results = runner.run_suite(sample_golden_set)
        
        assert "total" in results
        assert "pass_rate" in results
        assert "results" in results
        assert results["total"] == len(sample_golden_set)
    
    def test_run_suite_records_to_store(self, runner, sample_golden_set, tmp_path):
        """Test that runner records results to store."""
        store_path = tmp_path / "test_store.db"
        runner.store = EvalResultStore(store_path)
        
        for case in sample_golden_set:
            if case.context is None:
                case.context = {}
            case.context["actual"] = {"intent": "test", "reply": "test"}
        
        runner.run_suite(sample_golden_set)
        
        # Verify results were recorded
        recorded = runner.store.get_results(runner.task_name)
        assert len(recorded) == len(sample_golden_set)
    
    def test_run_suite_statistics(self, runner, sample_golden_set):
        """Test run suite generates correct statistics."""
        for case in sample_golden_set:
            if case.context is None:
                case.context = {}
            case.context["actual"] = {
                "intent": case.expected_intent,
                "reply": "您好，有什么可以帮助您？"
            }
        
        results = runner.run_suite(sample_golden_set)
        
        assert results["total"] == 2
        assert "average_scores" in results
        assert "failed_cases" in results
        assert isinstance(results["failed_cases"], list)


class TestIntegration:
    """Integration tests for full evaluation flow."""
    
    def test_full_evaluation_flow(self, tmp_path):
        """Test complete evaluation flow from golden set to results."""
        # Create golden set
        golden_data = [
            {
                "id": "integration-1",
                "input": "你好",
                "expected_output_schema": {"required": ["intent", "reply"]},
                "expected_intent": "chat"
            }
        ]
        golden_file = tmp_path / "golden.json"
        golden_file.write_text(json.dumps(golden_data), encoding="utf-8")
        
        # Create runner with isolated database
        db_path = tmp_path / "test_eval.db"
        runner = EvalRunner(
            task_name="integration_test",
            export_path=tmp_path / "results.json",
            store=EvalResultStore(db_path)
        )
        
        # Load and run
        cases = runner.load_golden_set(golden_file)
        for case in cases:
            case.context = {"actual": {"intent": "chat", "reply": "您好"}}
        
        results = runner.run_suite(cases)
        
        # Verify
        assert results["total"] == 1
        assert results["pass_rate"] > 0
        
        # Verify export file exists and contains only this test's results
        assert runner.export_path.exists()
        exported = json.loads(runner.export_path.read_text())
        # Filter to only our task
        our_results = [r for r in exported if r.get("task_name") == "integration_test"]
        assert len(our_results) == 1
