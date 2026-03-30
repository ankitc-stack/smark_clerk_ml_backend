"""
app/ml/config.py

Purpose:
- Read ML/Ollama configuration from environment variables.
- Keep this ML module portable across backends.
- When devs change backend config systems, you won't need to edit ML code.
"""

from __future__ import annotations
from app.config import settings


def get_ollama_base_url() -> str:
    return settings.OLLAMA_BASE_URL.rstrip("/")


def get_ollama_model() -> str:
    return settings.OLLAMA_CHAT_MODEL


def get_ollama_temperature() -> float:
    return settings.OLLAMA_TEMPERATURE
