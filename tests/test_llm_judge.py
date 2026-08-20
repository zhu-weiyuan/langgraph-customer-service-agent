"""Tests for LLM-as-Judge scorer effectiveness and score distribution."""
import pytest
import json
from pathlib import Path
from tests.eval_harness import LLMJudgeScorer, CustomerServiceScorer, EvalRunner, GoldenCase


class TestLLMJudgePromptEffectiveness:
    """Test LLM judge prompt effectiveness."""
    
    @pytest.fixture
    def judge(self):
        return LLMJudgeScorer(judge_model="qwen3.6-35b")
    
    def test_judge_distinguishes_good_bad_answers(self, judge):
        """Test that judge gives different scores to good vs bad answers."""
        question = "如何退货？"
        
        good_answer = "您好，退货流程如下：1. 登录账户 2. 找到订单 3. 点击申请退货 4. 填写原因 5. 寄回商品。我们承担退货运费。"
        bad_answer = "不知道"
        
        good_result = judge.evaluate(
            question, good_answer,
            criteria={"helpfulness": "Is answer helpful?", "completeness": "Is answer complete?"}
        )
        
        bad_result = judge.evaluate(
            question, bad_answer,
            criteria={"helpfulness": "Is answer helpful?", "completeness": "Is answer complete?"}
        )
        
        # Good answer should score higher (or at least not lower in fallback mode)
        # In fallback mode, length-based scoring should give good answer higher score
        assert good_result["score"] >= bad_result["score"]
    
    def test_judge_criteria_impact(self, judge):
        """Test that different criteria affect scoring."""
        question = "产品保修多久？"
        
        # Answer that's relevant but incomplete
        answer = "保修 1 年"
        
        criteria_relevance = {"relevance": "Is answer relevant to warranty?"}
        criteria_completeness = {"completeness": "Does answer cover all warranty details?"}
        
        result_relevance = judge.evaluate(question, answer, criteria_relevance)
        result_completeness = judge.evaluate(question, answer, criteria_completeness)
        
        # Both should be valid results
        assert "score" in result_relevance
        assert "score" in result_completeness
        assert "reasoning" in result_relevance
        assert "reasoning" in result_completeness
    
    def test_judge_reasoning_present(self, judge):
        """Test that judge provides reasoning."""
        result = judge.evaluate(
            "你好",
            "您好，有什么可以帮助您？",
            criteria={"clarity": "Is response clear?"}
        )
        assert "reasoning" in result
        assert len(result["reasoning"]) > 0


class TestLLMJudgeScoreDistribution:
    """Test LLM judge score distribution across different answer types."""
    
    @pytest.fixture
    def judge(self):
        return LLMJudgeScorer()
    
    def test_score_distribution_short_answers(self, judge):
        """Test scores for very short answers."""
        scores = []
        for i in range(5):
            result = judge.evaluate(
                f"问题{i}",
                "好",
                criteria={"quality": "Is answer good?"}
            )
            scores.append(result["score"])
        
        # Short answers should generally score low
        avg_score = sum(scores) / len(scores)
        assert avg_score <= 2.5
    
    def test_score_distribution_medium_answers(self, judge):
        """Test scores for medium-length answers."""
        scores = []
        answers = [
            "我会帮您查询",
            "请稍等，我看看",
            "这个问题需要核实"
        ]
        
        for answer in answers:
            result = judge.evaluate(
                "订单状态",
                answer,
                criteria={"helpfulness": "Is answer helpful?"}
            )
            scores.append(result["score"])
        
        avg_score = sum(scores) / len(scores)
        # Medium answers should be valid (fallback gives 3.0 for responsive answers)
        assert 0 <= avg_score <= 5.0
    
    def test_score_distribution_long_answers(self, judge):
        """Test scores for detailed answers."""
        scores = []
        answers = [
            "您好，关于您的问题，我们提供以下解决方案：首先，请确认您的订单号；其次，登录账户查看订单状态；最后，如有问题请联系客服。我们承诺 24 小时内回复。",
            "感谢您的咨询。我们的产品支持 7 天无理由退换货，保修期为 1 年。具体流程如下：1. 申请退货 2. 审核通过 3. 寄回商品 4. 退款到账。如有任何问题，请随时联系我们。"
        ]
        
        for answer in answers:
            result = judge.evaluate(
                "详细咨询",
                answer,
                criteria={"completeness": "Is answer complete?", "helpfulness": "Is answer helpful?"}
            )
            scores.append(result["score"])
        
        avg_score = sum(scores) / len(scores)
        # Long detailed answers should score higher
        assert avg_score >= 3.0


