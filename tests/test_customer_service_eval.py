from tests.eval_harness import CustomerServiceEvaluator


def test_intent_accuracy_scorer_exact_and_mismatch():
    evaluator = CustomerServiceEvaluator()
    assert evaluator.intent_accuracy_scorer("consult", "consult") == 1
    assert evaluator.intent_accuracy_scorer("complaint", "consult") == 0


def test_satisfaction_response_scorer_keyword_patterns():
    evaluator = CustomerServiceEvaluator()
    assert evaluator.satisfaction_response_scorer("很高兴您满意", ["满意", "感谢"]) == 1
    assert evaluator.satisfaction_response_scorer("我会继续处理", ["满意", "感谢"]) == 0


def test_pii_redaction_scorer_before_and_after_redaction():
    evaluator = CustomerServiceEvaluator()
    assert evaluator.pii_redaction_scorer("电话是13800138000") == 0
    assert evaluator.pii_redaction_scorer("电话是138****8000") == 1


def test_response_format_scorer_valid_and_invalid_json():
    evaluator = CustomerServiceEvaluator()
    assert evaluator.response_format_scorer('{"intent":"consult","reply":"您好"}') == 1
    assert evaluator.response_format_scorer('{"intent":"consult"}') == 0
    assert evaluator.response_format_scorer("not json") == 0


def test_escalation_correctness_scorer_high_severity_requires_escalation():
    evaluator = CustomerServiceEvaluator()
    assert evaluator.escalation_correctness_scorer("已为您升级人工专员", {"severity": "high"}) == 1
    assert evaluator.escalation_correctness_scorer("我会处理", {"severity": "critical"}) == 0
    assert evaluator.escalation_correctness_scorer("我会处理", {"severity": "low"}) == 1
