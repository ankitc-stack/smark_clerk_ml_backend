# Smart Clerk ML Backend

Local-first backend for drafting, uploading, and editing Indian Army documents with voice + AI assistance.

## Architecture Overview

Two microservices working together:

| Service | Port | Responsibility |
|---|---|---|
| **ML Pipeline** (`smark_clerk_ml_backend-main`) | 8000 | LLM orchestration, OCR, voice, intent parsing, DOCX export |
| **Doc-Engine** (`smartclerk-backend-docengine-main`) | 8001 | Structured document storage, blueprints, section versioning |

PostgreSQL (with pgvector) is shared via Docker. Ollama runs locally on the host.

```
Officer → POST /documents/generate (or /upload)
             ↓
       ML Pipeline :8000
       - Slot extraction (regex fallback)
       - LLM body para drafting (Ollama llama3.1:8b)
       - Creates doc in Doc-Engine via HTTP
             ↓
       Doc-Engine :8001
       - Stores structured sections
       - Enforces blueprint constraints
       - Returns versioned DocumentData
             ↓
       ML Pipeline generates DOCX (python-docx)
       Returns BlueprintDocResponse to officer
```

---

## Quick Start

### Prerequisites
- Python 3.11+
- Docker + Docker Compose
- [Ollama](https://ollama.ai) installed locally (Mac/Linux)

### 1. Place required data
```bash
mkdir -p data/templates data/prompt_library/few_shot_examples data/prompt_library/system_prompts
cp "JSSD 2025.pdf" data/rulebook.pdf
```

### 2. Pull Ollama model
```bash
ollama pull llama3.1:8b
```

### 3. Start with Docker Compose
```bash
# From smark_clerk_ml_backend-main/
docker compose up -d --build
```

Services:
- ML API: http://localhost:8000
- Doc-Engine API: http://localhost:8001
- Swagger UI: http://localhost:8000/docs

### 4. Run locally (without Docker)
```bash
# Terminal 1 — start postgres
docker start smartclerk-postgres   # or: docker compose up postgres -d

# Terminal 2 — doc-engine
cd smartclerk-backend-docengine-main
source .venv/bin/activate
uvicorn app.main:app --port 8001

# Terminal 3 — ML pipeline
cd smark_clerk_ml_backend-main
source .venv/bin/activate
cp .env.example .env   # set DOCENGINE_ENABLED=true, adjust DATABASE_URL
uvicorn app.main:app --port 8000 --reload
```

---

## Key Features

### Document Generation (`POST /documents/generate`)
Creates a new document from a blueprint template using LLM + slot extraction.

**Pre-seeded templates:**

| Template ID | Blueprint | Document Type |
|---|---|---|
| `tmpl_goi_001` | `bp_goi_letter_v1` | GOI Letter (Govt of India style) |
| `tmpl_do_001` | `bp_do_letter_v1` | DO Letter (Demi-official) |
| `tmpl_svc_001` | `bp_service_letter_v1` | Service Letter |
| `tmpl_inv_001` | `bp_invitation_letter_v1` | Invitation Letter |
| `tmpl_mov_001` | `bp_movement_order_v1` | Movement Order |
| `tmpl_leave_001` | `bp_leave_certificate_v1` | Leave Certificate |
| `tmpl_flex_001` | `bp_flexible_v1` | Uploaded Document (auto-detected) |

### Document Upload (`POST /documents/upload`)
Upload an existing letter (DOCX / PDF / scanned PDF / photo) — sections are extracted, doc is opened for editing.

Supported formats: `.docx`, `.pdf`, `.jpg`, `.jpeg`, `.png`, `.tiff`, `.bmp`
- Digital DOCX → python-docx paragraph extraction
- Digital PDF → PyMuPDF text extraction
- Scanned PDF / image → PaddleOCR (PP-OCRv4, CPU)

### AI Command Editing (`POST /documents/{id}/command`)
Edit any section with natural language — text or voice.

Supported actions (14): `REWRITE_CONTENT`, `EXPAND_CONTENT`, `SHORTEN_CONTENT`, `CHANGE_TONE`, `REPLACE_TEXT`, `INSERT_TEXT`, `DELETE_TEXT`, `SET_FORMAT`, `ADD_PARAGRAPH`, `REMOVE_PARAGRAPH`, `INSERT_SECTION`, `DELETE_SECTION`, `MOVE_SECTION`, `UNDO`

### Inline Formatting (`SET_FORMAT`)
Bold, italic, underline, font name, font size, highlight, text color, paragraph alignment — all preserved in Lexical JSON and rendered into DOCX export.

### Saved Templates (`POST /documents/{id}/save-as-template`)
Save an edited letter's structure as a reusable template. Static fields (letterhead, signee) are preserved; variable content (subject, paragraphs) is cleared.

### RLHF Feedback (`POST /documents/{id}/feedback`)
Thumbs-up/down ratings + corrections saved to `data/ft_collected/feedback.jsonl` for future fine-tuning.

### DOCX Export (`GET /documents/{id}/export?format=docx`)
Generates a correctly formatted DOCX using python-docx:
- Security classification header/footer
- Page numbering from page 2 (no number on page 1)
- Inline formatting preserved (bold/italic/font/highlight)
- Military style: numbered paragraphs, sub-paragraph indentation

---

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `ENV` | `development` | Set to `production` to enforce safety guard |
| `DATABASE_URL` | `sqlite:///./docai.db` | PostgreSQL or SQLite connection string |
| `DOCENGINE_ENABLED` | `false` | Enable doc-engine microservice integration |
| `DOCENGINE_URL` | `http://localhost:8001` | Doc-engine base URL |
| `DOCENGINE_TIMEOUT_S` | `30.0` | HTTP timeout for doc-engine calls |
| `ENABLE_COMMAND_API` | `false` | Enable `POST /documents/{id}/command` |
| `ENABLE_VOICE_INPUT` | `false` | Allow voice command payloads |
| `COMMAND_INTENT_USE_LLM` | `false` | Ollama intent extraction (vs. rule-based fallback) |
| `COMMAND_TRANSFORM_USE_LLM` | `false` | Ollama paragraph rewrite (vs. stub) |
| `ENABLE_DEBUG_ROUTES` | `false` | Enable debug/admin routes |
| `LLM_PROVIDER` | `ollama` | `ollama` or `stub` |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama endpoint |
| `OLLAMA_CHAT_MODEL` | `llama3.1:8b` | Chat model for generation |
| `OLLAMA_TEMPERATURE` | `0.2` | Generation temperature |
| `EMBED_PROVIDER` | `fastembed` | `fastembed` or `stub` |
| `EMBEDDING_DIM` | `384` | Vector dimension (matches bge-small-en-v1.5) |
| `LIBREOFFICE_BIN` | `soffice` | LibreOffice binary for PDF conversion |
| `AUTO_BOOTSTRAP` | `true` | Auto-load rulebook + seed data on startup |
| `DATA_DIR` | `./data` | Prompt library and seed data location |
| `RULEBOOK_FILENAME` | `rulebook.pdf` | JSSD rulebook filename in DATA_DIR |
| `STT_PROVIDER` | `faster_whisper` | STT backend: `faster_whisper` or `stub` |
| `STT_MODEL_NAME` | `large-v3-turbo` | Whisper model name |
| `STT_DEVICE` | `cpu` | `cpu`, `cuda`, or `auto` |
| `STT_COMPUTE_TYPE` | `int8` | `int8` (CPU) or `float16` (GPU) |
| `STT_MIN_CONFIDENCE` | `0.25` | Below this → STT error |
| `STT_CONFIRM_CONFIDENCE` | `0.45` | Between min and this → needs_clarification |

### Production safety guard
When `ENV=production`, startup hard-fails if any of these are enabled:
`ENABLE_COMMAND_API`, `ENABLE_VOICE_INPUT`, `COMMAND_INTENT_USE_LLM`, `COMMAND_TRANSFORM_USE_LLM`, `ENABLE_DEBUG_ROUTES`

---

## Model Setup

### Ollama (LLM)
```bash
# Install locally: https://ollama.ai
ollama pull llama3.1:8b      # 4.7 GB — minimum recommended
ollama pull qwen2.5:14b      # 9 GB — better quality for complex letters

# Verify
curl http://localhost:11434/api/tags
```

### Whisper (STT)
```bash
# CPU warmup (downloads ~1.5 GB model on first use)
python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8')"

# GPU warmup
python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cuda', compute_type='float16')"
```

### PaddleOCR (scanned document OCR)
Models download automatically (~60 MB) on first `/documents/upload` with an image or scanned PDF.

---

## CI / Must-Pass Checks

```bash
# Full CI gate (also run before every deploy)
CI_SKIP_PIP_INSTALL=1 python scripts/ci_check.py

# Individual checks
python -m py_compile $(find app scripts tests -type f -name '*.py' | sort)
python scripts/test_command_contract.py
python scripts/eval_intent_seed.py --cases tests/ml/intent_cases.json   # 278 cases, 100% required
python scripts/test_transform_flow.py --transform-mode stub
python scripts/test_voice_flow.py --enforce-targets                      # 37 cases, 100% required
```

CI forces `COMMAND_INTENT_USE_LLM=false` to avoid Ollama dependency in automated tests.

---

## Command API

### Statuses
`applied` | `needs_clarification` | `error`

### Supported Actions
| Action | Example prompt |
|---|---|
| `REWRITE_CONTENT` | "Rewrite paragraph 2" |
| `EXPAND_CONTENT` | "Expand paragraph 3" |
| `SHORTEN_CONTENT` | "Shorten paragraph 1" |
| `CHANGE_TONE` | "Make paragraph 1 formal" |
| `REPLACE_TEXT` | "Replace paragraph 2 text" |
| `INSERT_TEXT` | "Add compliance sentence to paragraph 1" |
| `DELETE_TEXT` | "Delete last sentence from paragraph 2" |
| `SET_FORMAT` | "Make paragraph 1 bold", "font size 14 paragraph 2", "highlight subject" |
| `ADD_PARAGRAPH` | "Add new paragraph after paragraph 2" |
| `REMOVE_PARAGRAPH` | "Remove paragraph 3" |
| `INSERT_SECTION` | "Insert a new section" |
| `DELETE_SECTION` | "Delete this section" |
| `MOVE_SECTION` | "Move section after next" |
| `UNDO` | "Undo", "go back" |

Hindi/Hinglish supported: "para 2 formal karo", "isko chhota kar do", "bold karo"

### Error codes
| Code | Meaning |
|---|---|
| `version_conflict` | Stale version sent — refresh and retry |
| `intent_parse_error` | Command could not be parsed |
| `unsupported_action` | Action not yet supported |
| `patch_apply_failed` | Planner/apply stage failed |
| `could_not_understand_audio` | STT failed or empty audio |
| `no_previous_version` | UNDO with no prior version |

### Structured logging
One JSON log line per request: `event=command_request`

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `503 Doc-Engine not available` | `DOCENGINE_ENABLED=false` or doc-engine not running | Set `DOCENGINE_ENABLED=true`, start doc-engine on :8001 |
| GOI/DO letter has generic boilerplate text | Ollama not running or model not pulled | `ollama pull llama3.1:8b`, check `OLLAMA_BASE_URL` |
| Upload returns 400 on .pdf | PaddleOCR import error | Check `libgl1` installed in container; re-run `docker compose build` |
| `409 version_conflict` | Stale version sent | Fetch latest version from `GET /documents/{id}`, retry |
| `patch_apply_failed` | Doc-engine rejected patch | Check doc-engine logs; retry with simpler command |
| STT `needs_clarification` | Low confidence audio | Re-record; check `STT_CONFIRM_CONFIDENCE` threshold |
| CUDA errors | STT GPU init failure | Force CPU: `STT_DEVICE=cpu STT_COMPUTE_TYPE=int8` |
| DOCX export missing formatting | `DOCENGINE_ENABLED=false` | Doc-engine must be running for rich Lexical state |

---

## Project Structure

```
smark_clerk_ml_backend-main/
├── app/
│   ├── main.py                    # FastAPI app, all endpoints
│   ├── config.py                  # Settings (pydantic-settings)
│   ├── models.py                  # SQLAlchemy models
│   ├── schemas.py                 # Pydantic request/response schemas
│   ├── crud.py                    # DB helpers
│   ├── ml/
│   │   ├── slots/                 # Per-doc-type slot extractors
│   │   │   ├── goi_letter.py      # GOI letter: slots + body para drafting
│   │   │   ├── do_letter.py       # DO/INVITE/LEAVE letter drafting
│   │   │   ├── movement_order.py  # Movement order drafting
│   │   │   └── common.py          # Shared: run_slot(), draft_* helpers
│   │   └── rulebook_doctype.py    # Rulebook RAG + doc type classification
│   └── services/
│       ├── doc_importer.py        # Section extraction (DOCX/PDF/image) + DOCX export
│       ├── docengine_client.py    # httpx wrapper for doc-engine API
│       ├── action_bridge.py       # ML ActionObject → doc-engine command
│       ├── intent_extractor.py    # Command intent parsing (LLM + rule-based)
│       ├── content_transform.py   # Paragraph rewrite/expand/shorten (LLM + stub)
│       ├── lexical_wrapper.py     # Lexical JSON builder + formatter
│       ├── render_adapter.py      # Blueprint → flat render list
│       ├── template_store.py      # Saved letter templates (JSON files)
│       └── command_contract.py    # Intent contract tests
├── data/
│   ├── rulebook.pdf               # JSSD Vol I (place here manually)
│   ├── prompt_library/
│   │   ├── system_prompts/        # .txt files: content_generation_v2, rewrite_v2, etc.
│   │   └── few_shot_examples/     # .json files: intent_extraction_v1.json
│   ├── saved_templates/           # Saved letter templates (JSON, created at runtime)
│   └── ft_collected/              # Feedback JSONL (created at runtime)
├── scripts/
│   ├── ci_check.py                # Main CI gate
│   ├── eval_intent_seed.py        # Intent seed eval (278 cases)
│   ├── test_command_contract.py   # Command contract tests
│   ├── test_transform_flow.py     # Transform pipeline tests
│   └── test_voice_flow.py         # Voice pipeline tests (37 cases)
├── tests/ml/
│   ├── intent_cases.json          # 278 intent test cases
│   └── voice_cases_seed.json      # 37 voice eval cases
├── docker-compose.yml             # Postgres + Ollama + doc-engine + ml-pipeline
├── Dockerfile                     # ML pipeline container
├── requirements.txt               # Python deps
├── smart_clerk_test_ui.html       # Standalone test UI (open in browser)
└── docs/
    ├── ENDPOINTS.md               # All endpoints with examples + "won't work" cases
    └── ARCHITECTURE.md            # Learning guide: what/why/how

smartclerk-backend-docengine-main/
├── app/main.py                    # Doc-engine FastAPI app
├── blueprints/                    # Blueprint JSON files
│   ├── bp_goi_letter_v1.json
│   ├── bp_do_letter_v1.json
│   ├── bp_service_letter_v1.json
│   ├── bp_invitation_letter_v1.json
│   ├── bp_movement_order_v1.json
│   ├── bp_leave_certificate_v1.json
│   └── bp_flexible_v1.json        # For uploaded documents
└── section_catalog/               # Section type definitions (JSON)
```

---

## Release Checklist
See [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) for pre-release toggles, smoke tests, and rollback steps.

## Endpoint Reference
See [docs/ENDPOINTS.md](docs/ENDPOINTS.md) for all 22 endpoints with curl examples and common mistakes.

## Architecture Guide
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for a full learning guide: what was built, why, which libraries, and how everything connects.
