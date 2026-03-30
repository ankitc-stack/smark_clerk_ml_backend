from __future__ import annotations

import io
import math
import os
import tempfile
import time
import wave
import audioop
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from functools import lru_cache
from typing import Any

from app.config import settings


class STTError(ValueError):
    """Base typed error for voice transcription failures."""


class STTValidationError(STTError):
    """Raised when audio payload fails guardrail validation."""


class STTTranscriptionError(STTError):
    """Raised when STT backend fails to produce usable text."""


class STTLowConfidenceError(STTError):
    """Raised when transcript confidence proxy is below threshold."""


_ALLOWED_MIME_TO_SUFFIX = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".mp4",
    "audio/x-m4a": ".m4a",
}


def _normalize_mime(mime_type: str) -> str:
    # Strip codec parameters so "audio/webm;codecs=opus" → "audio/webm"
    return (mime_type or "").strip().lower().split(";")[0].strip()


def _validate_audio_guardrails(audio_bytes: bytes, mime_type: str) -> str:
    normalized_mime = _normalize_mime(mime_type)
    if normalized_mime not in _ALLOWED_MIME_TO_SUFFIX:
        raise STTValidationError(f"Unsupported audio mime type: {mime_type}")

    if not audio_bytes:
        raise STTValidationError("Audio payload is empty")

    if len(audio_bytes) > int(settings.STT_MAX_AUDIO_BYTES):
        raise STTValidationError("Audio payload exceeds size limit")

    if len(audio_bytes) < int(settings.STT_MIN_AUDIO_BYTES):
        raise STTValidationError("Audio payload too short")

    # For WAV we can cheaply check duration and reject obvious accidental clicks/noise.
    if normalized_mime in {"audio/wav", "audio/x-wav", "audio/wave"}:
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
                frames = wav_file.getnframes()
                rate = wav_file.getframerate()
                duration_ms = int((frames / float(rate)) * 1000) if rate else 0
        except Exception as ex:
            raise STTValidationError(f"Invalid WAV payload: {ex}") from ex

        if duration_ms < int(settings.STT_MIN_DURATION_MS):
            raise STTValidationError("Audio duration too short")

    return normalized_mime


def _normalize_wav_bytes_for_stt(audio_bytes: bytes) -> bytes:
    """
    Normalize WAV input to 16 kHz mono 16-bit PCM.

    Why:
    - Keeps STT behavior stable across client recorder settings.
    - Reduces low-confidence transcriptions caused by mismatched audio formats.
    """
    with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        compression = wav_file.getcomptype()
        raw = wav_file.readframes(wav_file.getnframes())

    if compression != "NONE":
        raise STTValidationError("Compressed WAV is not supported; use PCM WAV")

    # Convert to signed 16-bit first so downstream transforms are consistent.
    if sample_width != 2:
        raw = audioop.lin2lin(raw, sample_width, 2)
        sample_width = 2

    if channels > 1:
        raw = audioop.tomono(raw, sample_width, 0.5, 0.5)
        channels = 1

    if sample_rate != 16000:
        raw, _ = audioop.ratecv(raw, sample_width, channels, sample_rate, 16000, None)
        sample_rate = 16000

    with io.BytesIO() as out:
        with wave.open(out, "wb") as wav_out:
            wav_out.setnchannels(channels)
            wav_out.setsampwidth(sample_width)
            wav_out.setframerate(sample_rate)
            wav_out.writeframes(raw)
        return out.getvalue()


@lru_cache(maxsize=1)
def _load_faster_whisper_model():
    try:
        from faster_whisper import WhisperModel
    except Exception as ex:  # pragma: no cover - dependency may be absent in sandbox
        raise STTTranscriptionError(
            "faster-whisper is not installed; install dependency to enable voice transcription"
        ) from ex

    return WhisperModel(
        settings.STT_MODEL_NAME,
        device=settings.STT_DEVICE,
        compute_type=settings.STT_COMPUTE_TYPE,
    )


def _get_faster_whisper_model_with_timeout():
    timeout_s = max(0.1, float(settings.STT_HEALTH_TIMEOUT_S))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_load_faster_whisper_model)
        try:
            return future.result(timeout=timeout_s)
        except FuturesTimeoutError as ex:
            raise STTTranscriptionError("STT model initialization timed out") from ex


def _confidence_from_segments(segments: list[Any]) -> float:
    avg_logprobs: list[float] = []
    no_speech_probs: list[float] = []
    for segment in segments:
        avg_logprob = getattr(segment, "avg_logprob", None)
        no_speech_prob = getattr(segment, "no_speech_prob", None)
        if isinstance(avg_logprob, (int, float)):
            avg_logprobs.append(float(avg_logprob))
        if isinstance(no_speech_prob, (int, float)):
            no_speech_probs.append(float(no_speech_prob))

    if avg_logprobs:
        # Compress typical avg_logprob range into [0,1] proxy.
        mean_lp = sum(avg_logprobs) / len(avg_logprobs)
        return 1.0 / (1.0 + math.exp(-4.0 * (mean_lp + 1.0)))
    if no_speech_probs:
        return max(0.0, min(1.0, 1.0 - (sum(no_speech_probs) / len(no_speech_probs))))
    return 0.5


def transcribe(audio_bytes: bytes, mime_type: str) -> tuple[str, dict[str, Any]]:
    """
    Transcribe audio payload and return transcript text with STT metadata.

    Contract:
    - Returns non-empty plain text or raises STTError subclass.
    - Includes deterministic metadata needed by command endpoint observability.
    """
    normalized_mime = _validate_audio_guardrails(audio_bytes, mime_type)
    started = time.perf_counter()

    if normalized_mime in {"audio/wav", "audio/x-wav", "audio/wave"}:
        audio_bytes = _normalize_wav_bytes_for_stt(audio_bytes)

    if settings.STT_PROVIDER.lower() != "faster_whisper":
        raise STTTranscriptionError("Voice transcription provider is not configured")

    model = _get_faster_whisper_model_with_timeout()

    suffix = _ALLOWED_MIME_TO_SUFFIX[normalized_mime]
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_file.write(audio_bytes)
            temp_file.flush()
            tmp_path = temp_file.name

        kwargs: dict[str, Any] = {
            "beam_size": 5,
            "vad_filter": True,
        }
        if settings.STT_FORCE_LANGUAGE:
            kwargs["language"] = settings.STT_FORCE_LANGUAGE

        segments_iter, info = model.transcribe(tmp_path, **kwargs)
        segments = list(segments_iter)
        transcript = " ".join((getattr(seg, "text", "") or "").strip() for seg in segments).strip()
        if not transcript:
            raise STTTranscriptionError("No speech content detected in audio")

        confidence = _confidence_from_segments(segments)
        if confidence < float(settings.STT_MIN_CONFIDENCE):
            raise STTLowConfidenceError("Audio confidence is too low; please repeat")

        latency_ms = int((time.perf_counter() - started) * 1000)
        meta = {
            "stt_model": settings.STT_MODEL_NAME,
            "stt_device": settings.STT_DEVICE,
            "stt_compute_type": settings.STT_COMPUTE_TYPE,
            "stt_language_detected": getattr(info, "language", None),
            "stt_confidence": confidence,
            "stt_latency_ms": latency_ms,
        }
        return transcript, meta
    except STTError:
        raise
    except Exception as ex:
        raise STTTranscriptionError(f"STT transcription failed: {ex}") from ex
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
