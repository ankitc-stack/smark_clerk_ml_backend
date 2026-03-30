#!/usr/bin/env python3
from __future__ import annotations

"""
Cross-platform CI gate runner for release readiness.

Why this exists:
- `ci_check.sh` is convenient on Linux/macOS.
- This Python entrypoint runs the same gates on Windows PowerShell and Linux CI.
"""

import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_BIN = sys.executable or "python"


def _is_true(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _run(cmd: list[str], *, label: str, extra_env: dict | None = None) -> None:
    # Keep command logging explicit so CI logs show exactly which gate failed.
    print(f"[ci] {label}")
    print(f"[ci] $ {' '.join(cmd)}")
    env = extra_env if extra_env is not None else None
    subprocess.run(cmd, cwd=REPO_ROOT, check=True, env=env)


def _python_files() -> list[Path]:
    files: list[Path] = []
    for rel in ("app", "scripts", "tests"):
        root = REPO_ROOT / rel
        if not root.exists():
            continue
        files.extend(sorted(root.rglob("*.py")))
    return files


def _py_compile_gate() -> None:
    # Compile each file in-process to avoid Windows command-length limits.
    import py_compile

    files = _python_files()
    if not files:
        raise RuntimeError("No Python files found under app/scripts/tests")
    print(f"[ci] Bytecode compile check ({len(files)} files)")
    for file_path in files:
        py_compile.compile(str(file_path), doraise=True)


def _ollama_healthy(base_url: str) -> bool:
    tags_url = base_url.rstrip("/") + "/api/tags"
    req = urllib.request.Request(tags_url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=2.0):
            return True
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


def main() -> int:
    print(f"[ci] python: {sys.version.split()[0]}")

    # Keep dependency install in the gate so a clean machine failure is caught early.
    if not _is_true(os.getenv("CI_SKIP_PIP_INSTALL")):
        _run([PYTHON_BIN, "-m", "pip", "install", "-r", "requirements.txt"], label="Installing requirements.txt")
    else:
        print("[ci] Skipping pip install because CI_SKIP_PIP_INSTALL=1")

    _py_compile_gate()
    # Command contract tests use deterministic rule-based path; force LLM intent off
    # so a running Ollama instance doesn't cause nondeterministic test failures.
    contract_env = {**os.environ, "COMMAND_INTENT_USE_LLM": "false"}
    _run([PYTHON_BIN, "scripts/test_command_contract.py"], label="Command contract", extra_env=contract_env)
    _run(
        [PYTHON_BIN, "scripts/eval_intent_seed.py", "--cases", "tests/ml/intent_cases.json"],
        label="Intent eval",
    )
    _run([PYTHON_BIN, "scripts/test_transform_flow.py", "--transform-mode", "stub"], label="Transform flow (stub mode)")
    _run([PYTHON_BIN, "scripts/test_voice_flow.py", "--enforce-targets"], label="Voice flow")

    # Optional LLM transform gate remains opt-in and health-gated to avoid flaky failures.
    if _is_true(os.getenv("COMMAND_TRANSFORM_USE_LLM")):
        ollama_base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        print(f"[ci] Checking Ollama health at {ollama_base.rstrip('/')}/api/tags")
        if _ollama_healthy(ollama_base):
            _run(
                [PYTHON_BIN, "scripts/test_transform_flow.py", "--transform-mode", "llm"],
                label="Transform flow (llm mode)",
            )
        else:
            print("[ci] Skipping llm transform flow because Ollama health check failed")

    print("[ci] PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as ex:
        print(f"[ci] FAIL (exit={ex.returncode})", file=sys.stderr)
        raise
    except Exception as ex:
        print(f"[ci] FAIL ({type(ex).__name__}: {ex})", file=sys.stderr)
        raise
