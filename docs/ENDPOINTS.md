# Smart Clerk — Endpoint Reference & Prompt Examples

All endpoints are on `http://localhost:8000` (ML pipeline).
Doc-engine internal API on `http://localhost:8001` (not called directly by clients).

---

## 1. `GET /health`

Returns service health status.

```bash
curl http://localhost:8000/health
# → {"ok": true}
```

**Won't work:** Nothing to break here. If it returns anything other than `{"ok": true}` the service is down.

---

## 2. `GET /ui`

Opens the interactive test UI in a browser.

```
http://localhost:8000/ui
```

---

## 3. `GET /contracts/action-object-v1`

Returns the JSON Schema for the ActionObject (used for client-side validation of commands).

```bash
curl http://localhost:8000/contracts/action-object-v1
```

---

## 4. `POST /templates/register`

Register a DOCX file as a named template (legacy slot-fill path, not blueprint).

```bash
# Register a leave certificate template
curl -X POST http://localhost:8000/templates/register \
  -F "name=Leave Certificate" \
  -F "doc_type=leave_certificate" \
  -F "file=@data/templates/Leave Certificate.docx"

# Register a movement order template
curl -X POST http://localhost:8000/templates/register \
  -F "name=Movement Order" \
  -F "doc_type=movement_order" \
  -F "file=@data/templates/Movement Order.docx"

# Register a GOI letter template
curl -X POST http://localhost:8000/templates/register \
  -F "name=GOI Letter" \
  -F "doc_type=goi_letter" \
  -F "file=@data/templates/Letter Format OG.docx"
```

**Won't work:**
- Non-DOCX files (PDF, TXT) — returns 400
- Missing `doc_type` field — Pydantic validation error
- `doc_type` not in supported list — template stored but won't be used by generation

---

## 5. `GET /templates`

List all registered templates.

```bash
# List all templates
curl http://localhost:8000/templates

# Filter by document type
curl "http://localhost:8000/templates?doc_type=goi_letter"
curl "http://localhost:8000/templates?doc_type=movement_order"
```

**Won't work:**
- Filtering by a `doc_type` that was never registered — returns empty list (not 404)

---

## 6. `GET /templates/{template_id}`

Get details of a single registered template.

```bash
curl http://localhost:8000/templates/tmpl_goi_001
curl http://localhost:8000/templates/tmpl_do_001
curl http://localhost:8000/templates/tmpl_mov_001
```

**Blueprint template IDs (pre-seeded):**
| ID | Type |
|---|---|
| `tmpl_goi_001` | GOI Letter |
| `tmpl_do_001` | DO Letter |
| `tmpl_mov_001` | Movement Order |
| `tmpl_leave_001` | Leave Certificate |
| `tmpl_inv_001` | Invitation Letter |
| `tmpl_svc_001` | Service Letter |
| `tmpl_flex_001` | Uploaded Document (flexible) |

**Won't work:**
- Non-existent template ID — returns 404

---

## 7. `POST /documents/generate`

Create a new document from a blueprint template with AI-filled content.
This is the **main generation endpoint**.

### GOI Letter Examples

```bash
# Budget allocation circular
curl -X POST http://localhost:8000/documents/generate \
  -H "Content-Type: application/json" \
  -d '{
    "template_id": "tmpl_goi_001",
    "prompt": "GOI letter for annual budget allocation to all Army formations for FY 2026-27, deadline 31 March 2026"
  }'

# Performance report submission
curl -X POST http://localhost:8000/documents/generate \
  -H "Content-Type: application/json" \
  -d '{
    "template_id": "tmpl_goi_001",
    "prompt": "GOI letter directing all HQ to submit Annual Confidential Reports for officers by 15 April 2026"
  }'
```

### DO Letter Examples

