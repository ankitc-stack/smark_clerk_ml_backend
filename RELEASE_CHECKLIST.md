# Release Checklist

## Pre-Release Toggles

### Safety flags — must be OFF unless explicitly approved
- [ ] `ENABLE_COMMAND_API=false`
- [ ] `ENABLE_VOICE_INPUT=false`
- [ ] `COMMAND_INTENT_USE_LLM=false`
- [ ] `COMMAND_TRANSFORM_USE_LLM=false`
- [ ] `ENABLE_DEBUG_ROUTES=false`
- [ ] If `ENV=production` is set, startup must reject if any flag above is `true`

### New integration flags — verify intentional state
- [ ] `DOCENGINE_ENABLED` — set to `true` only if doc-engine service is deployed and healthy
- [ ] `DOCENGINE_URL` — points to correct doc-engine host (not `localhost` in Docker deploys)

### Dependencies
- [ ] `pip install -r requirements.txt` runs cleanly in a fresh venv
- [ ] `libgl1` is installed in the Docker container (required for PaddleOCR/OpenCV)
- [ ] Ollama model pulled: `ollama pull llama3.1:8b` (or configured model)
- [ ] `data/rulebook.pdf` is present (JSSD Vol I)
- [ ] `data/prompt_library/system_prompts/` contains all prompt `.txt` files

---

## CI Gate

Run before every deploy:
```bash
CI_SKIP_PIP_INSTALL=1 python scripts/ci_check.py
```

Individual checks:
```bash
python -m py_compile $(find app scripts tests -type f -name '*.py' | sort)
python scripts/test_command_contract.py
python scripts/eval_intent_seed.py --cases tests/ml/intent_cases.json   # 278 cases, 100%
python scripts/test_transform_flow.py --transform-mode stub
python scripts/test_voice_flow.py --enforce-targets                      # 37 cases, 100%
```

---

## Smoke Tests

Setup:
```bash
API_BASE=http://localhost:8000
DE_BASE=http://localhost:8001
```

---

### 1. Health Check
```bash
curl -sS "$API_BASE/health" | python3 -m json.tool
# → {"status":"ok"} or {"status":"ok","docengine":"connected"}
curl -sS "$DE_BASE/health" | python3 -m json.tool
# → blueprints_loaded, sections_loaded lists
```

---

### 2. Generate GOI Letter
```bash
curl -sS -X POST "$API_BASE/documents/generate" \
  -H "Content-Type: application/json" \
  -d '{
    "template_id": "tmpl_goi_001",
    "prompt": "budget allocation for FY 2026-27 to all Commands",
    "user_id": "test"
  }' | python3 -m json.tool
```
Expected: `document_id`, `docengine_doc_id`, `version`, `blueprint_id=bp_goi_letter_v1`

Save for subsequent tests:
```bash
DOC_ID=<document_id from response>
VERSION=<version from response>
DE_VERSION=<docengine_version from response>
DE_DOC_ID=<docengine_doc_id from response>
```

---

### 3. Generate DO Letter
```bash
curl -sS -X POST "$API_BASE/documents/generate" \
  -H "Content-Type: application/json" \
  -d '{
    "template_id": "tmpl_do_001",
    "prompt": "annual sports meet at Command HQ on 15 Apr 2026",
    "user_id": "test"
  }' | python3 -m json.tool
```
Expected: `blueprint_id=bp_do_letter_v1`, body paragraphs reference sports meet (not generic boilerplate)

---

### 4. Upload DOCX
```bash
curl -sS -X POST "$API_BASE/documents/upload" \
  -F "file=@tests/fixtures/sample_letter.docx" \
  -F "user_id=test" | python3 -m json.tool
```
Expected: `blueprint_id=bp_flexible_v1`, sections detected (`reference_number`, `date`, `subject`, `paragraph`, `signee_block`)

---

### 5. Upload Image (OCR path)
```bash
curl -sS -X POST "$API_BASE/documents/upload" \
  -F "file=@tests/fixtures/sample_letter.jpg" \
  -F "user_id=test" | python3 -m json.tool
```
Expected: Same response shape as DOCX upload; at least one `paragraph` section detected

---

### 6. Text Command — Rewrite
```bash
curl -sS -X POST "$API_BASE/documents/$DOC_ID/command" \
  -H "Content-Type: application/json" \
  -d '{
    "version": '"$VERSION"',
    "context": {"current_section_id": null, "selected_section_ids": [], "cursor_position": 1},
    "input": {"type": "text", "value": "rewrite paragraph 1 in formal military style"}
  }' | python3 -m json.tool
```
Expected: `status=applied`, `version` increments

---

### 7. Text Command — Bold Formatting
```bash
curl -sS -X POST "$API_BASE/documents/$DOC_ID/command" \
  -H "Content-Type: application/json" \
  -d '{
    "version": '"$VERSION"',
    "context": {"current_section_id": null, "selected_section_ids": [], "cursor_position": 1},
    "input": {"type": "text", "value": "make paragraph 1 bold"}
  }' | python3 -m json.tool
```
Expected: `status=applied`

