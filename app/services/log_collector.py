from __future__ import annotations
import uuid
import logging
from typing import Any

_log = logging.getLogger("uvicorn.error")


def _bg(fn):
    """Run fn in the default thread executor — non-blocking, fire-and-forget."""
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.run_in_executor(None, fn)
        else:
            fn()
    except Exception:
        pass


def log_request(
    *,
    endpoint: str,
    user_id=None,
    doc_id: str | None = None,
    doc_type: str | None = None,
    prompt: str | None = None,
    status: str | None = None,
    status_code: int | None = None,
    latency_ms: int | None = None,
    extra: dict | None = None,
) -> None:
    def _w():
        from app.db import SessionLocal
        from app.models import RequestLog
        db = SessionLocal()
        try:
            db.add(RequestLog(
                id=uuid.uuid4().hex,
                endpoint=endpoint,
                user_id=str(user_id) if user_id else None,
                doc_id=doc_id,
                doc_type=doc_type,
                prompt=(prompt or "")[:2000],
                status=status,
                status_code=status_code,
                latency_ms=latency_ms,
                extra=extra,
            ))
            db.commit()
        except Exception as e:
            _log.warning("log_collector: request_log write failed: %s", e)
        finally:
            db.close()
    _bg(_w)


def log_stt(
    *,
    request_id: str | None = None,
    user_id=None,
    doc_id: str | None = None,
    source: str,
    transcript: str | None = None,
    mime_type: str | None = None,
    stt_model: str | None = None,
    confidence: float | None = None,
    latency_ms: int | None = None,
    succeeded: bool = True,
) -> None:
    def _w():
        from app.db import SessionLocal
        from app.models import SttLog
        db = SessionLocal()
        try:
            db.add(SttLog(
                id=uuid.uuid4().hex,
                request_id=request_id,
                user_id=str(user_id) if user_id else None,
                doc_id=doc_id,
                source=source,
                transcript=transcript,
                mime_type=mime_type,
                stt_model=stt_model,
                confidence=confidence,
                latency_ms=latency_ms,
                succeeded=succeeded,
            ))
            db.commit()
        except Exception as e:
            _log.warning("log_collector: stt_log write failed: %s", e)
        finally:
            db.close()
    _bg(_w)


def log_wake_word(
    *,
    event: str,
    method: str | None = None,
    score: float | None = None,
    transcript: str | None = None,
) -> None:
    def _w():
        from app.db import SessionLocal
        from app.models import WakeWordLog
        db = SessionLocal()
        try:
            db.add(WakeWordLog(
                id=uuid.uuid4().hex,
                event=event,
                method=method,
                score=score,
                transcript=transcript,
            ))
            db.commit()
        except Exception as e:
            _log.warning("log_collector: wake_word_log write failed: %s", e)
        finally:
            db.close()
    _bg(_w)


def log_error(
    *,
    endpoint: str | None = None,
    error_type: str | None = None,
    error_code: str | None = None,
    message: str | None = None,
    user_id=None,
    doc_id: str | None = None,
    request_id: str | None = None,
) -> None:
    def _w():
        from app.db import SessionLocal
        from app.models import ErrorLog
        db = SessionLocal()
        try:
            db.add(ErrorLog(
                id=uuid.uuid4().hex,
                endpoint=endpoint,
                error_type=error_type,
                error_code=error_code,
                message=(message or "")[:2000],
                user_id=str(user_id) if user_id else None,
                doc_id=doc_id,
                request_id=request_id,
            ))
            db.commit()
        except Exception as e:
            _log.warning("log_collector: error_log write failed: %s", e)
        finally:
            db.close()
    _bg(_w)