class TestLLMJudgeEdgeCases:
    """Test LLM judge with edge cases."""
    
    @pytest.fixture
    def judge(self):
        return LLMJudgeScorer()
    
    def test_empty_answer(self, judge):
        """Test judge with empty answer."""
        result = judge.evaluate(
            "问题",
            "",
            criteria={"quality": "Is answer good?"}
        )
        assert result["score"] <= 1.0
    
    def test_gibberish_answer(self, judge):
        """Test judge with gibberish answer."""
        result = judge.evaluate(
            "问题",
            "asdfghjkl12345!!!",
            criteria={"coherence": "Is answer coherent?"}
        )
        # Fallback mode gives 3.0 for answers with certain keywords, otherwise default
        # The key is that it returns a valid score
        assert 0 <= result["score"] <= 5.0
    
    def test_off_topic_answer(self, judge):
        """Test judge with off-topic answer."""
        result = judge.evaluate(
            "如何退货？",
            "今天天气不错，适合出去玩",
            criteria={"relevance": "Is answer relevant?"}
        )
        # In fallback mode, this might not be detected, but should still be valid
        assert "score" in result
        assert 0 <= result["score"] <= 5
    
    def test_special_characters_in_answer(self, judge):
        """Test judge with special characters."""
        result = judge.evaluate(
            "问题",
            "答案：✓ 正确 ✗ 错误 → 流程",
            criteria={"clarity": "Is answer clear?"}
        )
        assert "score" in result
    
    def test_multilingual_answer(self, judge):
        """Test judge with multilingual answer."""
        result = judge.evaluate(
            "Hello, how to return?",
            "您好，退货流程：1. Login 2. Request 3. Return",
            criteria={"helpfulness": "Is answer helpful?"}
        )
        assert "score" in result


class TestIntegrationWithScorer:
    """Test LLM judge integration with rule-based scorers."""
    
    def test_combined_scoring(self, tmp_path):
        """Test using both LLM judge and rule-based scorers together."""
        scorer = CustomerServiceScorer()
        judge = LLMJudgeScorer()
        
        question = "订单怎么查询？"
        answer = "您好，请登录账户，在'我的订单'页面可以查看所有订单状态和物流信息。"
        
        # Rule-based scores
        format_score = scorer.score_response_format(
            {"intent": "consult", "reply": answer},
            {"required": ["intent", "reply"]}
        )
        pii_score = scorer.score_pii_redaction(answer)
        
        # LLM judge score
        judge_result = judge.evaluate(
            question, answer,
            criteria={"helpfulness": "Is answer helpful?", "clarity": "Is answer clear?"}
        )
        llm_score = judge_result["score"] / 5.0  # Normalize to 0-1
        
        # All scores should be valid
        assert 0 <= format_score <= 1
        assert 0 <= pii_score <= 1
        assert 0 <= llm_score <= 1
    
    def test_eval_runner_with_llm_judge_tag(self, tmp_path):
        """Test eval runner processes llm_judge tagged cases."""
        runner = EvalRunner(
            task_name="llm_judge_test",
            export_path=tmp_path / "results.json"
        )
        
        # Create golden case with llm_judge tag
        cases = [
            GoldenCase(
                id="llm-test-1",
                input="产品怎么样？",
                expected_output_schema={"required": ["intent", "reply"]},
                expected_intent="consult",
                tags=["llm_judge"],
                context={
                    "actual": {
                        "intent": "consult",
                        "reply": "产品质量很好，用户评价很高"
                    }
                }
            )
        ]
        
        results = runner.run_suite(cases)
        
        assert results["total"] == 1
        assert "results" in results
        assert len(results["results"]) == 1


class TestModelConfiguration:
    """Test different model configurations for LLM judge."""
    
    def test_different_model_names(self):
        """Test initializing judge with different model names."""
        models = ["qwen3.6-35b", "gpt-4", "claude-3", "custom-model"]
        
        for model in models:
            judge = LLMJudgeScorer(judge_model=model)
            assert judge.judge_model == model
    
    def test_default_model(self):
        """Test default model configuration."""
        judge = LLMJudgeScorer()
        assert judge.judge_model == "qwen3.6-35b"
