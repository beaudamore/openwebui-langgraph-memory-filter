# User Memory Filter for Open WebUI

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)
![OpenWebUI](https://img.shields.io/badge/Open_WebUI-Filter-green)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Backend-blue?logo=postgresql&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-yellow)

A persistent per-user memory for [Open WebUI](https://github.com/open-webui/open-webui): facts about the user are
extracted by a small local model after each reply, stored in one PostgreSQL table, and the relevant ones are
injected into the next request. LangGraph was removed on 2026-10-18 (see [Storage](#-storage)); the installed
function id is `user_memory`.

## Author

**Beau D'Amore**  
[www.damore.ai](https://www.damore.ai)

## ✨ Features

- **Persistent Memory**: remembers identity, status, preferences, ownership, relationships, goals, skills and events
- **Relevance filtering**: a second small model picks which facts matter for the current conversation
- **Reply never waits for extraction**: extraction runs after the reply as a background task
- **PII Protection**: 3-layer defense-in-depth filtering prevents storage of phone numbers, SSNs, addresses, credit cards, and other personally identifiable information — while still storing personality traits, preferences, and ownership types
- **One table**: `user_facts`, one row per fact keyed on user + type + subject, no framework, no checkpoint history

## 📁 Project Structure

```
openwebui-langgraph-memory-filter/      # folder name predates the LangGraph removal
├── README.md                    # This file
├── filter/
│   └── user_memory_filter.py    # The filter (installed copy = DB function user_memory)
├── docker/
│   └── docker-compose.langgraph.yml  # PostgreSQL container (langgraph-postgres, db langgraph_memory)
├── docs/
│   ├── EXTRACTION_MODEL_SETUP.md     # Extraction model configuration
│   └── ...                           # Older design docs, each headed by a dated "superseded" note
└── prompt/
    ├── MERGE_PROMPT_memory-manager.md  # System prompt of the memory-manager model (extraction)
    ├── RELEVANCE_PROMPT.md             # System prompt of the memory-relevance model
    └── INSTRUCTIONS.md                 # Model setup walkthrough
```

## 🚀 Quick Start

### 1. Start PostgreSQL

```bash
cd docker
docker-compose -f docker-compose.langgraph.yml up -d
```

The container, database and role keep their original `langgraph` names; only the table changed.

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

### 4. Configure the two helper models

See [docs/EXTRACTION_MODEL_SETUP.md](docs/EXTRACTION_MODEL_SETUP.md) and [prompt/INSTRUCTIONS.md](prompt/INSTRUCTIONS.md).

Quick version:
1. Create a workspace model `memory-manager` with the system prompt from `prompt/MERGE_PROMPT_memory-manager.md`
2. Create a workspace model `memory-relevance` with the system prompt from `prompt/RELEVANCE_PROMPT.md`
3. The filter's `extraction_model_id` and `relevance_model_id` valves default to those two ids

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
- Host: `localhost` (or `langgraph-postgres` from other containers)
- Port: `5432`
- Database: `langgraph_memory`
- User: `langgraph`
- Password: `langgraph_password_change_me`

## ⚙️ Configuration

The filter has these valves (2026-10-18). Everything else was removed as dead or harmful configuration;
see "How it works" below for what is fixed behaviour.

| Valve | Default | Description |
|-------|---------|-------------|
| `priority` | `10` | Filter order in Open WebUI (lower runs first) |
| `postgres_host` / `postgres_port` / `postgres_database` / `postgres_user` / `postgres_password` | `langgraph-postgres` / `5432` / `langgraph_memory` / `langgraph` / ... | Where the fact store lives |
| `extraction_model_id` | `memory-manager` | Workspace model that returns fact changes (prompt: `prompt/MERGE_PROMPT_memory-manager.md`) |
| `relevance_model_id` | `memory-relevance` | Workspace model that picks relevant facts (prompt: `prompt/RELEVANCE_PROMPT.md`) |
| `relevance_filter_enabled` | `true` | Off = inject every stored fact |
| `always_inject_types` | `["identity"]` | Fact types that skip the relevance model |
| `show_status` | `true` | Status line in the chat while memories load |
| `pii_filter_enabled` | `true` | Enable PII detection and filtering |
| `pii_filter_mode` | `"remove"` | `remove` drops facts with PII; `redact` stores with `[REDACTED]` |
| `pii_scrub_input` | `true` | Scrub PII from messages before sending to extraction model |
| `pii_patterns_enabled` | all 10 | Which PII types to detect (see PII Protection section) |
| `debug_mode` | `false` | Debug log lines |

Per-user valves: `enabled`, `show_status`.

Reply length of the two helper models is set on their workspace presets (`max_tokens`), not by the filter.

## ⚙️ How it works (2026-10-18)

**Inlet** (before the chat model sees the request):
1. Read the user's facts from PostgreSQL (waits for a still-running extraction from the previous turn).
2. Send the non-identity facts as `{type, subject, value}` plus the last 6 messages to `memory-relevance`, which
   answers with the `{type, subject}` keys it picked. Identity facts always pass. Any failure injects all facts.
3. Render the picked facts grouped by type and append them to the latest user message (see next section).

**Outlet** (after the reply is streamed): schedule a background task, return immediately. The task sends
`memory-manager` today's date, the stored facts, the previous 3 user messages as context and the latest user message, and asks
only for what changes: new facts, updated facts (same type+subject), and `"remove": true` entries. Nothing changes
on most turns and the reply is `{"facts": []}`. Each change is applied to its own row (omitted facts are
kept, removal is a `DELETE`, a fact identical to the stored one is not written). Empty or unparseable replies write nothing.
A per-user lock serialises the task with the next inlet. Only user messages are sent; system prompts carry persona
text that would be misread as facts.

Measured before this change (2026-10-16 to 10-18, 43 turns): the inlet blocked each reply for a median 36.5 s
(extraction 27 s, because the model echoed all 15 facts every turn; relevance 10 s, because it copied full
fact objects back). Both are now out of the reply path or reduced to key lists.

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

### Watch it work

```bash
docker logs -f open-webui 2>&1 | grep "User Memory"
```

## � PII Protection

The filter includes a **3-layer defense-in-depth** system to prevent storage of personally identifiable information:

### Layer 1: Prompt Guardrails
The extraction model's system prompt explicitly instructs it to never extract PII. Examples are provided showing how to extract non-PII meaning from PII-containing messages (e.g., "I bought a house at 123 Oak Lane" → store `ownership: house, recently purchased a house`).

### Layer 2: Regex Pre-Scrub
Before messages reach the extraction model, all PII is replaced with `[REDACTED]`. The LLM never sees raw PII data.

### Layer 3: Post-Extraction Validation
Every extracted fact is scanned for PII in both `subject` and `value` fields, plus a subject blocklist. Facts with PII are either dropped or redacted based on config.

### Detected PII Types

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
