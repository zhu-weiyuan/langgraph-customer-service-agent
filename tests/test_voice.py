from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from starlette.testclient import TestClient

from agent.voice import (
    ASRResult,
    EdgeTTSEngine,
    MockTTSEngine,
    TTSResult,
    VOICE_MAX_AUDIO_BUFFER_BYTES,
    VOICE_MAX_AUDIO_FRAME_BYTES,
    VOICE_MAX_HOTWORDS,
    VOICE_LEGACY_MESSAGE_ALIASES,
    VOICE_MESSAGE_TYPES,
    VoiceSession,
    VoiceSessionManager,
    _clean_voice_config,
    _normalize_voice_message_type,
)


class _NoopVad:
    min_silence_ms = 0

    def detect(self, _audio: bytes, _sample_rate: int):
        return {"is_speech": False}


class _NoopASR:
    async def transcribe(self, _audio: bytes, _sample_rate: int, *, hotwords=None):
        return ASRResult(text="已识别", confidence=1.0, duration_ms=1)


class _SlowTask:
    async def wait_forever(self):
        await asyncio.sleep(30)


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


def _run(coro):
    return asyncio.run(coro)


def test_voice_message_protocol_accepts_current_and_legacy_types():
    assert {"audio", "flush", "interrupt", "config", "ping"}.issubset(VOICE_MESSAGE_TYPES)
    assert VOICE_LEGACY_MESSAGE_ALIASES["start"] == "start"
    assert VOICE_LEGACY_MESSAGE_ALIASES["audio_start"] == "start"
    assert VOICE_LEGACY_MESSAGE_ALIASES["stop"] == "flush"
    assert VOICE_LEGACY_MESSAGE_ALIASES["end"] == "flush"
    assert VOICE_LEGACY_MESSAGE_ALIASES["audio_end"] == "flush"


def test_voice_message_protocol_normalizes_current_and_legacy_types():
    assert _normalize_voice_message_type({"type": " AUDIO "}) == ("audio", "audio")
    assert _normalize_voice_message_type({"type": "audio_start"}) == ("audio_start", "start")
    assert _normalize_voice_message_type({"type": "end"}) == ("end", "flush")


def test_voice_message_protocol_rejects_unknown_or_missing_types():
    assert "unknown" not in VOICE_MESSAGE_TYPES
    assert "unknown" not in VOICE_LEGACY_MESSAGE_ALIASES
    with pytest.raises(ValueError, match="unsupported voice message type: unknown"):
        _normalize_voice_message_type({"type": "unknown"})
    with pytest.raises(ValueError, match="<missing>"):
        _normalize_voice_message_type({})


def test_voice_session_requires_authenticated_identity():
    manager = VoiceSessionManager()
    with pytest.raises(PermissionError):
        _run(manager.create_session("", "user", "tenant"))
    with pytest.raises(PermissionError):
        _run(manager.create_session("s", "", "tenant"))


def test_audio_frame_and_buffer_limits_clear_state():
    async def scenario():
        manager = VoiceSessionManager()
        manager.vad = _NoopVad()
        manager.asr = _NoopASR()
        session = await manager.create_session("voice-limit", "user-a", "tenant-a")
        with pytest.raises(ValueError, match="PCM16"):
            await manager.process_audio_chunk(session.session_id, b"x")
        with pytest.raises(ValueError, match="frame exceeds"):
            await manager.process_audio_chunk(session.session_id, b"\0" * (VOICE_MAX_AUDIO_FRAME_BYTES + 2))
        session.audio_buffer.extend(b"\0" * VOICE_MAX_AUDIO_BUFFER_BYTES)
        session.vad_state["speech_started"] = True
        with pytest.raises(ValueError, match="segment exceeds"):
            await manager.process_audio_chunk(session.session_id, b"\0\0")
        assert session.audio_buffer == bytearray()
        assert session.vad_state == {}
        await manager.remove_session(session.session_id)
    _run(scenario())


def test_flush_audio_transcribes_explicit_browser_stop_boundary():
    class _SpeechVad:
        min_silence_ms = 800

        def detect(self, _audio: bytes, _sample_rate: int):
            return {"is_speech": True}

    async def scenario():
        manager = VoiceSessionManager()
        manager.vad = _SpeechVad()
        manager.asr = _NoopASR()
        session = await manager.create_session("voice-flush", "user-a", "tenant-a")
        assert await manager.process_audio_chunk(session.session_id, b"\x01\x00" * 64) is None
        assert await manager.flush_audio(session.session_id) == "已识别"
        assert session.audio_buffer == bytearray()
        assert session.vad_state == {}
        await manager.remove_session(session.session_id)

    _run(scenario())