---

### 8. Needs-Clarification Case
```bash
curl -sS -X POST "$API_BASE/documents/$DOC_ID/command" \
  -H "Content-Type: application/json" \
  -d '{
    "version": '"$VERSION"',
    "context": {"current_section_id": null, "selected_section_ids": [], "cursor_position": 1},
    "input": {"type": "text", "value": "Make paragraph formal"}
  }' | python3 -m json.tool
```
Expected: `status=needs_clarification` (ambiguous — no paragraph number)

---

### 9. Version Conflict
```bash
STALE=$((VERSION - 1))
curl -sS -X POST "$API_BASE/documents/$DOC_ID/command" \
  -H "Content-Type: application/json" \
  -d '{
    "version": '"$STALE"',
    "context": {"current_section_id": null, "selected_section_ids": [], "cursor_position": 1},
    "input": {"type": "text", "value": "shorten paragraph 1"}
  }'
```
Expected: HTTP 409, `error.code=version_conflict`

---

### 10. UNDO
```bash
curl -sS -X POST "$API_BASE/documents/$DOC_ID/command" \
  -H "Content-Type: application/json" \
  -d '{
    "version": '"$VERSION"',
    "context": {"current_section_id": null, "selected_section_ids": [], "cursor_position": 1},
    "input": {"type": "text", "value": "undo"}
  }' | python3 -m json.tool
```
Expected: `status=applied`, `version` increments, content reverted

---

### 11. DOCX Export
```bash
curl -sS "$API_BASE/documents/$DOC_ID/export?format=docx" -o smoke_test_export.docx
ls -lh smoke_test_export.docx
```
Expected: File exists and is >5 KB; open in Word — check footer, page number, bold/italic preserved

---

### 12. Save as Template
```bash
curl -sS -X POST "$API_BASE/documents/$DOC_ID/save-as-template" \
  -H "Content-Type: application/json" \
  -d '{"letter_type": "goi_letter", "display_name": "Smoke Test GOI Template"}' \
  | python3 -m json.tool
```
Expected: `template_id`, `section_count` > 0

```bash
TMPL_ID=<template_id from response>
```

---

### 13. List Saved Templates
```bash
curl -sS "$API_BASE/saved-templates?letter_type=goi_letter" | python3 -m json.tool
```
Expected: Array contains the template saved above

---

### 14. New Doc from Template
```bash
curl -sS -X POST "$API_BASE/documents/from-template/$TMPL_ID?user_id=test" \
  | python3 -m json.tool
```
Expected: `BlueprintDocResponse`; letterhead + signee_block have content; subject + paragraphs are blank

---

### 15. Feedback Endpoint
```bash
curl -sS -X POST "$API_BASE/documents/$DOC_ID/feedback" \
  -H "Content-Type: application/json" \
  -d '{"version_id": 1, "rating": "up", "field": "overall", "correction": ""}' \
  | python3 -m json.tool
```
Expected: `{"feedback_id":"...","saved":true}`
```bash
cat data/ft_collected/feedback.jsonl | tail -1
```
Expected: JSONL record with `doc_id`, `rating`, `timestamp`

---

### 16. List Documents
```bash
curl -sS "$API_BASE/documents?user_id=test" | python3 -m json.tool
```
Expected: Array of `DocumentListItem` with `document_id`, `title`, `doc_type`, `created_at`

---

### 17. Revert
```bash
curl -sS -X POST "$API_BASE/documents/$DOC_ID/revert" \
  -H "Content-Type: application/json" \
  -d '{"version": '"$VERSION"'}' | python3 -m json.tool
```
Expected: `reverted_from_version_id`, `reverted_to_version_id`

---

## Rollback Steps

1. Disable runtime feature flags in `.env`:
   ```
   ENABLE_COMMAND_API=false
   ENABLE_VOICE_INPUT=false
   COMMAND_INTENT_USE_LLM=false
   COMMAND_TRANSFORM_USE_LLM=false
   DOCENGINE_ENABLED=false
   ```
2. Restart ML pipeline: `docker compose restart ml-pipeline`
3. Re-run smoke test 1 (health) and smoke test 6 (command) — command must return 403/disabled
4. If doc-engine is broken: restart it with `docker compose restart doc-engine`
5. If database is corrupt: restore from `pgdata` volume backup before any migration ran

---

## Post-Deploy Verification

- [ ] All 17 smoke tests pass
- [ ] `data/ft_collected/feedback.jsonl` written after smoke test 15
- [ ] DOCX export (smoke test 11) opens correctly in Microsoft Word
- [ ] No error-level log lines in `docker compose logs ml-pipeline --since=5m`
- [ ] No error-level log lines in `docker compose logs doc-engine --since=5m`
