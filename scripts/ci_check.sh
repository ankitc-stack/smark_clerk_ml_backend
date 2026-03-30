#!/usr/bin/env bash
set -euo pipefail

# Run all release-blocking checks from one entry point so CI can fail fast on regressions.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

echo "[ci] python: $(python --version 2>&1)"

# Dependency install sanity catches broken pins or missing wheels on clean machines.
if [[ "${CI_SKIP_PIP_INSTALL:-0}" != "1" ]]; then
  echo "[ci] Installing requirements.txt"
  python -m pip install -r requirements.txt
else
  echo "[ci] Skipping pip install because CI_SKIP_PIP_INSTALL=1"
fi

echo "[ci] Bytecode compile check"
# py_compile acts as a low-cost syntax gate before longer-running tests.
python -m py_compile $(find app scripts tests -type f -name '*.py' | sort)

echo "[ci] Command contract"
python scripts/test_command_contract.py

echo "[ci] Intent eval"
python scripts/eval_intent_seed.py --cases tests/ml/intent_cases.json

echo "[ci] Transform flow (stub mode)"
python scripts/test_transform_flow.py --transform-mode stub

echo "[ci] Voice flow"
python scripts/test_voice_flow.py --enforce-targets

# Optional LLM transform gate: run only when explicitly enabled and Ollama is healthy.
if [[ "${COMMAND_TRANSFORM_USE_LLM:-false}" == "true" ]]; then
  OLLAMA_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
  echo "[ci] Checking Ollama health at ${OLLAMA_URL}/api/tags"
  if curl -fsS "${OLLAMA_URL}/api/tags" >/dev/null; then
    echo "[ci] Transform flow (llm mode)"
    python scripts/test_transform_flow.py --transform-mode llm
  else
    echo "[ci] Skipping llm transform flow because Ollama health check failed"
  fi
fi

echo "[ci] PASS"