```bash
# Requesting ration supply
curl -X POST http://localhost:8000/documents/generate \
  -H "Content-Type: application/json" \
  -d '{
    "template_id": "tmpl_do_001",
    "prompt": "DO letter to Brig RS Sharma about critical ration shortage in 153 Inf Bn for last 3 weeks, request urgent action"
  }'

# Requesting officer deputation
curl -X POST http://localhost:8000/documents/generate \
  -H "Content-Type: application/json" \
  -d '{
    "template_id": "tmpl_do_001",
    "prompt": "DO letter to Col Anil Kumar requesting deputation of two officers for Annual Sports Meet at Ambala on 15 April 2026"
  }'
```

### Movement Order Examples

```bash
# Temporary duty order
curl -X POST http://localhost:8000/documents/generate \
  -H "Content-Type: application/json" \
  -d '{
    "template_id": "tmpl_mov_001",
    "prompt": "Movement order for Sep Raj Kumar 10525911F of 153 Inf Bn to proceed to DG INF New Delhi on 25 Mar 2026 FN for admin duties, return by 30 Mar 2026"
  }'

# Training deputation
curl -X POST http://localhost:8000/documents/generate \
  -H "Content-Type: application/json" \
  -d '{
    "template_id": "tmpl_mov_001",
    "prompt": "Movement order Nk Ramesh Singh 12345678A 10 Rajput proceed to DIPR Pune 01 Apr 2026 for signals training course, return 20 Apr 2026"
  }'
```

### Leave Certificate Examples

```bash
curl -X POST http://localhost:8000/documents/generate \
  -H "Content-Type: application/json" \
  -d '{
    "template_id": "tmpl_leave_001",
    "prompt": "Leave certificate for Maj Arun Verma, 15 days casual leave from 01 Apr 2026 to 15 Apr 2026, proceeding to Delhi"
  }'
```

### Service Letter Examples

```bash
curl -X POST http://localhost:8000/documents/generate \
  -H "Content-Type: application/json" \
  -d '{
    "template_id": "tmpl_svc_001",
    "prompt": "Service letter for annual administrative inspection of all units under HQ Eastern Command scheduled 10-15 May 2026"
  }'
```

### Invitation Letter Examples

```bash
curl -X POST http://localhost:8000/documents/generate \
  -H "Content-Type: application/json" \
  -d '{
    "template_id": "tmpl_inv_001",
    "prompt": "Invitation letter to GOC-in-C Southern Command for Army Day Parade at Delhi Cantonment on 15 January 2026"
  }'
```

**Won't work:**
- Empty `prompt` with no template_id — returns 422 (template_id required)
- Non-existent template_id like `tmpl_xyz_999` — returns 404 from doc-engine
- Prompt with no specific details: `"write a letter"` — generates with placeholders because Ollama has nothing to fill
- Prompt in pure script (Devanagari digits only, no names/units in roman) — may mis-extract some fields; Hinglish/Roman transliteration works better
- `template_id: "tmpl_flex_001"` for generation — flexible blueprint has no structure, use it only for uploads

---

## 8. `POST /documents/upload`

Upload an existing letter (DOCX / PDF / image) and auto-detect its sections.

```bash
# Upload a DOCX letter
curl -X POST http://localhost:8000/documents/upload \
  -F "file=@my_service_letter.docx" \
  -F "letter_type=service_letter" \
  -F "user_id=officer_001"

# Upload a scanned PDF (OCR applied automatically)
curl -X POST http://localhost:8000/documents/upload \
  -F "file=@scanned_goi_letter.pdf" \
  -F "letter_type=goi_letter"

# Upload a photo of a letter (JPG/PNG)
curl -X POST http://localhost:8000/documents/upload \
  -F "file=@photo_letter.jpg"
```

**`letter_type` values:** `goi_letter`, `do_letter`, `service_letter`, `movement_order`, `leave_certificate`, `invitation_letter`
If omitted, defaults to generic flexible blueprint.

