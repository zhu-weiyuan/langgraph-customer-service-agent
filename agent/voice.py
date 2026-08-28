# -*- coding: utf-8 -*-
"""
Voice Pipeline — ASR + VAD + TTS for multimodal customer service.

End-to-end flow:
  1. Client streams audio chunks (WebSocket) -> VAD detects speech segments
  2. ASR transcribes segment -> text query
  3. Graph processes query -> text response
  4. TTS synthesizes response -> audio stream back to client
  5. VAD supports interruption: user speaks during TTS -> cancel generation
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("agent.voice")

# ============================================================
# Configuration
# ============================================================

ASR_MODEL = os.getenv("ASR_MODEL", "funasr").lower()  # funasr | whisper | sensevoice
TTS_MODEL = os.getenv("TTS_MODEL", "cosyvoice").lower()  # cosyvoice | edge | gpt-sovits
VAD_MODEL = os.getenv("VAD_MODEL", "silero").lower()  # silero | ten-vad

ASR_SAMPLE_RATE = int(os.getenv("ASR_SAMPLE_RATE", "16000"))
TTS_SAMPLE_RATE = int(os.getenv("TTS_SAMPLE_RATE", "24000"))
VAD_THRESHOLD = float(os.getenv("VAD_THRESHOLD", "0.5"))
VAD_MIN_SILENCE_MS = int(os.getenv("VAD_MIN_SILENCE_MS", "800"))
VAD_MAX_SPEECH_S = float(os.getenv("VAD_MAX_SPEECH_S", "30"))

# Hotword boosting for ASR (product names, error codes)
ASR_HOTWORDS = os.getenv("ASR_HOTWORDS", "").split(",") if os.getenv("ASR_HOTWORDS") else []

# TTS voice configuration
TTS_VOICE = os.getenv("TTS_VOICE", "customer_service_female")
TTS_SPEED = float(os.getenv("TTS_SPEED", "1.0"))

# Per-session resource limits.  Voice input is untrusted network data, so do
# not let a forgotten browser tab grow buffers or leave background TTS work.
VOICE_MAX_SESSIONS = max(1, int(os.getenv("VOICE_MAX_SESSIONS", "100")))
VOICE_MAX_AUDIO_BUFFER_BYTES = max(1024, int(os.getenv("VOICE_MAX_AUDIO_BUFFER_BYTES", str(ASR_SAMPLE_RATE * 2 * 30))))
VOICE_MAX_AUDIO_FRAME_BYTES = max(512, int(os.getenv("VOICE_MAX_AUDIO_FRAME_BYTES", str(ASR_SAMPLE_RATE * 2 * 2))))
VOICE_MAX_TTS_TEXT_CHARS = max(32, int(os.getenv("VOICE_MAX_TTS_TEXT_CHARS", "4000")))
VOICE_TTS_QUEUE_SIZE = max(1, int(os.getenv("VOICE_TTS_QUEUE_SIZE", "16")))
VOICE_MAX_HOTWORDS = max(1, int(os.getenv("VOICE_MAX_HOTWORDS", "32")))
VOICE_MAX_HOTWORD_CHARS = max(1, int(os.getenv("VOICE_MAX_HOTWORD_CHARS", "48")))
VOICE_ALLOWED_TTS_VOICES = frozenset(
    voice.strip() for voice in os.getenv("VOICE_ALLOWED_TTS_VOICES", TTS_VOICE).split(",") if voice.strip()
) or frozenset({TTS_VOICE})

# Current browser protocol plus a small, explicit compatibility layer for older
# voice clients that used start/stop-style message names. Unknown messages are
# still rejected instead of being silently ignored.
VOICE_MESSAGE_TYPES = frozenset({"audio", "flush", "interrupt", "config", "ping"})
VOICE_LEGACY_MESSAGE_ALIASES = {
    "start": "start",
    "audio_start": "start",
    "stop": "flush",
    "end": "flush",
    "audio_end": "flush",
}

# ============================================================
# Data Classes
# ============================================================

@dataclass
class VoiceSession:
    session_id: str
    user_id: str
    tenant_id: str
    created_at: float = field(default_factory=time.time)
    is_speaking: bool = False
    current_tts_task: Optional[asyncio.Task] = None
    tts_queue: Optional[asyncio.Queue] = None
    vad_state: Dict[str, Any] = field(default_factory=dict)
    audio_buffer: bytearray = field(default_factory=bytearray)
    tts_voice: str = TTS_VOICE
    asr_hotwords: List[str] = field(default_factory=lambda: list(ASR_HOTWORDS))


@dataclass
class ASRResult:
    text: str
    confidence: float
    duration_ms: float
    language: str = "zh"


@dataclass
class TTSResult:
    audio_data: bytes
    sample_rate: int
    duration_ms: float
    # Audio encoding of ``audio_data``.  The browser must not guess this.
    format: str = "pcm_s16le"


# ============================================================
# ASR Interface (Pluggable)
# ============================================================

class ASREngine:
    """Base ASR engine interface."""

    async def transcribe(self, audio_data: bytes, sample_rate: int, *, hotwords: Optional[List[str]] = None) -> ASRResult:
        raise NotImplementedError

    async def transcribe_streaming(self, audio_iterator: AsyncIterator[bytes]) -> AsyncIterator[ASRResult]:
        """Streaming transcription - yields partial results."""
        raise NotImplementedError

    async def warmup(self) -> None:
        pass


class FunASREngine(ASREngine):
    """FunASR streaming ASR with hotword support."""

    def __init__(self, model_dir: Optional[str] = None):
        self.model_dir = model_dir or os.getenv("FUNASR_MODEL_DIR", "")
        self._model = None
        self._initialized = False

    async def _ensure_model(self):
        if self._initialized:
            return
        try:
            from funasr import AutoModel
            self._model = AutoModel(
                model=self.model_dir or "paraformer-zh",
                vad_model="fsmn-vad",
                punc_model="ct-punc",
                disable_update=True,
            )
            self._initialized = True
            logger.info("FunASR model loaded")
        except ImportError:
            logger.warning("FunASR not installed, using mock")
            self._initialized = True
        except Exception as e:
            logger.error("FunASR init failed: %s", e)
            self._initialized = True

    async def transcribe(self, audio_data: bytes, sample_rate: int, *, hotwords: Optional[List[str]] = None) -> ASRResult:
        await self._ensure_model()
        t0 = time.perf_counter()
        if self._model is None:
            # Mock for testing
            return ASRResult(text="测试语音识别", confidence=0.95, duration_ms=(time.perf_counter()-t0)*1000)
        try:
            # Convert bytes to numpy array
            import numpy as np
            audio_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
            if sample_rate != ASR_SAMPLE_RATE:
                import librosa
                audio_np = librosa.resample(audio_np, orig_sr=sample_rate, target_sr=ASR_SAMPLE_RATE)
            result = self._model.generate(
                input=audio_np,
                hotword=" ".join(hotwords or ASR_HOTWORDS) if (hotwords or ASR_HOTWORDS) else None,
            )
            text = result[0]["text"] if result else ""
            return ASRResult(text=text, confidence=0.95, duration_ms=(time.perf_counter()-t0)*1000)
        except Exception as e:
            logger.error("FunASR transcribe failed: %s", e)
            return ASRResult(text="", confidence=0.0, duration_ms=(time.perf_counter()-t0)*1000)

    async def transcribe_streaming(self, audio_iterator: AsyncIterator[bytes]) -> AsyncIterator[ASRResult]:
        await self._ensure_model()
        if self._model is None:
            yield ASRResult(text="测试流式识别", confidence=0.9, duration_ms=100)
            return
        try:
            import numpy as np
            chunk_buffer = bytearray()
            async for chunk in audio_iterator:
                chunk_buffer.extend(chunk)
                if len(chunk_buffer) >= ASR_SAMPLE_RATE * 2:  # ~1 second
                    audio_np = np.frombuffer(chunk_buffer, dtype=np.int16).astype(np.float32) / 32768.0
                    result = self._model.generate(input=audio_np, is_final=False)
                    text = result[0]["text"] if result else ""
                    if text:
                        yield ASRResult(text=text, confidence=0.8, duration_ms=500)
                    chunk_buffer.clear()
            # Final chunk
            if chunk_buffer:
                audio_np = np.frombuffer(chunk_buffer, dtype=np.int16).astype(np.float32) / 32768.0
                result = self._model.generate(input=audio_np, is_final=True)
                text = result[0]["text"] if result else ""
                if text:
                    yield ASRResult(text=text, confidence=0.95, duration_ms=300)
        except Exception as e:
            logger.error("FunASR streaming failed: %s", e)


class MockASREngine(ASREngine):
    """Mock ASR for testing without GPU."""

    async def transcribe(self, audio_data: bytes, sample_rate: int, *, hotwords: Optional[List[str]] = None) -> ASRResult:
        await asyncio.sleep(0.1)  # Simulate processing
        return ASRResult(text="这是模拟的语音识别结果", confidence=0.99, duration_ms=100)

    async def transcribe_streaming(self, audio_iterator: AsyncIterator[bytes]) -> AsyncIterator[ASRResult]:
        async for _ in audio_iterator:
            await asyncio.sleep(0.05)
            yield ASRResult(text="流式识别中...", confidence=0.8, duration_ms=50)
        yield ASRResult(text="这是模拟的最终识别结果", confidence=0.99, duration_ms=100)


# ============================================================
# VAD Interface
# ============================================================

class VADEngine:
    """Voice Activity Detection interface."""

    def __init__(self, threshold: float = VAD_THRESHOLD, min_silence_ms: int = VAD_MIN_SILENCE_MS):
        self.threshold = threshold
        self.min_silence_ms = min_silence_ms
        self._model = None

    async def _ensure_model(self):
        if self._model is not None:
            return
        try:
            if VAD_MODEL == "silero":
                import torch
                self._model, _ = torch.hub.load(
                    repo_or_dir="snakers4/silero-vad",
                    model="silero_vad",
                    force_reload=False,
                    trust_repo=True,
                )
                logger.info("Silero VAD loaded")
            elif VAD_MODEL == "ten-vad":
                from ten_vad import TenVAD
                self._model = TenVAD()
                logger.info("TenVAD loaded")
        except Exception as e:
            logger.warning("VAD model load failed, using energy-based fallback: %s", e)
            self._model = "fallback"

    def detect(self, audio_chunk: bytes, sample_rate: int) -> Dict[str, Any]:
        """
        Returns: {"is_speech": bool, "probability": float, "energy": float}
        """
        # Empty or incomplete PCM16 frames have no meaningful energy.
        if not audio_chunk or len(audio_chunk) < 2:
            return {"is_speech": False, "probability": 0.0, "energy": 0.0}
        if len(audio_chunk) % 2:
            raise ValueError("audio frame must be PCM16 little-endian")

        # Energy-based fallback (always works)
        import numpy as np
        audio_np = np.frombuffer(audio_chunk, dtype=np.int16).astype(np.float32)
        energy = float(np.mean(audio_np ** 2))
        normalized_energy = min(1.0, energy / 1000.0)  # Rough normalization
        is_speech = normalized_energy > self.threshold

        if self._model == "fallback" or self._model is None:
            return {"is_speech": is_speech, "probability": normalized_energy, "energy": energy}

        try:
            import torch
            audio_tensor = torch.from_numpy(audio_np / 32768.0).unsqueeze(0)
            if sample_rate != 16000:
                import torchaudio
                audio_tensor = torchaudio.functional.resample(audio_tensor, sample_rate, 16000)
            with torch.no_grad():
                prob = self._model(audio_tensor, 16000).item()
            return {"is_speech": prob > self.threshold, "probability": prob, "energy": energy}
        except Exception:
            return {"is_speech": is_speech, "probability": normalized_energy, "energy": energy}


# ============================================================
# TTS Interface
# ============================================================

class TTSEngine:
    """Base TTS engine interface.

    ``audio_format`` is part of the WebSocket protocol: each audio frame
    declares its encoding so browser clients can choose a safe decoder.
    """

    audio_format = "pcm_s16le"

    async def synthesize(self, text: str, voice: str = TTS_VOICE, speed: float = TTS_SPEED) -> TTSResult:
        raise NotImplementedError

    async def synthesize_streaming(self, text: str, voice: str = TTS_VOICE, speed: float = TTS_SPEED) -> AsyncIterator[bytes]:
        """Streaming synthesis - yields audio chunks."""
        raise NotImplementedError

    async def warmup(self) -> None:
        pass


class CosyVoiceEngine(TTSEngine):
    """CosyVoice streaming TTS."""

    audio_format = "pcm_s16le"

    def __init__(self):
        self._model = None

    async def _ensure_model(self):
        if self._model is not None:
            return
        try:
            from cosyvoice.cli.cosyvoice import CosyVoice
            self._model = CosyVoice(os.getenv("COSYVOICE_MODEL_DIR", "pretrained_models/CosyVoice-300M"))
            logger.info("CosyVoice loaded")
        except ImportError:
            logger.warning("CosyVoice not installed, using mock")
            self._model = "mock"
        except Exception as e:
            logger.error("CosyVoice init failed: %s", e)
            self._model = "mock"

    async def synthesize(self, text: str, voice: str = TTS_VOICE, speed: float = TTS_SPEED) -> TTSResult:
        await self._ensure_model()
        t0 = time.perf_counter()
        if self._model == "mock":
            await asyncio.sleep(0.2)
            # Generate mock audio (silence)
            import numpy as np
            duration = len(text) * 0.1  # Rough estimate
            samples = int(TTS_SAMPLE_RATE * duration)
            audio = np.zeros(samples, dtype=np.int16).tobytes()
            return TTSResult(audio_data=audio, sample_rate=TTS_SAMPLE_RATE, duration_ms=(time.perf_counter()-t0)*1000, format=self.audio_format)
        try:
            import numpy as np
            audio_chunks = []
            for chunk in self._model.inference_zero_shot(text, voice, speed=speed):
                audio_chunks.append(chunk)
            audio = np.concatenate(audio_chunks)
            return TTSResult(
                audio_data=audio.astype(np.int16).tobytes(),
                sample_rate=TTS_SAMPLE_RATE,
                duration_ms=(time.perf_counter()-t0)*1000,
                format=self.audio_format,
            )
        except Exception as e:
            logger.error("CosyVoice synthesize failed: %s", e)
            return TTSResult(audio_data=b"", sample_rate=TTS_SAMPLE_RATE, duration_ms=0, format=self.audio_format)

    async def synthesize_streaming(self, text: str, voice: str = TTS_VOICE, speed: float = TTS_SPEED) -> AsyncIterator[bytes]:
        await self._ensure_model()
        if self._model == "mock":
            # Mock streaming
            for i in range(0, len(text), 10):
                await asyncio.sleep(0.05)
                yield b"\x00" * 960  # 20ms @ 24kHz
            return
        try:
            import numpy as np
            for chunk in self._model.inference_zero_shot(text, voice, speed=speed, stream=True):
                yield chunk.astype(np.int16).tobytes()
        except Exception as e:
            logger.error("CosyVoice streaming failed: %s", e)


class EdgeTTSEngine(TTSEngine):
    """Microsoft Edge TTS (cloud, no GPU needed)."""

    # edge-tts streams compressed MPEG audio rather than raw PCM samples.
    audio_format = "mp3"

    async def synthesize(self, text: str, voice: str = TTS_VOICE, speed: float = TTS_SPEED) -> TTSResult:
        t0 = time.perf_counter()
        try:
            import edge_tts
            communicate = edge_tts.Communicate(text, voice, rate=f"{int((speed-1)*100)}%")
            audio_data = b""
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_data += chunk["data"]
            return TTSResult(audio_data=audio_data, sample_rate=24000, duration_ms=(time.perf_counter()-t0)*1000, format=self.audio_format)
        except Exception as e:
            logger.error("EdgeTTS failed: %s", e)
            return TTSResult(audio_data=b"", sample_rate=24000, duration_ms=0, format=self.audio_format)

    async def synthesize_streaming(self, text: str, voice: str = TTS_VOICE, speed: float = TTS_SPEED) -> AsyncIterator[bytes]:
        try:
            import edge_tts
            communicate = edge_tts.Communicate(text, voice, rate=f"{int((speed-1)*100)}%")
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    yield chunk["data"]
        except Exception as e:
            logger.error("EdgeTTS streaming failed: %s", e)


class MockTTSEngine(TTSEngine):
    """Mock TTS for testing."""

    audio_format = "pcm_s16le"

    async def synthesize(self, text: str, voice: str = TTS_VOICE, speed: float = TTS_SPEED) -> TTSResult:
        await asyncio.sleep(0.15)
        import numpy as np
        duration = max(0.5, len(text) * 0.08)
        samples = int(TTS_SAMPLE_RATE * duration)
        audio = np.zeros(samples, dtype=np.int16).tobytes()
        return TTSResult(audio_data=audio, sample_rate=TTS_SAMPLE_RATE, duration_ms=150, format=self.audio_format)

    async def synthesize_streaming(self, text: str, voice: str = TTS_VOICE, speed: float = TTS_SPEED) -> AsyncIterator[bytes]:
        for i in range(0, len(text), 15):
            await asyncio.sleep(0.04)
            yield b"\x00" * 960  # 20ms @ 24kHz


# ============================================================
# Factory Functions
# ============================================================

def get_asr_engine() -> ASREngine:
    if ASR_MODEL == "funasr":
        return FunASREngine()
    elif ASR_MODEL == "mock":
        return MockASREngine()
    return MockASREngine()


def get_vad_engine() -> VADEngine:
    return VADEngine()


def get_tts_engine() -> TTSEngine:
    if TTS_MODEL == "cosyvoice":
        return CosyVoiceEngine()
    elif TTS_MODEL == "edge":
        return EdgeTTSEngine()
    elif TTS_MODEL == "mock":
        return MockTTSEngine()
    return MockTTSEngine()


# ============================================================
# Voice Session Manager
# ============================================================

class VoiceSessionManager:
    """Own voice sessions and one cancellable producer task per session."""

    def __init__(self):
        self._sessions: Dict[str, VoiceSession] = {}
        self._lock = asyncio.Lock()
        self.asr = get_asr_engine()
        self.vad = get_vad_engine()
        self.tts = get_tts_engine()

    async def create_session(self, session_id: str, user_id: str, tenant_id: str) -> VoiceSession:
        if not session_id or not user_id or not tenant_id:
            raise PermissionError("voice session requires an authenticated identity")
        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing:
                if existing.user_id != user_id or existing.tenant_id != tenant_id:
                    raise PermissionError("voice session belongs to another user")
                return existing
            if len(self._sessions) >= VOICE_MAX_SESSIONS:
                raise RuntimeError("voice session capacity reached")
            session = VoiceSession(session_id=session_id, user_id=user_id, tenant_id=tenant_id)
            self._sessions[session_id] = session
            return session

    async def get_session(self, session_id: str) -> Optional[VoiceSession]:
        async with self._lock:
            return self._sessions.get(session_id)

    async def _cancel_tts(self, session: VoiceSession) -> None:
        task = session.current_tts_task
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        session.current_tts_task = None
        session.tts_queue = None
        session.is_speaking = False

    async def remove_session(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session:
            await self._cancel_tts(session)
            session.audio_buffer.clear()
            session.vad_state.clear()

    async def process_audio_chunk(self, session_id: str, audio_data: bytes) -> Optional[str]:
        """Accumulate a bounded PCM16 segment and return final ASR text on silence."""
        session = await self.get_session(session_id)
        if not session:
            raise PermissionError("voice session not found")
        if not audio_data:
            return None
        if len(audio_data) % 2:
            raise ValueError("audio frame must be PCM16 little-endian")
        if len(audio_data) > VOICE_MAX_AUDIO_FRAME_BYTES:
            raise ValueError("audio frame exceeds limit")
        if len(session.audio_buffer) + len(audio_data) > VOICE_MAX_AUDIO_BUFFER_BYTES:
            session.audio_buffer.clear()
            session.vad_state.clear()
            raise ValueError("audio segment exceeds limit")

        vad_result = self.vad.detect(audio_data, ASR_SAMPLE_RATE)
        session.audio_buffer.extend(audio_data)
        if vad_result["is_speech"]:
            session.vad_state["last_speech_time"] = time.monotonic()
            session.vad_state["speech_started"] = True
            return None
        if not session.vad_state.get("speech_started"):
            # Avoid retaining silence forever.
            session.audio_buffer.clear()
            return None
        silence_duration = (time.monotonic() - session.vad_state.get("last_speech_time", time.monotonic())) * 1000
        if silence_duration < self.vad.min_silence_ms:
            return None
        audio_bytes = bytes(session.audio_buffer)
        session.audio_buffer.clear()
        session.vad_state.clear()
        asr_result = await self.asr.transcribe(audio_bytes, ASR_SAMPLE_RATE, hotwords=session.asr_hotwords)
        return asr_result.text.strip() or None

    async def flush_audio(self, session_id: str) -> Optional[str]:
        """Finalize the current utterance when the browser stops recording.

        VAD normally waits for a silence interval.  A click-to-stop UI has no
        reason to keep the microphone open just to manufacture that silence,
        so this explicit boundary transcribes the already accepted PCM16
        segment.  Empty/silence-only buffers are discarded safely.
        """
        session = await self.get_session(session_id)
        if not session:
            raise PermissionError("voice session not found")
        if not session.audio_buffer or not session.vad_state.get("speech_started"):
            session.audio_buffer.clear()
            session.vad_state.clear()
            return None
        audio_bytes = bytes(session.audio_buffer)
        session.audio_buffer.clear()
        session.vad_state.clear()
        asr_result = await self.asr.transcribe(
            audio_bytes, ASR_SAMPLE_RATE, hotwords=session.asr_hotwords,
        )
        return asr_result.text.strip() or None

    async def interrupt(self, session_id: str) -> None:
        session = await self.get_session(session_id)
        if session:
            await self._cancel_tts(session)
            logger.info("voice session interrupted: session=%s", session_id)

    async def speak(self, session_id: str, text: str) -> AsyncIterator[bytes]:
        """Run exactly one TTS producer and stream its bounded queue to caller."""
        session = await self.get_session(session_id)
        if not session:
            return
        text = str(text or "").strip()
        if not text:
            return
        if len(text) > VOICE_MAX_TTS_TEXT_CHARS:
            text = text[:VOICE_MAX_TTS_TEXT_CHARS]
        await self._cancel_tts(session)
        queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=VOICE_TTS_QUEUE_SIZE)
        session.tts_queue = queue
        session.is_speaking = True

        async def producer() -> None:
            try:
                async for chunk in self.tts.synthesize_streaming(text, voice=session.tts_voice):
                    if chunk:
                        await queue.put(bytes(chunk))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("TTS producer failed: session=%s", session_id)
            finally:
                # The consumer is active while the producer normally finishes,
                # so waiting for the sentinel is safe and cannot silently drop
                # completion when the bounded queue is temporarily full. If the
                # producer is cancelled (interrupt/disconnect), cancellation
                # interrupts this wait and the consumer's finally cleans up.
                with suppress(asyncio.CancelledError):
                    await queue.put(None)

        task = asyncio.create_task(producer(), name=f"voice-tts-{session_id}")
        session.current_tts_task = task
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            if session.current_tts_task is task:
                await self._cancel_tts(session)


_voice_manager: Optional[VoiceSessionManager] = None


def get_voice_manager() -> VoiceSessionManager:
    global _voice_manager
    if _voice_manager is None:
        _voice_manager = VoiceSessionManager()
    return _voice_manager


def _normalize_voice_message_type(msg: Dict[str, Any]) -> tuple[str, str]:
    """Normalize the bounded voice WebSocket protocol without inspecting payload data."""
    raw_msg_type = str(msg.get("type") or "").strip().lower()
    if raw_msg_type in VOICE_MESSAGE_TYPES:
        return raw_msg_type, raw_msg_type
    if raw_msg_type in VOICE_LEGACY_MESSAGE_ALIASES:
        return raw_msg_type, VOICE_LEGACY_MESSAGE_ALIASES[raw_msg_type]
    raise ValueError(f"unsupported voice message type: {raw_msg_type or '<missing>'}")


def _clean_voice_config(session: VoiceSession, msg: Dict[str, Any]) -> None:
    voice = msg.get("tts_voice")
    if voice is not None:
        voice = str(voice).strip()
        if voice not in VOICE_ALLOWED_TTS_VOICES:
            raise ValueError("unsupported tts voice")
        session.tts_voice = voice
    if "asr_hotwords" in msg:
        raw = msg["asr_hotwords"]
        if not isinstance(raw, list):
            raise ValueError("asr_hotwords must be an array")
        clean = []
        for word in raw:
            word = str(word).strip()
            if word and len(word) <= VOICE_MAX_HOTWORD_CHARS and word not in clean:
                clean.append(word)
        if len(clean) > VOICE_MAX_HOTWORDS:
            raise ValueError("too many hotwords")
        session.asr_hotwords = clean


async def handle_voice_websocket(
    websocket: WebSocket, session_id: str, user_id: str, tenant_id: str, graph_runner: Callable,
) -> None:
    """Authenticated WebSocket voice protocol with bounded untrusted input."""
    await websocket.accept()
    manager = get_voice_manager()
    try:
        session = await manager.create_session(session_id, user_id, tenant_id)
    except (PermissionError, RuntimeError) as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
        await websocket.close(code=1008)
        return

    try:
        await asyncio.gather(manager.asr.warmup(), manager.tts.warmup())
        while True:
            msg = await websocket.receive_json()
            if not isinstance(msg, dict):
                raise ValueError("message must be an object")
            raw_msg_type = str(msg.get("type") or "").strip().lower()
            try:
                _, msg_type = _normalize_voice_message_type(msg)
            except ValueError:
                logger.warning(
                    "unsupported voice websocket message: session=%s type=%r keys=%s",
                    session_id,
                    raw_msg_type or "<missing>",
                    sorted(str(key) for key in msg.keys()),
                )
                raise
            if raw_msg_type != msg_type:
                logger.info(
                    "voice websocket legacy message alias: session=%s raw_type=%r normalized_type=%r",
                    session_id,
                    raw_msg_type,
                    msg_type,
                )

            if msg_type == "start":
                await websocket.send_json({"type": "started"})
                continue
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if msg_type == "audio":
                audio_b64 = msg.get("data", "")
                if not isinstance(audio_b64, str) or len(audio_b64) > VOICE_MAX_AUDIO_FRAME_BYTES * 2:
                    raise ValueError("audio payload exceeds limit")
                try:
                    audio_bytes = base64.b64decode(audio_b64, validate=True)
                except Exception as exc:
                    raise ValueError("audio payload is not valid base64") from exc
                transcript = await manager.process_audio_chunk(session_id, audio_bytes)
            elif msg_type == "flush":
                # A browser click-to-stop is an explicit end-of-utterance
                # boundary; do not require it to keep recording VAD silence.
                transcript = await manager.flush_audio(session_id)
            else:
                transcript = None

            if msg_type in {"audio", "flush"}:
                if not transcript:
                    continue
                await websocket.send_json({"type": "transcript", "text": transcript, "is_final": True})
                await websocket.send_json({"type": "progress", "stage": "graph"})
                result = await graph_runner(session_id, transcript, user_id=user_id, tenant_id=tenant_id)
                reply = str((result or {}).get("reply") or "")
                await websocket.send_json({"type": "progress", "stage": "tts"})
                async for chunk in manager.speak(session_id, reply):
                    if await is_disconnected(websocket):
                        break
                    await websocket.send_json({
                        "type": "audio",
                        "data": base64.b64encode(chunk).decode("ascii"),
                        "sample_rate": TTS_SAMPLE_RATE,
                        "format": getattr(manager.tts, "audio_format", "pcm_s16le"),
                    })
                await websocket.send_json({"type": "done", "reply": reply})
            elif msg_type == "interrupt":
                await manager.interrupt(session_id)
                await websocket.send_json({"type": "interrupted"})
            elif msg_type == "config":
                _clean_voice_config(session, msg)
                await websocket.send_json({"type": "config_updated"})
    except WebSocketDisconnect:
        logger.info("voice websocket disconnected: session=%s", session_id)
    except Exception as exc:
        logger.warning("voice websocket failed: session=%s error=%s", session_id, exc, exc_info=True)
        with suppress(Exception):
            await websocket.send_json({"type": "error", "message": str(exc)})
    finally:
        await manager.remove_session(session_id)


async def is_disconnected(websocket: WebSocket) -> bool:
    return websocket.client_state.name == "DISCONNECTED"
