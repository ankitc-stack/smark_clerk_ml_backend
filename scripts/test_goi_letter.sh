#!/usr/bin/env bash
# Test GOI letter generation with 3 prompts

BASE="http://localhost:8000"
SKELETON=$(curl -s "$BASE/templates/skeleton?doc_type=GOI_LETTER" | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d['skeleton']))" 2>/dev/null)

if [ -z "$SKELETON" ]; then
  echo "ERROR: Could not fetch skeleton"
  exit 1
fi

run_test() {
  local label="$1"
  local prompt="$2"
  echo ""
  echo "=========================================="
  echo "TEST: $label"
  echo "PROMPT: $prompt"
  echo "=========================================="

  PAYLOAD=$(python3 -c "
import json, sys
skeleton = json.loads(sys.argv[1])
payload = {
    'user_id': 'test_user',
    'doc_type': 'GOI_LETTER',
    'prompt': sys.argv[2],
    'skeleton': skeleton
}
print(json.dumps(payload))
" "$SKELETON" "$prompt")

  RESP=$(curl -s -X POST "$BASE/documents/fill" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD")

  echo "$RESP" | python3 -c "
import json, sys
data = json.load(sys.stdin)
r = data.get('render_state', {})
f = r.get('fields', {})
b = r.get('blocks', {})
print('FIELDS:')
for k,v in f.items():
    if v: print(f'  {k}: {repr(v)}')
print('BLOCKS (body_paras):')
for p in b.get('body_paras', []):
    print(f'  - {repr(p)}')
warns = data.get('warnings', [])
if warns:
    print('WARNINGS:')
    for w in warns: print(f'  ! {w}')
"
}

run_test \
  "ACR Submission Request" \
  "GOI letter to Secy MoD about Annual Confidential Report of Army Officers for the year 2025-26, dated 28 Feb 2026, Ref No B/45678/AG/2026"

run_test \
  "Parliament Session Inputs" \
  "GOI letter requesting inputs for Parliament Session from all Directorates by 15 Mar 2026, reference No AG/12345/Parl/2026 dated 01 Mar 2026"

run_test \
  "Budget Allocation" \
  "GOI letter regarding budget allocation for modernization of Army Signal Corps equipment for FY 2026-27, to JS Finance MoD"