**Won't work:**
- File > 10 MB — returns 400 (size limit)
- Password-protected PDF — PyMuPDF can't extract text, returns empty sections
- Very low quality scans (below ~150 DPI) — Surya OCR may miss text or misread characters
- XLSX, DOC (old binary format), RTF — unsupported extensions, returns 400
- Completely blank/image-only PDF with no text layer and bad scan quality — returns document with empty sections

---

## 9. `POST /documents/{document_id}/command`

Apply a text or voice command to an existing document. The **main editing endpoint**.

> Requires `ENABLE_COMMAND_API=true` in `.env`

### Content Commands

```bash
DOC_ID="your-document-id"
VERSION=1

# Rewrite paragraph 1
curl -X POST "http://localhost:8000/documents/$DOC_ID/command" \
  -H "Content-Type: application/json" \
  -d "{\"version\": $VERSION, \"context\": {}, \"input\": {\"type\": \"text\", \"value\": \"rewrite paragraph 1\"}}"

# Expand paragraph 2 with more detail
curl -X POST "http://localhost:8000/documents/$DOC_ID/command" \
  -H "Content-Type: application/json" \
  -d "{\"version\": $VERSION, \"context\": {}, \"input\": {\"type\": \"text\", \"value\": \"expand para 2\"}}"

# Shorten paragraph 3
curl -X POST "http://localhost:8000/documents/$DOC_ID/command" \
  -H "Content-Type: application/json" \
  -d "{\"version\": $VERSION, \"context\": {}, \"input\": {\"type\": \"text\", \"value\": \"shorten paragraph 3\"}}"

# Change tone to formal
curl -X POST "http://localhost:8000/documents/$DOC_ID/command" \
  -H "Content-Type: application/json" \
  -d "{\"version\": $VERSION, \"context\": {}, \"input\": {\"type\": \"text\", \"value\": \"make paragraph 1 formal\"}}"
```

### Formatting Commands

```bash
# Bold entire paragraph 1
curl -X POST "http://localhost:8000/documents/$DOC_ID/command" \
  -H "Content-Type: application/json" \
  -d "{\"version\": $VERSION, \"context\": {}, \"input\": {\"type\": \"text\", \"value\": \"make paragraph 1 bold\"}}"

# Highlight a specific word
curl -X POST "http://localhost:8000/documents/$DOC_ID/command" \
  -H "Content-Type: application/json" \
  -d "{\"version\": $VERSION, \"context\": {}, \"input\": {\"type\": \"text\", \"value\": \"highlight the word URGENT in paragraph 2\"}}"

# Move signee block to right
curl -X POST "http://localhost:8000/documents/$DOC_ID/command" \
  -H "Content-Type: application/json" \
  -d "{\"version\": $VERSION, \"context\": {}, \"input\": {\"type\": \"text\", \"value\": \"move signee block to right\"}}"

# Change font size
curl -X POST "http://localhost:8000/documents/$DOC_ID/command" \
  -H "Content-Type: application/json" \
  -d "{\"version\": $VERSION, \"context\": {}, \"input\": {\"type\": \"text\", \"value\": \"font size 14 for paragraph 1\"}}"
```

### Structural Commands

```bash
# Add a new paragraph
curl -X POST "http://localhost:8000/documents/$DOC_ID/command" \
  -H "Content-Type: application/json" \
  -d "{\"version\": $VERSION, \"context\": {}, \"input\": {\"type\": \"text\", \"value\": \"add a new paragraph after paragraph 2\"}}"

# Remove paragraph 3
curl -X POST "http://localhost:8000/documents/$DOC_ID/command" \
  -H "Content-Type: application/json" \
  -d "{\"version\": $VERSION, \"context\": {}, \"input\": {\"type\": \"text\", \"value\": \"remove paragraph 3\"}}"

# Undo last change
curl -X POST "http://localhost:8000/documents/$DOC_ID/command" \
  -H "Content-Type: application/json" \
  -d "{\"version\": $VERSION, \"context\": {}, \"input\": {\"type\": \"text\", \"value\": \"undo\"}}"
```

