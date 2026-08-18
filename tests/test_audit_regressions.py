import asyncio
import sys
import types
from unittest.mock import Mock

import pytest

from agent import token_estimator as te
from agent.agentic_rag import _has_requested_entity_evidence
from agent.context_compaction import AIMessage, ContextCompactor, HumanMessage
from agent.eval_enhanced import faithfulness_score
from agent.hybrid_rag import QueryRewriter
from agent.json_parsing import parse_json_array, parse_json_object
from agent.llm_client import LLMClient, LLMStreamInterruptedError
from agent.llm_gateway import GatewayRequest, LLMGateway, estimate_tokens
from agent.token_estimator import estimate_messages_tokens


def test_embedded_json_parser_avoids_greedy_cross_array_match():
    text = '示例 []，解释 ["不会被拼接"]，最终输出：["退款流程", "退货条件"]。'
    assert parse_json_array(text) == ["不会被拼接"]
    # A direct QueryRewriter consumer gets a bounded variant list rather than
    # falling back because a greedy regex swallowed both arrays.
    rewriter = QueryRewriter(llm_fn=lambda _query: text)
    assert rewriter._llm_variants("怎么退款") == ["不会被拼接"]


def test_json_parser_skips_invalid_object_and_requires_score():
    text = '说明 {not json} 结果 {"score": 0.8, "claims": []}'
    assert parse_json_object(text, required_keys=("score",)) == {"score": 0.8, "claims": []}


def test_entity_evidence_accepts_positive_chunk_despite_negative_chunk():
    hits = [
        {"title": "旧说明", "text": "空调暂不支持远程控制。"},
        {"title": "新说明", "text": "升级后的空调支持在 App 中远程调节温度。"},
    ]
    assert _has_requested_entity_evidence("空调可以远程调温吗", hits)


def test_entity_evidence_rejects_local_negation():
    hits = [{"title": "功能限制", "text": "当前产品不支持空调控制。"}]
    assert not _has_requested_entity_evidence("空调可以远程调温吗", hits)


def test_token_estimator_retries_after_transient_failure(monkeypatch):
    class Encoder:
        def encode(self, text):
            return list(text)

    fake_tiktoken = types.SimpleNamespace(get_encoding=lambda _name: Encoder())
    monkeypatch.setenv("TOKEN_ESTIMATOR_RETRY_SECONDS", "60")
    monkeypatch.setattr(te.time, "monotonic", lambda: 10.0)
    old_encoder, old_failed = te._ENCODER, te._ENCODER_FAILED_AT
    try:
        te._ENCODER, te._ENCODER_FAILED_AT = False, 0.0
        assert te._get_encoder() is None  # cooldown: no import/retry yet
        monkeypatch.setattr(te.time, "monotonic", lambda: 61.0)
        monkeypatch.setitem(sys.modules, "tiktoken", fake_tiktoken)
        assert te._get_encoder() is not None
    finally:
        te._ENCODER, te._ENCODER_FAILED_AT = old_encoder, old_failed


def _messages(turns=10, middle_extra=""):
    messages = []
    for index in range(turns):
        messages.append(HumanMessage(content=f"用户问题 {index} {middle_extra}"))
        messages.append(AIMessage(content=f"客服回答 {index}"))
    return messages


def test_context_compaction_cache_is_content_addressed_and_lru_bounded(monkeypatch):
    monkeypatch.setenv("CONTEXT_SUMMARY_CACHE_SIZE", "1")
    compactor = ContextCompactor()
    compactor._compact_old_messages = Mock(side_effect=["摘要一", "摘要二", "摘要三"])

    first = _messages()
    assert compactor.maybe_compact(first, session_id="same").summary == "摘要一"
    repeat = compactor.maybe_compact(first, session_id="same")
    assert repeat.summary == "摘要一" and repeat.compacted is False

    changed = _messages(middle_extra="新订单号")
    assert compactor.maybe_compact(changed, session_id="same").summary == "摘要二"
    assert compactor._compact_old_messages.call_count == 2
    assert len(compactor._summary_cache) == 1