def test_config_enforces_tts_voice_and_hotword_bounds():
    session = VoiceSession(session_id="voice-config", user_id="user-a", tenant_id="tenant-a")
    with pytest.raises(ValueError, match="unsupported tts voice"):
        _clean_voice_config(session, {"tts_voice": "not-allowed"})
    with pytest.raises(ValueError, match="array"):
        _clean_voice_config(session, {"asr_hotwords": "AGV"})
    with pytest.raises(ValueError, match="too many"):
        _clean_voice_config(session, {"asr_hotwords": [f"word-{i}" for i in range(VOICE_MAX_HOTWORDS + 1)]})
    _clean_voice_config(session, {"asr_hotwords": ["AGV", "AGV", "E102"]})
    assert session.asr_hotwords == ["AGV", "E102"]


def test_interrupt_cancels_tts_task_and_session_cleanup():
    async def scenario():
        manager = VoiceSessionManager()
        session = await manager.create_session("voice-interrupt", "user-a", "tenant-a")
        task = asyncio.create_task(_SlowTask().wait_forever())
        session.current_tts_task = task
        session.tts_queue = asyncio.Queue()
        session.is_speaking = True
        session.audio_buffer.extend(b"\0\0")
        session.vad_state["speech_started"] = True
        await manager.interrupt(session.session_id)
        assert task.cancelled()
        assert session.current_tts_task is None
        assert session.tts_queue is None
        assert session.is_speaking is False
        await manager.remove_session(session.session_id)
        assert await manager.get_session(session.session_id) is None
    _run(scenario())



def test_tts_payload_format_is_explicit_for_browser_clients():
    assert TTSResult(audio_data=b"", sample_rate=24000, duration_ms=0).format == "pcm_s16le"
    assert MockTTSEngine.audio_format == "pcm_s16le"
    assert EdgeTTSEngine.audio_format == "mp3"

def test_voice_websocket_rejects_missing_or_invalid_token(monkeypatch):
    import app_fastapi

    app_fastapi.app.router.lifespan_context = _noop_lifespan
    monkeypatch.setattr(app_fastapi.AuthMiddleware, "_decode_jwt", staticmethod(lambda _token: None))
    with TestClient(app_fastapi.app) as client:
        with pytest.raises(Exception) as exc:
            with client.websocket_connect("/api/voice/ws/session-a"):
                pass
    assert exc.value.code == 1008


def test_voice_websocket_only_enters_handler_for_session_owner(monkeypatch):
    import app_fastapi
    from agent import memory

    observed = {}

    async def fake_handler(websocket, session_id, user_id, tenant_id, graph_runner):
        observed.update(session_id=session_id, user_id=user_id, tenant_id=tenant_id)
        await websocket.accept()
        await websocket.send_json({"type": "authenticated"})
        await websocket.close()

    app_fastapi.app.router.lifespan_context = _noop_lifespan
    monkeypatch.setattr(app_fastapi.AuthMiddleware, "_decode_jwt", staticmethod(lambda _token: {"sub": "user-a", "tenant_id": "tenant-a"}))
    monkeypatch.setattr(memory, "get_session_owner", lambda _session_id: "user-a")
    monkeypatch.setattr(app_fastapi, "handle_voice_websocket", fake_handler)
    with TestClient(app_fastapi.app) as client:
        with client.websocket_connect("/api/voice/ws/session-a?access_token=test") as websocket:
            assert websocket.receive_json() == {"type": "authenticated"}
    assert observed == {"session_id": "session-a", "user_id": "user-a", "tenant_id": "tenant-a"}


def test_voice_websocket_rejects_non_owner(monkeypatch):
    import app_fastapi
    from agent import memory

    app_fastapi.app.router.lifespan_context = _noop_lifespan
    monkeypatch.setattr(app_fastapi.AuthMiddleware, "_decode_jwt", staticmethod(lambda _token: {"sub": "user-a", "tenant_id": "tenant-a"}))
    monkeypatch.setattr(memory, "get_session_owner", lambda _session_id: "user-b")
    with TestClient(app_fastapi.app) as client:
        with pytest.raises(Exception) as exc:
            with client.websocket_connect("/api/voice/ws/session-a?access_token=test"):
                pass
    assert exc.value.code == 1008
