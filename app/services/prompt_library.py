from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

from app.config import settings


_DEFAULT_INTENT_SYSTEM_PROMPT = """You are an intent extractor for a document command API.
Return STRICT JSON only.
Do not include markdown.
Output must follow the ActionObject schema exactly.
Rules:
- Determine action/scope/target from user command and context.
- If target is ambiguous, set needs_clarification=true and provide a short question/options.
- Keep confidence between 0 and 1.
- Do not invent section ids or paragraph ids that are not present in provided context.
"""


def _prompt_library_dir() -> str:
    return os.path.join(settings.DATA_DIR, "prompt_library")


def _system_prompt_path(name: str) -> str:
    return os.path.join(_prompt_library_dir(), "system_prompts", f"{name}.txt")


def _few_shot_path(name: str) -> str:
    return os.path.join(_prompt_library_dir(), "few_shot_examples", f"{name}.json")


@lru_cache(maxsize=64)
def load_system_prompt(name: str) -> str:
    path = _system_prompt_path(name)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
            if text:
                return text

    # Fallback ensures the sandbox remains runnable even if prompt files are missing.
    if name == "intent_extraction_v1":
        return _DEFAULT_INTENT_SYSTEM_PROMPT
    return "Return STRICT JSON only."


@lru_cache(maxsize=64)
def load_few_shot_examples(name: str) -> list[dict[str, Any]]:
    path = _few_shot_path(name)
    if not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return []

    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        examples = raw.get("examples")
        if isinstance(examples, list):
            return [x for x in examples if isinstance(x, dict)]
    return []