### Hinglish Commands (also supported)

```bash
# "Make para 2 short" in Hinglish
-d '{"version": 1, "context": {}, "input": {"type": "text", "value": "para 2 chhota karo"}}'

# "Make formal" in Hinglish
-d '{"version": 1, "context": {}, "input": {"type": "text", "value": "isko formal karo"}}'

# "Undo" in Hindi
-d '{"version": 1, "context": {}, "input": {"type": "text", "value": "wapas karo"}}'
```

**Won't work:**
- Stale `version` number (e.g. sending version=1 when document is at version=5) — returns `409 version_conflict`; always fetch latest version first
- `"Delete everything"` or `"Clear the document"` — ambiguous, returns `needs_clarification`
- `"Make it better"` — too vague, returns `needs_clarification`
- Undo when already at version 1 — returns `no_previous_version` error
- `"Move paragraph 1 before paragraph 0"` — no paragraph 0 exists, returns `section_not_found`
- Voice commands without `ENABLE_VOICE_INPUT=true` — returns 422

---

## 10. `PATCH /documents/{doc_id}/sections/{section_id}`

Directly update a section's content with Lexical JSON (bypasses AI, direct edit).

```bash
# Direct text update to a section
curl -X PATCH "http://localhost:8000/documents/$DOC_ID/sections/$SECTION_ID" \
  -H "Content-Type: application/json" \
  -d '{
    "version": 1,
    "content": {
      "richtext": {
        "format": "lexical",
        "state": {
          "root": {
            "type": "root", "version": 1, "direction": "ltr", "format": "", "indent": 0,
            "children": [{
              "type": "paragraph", "version": 1, "direction": "ltr",
              "format": "left", "indent": 0,
              "children": [{"type": "text", "text": "1. Updated paragraph content here.", "format": 0, "version": 1, "detail": 0, "mode": "normal", "style": ""}]
            }]
          }
        }
      }
    }
  }'

# Alignment-only update (move to right)
curl -X PATCH "http://localhost:8000/documents/$DOC_ID/sections/$SECTION_ID" \
  -H "Content-Type: application/json" \
  -d '{"version": 1, "alignment": "right"}'
```

**Won't work:**
- Wrong `version` number — returns 409 conflict from doc-engine
- Missing `section_id` that doesn't exist in the document — returns 404
- Malformed Lexical JSON (missing `root`, wrong `type` values) — doc-engine validation error

---

## 11. `POST /stt/transcribe`

Convert base64-encoded audio to text using Whisper.

> Requires `ENABLE_VOICE_INPUT=true`

```bash
# Encode WAV file and transcribe
AUDIO_B64=$(base64 -w0 command.wav)
curl -X POST http://localhost:8000/stt/transcribe \
  -H "Content-Type: application/json" \
  -d "{\"audio_base64\": \"$AUDIO_B64\", \"mime_type\": \"audio/wav\"}"

# WebM audio (from browser MediaRecorder)
AUDIO_B64=$(base64 -w0 recording.webm)
curl -X POST http://localhost:8000/stt/transcribe \
  -H "Content-Type: application/json" \
  -d "{\"audio_base64\": \"$AUDIO_B64\", \"mime_type\": \"audio/webm\"}"
```

**Response fields:** `transcript`, `stt_confidence`, `stt_latency_ms`, `needs_clarification`

**Won't work:**
- Audio file > 10 MB — returns 400
- Audio < 200ms — too short, returns error
- Confidence < 0.25 — hard fail, returns `could_not_understand_audio`
- Confidence 0.25–0.45 — returns `needs_clarification: true` with transcript for user confirmation
- MP3 files without FFmpeg installed in container — audio decode failure
- Empty/silent audio — confidence near 0, hard fail

---

## 12. `GET /documents`

List all documents for a user.

```bash
# List documents for default user
curl "http://localhost:8000/documents?user_id=local-user"

# List with custom limit
curl "http://localhost:8000/documents?user_id=officer_001&limit=10"

# Default (no user_id) — returns all docs
curl http://localhost:8000/documents
```

