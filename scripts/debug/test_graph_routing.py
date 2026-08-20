"""Offline routing tests for the customer-service LangGraph topology."""

from agent.graph import route_after_intent, route_after_reply, should_resolve


def test_regular_reply_waits_for_next_user_turn():
    assert route_after_reply({"ending": False, "retry_count": 0}) == "__end__"


def test_retry_reply_asks_for_satisfaction_again():
    assert route_after_reply({"ending": False, "retry_count": 1}) == "check_satisfaction"


def test_pending_satisfaction_routes_the_next_turn_to_feedback_processing():
    assert route_after_intent({"awaiting_satisfaction": True}) == "process_satisfaction"


def test_regular_turn_routes_to_reply_generation():
    assert route_after_intent({"awaiting_satisfaction": False}) == "generate_reply"


def test_satisfied_customer_finalizes():
    assert should_resolve({"satisfaction": True, "retry_count": 0}) == "finalize"


def test_unsatisfied_customer_retries_before_limit():
    assert should_resolve({"satisfaction": False, "retry_count": 2}) == "generate_reply"


def test_unsatisfied_customer_escalates_at_retry_limit():
    assert should_resolve({"satisfaction": False, "retry_count": 3}) == "escalate_to_human"
