"""
app/ml/transport.py

Purpose:
- Isolate all HTTP calls used by the ML layer into ONE file.
- If backend devs later change httpx versions, add retries, proxies, etc.,
  only this file needs editing.
"""

from __future__ import annotations
from typing import Any, Dict
import json
import httpx


async def post_json(url: str, payload: Dict[str, Any], timeout_s: float = 180.0) -> Dict[str, Any]:
    """
    POST JSON payload and return JSON response.

    Why:
    - Keeps HTTP client details (timeouts, client lifecycle) out of ML logic.

    What happens:
    - Creates an AsyncClient
    - Makes a POST request
    - Raises if non-2xx
    - Returns parsed JSON response dict
    """
    timeout = httpx.Timeout(timeout_s, connect=10.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


async def post_json_stream_text(url: str, payload: Dict[str, Any]) -> str:
    """
    POST JSON payload to an NDJSON streaming endpoint and return the concatenated text.

    Used for Ollama /api/chat with stream=true. Each line is a JSON object with
    message.content; we collect all chunks until done=true.

    Read timeout is disabled (None) so slow CPU inference (long time-to-first-token)
    never triggers a premature timeout. Connect timeout stays at 10s so we fail fast
    if Ollama is not running at all.
    """
    timeout = httpx.Timeout(None, connect=10.0)
    chunks: list[str] = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                content = (chunk.get("message") or {}).get("content", "")
                if content:
                    chunks.append(content)
                if chunk.get("done"):
                    break

    return "".join(chunks)