**Won't work:**
- `limit=0` — returns empty list
- `limit` > 1000 — still works but may be slow on large datasets

---

## 13. `GET /documents/{document_id}`

Get document metadata (not content — use doc-engine directly for full section data).

```bash
curl http://localhost:8000/documents/abc123-def456
```

**Won't work:**
- Non-existent document_id — returns 404

---

## 14. `GET /documents/{document_id}/versions`

List all versions of a document with timestamps and previews.

```bash
curl http://localhost:8000/documents/abc123-def456/versions
```

Returns: `[{version_id, created_at, change_log, preview_text}]`

**Won't work:**
- Non-existent document_id — returns 404

---

## 15. `GET /documents/{doc_id}/export`

Export the latest version as DOCX or PDF.

```bash
# Export as DOCX (default)
curl "http://localhost:8000/documents/$DOC_ID/export?format=docx" -o letter.docx

# Export as PDF (requires LibreOffice in container)
curl "http://localhost:8000/documents/$DOC_ID/export?format=pdf" -o letter.pdf
```

**Won't work:**
- `format=xlsx` or any unsupported format — returns 400
- PDF export when LibreOffice not installed — returns 500 (Docker image includes it)
- Document with no versions yet — returns 404

---

## 16. `GET /documents/{doc_id}/versions/{version_id}/download`

Download a specific version (not just the latest).

```bash
# Download version 3 as DOCX
curl "http://localhost:8000/documents/$DOC_ID/versions/3/download?format=docx" -o letter_v3.docx

# Download version 1 (original) as PDF
curl "http://localhost:8000/documents/$DOC_ID/versions/1/download?format=pdf" -o original.pdf
```

**Won't work:**
- Version ID that doesn't belong to the document — returns 404
- Version where DOCX was never rendered (edge case on failed commands) — returns 404

---

## 17. `POST /documents/{doc_id}/revert`

Revert document to a previous version (creates a new version for audit trail).

```bash
# Revert to version 2 (current is version 5)
curl -X POST "http://localhost:8000/documents/$DOC_ID/revert" \
  -H "Content-Type: application/json" \
  -d '{"target_version_id": 2, "version": 5}'

# Undo one step (revert to previous version)
curl -X POST "http://localhost:8000/documents/$DOC_ID/revert" \
  -H "Content-Type: application/json" \
  -d '{"target_version_id": 4, "version": 5}'
```

**Won't work:**
- `target_version_id` newer than current version — logical error, returns 422
- `version` field not matching current version — returns 409 conflict
- Reverting to version that doesn't exist for this document — returns 404

---

## 18. `POST /documents/{doc_id}/save-as-template`

Save a document's structure as a reusable template (keeps letterhead/signee, clears variable content).

```bash
# Save as service letter template
curl -X POST "http://localhost:8000/documents/$DOC_ID/save-as-template" \
  -H "Content-Type: application/json" \
  -d '{"letter_type": "service_letter", "display_name": "HQ TA Directorate Service Letter"}'

# Save as GOI letter template
curl -X POST "http://localhost:8000/documents/$DOC_ID/save-as-template" \
  -H "Content-Type: application/json" \
  -d '{"letter_type": "goi_letter", "display_name": "MOD Budget Circular"}'

# Minimal (auto-name from document title)
curl -X POST "http://localhost:8000/documents/$DOC_ID/save-as-template" \
  -H "Content-Type: application/json" \
  -d '{"letter_type": "do_letter"}'
```

**Sticky fields (content preserved):** `letterhead`, `signee_block`
**Cleared fields:** `subject`, `reference_number`, `date`, `receiver_block`, `paragraph`

**Won't work:**
- `letter_type` not in the supported list — template saved but won't resolve correctly in `from-template`
- Document without a doc-engine association (legacy template-filled docs) — 503 error

---

