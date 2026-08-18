"""Regression tests for unified context token monitoring."""

from agent.context_monitor import TokenEstimator
from agent.token_estimator import estimate_messages_tokens, estimate_tokens


def test_monitor_estimator_delegates_to_unified_token_estimator():
    estimator = TokenEstimator()
    text = "智能音箱 WiFi 连接失败，错误码 E-200"

    assert estimator.estimate_text(text) == estimate_tokens(text)
    assert estimator.estimate_messages([
        {"role": "user", "content": text},
        {"role": "assistant", "content": "请检查路由器设置。"},
    ]) == estimate_messages_tokens([
        {"role": "user", "content": text},
        {"role": "assistant", "content": "请检查路由器设置。"},
    ])


def test_monitor_reports_threshold_levels_from_unified_count():
    estimator = TokenEstimator()
    usage = estimator.monitor(system_prompt="系统提示", context_window=1)

    assert usage.input_tokens == estimate_tokens("系统提示")
    assert usage.level == "dumb_zone"
