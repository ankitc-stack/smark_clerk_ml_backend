# Smart Clerk Architecture Guide

A learning guide covering what was built, why each decision was made, and how every component connects. Written for a developer joining the project mid-way.

---

## Table of Contents

1. [What Is Smart Clerk?](#1-what-is-smart-clerk)
2. [Two-Service Architecture](#2-two-service-architecture)
3. [Document Lifecycle](#3-document-lifecycle)
4. [Blueprints and Section Types](#4-blueprints-and-section-types)
5. [Slot Extraction and LLM Body Drafting](#5-slot-extraction-and-llm-body-drafting)
6. [Command Pipeline (AI Editing)](#6-command-pipeline-ai-editing)
7. [Upload and OCR Pipeline](#7-upload-and-ocr-pipeline)
8. [Formatting: Lexical JSON](#8-formatting-lexical-json)
9. [DOCX Export](#9-docx-export)
10. [Voice Pipeline](#10-voice-pipeline)
11. [Saved Templates and Feedback](#11-saved-templates-and-feedback)
12. [Libraries and Why We Chose Them](#12-libraries-and-why-we-chose-them)
13. [Key Design Decisions and Trade-offs](#13-key-design-decisions-and-trade-offs)
14. [Known Limitations](#14-known-limitations)
15. [Phase History](#15-phase-history)

---

## 1. What Is Smart Clerk?

Smart Clerk is an AI-assisted document drafting tool for Indian Army officers. Officers write official letters (GOI letters, service letters, DO letters, movement orders, leave certificates) according to strict formats defined in the **JSSD Vol I rulebook** (Joint Secretary's Services Directive, 2025 Edition).

The key insight: officers spend a lot of time formatting. The content is usually clear in their heads — the friction is the rigid layout, numbering conventions, dual dates, salutation rules, and formal language style. Smart Clerk removes that friction:

- Upload a scanned letter → it's extracted, structured, and ready to edit
- Say "make paragraph 1 formal" → done via AI
- Click "Generate DO Letter about sports meet" → a correctly structured draft appears in seconds
- Export → download a properly formatted DOCX

---

## 2. Two-Service Architecture

### Why two services?

A single monolith was the original v1. We split into two services because:

1. **Separation of concerns**: Document structure (blueprints, section types, ordering rules) is stable and well-defined. AI/LLM logic (intent extraction, paragraph rewriting, OCR) is experimental and changes often. Keeping them separate means the doc-engine can be locked and reliable while the ML pipeline is iterated on freely.

2. **Structured storage**: The doc-engine stores documents as **structured Lexical JSON** per section — not as a flat text blob. This makes it possible to bold a single word, number paragraphs correctly, enforce blueprint ordering rules, and export to DOCX with full formatting intact. A flat text store cannot do this without re-parsing.

3. **Versioning at two levels**: The doc-engine has its own version counter (for PATCH safety). The ML pipeline has its own version counter (for command undo, user history). They stay in sync via the `docengine_version` field in every response.

### How they communicate

```
Officer  →  POST /documents/generate
                 ↓
           ML Pipeline (port 8000)
           app/services/docengine_client.py  (httpx)
                 ↓  HTTP
           Doc-Engine (port 8001)
           - Creates document from blueprint
           - Stores sections as Lexical JSON
           - Returns DocumentData
                 ↑
           ML Pipeline patches each section (PATCH /sections/{id})
           Generates DOCX (doc_importer.py)
           Returns BlueprintDocResponse to officer
```

`docengine_client.py` is a thin httpx wrapper. Every method maps one-to-one with a doc-engine endpoint: `create_document`, `get_document`, `patch_section`, `apply_command`, `insert_section`, `delete_section`, `move_section`.

If `DOCENGINE_ENABLED=false`, the ML pipeline falls back to legacy flat-text storage. This was kept for zero-downtime rollback.

---

## 3. Document Lifecycle

### Generate path
```
POST /documents/generate
  → voice or text → slot extraction (regex)
  → LLM body para drafting (Ollama)
  → docengine_client.create_document(template_id)
  → for each section: docengine_client.patch_section(section_id, lexical_json)
  → crud.create_document() → SQL row in ML pipeline DB
  → crud.add_version() → first version in ML pipeline DB
  → generate_plain_docx() → DOCX file on disk
  → return BlueprintDocResponse
```

### Upload path
```
POST /documents/upload
  → detect file type (ext)
  → extract_sections_from_docx / _from_pdf / _from_image
  → _detect_sections() → [{type, text, confidence}]
  → docengine_client.create_document("tmpl_flex_001")
  → for each detected section: INSERT_SECTION + patch_section
  → same SQL + DOCX as generate path
  → return BlueprintDocResponse
```

### Edit path (command)
```
POST /documents/{id}/command
  → fetch current ML version
  → parse input (text → intent extractor; voice → Whisper STT → intent extractor)
  → route by action type:
      structural (ADD_PARA, DELETE_SECTION, MOVE_SECTION, INSERT_SECTION)
        → action_bridge.py → docengine_client.apply_command()
      content (REWRITE, EXPAND, SHORTEN, CHANGE_TONE)
        → content_transform.py → LLM or stub → docengine_client.patch_section()
      format (SET_FORMAT, alignment)
        → lexical_wrapper.py → apply style to Lexical nodes → patch_section()
      UNDO
        → ML pipeline restores previous ML version state
  → crud.add_version() → new ML version
  → return CommandAppliedResponse
```

### Export path
```
GET /documents/{id}/export?format=docx
  → fetch current ML version → doc_state
  → doc_state comes from doc-engine (sections with Lexical JSON)
  → sections_for_render() → flat list with text + richtext_state
  → generate_plain_docx() → python-docx builds .docx
  → serve file
```

---

## 4. Blueprints and Section Types

### What is a blueprint?

A blueprint is a JSON file in `smartclerk-backend-docengine-main/blueprints/` that defines:
- Which section types are required vs. optional
- The correct ordering of sections
- Whether extras (additional paragraphs) are allowed
- Layout hints (e.g., ref_number inline-left, date inline-right)
- Style defaults (font, margins)
- AI hints (tone, example paragraph for LLM guidance)

Example: `bp_goi_letter_v1.json` enforces that a GOI letter has `letterhead`, `reference_number`, `date`, `receiver_block`, body `paragraph`s, `signee_block`, and (optionally) `copy_to`.

### What is a section type?

A section type is defined in `smartclerk-backend-docengine-main/section_catalog/`. Each JSON file describes:
- The `type` identifier (e.g., `"paragraph"`, `"subject"`, `"signee_block"`)
- `category`: header / body / footer / meta
- `content`: default Lexical JSON state
- `meta`: editable, repeatable, locked, ai_generatable, voice_editable flags

The doc-engine uses section catalog entries as the starting template when a new section is inserted.

### Section types in this project

| Type | Description |
|---|---|
| `letterhead` | Unit address, phone, fax at top |
| `reference_number` | File number (e.g., "No 45/Estt/2026") |
| `date` | Document date |
| `receiver_block` | Addressee(s) |
| `subject` | Subject line (ALL CAPS, bold) |
| `salutation` | "Sir," / "Dear Sir," (DO letters only) |
| `paragraph` | Body paragraph (numbered) |
| `signee_block` | Signature block (name, rank, appointment) |
| `security_classification` | "UNCLASSIFIED" / classification level |
| `precedence` | "IMMEDIATE" / "PRIORITY" / "ROUTINE" |
| `copy_to` | Copy distribution list |
| `distribution_list` | Wider distribution |
| `enclosure` | Enclosure list |
| `annexure_block` | Annexure reference |
| `endorsement` | Endorsement block |
| `remarks_block` | Remarks |
| `noo` | "NOT ON ORIGINAL" stamp |

### Two version numbers

Every response has both:
- `version` → ML pipeline version (integer, for `/command` calls)
- `docengine_version` → doc-engine version (integer, for `PATCH /sections` calls)

Both must be kept in sync. The ML version tracks user history (undo, revert). The doc-engine version is an optimistic lock — if you send a stale version, it rejects with 409.

---

## 5. Slot Extraction and LLM Body Drafting

### Why two-stage generation?

1. **Slot extraction (regex + rule-based)**: Pulls structured fields — reference number, date, addressee name, rank, unit — directly from the prompt using regex. This is fast, deterministic, and never hallucinates. No LLM needed for "No 45/Estt/2026, dt 17 Mar 2026".

2. **Body paragraph drafting (LLM)**: The numbered paragraphs need natural language. A small LLM (llama3.1:8b) drafts these from the officer's prompt.

### How slot extraction works

Each document type has its own slot module:
- `app/ml/slots/goi_letter.py` — handles GOI letters
- `app/ml/slots/do_letter.py` — handles DO, invitation, leave letters
- `app/ml/slots/movement_order.py` — handles movement orders
- `app/ml/slots/common.py` — shared `run_slot()`, `draft_numbered_paras()`, etc.

The flow:
```python
# 1. Try regex extraction
result = _regex_fallback_goi(prompt)
# → {"reference_number": "45/Estt/2026", "date": "17 Mar 2026", ...}

# 2. Draft body paragraphs with LLM
paras = draft_numbered_paras(prompt, schema_hint, min_paras=1, max_paras=3)

# 3. Detect off-topic response (word-boundary match vs. prompt keywords)
if _is_off_topic(paras, prompt):
    paras = draft_numbered_paras(prompt, retry_task)  # simpler retry

# 4. Merge into section content dict
result["paragraph_1"] = paras[0]
result["paragraph_2"] = paras[1] if len(paras) > 1 else ""
```

### The off-topic detection problem

llama3.1:8b (a small 8B parameter model) tends to copy example text from the task prompt verbatim. For instance, if the task says "Example output: I write to bring your attention to the sports meet..." and the officer's prompt is about budget allocation, the model sometimes generates the example text instead of the actual content.

Fix: `_is_off_topic(paras, prompt)` extracts significant words (4+ characters, not stopwords) from the officer's prompt, then does word-boundary regex matching on the generated text. If none of the prompt's significant words appear in the output → it's off-topic → retry with a simpler direct prompt.

**Critical**: Use `re.search(r"\b(?:word1|word2)\b", combined)` — NOT `"word" in combined`. Python's `in` operator does substring matching: `"ration" in "consideration"` is `True`.

### Dual Saka date (GOI letters)

GOI letters require both Gregorian and Indian National Calendar (Saka) date by JSSD rulebook mandate.

```python
from convertdate import indian_civil
y, m, d = indian_civil.from_gregorian(dt.year, dt.month, dt.day)
# "17 Mar 2026" → "26 Phalguna 1947 Saka"
```

Library: `convertdate==2.4.0` (pure Python, no C deps).

---

## 6. Command Pipeline (AI Editing)

### Intent extraction

`app/services/intent_extractor.py` parses a natural language command into an `ActionObject`:

```python
ActionObject(
    action=CommandAction.REWRITE_CONTENT,
    scope=CommandScope.PARAGRAPH,
    target=ActionTarget(para_index=0),  # "paragraph 1" → index 0
    params=ActionParams(),
    confidence=0.92,
)
```

Two modes:
- **Rule-based fallback** (default, `COMMAND_INTENT_USE_LLM=false`): regex patterns + keyword matching. Fast, deterministic, used in CI. Handles 278 documented cases at 100% accuracy.
- **LLM mode** (`COMMAND_INTENT_USE_LLM=true`): sends the command to Ollama with few-shot examples. Better for complex/ambiguous commands. Slower.

### Routing by action type

`app/main.py` routes after intent extraction:

```
Structural ops (ADD_PARAGRAPH, DELETE_SECTION, MOVE_SECTION, INSERT_SECTION):
  → action_bridge.py maps ML ActionObject → doc-engine command format
  → docengine_client.apply_command(de_doc_id, de_version, command_payload)
  → doc-engine handles the structural change atomically

Content ops (REWRITE_CONTENT, EXPAND_CONTENT, SHORTEN_CONTENT, CHANGE_TONE):
  → content_transform.py calls LLM (or stub)
  → returns new text
  → lexical_wrapper.text_to_lexical_node(new_text)
  → docengine_client.patch_section(section_id, de_version, lexical_json)

Format ops (SET_FORMAT):
  → lexical_wrapper._apply_style_to_node(node, style)
  → modifies Lexical node format bitmask + CSS style string
  → docengine_client.patch_section(...)

UNDO:
  → ML pipeline finds previous ML version
  → restores doc_state from that version (doc-engine state is NOT rolled back — a new version is created)
```

### Why content ops bypass the doc-engine command endpoint

The doc-engine's `/command` endpoint handles structural changes (insert/delete/move sections). It does NOT run LLMs — it's a structural store. For content transforms (rewrite, expand, shorten), the ML pipeline calls the LLM itself, then pushes the result back via PATCH. This keeps the doc-engine's LLM-free design intact.

### Version conflict handling

Both the ML pipeline and doc-engine use optimistic concurrency:
- Client sends the version they last saw
- If the server's version has advanced → 409 version_conflict
- Client must re-fetch and retry

The `auto_retry: true` flag in `CommandRequest` lets the ML pipeline do one automatic version-refresh retry for pure version drift.

---

## 7. Upload and OCR Pipeline

`app/services/doc_importer.py` handles all extraction.

### Three extraction paths

**DOCX (`python-docx`)**:
```python
doc = DocxDocument(path)
paragraphs = [p.text.strip() for p in doc.paragraphs]
```
Fast, lossless for digitally created DOCX files.

**Digital PDF (`PyMuPDF / fitz`)**:
```python
page.get_text("text")
```
Fast text extraction. Preserves paragraph breaks. No OCR needed.

**Scanned PDF / Image (`PaddleOCR`)**:
- For PDFs: renders each page to an image (2× zoom, ~144 dpi) using PyMuPDF, then runs OCR
- For images: directly passed to PaddleOCR
- PaddleOCR is loaded as a lazy singleton (`_get_ocr()`) — first call downloads models (~60 MB), subsequent calls reuse the loaded model in memory

### Why PaddleOCR over Tesseract?

PaddleOCR (PP-OCRv4) has significantly better accuracy for dense text layouts — important for military letters with tight spacing, stamps, and handwritten annotations. It also handles skewed/rotated pages via its angle classifier. Tesseract needs careful preprocessing (deskew, binarize) for comparable accuracy.

**ARM64 note**: `paddlepaddle` must be installed from PaddlePaddle's own wheel channel before `requirements.txt` on ARM (Apple Silicon / ARM servers):
```bash
pip install paddlepaddle==2.6.2 -f https://www.paddlepaddle.org.cn/whl/linux/cpu-aarch64/stable.html
```

### Section detection

`_detect_sections(paragraphs)` scans paragraphs top-to-bottom and assigns section types using regex + heuristics:

| Signal | Section type | Confidence |
|---|---|---|
| Starts with reference number pattern | `reference_number` | 0.9 |
| Contains date pattern (1-2 digits + month name + 4-digit year) | `date` | 0.9 |
| `SUBJECT:` or `Sub:` prefix | `subject` | 0.95 |
| Starts with digit + `.` or `)` (numbered para) | `paragraph` | 0.9 |
| ALL CAPS line only (e.g., "IMMEDIATE") | `precedence` | 0.95 |
| "Sir," / "Dear Sir," alone on a line | `salutation` | 0.95 |
| "Copy to" / "Distr" prefix | `copy_to` / `distribution_list` | 0.9 |
| Last meaningful block before EOF | `signee_block` | 0.75 |
| Anything before subject not matched above | `receiver_block` | 0.75 |
| Unmatched body text | `paragraph` | 0.5 |

Sections with `confidence < 0.6` are flagged with `"low_confidence": True` — the frontend can show these differently.

---

## 8. Formatting: Lexical JSON

### Why Lexical JSON?

Lexical (by Meta) is the rich text format used by the doc-engine's frontend editor. Every section's content is stored as a Lexical JSON tree:

```json
{
  "root": {
    "type": "root",
    "children": [
      {
        "type": "paragraph",
        "children": [
          {
            "type": "text",
            "text": "This is ",
            "format": 0
          },
          {
            "type": "text",
            "text": "bold text",
            "format": 1,
            "style": "font-size: 14pt; color: #FF0000;"
          }
        ]
      }
    ]
  }
}
```

**Format bitmask**: `1=bold`, `2=italic`, `4=strikethrough`, `8=underline`, `16=code`, `32=subscript`, `64=superscript`

**Style string**: CSS key-value pairs — `font-family`, `font-size`, `color`, `background-color` (highlight)

### How formatting is applied

`app/services/lexical_wrapper.py`:
- `_apply_style_to_node(node, style_dict)` — modifies format bitmask + CSS string
- `text_to_lexical_node(text)` — wraps plain text as a minimal Lexical node (no formatting)
- `lexical_nodes_to_rich_text(state)` — reads Lexical → returns plain text + style kwargs
- `_parse_css / _serialize_css` — lightweight CSS key-value parse/serialize (no external dep)

The `SET_FORMAT` command path:
1. Intent extractor extracts `{"bold": True}` or `{"font_size": 14}` into `ActionParams.style_params`
2. ML pipeline fetches current section Lexical state from doc-engine
3. `_apply_style_to_node` walks all text nodes and applies the style
4. `patch_section` pushes the modified Lexical state back

### Highlight storage

Highlight was the last formatting feature added. The DOCX renderer maps any highlight to `WD_COLOR_INDEX.YELLOW` — python-docx only supports 16 fixed colors (Word limitation), not arbitrary hex colors.

---

## 9. DOCX Export

`app/services/doc_importer.py` → `generate_plain_docx(sections, title, output_path)`.

### Why generate DOCX in the ML pipeline?

The doc-engine stores structure. For rendering to DOCX we need:
1. Military numbering rules (1., 1.1., 1.1.1.)
2. Section-specific styles (subject: bold+underline centered; precedence: right-aligned bold)
3. Footer with security classification + page numbering
4. Inline formatting from Lexical state (bold/italic/font/highlight)

These are application-level rendering concerns, not generic document-store concerns. So the ML pipeline handles them.

### Rendering pipeline

```python
sections_for_render(doc_state)
  → flat list: [{type, text, alignment, richtext_state}]
     ↑ richtext_state = Lexical root dict (None for legacy/plain sections)

generate_plain_docx(sections, title, output_path):
  doc = Document()
  for sec in sections:
      rs = sec.get("richtext_state")

      if sec.type == "subject":
          p = doc.add_paragraph()
          p.alignment = CENTER
          if rs and _has_inline_format(rs):
              _apply_runs_to_para(p, first_para_node)  # preserve custom formatting
          else:
              run = p.add_run(text.upper())
              run.bold = True; run.underline = True   # always bold+underline

      elif sec.type == "paragraph":
          # Numbered paragraph with hanging indent
          p = doc.add_paragraph()
          if rs and _has_inline_format(rs):
              _apply_runs_to_para(p, lx_para_node)    # rich runs
          else:
              for line in text.splitlines():
                  p.add_run(line)

      # ... other section types

  # Footer: security classification + page number (from page 2)
  section.different_first_page_header_footer = True
  fp = section.footer.paragraphs[0]
  fp.add_run(sec_class_text).bold = True
```

### `_apply_runs_to_para`

Walks a Lexical paragraph node's children and adds python-docx `Run` objects:
```python
run = p.add_run(text)
if fmt & 1: run.bold = True
if fmt & 2: run.italic = True
if fmt & 8: run.underline = True
# CSS style string → font size, font name, highlight color
```

---

## 10. Voice Pipeline

`ENABLE_VOICE_INPUT=true` enables voice command payloads.

Flow:
```
POST /documents/{id}/command
  input.type = "voice"
  input.audio_base64 = "<base64 WAV/WebM>"
  input.mime_type = "audio/wav"
       ↓
  base64_decode → write to temp file
       ↓
  faster-whisper (large-v3-turbo) → transcript + confidence
       ↓
  if confidence < STT_MIN_CONFIDENCE (0.25):
      return status=error, code=could_not_understand_audio
  if confidence < STT_CONFIRM_CONFIDENCE (0.45):
      return status=needs_clarification, transcript=... (user confirms)
  else:
      transcript → intent extractor → same pipeline as text command
```

### Why faster-whisper?

`faster-whisper` is a reimplementation of OpenAI Whisper using CTranslate2 — 4× faster than the original, with int8 quantization for CPU. The `large-v3-turbo` model is ~1.5 GB and runs on CPU in real-time for short audio clips (5-30 second commands).

Hindi/Hinglish support: Whisper large-v3-turbo handles mixed Hindi-English naturally. The intent extractor has Hindi/Hinglish patterns: "para 2 formal karo" → `CHANGE_TONE`, "isko chhota kar do" → `SHORTEN_CONTENT`.

---

## 11. Saved Templates and Feedback

### Saved templates (`app/services/template_store.py`)

Officers can save the structure of a good letter for reuse:
- `POST /documents/{id}/save-as-template` → saves section schema to `data/saved_templates/{type}_{8hex}.json`
- Static content (letterhead, signee_block) is preserved; variable content (subject, paragraphs, date) is cleared
- `POST /documents/from-template/{template_id}` → creates a new blank doc pre-wired with the saved structure

Why JSON files instead of a database table? Saved templates are user-specific, infrequently accessed, and don't need querying beyond list/load/delete. A directory of JSON files is simpler, survives schema migrations, and can be backed up with `cp -r`.

### RLHF-lite feedback (`POST /documents/{id}/feedback`)

Every AI-generated output can be rated thumbs-up/down with an optional correction. Records are appended to `data/ft_collected/feedback.jsonl`:

```json
{"feedback_id":"...","doc_id":"...","version_id":1,"rating":"down","field":"paragraph_1","correction":"Should start with 'I am directed to...'","timestamp":"2026-03-17T..."}
```

This is preparation for future fine-tuning. The JSONL format is directly compatible with Unsloth + TRL SFTTrainer for QLoRA fine-tuning.

---

## 12. Libraries and Why We Chose Them

| Library | Version | Purpose | Why chosen |
|---|---|---|---|
| **FastAPI** | 0.115 | HTTP framework | Async-native, Pydantic integration, auto Swagger |
| **pydantic** | 2.8 | Request/response validation | Fast, type-safe, v2 API is cleaner than v1 |
| **pydantic-settings** | 2.4 | Config from env vars + .env | Zero boilerplate for settings with defaults |
| **sqlalchemy** | 2.0 | ORM + DB session management | Async-compatible, type-safe in 2.0 |
| **httpx** | 0.27 | HTTP client for doc-engine calls | Async-native, same interface as requests |
| **python-docx** | 1.1 | DOCX generation | Only mature Python DOCX builder; supports runs/styles/footers |
| **PyMuPDF (fitz)** | 1.24 | PDF text extraction + page rendering | Faster and more accurate than pdfminer; renders pages to images for OCR |
| **PaddleOCR** | <3.0 | OCR for scanned PDFs and images | Better accuracy than Tesseract for dense layouts; angle-aware |
| **paddlepaddle** | 2.6.2 | PaddleOCR backend | Required by PaddleOCR; CPU-only build used |
| **Pillow** | 10.4 | Image loading/preprocessing | Required by PaddleOCR; also used for PDF page rendering |
| **faster-whisper** | 1.1 | Speech-to-text (STT) | 4× faster than original Whisper; int8 CPU mode; Hindi support |
| **fastembed** | 0.4 | Text embeddings for rulebook RAG | Runs locally, no API calls, ~80ms per chunk |
| **pgvector** | 0.3 | Vector similarity search in Postgres | Extensions for cosine similarity; needed for rulebook RAG |
| **convertdate** | 2.4 | Gregorian → Saka Indian calendar | Pure Python; correct Indian National Calendar conversion |
| **tenacity** | 9.0 | Retry logic with backoff | Used for Ollama LLM retries on timeout |
| **docxtpl** | 0.17 | Jinja2-based DOCX template fill | Used for older template-based generation (pre-doc-engine) |
| **psycopg[binary]** | 3.2 | Async PostgreSQL driver | psycopg3 is the current standard; psycopg2 is legacy |
| **numpy** | 2.0 | Array operations for embeddings | Required by fastembed and PaddleOCR |

### Libraries considered but not used

| Library | Why not used |
|---|---|
| **Tesseract / pytesseract** | Lower accuracy for dense layouts; needs system-level `tesseract` install; no angle correction |
| **LangChain** | Too much abstraction over Ollama calls; we only need single-turn chat + embeddings — direct httpx is clearer |
| **HuggingFace transformers** | Too heavy; PaddleOCR + faster-whisper cover our specific needs without 10+ GB of CUDA deps |
| **spaCy** | Overkill for our intent parsing; rule-based regex is faster and 100% accurate on the 278 documented cases |
| **Redis** | No need for cross-process caching yet; single-worker deployment with in-process lru_cache suffices |

---

## 13. Key Design Decisions and Trade-offs

### 1. Rule-based intent extraction by default

`COMMAND_INTENT_USE_LLM=false` means intent extraction uses regex + keyword matching. The LLM path is opt-in.

**Why**: The 278 intent test cases cover the real-world command vocabulary. Rule-based gets 100% on all of them, runs in <1ms, and never hallucinates a wrong action. The LLM path is useful for complex/ambiguous commands that regex can't handle, but adds 200-500ms latency and Ollama dependency.

**Trade-off**: New command phrasings not covered by the regex bank return `needs_clarification` instead of resolving. The fix is to add patterns to the regex bank, not to always use LLM.

### 2. Lexical JSON as the canonical format

All section content is stored as Lexical JSON in the doc-engine. The ML pipeline stores the same Lexical state in its `doc_state` JSONB column.

**Why**: Lexical is the format the doc-engine frontend editor uses. Storing it natively means zero conversion on read — the editor can display it directly. Conversion only happens at DOCX export time.

**Trade-off**: Lexical JSON is verbose (~10× larger than plain text). A 3-paragraph letter can be 15 KB of JSON. For this scale it's fine.

### 3. python-docx for DOCX export, not LibreOffice

We use python-docx directly for export, not LibreOffice headless conversion.

**Why**: LibreOffice adds ~2s per export, requires `soffice` binary, and can't be controlled at the run-level (bold/italic/font/footer must all be set via the DOCX XML, which LibreOffice sometimes re-interprets). python-docx gives precise control over every XML element.

**Trade-off**: We must manually implement every formatting feature (numbered paragraphs, hanging indents, footer page numbers, section-specific styles). But for a domain-specific tool with well-defined formats, this is acceptable — the output is exactly what the rulebook requires.

### 4. Local-only, no external API calls

All AI features run locally: Ollama (LLM), faster-whisper (STT), PaddleOCR (OCR), fastembed (embeddings).

**Why**: The Army's documents are classified. Sending content to an external API (OpenAI, Anthropic, Google) is a security non-starter. Local models avoid this entirely.

**Trade-off**: Quality is lower than GPT-4 or Claude for complex drafting. llama3.1:8b sometimes generates generic boilerplate instead of prompt-specific content (hence `_is_off_topic` retry). With `qwen2.5:14b` or `llama3.1:70b`, quality improves significantly but hardware requirements jump.

### 5. Optimistic concurrency (not locking)

Both the ML pipeline and doc-engine use version-based optimistic concurrency instead of DB-level locks.

**Why**: A single officer is almost always the only writer for a given document. Locking would add latency with no benefit. On the rare case of a conflict (same officer on two tabs), the 409 response is clear and the user simply refreshes.

---

## 14. Known Limitations

### LLM quality (llama3.1:8b)
Small models copy example text, generate generic boilerplate, and occasionally hallucinate names/dates. The `_is_off_topic` retry helps but doesn't fully solve it. Upgrading to `qwen2.5:14b` (~9 GB) or `llama3.1:70b` (~40 GB) would significantly improve quality.

### OCR on low-quality scans
PaddleOCR handles most scanned letters well, but very low-resolution or heavily stamped documents may extract garbled text. Confidence values in the detected sections help identify these.

### Section detection edge cases
`_detect_sections` uses heuristics. Unusual layouts (no subject line, non-standard numbering, merged letterhead+address) may produce wrong section types. The `low_confidence` flag helps the frontend highlight these.

### DOCX highlight colors
python-docx only supports 16 fixed Word highlight colors (`WD_COLOR_INDEX`). Any highlight (regardless of hex color stored in Lexical) maps to `YELLOW` in DOCX export. This is a python-docx/Word limitation.

### No real-time collaboration
Documents are single-writer. Concurrent edits from two sessions will version-conflict. Not a current requirement.

### Feedback loop not yet connected to fine-tuning
`data/ft_collected/feedback.jsonl` collects ratings and corrections, but the fine-tuning pipeline (Unsloth + TRL SFTTrainer) is not yet implemented. The data is being collected for future use.

---

## 15. Phase History

Understanding which phase each feature was built in helps trace why certain design decisions were made.

| Phase | Feature | Key files added/changed |
|---|---|---|
| **1-3** | Core: document generation, rule-based intent, command contract | `app/main.py`, `app/ml/`, `scripts/ci_check.py` |
| **4** | Whisper STT integration, voice commands | `app/services/stt.py`, `tests/ml/voice_cases_seed.json` |
| **5** | Rulebook RAG, doc type classification, embeddings | `app/ml/rulebook_doctype.py`, `app/services/embed.py` |
| **6** | Doc-engine microservice integration + Upload (DOCX/PDF/image/OCR) | `app/services/docengine_client.py`, `app/services/doc_importer.py`, `app/services/action_bridge.py`, `smartclerk-backend-docengine-main/blueprints/bp_flexible_v1.json` |
| **7** | Saved letter templates | `app/services/template_store.py`, 3 endpoints in `main.py` |
| **8** | Service letter spec alignment (Appendix E JSSD): salutation + security_classification sections | `section_catalog/salutation.json`, `section_catalog/security_classification.json`, `bp_service_letter_v1.json` |
| **9** | RLHF-lite feedback endpoint + signee block rulebook fix | `POST /documents/{id}/feedback`, `FeedbackRequest` schema |
| **10** | Rulebook gap closures: dual Saka date, subject no trailing dot, colon-dash rule, GOI signee, DOCX footer (security + page number), PRECEDENCE section, NOO section, Personal Application detection, GOI copy_to format | `goi_letter.py`, `doc_importer.py`, `section_catalog/precedence.json`, `section_catalog/noo.json`, `requirements.txt` (convertdate) |
| **Post-10** | Inline formatting end-to-end (bold/italic/underline/font/size/highlight in DOCX export) | `lexical_wrapper.py`, `doc_importer.py` (`_apply_runs_to_para`, `_has_inline_format`) |
| **Post-10** | DO/LEAVE/INVITE letter off-topic retry fix | `do_letter.py` (`_is_off_topic`, word-boundary regex), `movement_order.py` |
