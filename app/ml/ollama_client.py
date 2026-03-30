"""
app/ml/ollama_client.py

Purpose:
- Provide a single function to call Ollama /api/chat.
- Keeps model calling code centralized and reusable across:
  - doctype classification
  - section text generation
  - edit->PatchOps generation
  - JSON repair calls
"""

from __future__ import annotations
from typing import Any, Dict

from app.ml.config import get_ollama_base_url, get_ollama_model, get_ollama_temperature
from app.ml.transport import post_json_stream_text


async def ollama_chat(system: str, user: str) -> str:
    """
    Send a chat request to Ollama and return the assistant message content.

    Inputs:
    - system: system prompt text (rules + constraints)
    - user: user prompt text (task payload)

    Output:
    - raw assistant text (may include extra text around JSON)
    """
    base_url = get_ollama_base_url()
    model = get_ollama_model()
    temperature = get_ollama_temperature()

    url = f"{base_url}/api/chat"

    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {
            "temperature": temperature,
            "num_predict": 512,   # cap output tokens so slow CPU inference stays under ~1 min
        },
        "stream": True,
        "keep_alive": "10m",  # keep model loaded in memory between requests
    }

    return await post_json_stream_text(url, payload)
