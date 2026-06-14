[//]: # (markdownlint-disable MD013 MD033 MD041)
# P-RAG Codebase Guide

A practical reference for developers. Explains what each file does, why it exists, and how files connect to each other.

---

## Project Structure

```
P-RAG/
├── main/
│   ├── main.py                  ← FastAPI application entry point
│   ├── auth0middleware.py        ← JWT authentication middleware
│   └── member_info_provider.py  ← Member plan data resolver
│
├── clients/
│   └── client.py                ← Core query orchestration engine
│
├── insurance_mcp/
│   ├── server.py                ← MCP server + external agent tools
│   └── tools.py                 ← Scoring and retrieval logic
│
├── utility/
│   ├── category.py              ← Query category detection (medical/dental/vision/rx)
│   ├── topic_resolver.py        ← Insurance topic and keyword extraction
│   ├── response_builder.py      ← Structured table builder (cost + info)
│   ├── prompts.py               ← LLM prompt templates
│   └── utils.py                 ← Shared helper functions
│
├── indexers/
│   ├── indexer.py               ← Master indexer orchestrator
│   ├── run_indexer.py           ← Shared indexer runner
│   ├── medical_indexer.py       ← Medical booklet parser
│   ├── dental_indexer.py        ← Dental booklet parser
│   ├── vision_indexer.py        ← Vision booklet parser
│   └── sbc_indexer.py           ← SBC document parser
│
├── infrastructure/
│   ├── blob_storage.py          ← Azure Blob Storage client
│   ├── cache.py                 ← Redis cache client
│   ├── db.py                    ← PostgreSQL / SQLite client
│   ├── post_index.py            ← Post-indexing cloud upload helper
│   └── service_bus.py           ← Azure Service Bus client
│
├── tests/
│   ├── golden_test.py           ← End-to-end regression test suite
│   └── baselines/               ← Saved baseline responses per query
│
├── insurance-frontend/          ← React/TypeScript MFE chat UI
│   ├── src/App.tsx              ← Main chat component
│   └── vite.config.ts           ← Vite + Module Federation config
│
├── config.py                    ← Environment-aware settings loader
└── .env                         ← Local dev secrets (gitignored)
```

---

## Core Files

---

### `main/main.py` — FastAPI Application

**What it does:**
The entry point for the entire application. Defines all HTTP endpoints and wires together authentication, member info resolution, and the query pipeline.

**Key endpoints:**
```
GET  /health          → Health check (no auth required)
GET  /member-info     → Resolve member plan details from member API
POST /chat            → Main benefit query endpoint
```

**Request flow for POST /chat:**
```
1. Auth0Middleware validates JWT token
2. Parse form fields: prompt, member_info, history, current_category
3. Call get_ai_response() from client.py
4. Return JSON: { answer, pages, source }
```

**Key design decisions:**
- `member_info` can come from the request body (UI sends it) OR be fetched fresh via `member_key` + `group_number` (external agents use this)
- Auth0 validation is skipped in dev when `AUTH0_DOMAIN` is not set
- `/health` is excluded from auth to support load balancer health checks

**Connects to:** `client.py` (get_ai_response), `auth0middleware.py`, `member_info_provider.py`

---

### `clients/client.py` — Query Orchestration Engine

**What it does:**
The brain of the system. Takes a member query and orchestrates the entire pipeline from category detection to final response. This is where the 3-tier token optimization lives.

**Pipeline (in order):**
```
1. is_conversational()          → Return guidance if greeting/non-benefit query
2. detect_category()            → medical / dental / vision / rx (rule-based first, LLM fallback)
3. resolve_insurance_topic()    → Extract topics + keywords from query
4. get_plan_data_from_disk()    → Direct retrieval (no LLM tool overhead)
5. build_cost_table()           → Structured table parser (Tier 1 — 0 tokens)
6. LLM synthesis                → Natural language response (Tier 2/3 — only when needed)
```

**Key constant:**
```python
DIRECT_CATEGORIES = {"medical", "dental", "vision", "rx"}
```
These bypass LLM tool orchestration entirely — direct function call to `tools.py`. Future categories like `claims` will use LLM tool orchestration.

**Why this matters:**
- `medical/dental/vision/rx` queries: 0 tokens for retrieval
- LLM only used for category fallback (~100 tokens), topic fallback (~150 tokens), and synthesis (~300 tokens when needed)
- Average: 30-150 tokens vs 1,500-5,000 for traditional RAG

**Connects to:** `category.py`, `topic_resolver.py`, `tools.py`, `response_builder.py`, `prompts.py`

---

### `insurance_mcp/server.py` — MCP Server + External Tools

**What it does:**
Exposes the benefit query system as an MCP (Model Context Protocol) server so external AI agents can call it as a tool. Also provides a direct REST-accessible tool for agent-to-agent workflows.

**Two tools exposed:**
```python
query_insurance_benefits(query, topics, category, keywords, member_info)
    → Raw chunk retrieval from index (used internally by client.py)

query_benefits(query, member_key, group_number)
    → Full end-to-end response including synthesis (used by external agents)
```

**Design:** Server is intentionally thin. All scoring logic was extracted to `tools.py` to avoid circular imports and keep the MCP server focused on protocol handling.

**Connects to:** `tools.py` (scoring), `client.py` (get_ai_response for query_benefits)

---

### `insurance_mcp/tools.py` — Scoring and Retrieval Logic

**What it does:**
The retrieval and scoring engine. Given a query, topics, category, and keywords — finds the right plan index, scores all chunks, and returns the top-ranked results.

**Key function:**
```python
get_plan_data_from_disk(query, topics, category, keywords, member_info)
```

**What it does internally:**
```
1. Parse member_info → extract plan details (year, group, variant)
2. Query SQLite/PostgreSQL master index → find correct JSON index file
3. Load index (from Redis cache or blob/local file)
4. Score every chunk:
   - Topic match score (600 points for exact match)
   - Keyword match score (400 points per phrase, 200 per word)
   - Non- prefix penalty (reduces score for "Non-Emergency" when querying ER)
5. Select top chunks by score
6. Build SECTION: COST and SECTION: INFO output
```

**Scoring is deterministic** — same query always returns same chunks. No LLM involved in retrieval or scoring. This is why dollar amounts are always accurate.

**Connects to:** `db.py` (plan lookup), `cache.py` (index loading), `blob_storage.py` (prod index download)

---

## Utility Files

---

### `utility/category.py` — Category Detection

**What it does:**
Determines which plan type (medical/dental/vision/rx) a query belongs to. Uses rule-based detection first, falls back to mini-LLM only when rules fail.

**Two functions:**
```python
is_conversational(query)
    → True for greetings, follow-ups, non-benefit queries
    → Short-circuits the entire pipeline, returns guidance message

detect_category(query_words, query)
    → Rule-based: checks dental/vision/rx/medical keyword lists
    → LLM fallback: ~100 tokens, returns single category word
```

**Rule-based coverage:** ~85% of queries matched without LLM
**LLM fallback:** Only for ambiguous queries like "is metformin covered?" where it's unclear if asking about medical or rx

**Connects to:** `client.py` (called first in pipeline), `prompts.py` (LLM prompt template)

---

### `utility/topic_resolver.py` — Topic and Keyword Extraction

**What it does:**
Translates member language into insurance document terminology. This is where domain knowledge lives — the mappings that make "cleaning" find "prophylaxis" and "ER" find "Emergency Room".

**Key function:**
```python
resolve_insurance_topic(query_words, query_lower)
    → Returns { topics: [...], keywords: [...] }
```

**Examples:**
```
"how much is a cleaning"  → topics: ["class i"], keywords: ["cleaning", "prophylaxis", "diagnostic and preventive"]
"how much is an ER visit" → topics: ["emergency"], keywords: ["emergency room", "emergency"]
"how much is an apicoectomy" → topics: ["apicoectomy"], keywords: ["apicoectomy"]
```

**Why it matters:**
This is your competitive advantage. Without this mapping layer, an LLM would have to guess that "cleaning" = "prophylaxis" on every query — costing tokens and risking errors. The rule-based resolver does it instantly at zero cost.

**Connects to:** `client.py` (called after category detection)

---

### `utility/response_builder.py` — Structured Table Builder

**What it does:**
Converts raw scored chunks into formatted markdown tables for the UI. This is the Tier 1 parser — handles most queries with zero LLM tokens.

**Two functions:**
```python
build_cost_table(context, query, keywords)
    → Builds the "Benefit / Service / In-Network / Out-of-Network / Limitations" table
    → Applies relevance filter to remove unrelated rows
    → Returns (markdown_table, page_numbers)

build_info_response(context, query, keywords)
    → Builds the "Topic / Coverage Information" table
    → Filters to only relevant info chunks
    → Returns (markdown_table, page_numbers)
```

**Key design:** Dollar amounts are extracted directly from the structured index — never generated by LLM. This guarantees 100% numerical accuracy.

**Connects to:** `client.py` (called after retrieval, before LLM synthesis)

---

### `utility/prompts.py` — LLM Prompt Templates

**What it does:**
Centralizes all LLM prompts used across the system. Single source of truth for prompt engineering.

**Key prompts:**
```python
TOPIC_EXTRACTION_PROMPT      → Asks LLM to extract insurance topics from query
GUIDANCE_CONVERSATIONAL      → Response for greetings and non-benefit queries
GUIDANCE_NO_CATEGORY         → Response when query category cannot be determined
GUIDANCE_MEDICAL_VAGUE       → Response when medical query is too vague
GUIDANCE_DENTAL_VAGUE        → Response when dental query is too vague
GUIDANCE_VISION_VAGUE        → Response when vision query is too vague
```

**Connects to:** `client.py`, `category.py`

---

## Infrastructure Files

---

### `infrastructure/db.py` — Database Client

**What it does:**
Master plan index lookup. Given plan attributes (year, group_number, plan_category, variant) — returns the path to the correct JSON index file.

**Dev:** SQLite at `indexers/p_insurance_index.db`
**Prod:** Azure PostgreSQL via asyncpg connection pool

**Key function:**
```python
get_index_path(year, plan_category, group_number, plan, plan_type, variant, network)
    → Returns local file path (dev) or blob path (prod)
```

---

### `infrastructure/cache.py` — Redis Cache Client

**What it does:**
Caches JSON index files in Redis so repeated queries don't re-download from blob storage. Event-driven invalidation — cache is cleared when a document is re-indexed.

**Dev:** In-process Python dict (no Redis needed)
**Prod:** Azure Redis Cache

**Key functions:**
```python
get_index(redis_key)    → Returns cached chunks or None
set_index(redis_key, chunks)  → Stores chunks in cache
invalidate_index(redis_key)   → Deletes cache entry (called on re-index)
```

---

### `infrastructure/blob_storage.py` — Azure Blob Storage Client

**What it does:**
Handles all PDF and JSON index file operations in Azure Blob Storage.

**Two containers:**
- `insurance-pdfs` — Source PDF booklets uploaded by admin
- `insurance-indices` — Generated JSON index files

**Key functions:**
```python
download_index(blob_path)          → Download JSON index file
upload_index(blob_path, chunks)    → Upload newly generated index
download_pdf(blob_path, temp_path) → Download PDF for indexing
list_pdf_blobs(prefix)             → List PDFs with metadata for change detection
get_blob_last_modified(blob_path)  → Check if PDF changed since last index
```

**Dev fallback:** All functions check if `AZURE_BLOB_CONNECTION_STRING` is set. If not, they use local file system silently.

---

### `infrastructure/post_index.py` — Post-Index Upload Orchestrator

**What it does:**
After an indexer generates a JSON file, this orchestrates the cloud upload chain:
1. Upload JSON to Azure Blob
2. Upsert PostgreSQL master index with blob path + redis key
3. Send Service Bus message to trigger cache invalidation

**Key function:**
```python
post_index_upload(local_json_path, year, group_number, plan_category, plan, ...)
    → Uploads to blob, updates PostgreSQL, sends Service Bus message
    → No-op in dev (logs a message and returns)
```

**Blob path structure:**
```
{year}/{group_number}/{plan_category}/{plan_name_slug}_index.json
e.g. 2026/1000016/medical/premera_employees_ppo_retiree_index.json
```

---

### `infrastructure/service_bus.py` — Cache Invalidation Messenger

**What it does:**
Sends a message to Azure Service Bus when a document is re-indexed. An Azure Function reads this message and deletes the corresponding Redis cache key — ensuring members always get fresh data after a re-index.

**Cache invalidation flow:**
```
Indexer re-indexes PDF
    → post_index.py uploads new JSON to blob
    → service_bus.py sends { redis_key, blob_path, reason: "reindexed" }
    → Azure Function receives message
    → Deletes Redis key
    → Next query: Redis miss → download from blob → cache → serve fresh
```

**Dev:** No-op (no Service Bus connection)

---

## Test Files

---

### `tests/golden_test.py` — End-to-End Regression Suite

**What it does:**
Calls the real `/chat` API for every query in the test suite and compares results against saved baselines. The only way to catch regressions before they reach production.

**Two modes:**
```bash
python -m tests.golden_test --capture   # Save current responses as baselines
python -m tests.golden_test --verify    # Compare current responses to baselines
```

**Pass/fail rules:**
```
HARD FAIL → Any dollar amount, copay, or coinsurance value changed
REVIEW    → Answer changed but no cost values changed (approve/reject interactively)
PASS      → Response matches baseline exactly
```

**Test coverage:**
```
medical            → 34 queries
dental_willamette  → 38 queries
dental_premera     → 17 queries
vision             → 15 queries
Total              → 104 queries
```

**Why it matters:**
Every code change runs this suite before check-in. A cost value regression is a hard block — no exceptions. This is what makes 100% numerical accuracy a guarantee, not a claim.

---

## Indexer Files

---

### `indexers/indexer.py` — Master Indexer Orchestrator

**What it does:**
Walks all plan PDF folders, classifies each document, generates the index, and uploads to cloud. This is what runs daily in production via an Azure Container Job.

**Dev:** Reads from `docs/2026/` local folder, writes to `indices/` local folder
**Prod:** Reads PDFs from Azure Blob, writes JSON indices to Azure Blob, updates PostgreSQL

**Change detection (prod only):**
```
For each PDF blob:
    if blob.last_modified > last_indexed (from PostgreSQL):
        → re-index this PDF
    else:
        → skip (no change)
```

---

### `indexers/{medical/dental/vision/sbc}_indexer.py` — Plan-Specific Parsers

**What each does:**
Parses a specific type of PDF booklet into structured JSON chunks.

| File | Parses | Output |
|------|--------|--------|
| `medical_indexer.py` | Medical benefit booklet | Cost chunks (copay/coinsurance per service) + info chunks (coverage descriptions) |
| `dental_indexer.py` | Dental booklet (Premera + Willamette) | D-code procedure entries + class I/II/III descriptions |
| `vision_indexer.py` | Vision plan booklet | Exam + hardware cost entries |
| `sbc_indexer.py` | Summary of Benefits (SBC) | Summary cost table entries |

**Each indexer has:**
```python
PLAN_CATEGORY = "medical"  # or dental/vision/sbc

classify_document(pdf_path) → dict   # LLM extracts plan metadata
generate_sub_index(output_path, pdf_path) → list  # Parses PDF into chunks

if __name__ == "__main__":
    from indexers.run_indexer import run
    run(PLAN_CATEGORY, classify_document, generate_sub_index)
```

---

## Configuration

---

### `config.py` — Settings Loader

**What it does:**
Python equivalent of .NET's `Program.cs` configuration system. Loads environment variables in the correct order and exposes a typed `settings` object used by all files.

**Load order (later overrides earlier):**
```
1. .env.{APP_ENV}   ← non-sensitive environment config (checked in)
2. .env             ← local dev secrets (gitignored, never deployed)
3. OS env vars      ← Azure Key Vault secrets injected by pipeline (always win)
```

**Usage:**
```python
from config import settings

model = settings.OLLAMA_MODEL
is_prod = settings.is_production  # True when AZURE_BLOB_CONNECTION_STRING is set
```

**Key property:**
```python
@property
def is_production(self) -> bool:
    return bool(self.AZURE_BLOB_CONNECTION_STRING)
```
This single flag switches between dev (SQLite + local files) and prod (PostgreSQL + Blob + Redis) without any code changes.

---

## How Files Connect — Request Flow

```
Browser/Agent
    ↓ POST /chat
main.py
    ↓ validates JWT (auth0middleware.py)
    ↓ resolves member info (member_info_provider.py)
    ↓ calls get_ai_response()
client.py
    ↓ is_conversational? → return guidance (prompts.py)
    ↓ detect_category() → medical/dental/vision/rx (category.py)
    ↓ resolve_insurance_topic() → topics + keywords (topic_resolver.py)
    ↓ get_plan_data_from_disk() → scored chunks (tools.py)
        ↓ get_index_path() → find JSON file (db.py)
        ↓ get_index() → load chunks (cache.py → blob_storage.py)
        ↓ score_chunks() → rank by relevance
    ↓ build_cost_table() → markdown table (response_builder.py)
    ↓ LLM synthesis → natural language (ollama/prompts.py) [only if needed]
    ↓ return { answer, pages, source }
main.py
    ↓ return JSON response
Browser/Agent
```

---

## Environment Variables Quick Reference

| Variable | Used By | Dev Default |
|----------|---------|-------------|
| `APP_ENV` | config.py | `development` |
| `OLLAMA_MODEL` | category.py, client.py | `llama3.1` |
| `AUTH0_DOMAIN` | auth0middleware.py | blank (skips auth) |
| `AUTH0_AUDIENCE` | auth0middleware.py | blank (skips auth) |
| `POSTGRES_DSN` | db.py | blank (SQLite fallback) |
| `AZURE_BLOB_CONNECTION_STRING` | blob_storage.py, indexer.py | blank (local fallback) |
| `REDIS_CONNECTION_STRING` | cache.py | blank (dict fallback) |
| `AZURE_SERVICE_BUS_CONNECTION_STRING` | service_bus.py | blank (no-op) |
| `MEMBER_INFO_API_URL` | member_info_provider.py | blank (hardcoded demo member) |