## 19. `GET /saved-templates`

List all saved templates.

```bash
# List all saved templates
curl http://localhost:8000/saved-templates

# Filter by letter type
curl "http://localhost:8000/saved-templates?letter_type=service_letter"
curl "http://localhost:8000/saved-templates?letter_type=do_letter"
```

**Won't work:**
- Filtering by a `letter_type` with no saved templates — returns empty list (not 404)

---

## 20. `DELETE /saved-templates/{template_id}`

Delete a saved template.

```bash
curl -X DELETE http://localhost:8000/saved-templates/service_letter_abc12345
```

**Won't work:**
- Non-existent template_id — returns 404

---

## 21. `POST /documents/from-template/{template_id}`

Create a new document from a saved template with optional AI fill.

```bash
# Create blank document from saved template
curl -X POST "http://localhost:8000/documents/from-template/service_letter_abc12345?user_id=officer_001"

# Create with AI-filled content via prompt
curl -X POST "http://localhost:8000/documents/from-template/service_letter_abc12345?user_id=officer_001&prompt=inspection%20of%20all%20units%20under%20HQ%20Eastern%20Command%20scheduled%2010-15%20May%202026"

# Hinglish prompt
curl -X POST "http://localhost:8000/documents/from-template/do_letter_xyz98765?prompt=ration+shortage+153+Inf+Bn+ke+liye+letter"
```

**Won't work:**
- Non-existent `template_id` — returns 404
- Template whose `letter_type` has no matching blueprint in doc-engine — 503
- Very short prompt like `"letter"` — generates with placeholder content

---

## 22. `POST /documents/{doc_id}/feedback`

Submit thumbs up/down feedback with optional correction (used for training data collection).

```bash
# Thumbs down on signee block with correction
curl -X POST "http://localhost:8000/documents/$DOC_ID/feedback" \
  -H "Content-Type: application/json" \
  -d '{
    "version_id": 3,
    "rating": "down",
    "field": "signee",
    "correction": "Should start with Yours faithfully,"
  }'

# Thumbs up on overall
curl -X POST "http://localhost:8000/documents/$DOC_ID/feedback" \
  -H "Content-Type: application/json" \
  -d '{"version_id": 3, "rating": "up", "field": "overall"}'

# Correction on subject line
curl -X POST "http://localhost:8000/documents/$DOC_ID/feedback" \
  -H "Content-Type: application/json" \
  -d '{
    "version_id": 2,
    "rating": "down",
    "field": "subject",
    "correction": "BUDGET ALLOCATION FOR FY 2026-27 — NOT budget allocation 2026"
  }'
```

**`field` values:** `overall`, `subject`, `paragraph_1`, `paragraph_2`, `salutation`, `signee`
**`rating` values:** `up`, `down`

Feedback is saved to `data/ft_collected/feedback.jsonl`.

**Won't work:**
- `rating` value other than `"up"` or `"down"` — Pydantic validation error
- Non-existent `doc_id` — returns 404
- `version_id` that doesn't exist — still saves (no version validation on feedback)

---

## Common Mistakes Across All Endpoints

| Mistake | What Happens | Fix |
|---|---|---|
| Sending stale `version` in command | `409 version_conflict` | Fetch latest version first with `GET /documents/{id}/versions` |
| Prompt with no specific details | LLM generates placeholder content | Include names, units, dates, quantities in the prompt |
| Wrong `Content-Type` header | 422 Unprocessable Entity | Always set `Content-Type: application/json` for JSON bodies |
| Forgetting `ENABLE_COMMAND_API=true` | 404 or 422 on command endpoint | Add to `.env` and restart |
| Uploading password-protected PDF | Empty sections in response | Remove password protection before uploading |
| Very vague command like "fix it" | `needs_clarification` response | Be specific: "rewrite paragraph 1 in formal tone" |
| Hindi script (Devanagari) in prompt | May partially work | Use Roman transliteration: "ration" not "राशन" |