def test_gateway_uses_unified_estimator_and_reuses_sync_loop():
    messages = [{"role": "user", "content": "你好，帮我查询订单"}]
    assert estimate_tokens(messages) == estimate_messages_tokens(messages)

    gateway = LLMGateway(http_client=object())
    loop_ids = []

    async def fake_chat(_req):
        loop_ids.append(id(asyncio.get_running_loop()))
        return "ok"

    gateway.chat = fake_chat
    try:
        assert gateway.chat_sync(GatewayRequest(messages=messages)) == "ok"
        assert gateway.chat_sync(GatewayRequest(messages=messages)) == "ok"
        assert len(set(loop_ids)) == 1
    finally:
        gateway.close_sync()


def test_gateway_sync_loop_can_close_and_reopen_without_closing_injected_client():
    class InjectedClient:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    client = InjectedClient()
    gateway = LLMGateway(http_client=client)

    async def fake_chat(_req):
        return "ok"

    gateway.chat = fake_chat
    request = GatewayRequest(messages=[{"role": "user", "content": "ping"}])
    assert gateway.chat_sync(request) == "ok"
    first_loop = gateway._sync_loop
    assert first_loop is not None

    asyncio.run(gateway.aclose())
    assert client.closed is False  # ownership remains with the caller
    assert gateway._sync_loop is None
    assert gateway._sync_loop_thread is None

    assert gateway.chat_sync(request) == "ok"
    assert gateway._sync_loop is not None
    assert gateway._sync_loop is not first_loop
    gateway.close_sync()


def test_direct_stream_does_not_retry_after_partial_output(monkeypatch):
    import requests

    class BrokenAfterTokenResponse:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def raise_for_status(self):
            return None
        def iter_lines(self, **_kwargs):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}'
            raise requests.exceptions.ConnectionError("broken")

    calls = []
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: calls.append(1) or BrokenAfterTokenResponse())
    client = LLMClient(base_url="http://unit.test/v1", api_key="unit", use_gateway=False)
    with pytest.raises(LLMStreamInterruptedError):
        list(client.chat_stream([{"role": "user", "content": "hello"}]))
    assert len(calls) == 1


def test_legacy_faithfulness_marks_judge_failure_unavailable(monkeypatch):
    monkeypatch.setattr("agent.eval_enhanced.get_llm_client", lambda: types.SimpleNamespace(chat=lambda *_a, **_kw: "not json"))
    assert faithfulness_score("q", "a", ["context"]) is None



def test_api_keys_get_distinct_opaque_subjects(monkeypatch):
    monkeypatch.setenv("API_KEYS", "key-one,key-two")
    first = type("Handler", (), {"headers": {"X-API-Key": "key-one"}, "path": "/api/chat"})()
    second = type("Handler", (), {"headers": {"X-API-Key": "key-two"}, "path": "/api/chat"})()
    from agent.auth import AuthMiddleware

    assert AuthMiddleware.check_api_key(first)
    assert AuthMiddleware.check_api_key(second)
    assert first.auth_scheme == second.auth_scheme == "api_key"
    assert first.auth_subject != second.auth_subject
    assert first.auth_subject.startswith("api-")
    assert "key-one" not in first.auth_subject


def test_anonymous_client_ids_are_isolated_from_ip(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_ALLOW_HEADER_FALLBACK", "0")
    import app_fastapi
    from starlette.requests import Request

    def make_request(client_id):
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/sessions",
            "headers": [(b"x-client-id", client_id.encode()), (b"host", b"test")],
            "client": ("10.0.0.1", 1234),
            "scheme": "http",
            "server": ("test", 80),
            "query_string": b"",
        }
        request = Request(scope)
        request.state.user_id = app_fastapi._anonymous_user_id(request)
        return request

    first = make_request("client-one-123456789")
    second = make_request("client-two-123456789")
    assert app_fastapi._user_id_for_request(first) != app_fastapi._user_id_for_request(second)
    assert app_fastapi._anonymous_user_id(first) != app_fastapi._anonymous_user_id(second)


def test_session_owner_check_fails_closed_for_non_jwt(monkeypatch):
    import app_fastapi

    request = type("Request", (), {})()
    request.state = type("State", (), {
        "auth_scheme": "anonymous",
        "auth_subject": "anon-test",
        "user_id": "anon-test",
    })()
    monkeypatch.setattr("agent.memory.get_session_owner", lambda _session_id: "other-user")
    assert not app_fastapi._owns_session(request, "session-owned-by-other")
    monkeypatch.setattr("agent.memory.get_session_owner", lambda _session_id: None)
    assert app_fastapi._owns_session(request, "new-session", allow_unregistered=True)
    assert not app_fastapi._owns_session(request, "new-session", allow_unregistered=False)
