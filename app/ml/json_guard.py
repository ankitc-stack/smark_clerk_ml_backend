"""
app/ml/json_guard.py

Purpose:
- Extract and parse JSON from model outputs that may contain extra text.
- LLMs often add words like "Sure, here's the JSON:" — we must ignore that.

Design:
- Extract first JSON array ([...]) OR object ({...}) region.
- Prefer arrays because PatchOps are usually a JSON array.
"""

from __future__ import annotations
from typing import Optional, Union
import json

JsonType = Union[dict, list]


def extract_json_block(text: str) -> Optional[str]:
    """
    Attempt to extract a JSON block from raw text.

    What happens:
    1) Find the first '{' or '[' in the text
    2) Track nested braces/brackets until the matching close
    3) Ignore brackets that appear inside quoted strings
    4) Return None if no complete top-level JSON value is found

    Note:
    This is a pragmatic extractor for "one JSON answer" responses.
    """
    if not text:
        return None

    start = -1
    stack: list[str] = []
    in_string = False
    escaped = False

    for idx, ch in enumerate(text):
        if start == -1:
            if ch in "{[":
                start = idx
                stack.append(ch)
            continue

        # Inside a JSON string: only handle escapes and closing quote.
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch in "{[":
            stack.append(ch)
            continue

        if ch in "}]":
            if not stack:
                # Corrupt structure, restart scanning after this point.
                start = -1
                continue

            top = stack[-1]
            if (top == "{" and ch == "}") or (top == "[" and ch == "]"):
                stack.pop()
                if not stack:
                    return text[start : idx + 1].strip()
            else:
                # Mismatched close; reset and keep scanning for next candidate.
                start = -1
                stack.clear()
                in_string = False
                escaped = False

    return None


def parse_json_strict(text: str) -> Optional[JsonType]:
    """
    Extract + parse JSON safely.

    Returns:
    - dict/list if parse successful
    - None if extraction or JSON parsing fails
    """
    block = extract_json_block(text)
    if not block:
        return None

    try:
        return json.loads(block)
    except json.JSONDecodeError:
        return None
