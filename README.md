# User Memory Filter for Open WebUI

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)
![OpenWebUI](https://img.shields.io/badge/Open_WebUI-Filter-green)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Backend-blue?logo=postgresql&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-yellow)

A persistent per-user memory for [Open WebUI](https://github.com/open-webui/open-webui): facts about the user are
extracted by a small local model after each reply, stored in one PostgreSQL table with a pgvector embedding per
fact, and the nearest ones are injected into the next request by vector search (reranked when Open WebUI has a
reranker configured). Embeddings come from Open WebUI's own configured embedding model. LangGraph was removed on
2026-10-18, the relevance model on 2026-10-24 (v7.0); the installed function id is `user_memory`.

## Author

**Beau D'Amore**  
[www.damore.ai](https://www.damore.ai)

## ✨ Features

- **Persistent Memory**: remembers identity, status, preferences, ownership, relationships, goals, skills and events
- **Vector retrieval, no relevance model**: the recent chat is embedded and the nearest facts are injected, ordered by Open WebUI's reranker when one is configured; the inlet makes no model call
- **De-duplication**: a new fact that is near-identical to an existing one of the same type (e.g. `job` vs `occupation`) updates that row instead of adding a second
- **Embeddings follow Open WebUI**: the filter uses whatever embedding model Open WebUI has configured and re-embeds every fact automatically when that model or its dimension changes
- **Reply never waits for extraction**: extraction runs after the reply as a background task
- **PII Protection, optional**: `pii_engine` = `none` (store exactly what was extracted), `regex` (built-in patterns) or `pii_tracer` (local PII-Tracer server)
- **One table**: `user_facts`, one row per fact keyed on user + type + subject plus its embedding, no framework, no checkpoint history

## 📁 Project Structure

```
openwebui-langgraph-memory-filter/      # folder name predates the LangGraph removal
├── README.md                    # This file
├── filter/
│   └── user_memory_filter.py    # The filter (installed copy = DB function user_memory)
├── docker/
│   ├── docker-compose-langgraph-pgvector.yml  # PostgreSQL 16 + pgvector (langgraph-pgvector, db langgraph_memory)
│   └── docker-compose.langgraph.yml           # The previous plain postgres:16 container (no vector extension)
├── docs/
│   ├── EXTRACTION_MODEL_SETUP.md     # Extraction model configuration
│   └── ...                           # Older design docs, each headed by a dated "superseded" note
└── prompt/
    ├── MERGE_PROMPT_memory-manager.md  # System prompt of the memory-manager model (extraction)
    └── INSTRUCTIONS.md                 # Model setup walkthrough
```

## 🚀 Quick Start

### 1. Start PostgreSQL with pgvector

```bash
cd docker
docker compose -f docker-compose-langgraph-pgvector.yml up -d
```

The server must have the `vector` extension available (image `pgvector/pgvector:pg16`); the filter runs
`CREATE EXTENSION IF NOT EXISTS vector` itself. Moving from the old plain `postgres:16` container: `pg_dump` the
`langgraph_memory` database out of it and restore into the new one; the filter adds the embedding column and
embeds every existing row on its first start. The database and role keep their `langgraph` names.

### 2. Install Dependencies

Nothing to install. The filter uses `psycopg` only, which Open WebUI ships with
(pinned in its own requirements). Default Open WebUI installs do not pip-install
frontmatter `requirements:` (`ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS` is off),
so the filter deliberately needs no extra package. Filter 5.1 and earlier needed
`psycopg-pool`; 5.2 opens one plain connection per query instead.

### 3. Install the Filter

1. Go to Open WebUI **Admin Panel** → **Functions**
2. Click **"+ Add Function"**, id `user_memory`
3. Paste the contents of `filter/user_memory_filter.py`
4. Save, enable the filter, and attach it to the models that should remember (model → Filters)

### 4. Configure the helper model and the embedder

See [docs/EXTRACTION_MODEL_SETUP.md](docs/EXTRACTION_MODEL_SETUP.md) and [prompt/INSTRUCTIONS.md](prompt/INSTRUCTIONS.md).

Quick version:
1. Create a workspace model `memory-manager` with the system prompt from `prompt/MERGE_PROMPT_memory-manager.md`;
   the filter's `extraction_model_id` valve defaults to that id.
2. Embeddings and reranking are not configured in the filter. It uses what Open WebUI has under
   **Admin Panel > Settings > Documents**: the embedding model (required) and the reranking model (optional; when set,
   it orders the retrieved facts). Changing the embedding model later is fine: the filter notices on its next start and
   re-embeds every fact.
3. Set the `postgres_url` valve (see below). Nothing else is required.

## 📖 Documentation

| Document | Description |
|----------|-------------|
| [EXTRACTION_MODEL_SETUP.md](docs/EXTRACTION_MODEL_SETUP.md) | Extraction model configuration |
| [prompt/INSTRUCTIONS.md](prompt/INSTRUCTIONS.md) | Model setup, flow and troubleshooting |
| [LANGGRAPH_SETUP.md](docs/LANGGRAPH_SETUP.md), [MEMORY_GRAPH_V4.md](docs/MEMORY_GRAPH_V4.md), [AI_DEVELOPMENT_GUIDE.md](docs/AI_DEVELOPMENT_GUIDE.md), [MEMORY_FILTER_V2_PLAN.md](docs/MEMORY_FILTER_V2_PLAN.md) | Historical (LangGraph era); each starts with a dated note on what no longer applies |

## 🔧 Requirements

- **Open WebUI**: >= 0.11.0 (0.12.3 in use)
- **Python**: 3.10+
- **PostgreSQL**: 16+ (provided via Docker)
- **Dependencies**: `psycopg[binary]` (bundled with Open WebUI, no install step)

## 🐳 Docker Setup

### PostgreSQL Container

```bash
cd docker
docker-compose -f docker-compose.langgraph.yml up -d
```

**Default Connection:**
- Server: valve `postgres_url`, `scheme://host` or `scheme://host:port` (default port 5432), e.g. `http://langgraph-pgvector` or `http://10.5.10.100:5432`
- Database: `langgraph_memory`
- User: `langgraph`
- Password: `langgraph_password_change_me`

## ⚙️ Configuration

The filter has these valves (2026-10-18). Everything else was removed as dead or harmful configuration;
see "How it works" below for what is fixed behaviour.

| Valve | Default | Description |
|-------|---------|-------------|
| `priority` | `10` | Filter order in Open WebUI (lower runs first) |
| `postgres_url` / `postgres_database` / `postgres_user` / `postgres_password` | (required) / `langgraph_memory` / `langgraph` / ... | Where the fact store lives. `postgres_url` is `scheme://host` or `scheme://host:port` (default port 5432), e.g. `http://langgraph-pgvector` or `http://10.5.10.100:5432`. The filter does not care how the database is deployed. |
| `extraction_model_id` | `memory-manager` | Workspace model that returns fact changes (prompt: `prompt/MERGE_PROMPT_memory-manager.md`) |
| `query_window_messages` | `6` | How many of the most recent user/assistant messages are embedded as the search text. `1` = the latest user message only |
| `relevance_threshold` | `0.45` | Minimum cosine similarity for a fact to be a candidate; `0` = no cutoff. Scale depends on the embedding model: with bge-m3 on short facts, unrelated facts measure 0.40-0.50 and on-topic ones 0.50-0.65 (2026-10-24), so cosine alone separates weakly and the reranker does the real ordering |
| `max_memories` | `20` | Top N candidates injected (after reranking when Open WebUI has a reranker, by similarity otherwise), not counting `always_inject_types`; also how many nearest facts the extraction model sees. `0` = no cap |
| `always_inject_types` | `["identity"]` | Fact types that are always injected, regardless of similarity |
| `dedupe_threshold` | `0.9` | Similarity above which a new fact of the same type with a different subject wording updates the existing row instead of adding one (`job` vs `occupation` measured 0.951 with bge-m3). `1` = off |
| `show_status` | `true` | Status line in the chat while memories load |
| `pii_engine` | `"regex"` | `none` stores facts exactly as extracted (no scrub, no check); `regex` = built-in patterns; `pii_tracer` = Perplexity's PII-Tracer model on a local server. `none` added in v7.0 |
| `pii_filter_mode` | `"remove"` | `remove` drops facts with PII; `redact` stores with `[REDACTED]` |
| `pii_scrub_input` | `true` | Scrub PII from messages before sending to extraction model (ignored with `none`) |
| `pii_patterns_enabled` | all 10 | Which PII types the **regex** engine detects (see PII Protection section) |
| `pii_tracer_url` | (empty) | PII-Tracer server URL, e.g. `http://pii-tracer:8193`; required with `pii_engine = pii_tracer`, unused otherwise. An empty URL counts as "server down" and `pii_tracer_fallback` applies |
| `pii_tracer_timeout` | `15` | Seconds per server call |
| `pii_tracer_labels` | 7 of 9 | Model labels treated as PII on the memory path. `private_person` and `private_date` are left out by default so names and birthdays stay storable as identity facts |
| `pii_tracer_fallback` | `"regex"` | Server down or timing out: `regex` uses the patterns for that update, `allow` treats the text as clean |
| `debug_mode` | `false` | Debug log lines |

Per-user valves: `enabled`, `show_status`.

Reply length of the helper model is set on its workspace preset (`max_tokens`), not by the filter.

## ⚙️ How it works (2026-10-24, v7.0)

**Inlet** (before the chat model sees the request; no model call):
1. First use only: open the store, create the extension/tables, and check the stored embedding model and
   dimension against Open WebUI's current one; on any difference, rebuild the `embedding` column and re-embed every row.
2. Embed the last `query_window_messages` user/assistant messages as one search text (waits for a still-running
   extraction from the previous turn).
3. `SELECT` the user's `always_inject_types` facts, plus the nearest other facts by cosine distance
   (`embedding <=> query`) with similarity ≥ `relevance_threshold`. When Open WebUI has a reranker configured
   (`request.app.state.RERANKING_FUNCTION`), the candidates are rescored by it; the top `max_memories` are kept.
4. Render the facts grouped by type and append them to the latest user message (see next section).

**Outlet** (after the reply is streamed): schedule a background task, return immediately. The task embeds the
(scrubbed) latest user message and sends `memory-manager` today's date, the `always_inject_types` facts plus the
`max_memories` nearest existing facts, the previous 3 user messages as context and the latest user message, and asks
only for what changes: new facts, updated facts (same type+subject), and `"remove": true` entries. Nothing changes
on most turns and the reply is `{"facts": []}`. Each change is embedded (`subject: value`) and applied to its own row:
removal is a `DELETE`; a new `(type, subject)` whose embedding is ≥ `dedupe_threshold` similar to an existing fact of
the same type is applied to that row instead (logged as *merged*); otherwise `INSERT ... ON CONFLICT` writes value,
sentiment, confidence and embedding only when something differs. Empty or unparseable replies write nothing.
A per-user lock serialises the task with the next inlet. Only user messages are sent; system prompts carry persona
text that would be misread as facts.

**Storage**: table `user_facts` (`user_id, type, subject, value, sentiment, confidence, first_mentioned,
last_updated, embedding vector(N)`, primary key user + type + subject, HNSW cosine index when N ≤ 2000) and
`user_facts_meta` (one row: `embedding_model`, `embedding_dim`). N is whatever Open WebUI's embedding model returns.

Measured before 2026-10-18 (43 turns): the inlet blocked each reply for a median 36.5 s (extraction 27 s,
relevance 10 s). Since 2026-10-18 extraction is off the reply path; since 2026-10-24 the inlet makes no model call at
all (one embedding call, one SQL query, an optional reranker call).

## 📍 Where the memories are injected (2026-10-16)

The inlet adds the recalled memories to the outgoing request only; the stored chat never contains them. They are
appended to the latest user message under a `[Relevant memories about the user]` header. The persona system prompt,
tool specs and chat history therefore stay byte-identical from turn to turn, so the server reuses its prompt cache
(llama.cpp reuses the longest identical token prefix); only the block and the new message are prefilled. This works
with every chat template and survives Open WebUI's tool-call continuation requests, which are not filtered again.

There is no placement valve (removed 2026-10-18). The alternatives were measured and rejected: a separate system
message after the user turn is merged by Open WebUI into the first system message before sending, and prepending to
the system prompt re-prefills the whole conversation whenever the recalled set changes (65 s at 12.5k tokens).

Server side, `--checkpoint-min-step 2048` on the llama.cpp chat server bounds the redo when the prompt changes mid-way
(details in `~/Source/local-llm-config/README.md`, "Prompt cache, checkpoints and the memory filter").
The installed copy is the DB function `user_memory` and equals `filter/user_memory_filter.py`.

## 🧪 Testing

### Test Database Connection

```bash
docker exec -it langgraph-postgres psql -U langgraph -d langgraph_memory -c "SELECT 1;"
```

### View Stored Memories

```bash
docker exec -it langgraph-postgres psql -U langgraph -d langgraph_memory -c \
  "SELECT user_id, count(*) AS facts, max(last_updated) FROM user_facts GROUP BY user_id;"
docker exec -it langgraph-postgres psql -U langgraph -d langgraph_memory -c \
  "SELECT type, subject, value, sentiment, confidence, first_mentioned, last_updated
   FROM user_facts WHERE user_id = '<user id>' ORDER BY first_mentioned;"
# fix one fact by hand
docker exec -it langgraph-postgres psql -U langgraph -d langgraph_memory -c \
  "UPDATE user_facts SET value = 'in 50s', last_updated = now()
   WHERE user_id = '<user id>' AND type = 'identity' AND subject = 'age';"
```

### PII engine offline test (v7.0)
`tests/test_pii_engine.py` fakes the PII-Tracer server, the extraction model and the database and checks both engines, the
label defaults (names kept), remove/redact, both fallbacks, and one whole background update. Run it inside the Open WebUI
container (it has psycopg, aiohttp and open_webui):

```
docker cp filter/user_memory_filter.py open-webui:/tmp/ && docker cp tests/test_pii_engine.py open-webui:/tmp/
docker exec -e PYTHONPATH=/app/backend open-webui python /tmp/test_pii_engine.py /tmp/user_memory_filter.py   # ends with ALL PII ENGINE TESTS PASSED
# (importing the filter imports open_webui, whose startup logging appears first; the read-only static-file errors are harmless)
```

### Watch it work

```bash
docker logs -f open-webui 2>&1 | grep "User Memory"
```

## � PII Protection

The filter includes a **3-layer defense-in-depth** system to prevent storage of personally identifiable information:

### Layer 1: Prompt Guardrails
The extraction model's system prompt explicitly instructs it to never extract PII. Examples are provided showing how to extract non-PII meaning from PII-containing messages (e.g., "I bought a house at 123 Oak Lane" → store `ownership: house, recently purchased a house`).

### Layer 2: Pre-Scrub
Before messages reach the extraction model, all PII is replaced with `[REDACTED]`. The LLM never sees raw PII data.
What counts as PII is decided by the engine chosen with `pii_engine` (below).

### Layer 3: Post-Extraction Validation
Every extracted fact is scanned for PII in both `subject` and `value` fields, plus a subject blocklist. Facts with PII are either dropped or redacted based on config. Same engine as layer 2.

### The three settings (`pii_engine`; `regex`/`pii_tracer` since v6.1, `none` since v7.0)

- **`none`**: no pre-scrub and no post-check; facts are stored exactly as the extraction model returns them. For
  setups that do not want any PII handling in the memory path.
- **`regex`** (default): the ten built-in patterns in the table below, selectable with `pii_patterns_enabled`. No network, no model, no names: a pattern cannot tell a person's name from any other words, so names were never matched and identity facts survive.
- **`pii_tracer`**: [PII-Tracer](https://huggingface.co/perplexity-ai/PII-Tracer), Perplexity's 0.6B PII detector, running as a local server (set its URL in `pii_tracer_url`; the companion `openwebui-pii-tracer-filter` can use the same server for the chat path, but the two filters are independent of each other). It reads context, so it also finds account numbers, secrets, addresses and other values that have no fixed shape. Each pre-scrubbed message and each extracted fact's subject and value is one call to `POST /v1/pii/detect`; findings whose label is in `pii_tracer_labels` are redacted. By default `private_person` and `private_date` are **not** in that list, so "name = Beau" or a birthday is still stored, as with the regex engine; add them for a memory that keeps no names at all. With the model engine the log names the label of a blocked fact, never the matched text (the regex engine's log lines are unchanged and still quote the match). If the server cannot be reached, `pii_tracer_fallback` decides: `regex` (default) runs the patterns for that update and logs the failure once, `allow` treats the text as clean.

Switching engines does not touch stored facts; it only changes what the next updates scrub and block.

### Detected PII Types (regex engine)

| Pattern | Example |
|---------|---------|
| Social Security Number | `123-45-6789` |
| Credit Card | `4111-1111-1111-1111` |
| Phone Number | `(555) 123-4567` |
| Email Address | `user@example.com` |
| Street Address | `123 Main Street Apt 4B` |
| Passport Number | `Passport: A12345678` |
| Driver's License | `Driver's license: D1234567` |
| Bank Account | `Account #: 123456789` |
| Date of Birth | `DOB: 01/15/1990` |
| IP Address | `192.168.1.1` |

### What IS Stored
- Personality traits, preferences, opinions
- Ownership types ("owns a Tesla" — not the VIN)
- Relationships ("married to Sarah" — not her phone number)
- Goals, skills, interests, hobbies
- Professional info ("engineer at Acme" — not employee ID)

## 📊 Storage

One row per fact in table `user_facts` of database `langgraph_memory`:

| Column | Type | Meaning |
|---|---|---|
| `user_id` | text | Open WebUI user id |
| `type` | text | fact type, stored lower-cased |
| `subject` | text | fact subject, stored lower-cased |
| `value` | text | the fact |
| `sentiment` | text, nullable | `positive`, `negative`, `neutral` |
| `confidence` | real, default 0.8 | model's confidence |
| `first_mentioned` | timestamptz | row insert time |
| `last_updated` | timestamptz | last time value, sentiment or confidence changed |

Primary key `(user_id, type, subject)`, so the prompt tells the model to give each item of the same kind its own
subject (`pet_pepper`, `pet_bunny_boy`). The filter creates the table on first use. Each change from the
extraction model is one `INSERT ... ON CONFLICT DO UPDATE` (only when something differs) or one `DELETE`, all in
one transaction; there is no history. History of the layout: until 2026-10-18 the data lived in LangGraph
checkpoint tables (279 rows for 15 facts, a full state copy per write); from 2026-10-18 to 2026-10-19 it was one
row per user with a `facts` JSONB list, which made single-fact edits positional and stamped every fact's
`last_updated` on every write; on 2026-10-19 the 27 facts were migrated to one row each.

## 🤖 For AI Assistants

The whole filter is one file, `filter/user_memory_filter.py`. Rules that matter when changing it:
- Anything that reads user intent (forget, update, what is a fact) goes through the extraction model and its
  prompt, never through regex or keyword matching in code.
- Prompt changes go into the prompt file **and** the matching `model` table row.
- No valves for hypothetical setups: one working path. The installed copy is the DB function `user_memory`;
  keep it byte-identical to the repo file.
- [docs/AI_DEVELOPMENT_GUIDE.md](docs/AI_DEVELOPMENT_GUIDE.md) is historical (schema versions, LangGraph).

## 🐛 Troubleshooting

### Common Errors

**`ImportError: no pq wrapper available`**
```bash
pip install "psycopg[binary]"
```

**`Error loading module: user_memory: No module named 'psycopg_pool'`** on create

You are installing filter 5.1 or older. Use 5.2+, which has no `psycopg-pool`
dependency. (Workaround for the old version: `pip install psycopg-pool` inside the
container; it is lost when the container is recreated.)

**`TypeError: expected string or bytes-like object, got 'list'`** in `_update_user_memory_state`, or
**`request (N tokens) exceeds the available context size`** from the relevance model
Happened when a chat message had an image attached: Open WebUI then sends `content` as a list of
parts (text + base64 `image_url`) instead of a string, which broke the PII regex scrub and pushed the
whole image into the relevance prompt. Fixed in the filter (2026-10-14): `content_to_text()` keeps only
the text parts and drops image data. There is no per-message length cap (removed 2026-10-18): the
filter never sees attached files (Open WebUI runs file retrieval after the filter inlet), so the only
oversized input is a raw paste. Keep **Paste Large Text as File** enabled in Settings > Interface; a raw
paste larger than the helper model's context makes that helper call fail and the filter skips the
memory update for that turn without writing anything.

**Existing facts disappear after a chat**
The extraction model is only asked for changes and the filter upserts its output onto the stored facts: same
type+subject updates the fact and keeps `first_mentioned`, new type+subject is appended, and facts the model did
not return are kept. A fact is dropped only when the model returns it with `"remove": true` (or `"status":
"removed"`). The memory-manager preset's system prompt must match this contract (it is tracked in
[prompt/MERGE_PROMPT_memory-manager.md](prompt/MERGE_PROMPT_memory-manager.md) and installed in the `model` table).

**Forget commands.** Both "forget everything you know about me" and targeted forgets such as
"forget my cars" go through the extraction model, which must return each matching fact (or every fact)
with `"remove": true`. The log line `Merge upsert: ... removed` shows what it did. Memory extraction for that message
was skipped, so re-send the fact in a new message.

## 📝 License

MIT License

## 🙏 Acknowledgments

- [Open WebUI](https://github.com/open-webui/open-webui) - The amazing AI chat interface
- [psycopg](https://www.psycopg.org/) - PostgreSQL driver
