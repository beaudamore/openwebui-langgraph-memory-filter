# LangGraph Memory Filter for Open WebUI

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)
![OpenWebUI](https://img.shields.io/badge/Open_WebUI-Filter-green)
![LangGraph](https://img.shields.io/badge/LangGraph-State_Machine-orange)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Backend-blue?logo=postgresql&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-yellow)

A sophisticated persistent memory system for [Open WebUI](https://github.com/open-webui/open-webui) that extracts and maintains user information across conversations using LangGraph and PostgreSQL.

## Author

**Beau D'Amore**  
[www.damore.ai](https://www.damore.ai)

## ✨ Features

- **Persistent Memory**: Remembers user preferences, relationships, goals, interests, and personal information
- **PII Protection**: 3-layer defense-in-depth filtering prevents storage of phone numbers, SSNs, addresses, credit cards, and other personally identifiable information — while still storing personality traits, preferences, and ownership types
- **Preference Evolution Tracking**: Tracks how user preferences change over time (e.g., "used to like coffee → now prefers tea")
- **PostgreSQL Backend**: Reliable, persistent storage with LangGraph checkpoint support
- **Multi-Model Support**: Works with GPT-4, Llama, Qwen, Gemini, and other extraction models
- **Schema Versioning**: Built-in migration system for data structure evolution

## 📁 Project Structure

```
memory_langgraph/
├── README.md                    # This file
├── DEPENDENCY_RESOLUTION.md     # Dependency troubleshooting guide
├── test_imports.py              # Dependency verification script
├── filter/
│   ├── langgraph_memory_filter.py    # Main filter code
│   └── requirements-langgraph.txt    # Python dependencies
├── docker/
│   └── docker-compose.langgraph.yml  # PostgreSQL container
├── docs/
│   ├── LANGGRAPH_SETUP.md            # Full installation guide
│   ├── EXTRACTION_MODEL_SETUP.md     # Extraction model configuration
│   ├── AI_DEVELOPMENT_GUIDE.md       # Guide for AI assistants
│   └── ...
└── prompt/
    └── EXTRACTION_PROMPT.md          # Extraction model system prompt
```

## 🚀 Quick Start

### 1. Start PostgreSQL

```bash
cd docker
docker-compose -f docker-compose.langgraph.yml up -d
```

### 2. Install Dependencies

```bash
# In your Open WebUI container
docker exec -it <open-webui-container> pip install \
  "langgraph~=0.6.11" \
  "langgraph-sdk<0.4" \
  "langgraph-checkpoint-postgres<3" \
  "psycopg[binary]" \
  psycopg-pool
```

### 3. Install the Filter

1. Go to Open WebUI **Admin Panel** → **Functions**
2. Click **"+ Add Function"**
3. Paste the contents of `filter/langgraph_memory_filter.py`
4. Save and enable the filter

### 4. Configure Extraction Model

See [docs/EXTRACTION_MODEL_SETUP.md](docs/EXTRACTION_MODEL_SETUP.md) for detailed instructions.

Quick version:
1. Create a custom model in Open WebUI with the system prompt from `prompt/EXTRACTION_PROMPT.md`
2. Set the filter's `extraction_model_id` valve to your model's ID

## 📖 Documentation

| Document | Description |
|----------|-------------|
| [LANGGRAPH_SETUP.md](docs/LANGGRAPH_SETUP.md) | Complete installation and setup guide |
| [EXTRACTION_MODEL_SETUP.md](docs/EXTRACTION_MODEL_SETUP.md) | Extraction model configuration |
| [AI_DEVELOPMENT_GUIDE.md](docs/AI_DEVELOPMENT_GUIDE.md) | Guide for AI assistants modifying this code |
| [MEMORY_FILTER_V2_PLAN.md](docs/MEMORY_FILTER_V2_PLAN.md) | Planned conversation-memory and per-speaker summary architecture |
| [DEPENDENCY_RESOLUTION.md](DEPENDENCY_RESOLUTION.md) | Dependency troubleshooting |

## 🔧 Requirements

- **Open WebUI**: Latest version
- **Python**: 3.10+
- **PostgreSQL**: 16+ (provided via Docker)
- **Dependencies**:
  ```
  langgraph~=0.6.11          # not 1.x: see DEPENDENCY_RESOLUTION.md (websockets downgrade)
  langgraph-sdk<0.4
  langgraph-checkpoint-postgres<3
  psycopg[binary]
  psycopg-pool
  ```

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

The filter has configurable "valves" (settings):

| Valve | Default | Description |
|-------|---------|-------------|
| `enable_filter` | `true` | Enable/disable the filter |
| `postgres_connection_string` | `postgresql://...` | Database connection |
| `extraction_model_id` | `""` | Model ID for memory extraction |
| `debug_logging` | `false` | Enable verbose logging |
| `enable_memory_injection` | `true` | Inject memories into context |
| `pii_filter_enabled` | `true` | Enable PII detection and filtering |
| `pii_filter_mode` | `"remove"` | `remove` drops facts with PII; `redact` stores with `[REDACTED]` |
| `pii_scrub_input` | `true` | Scrub PII from messages before sending to extraction model |
| `pii_patterns_enabled` | all 10 | Which PII types to detect (see PII Protection section) |

See [PII Protection](#-pii-protection) for details on privacy filtering.

## 🧪 Testing

### Verify Dependencies

```bash
python test_imports.py
```

### Test Database Connection

```bash
docker exec -it langgraph-postgres psql -U langgraph -d langgraph_memory -c "SELECT 1;"
```

### View Stored Memories

```bash
docker exec -it langgraph-postgres psql -U langgraph -d langgraph_memory -c \
  "SELECT thread_id, checkpoint FROM checkpoints ORDER BY thread_id LIMIT 5;"
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

## 📊 Schema Version

**Current: Schema v4** (LLM-Powered Semantic Merge + PII Protection)

Schema v4 features:
- LLM-powered semantic merge replaces code-based deduplication
- Flexible fact-based storage (replaces rigid type system)
- 3-layer PII filtering (prompt guardrails, regex pre-scrub, post-extraction validation)
- `schema_migrations` table for version tracking

## 🤖 For AI Assistants

If you're an AI assistant modifying this codebase, please read:

📚 **[docs/AI_DEVELOPMENT_GUIDE.md](docs/AI_DEVELOPMENT_GUIDE.md)**

This document contains critical information about:
- Schema version requirements
- Pitfalls to avoid
- Code modification procedures
- Testing requirements

## 🐛 Troubleshooting

### Common Errors

**`cannot import name 'RunnableSerializable'`**
```bash
pip install "langgraph~=0.6.11"   # langgraph 0.2.x is too old for modern langchain-core
```

**`cannot import name 'OP_BINARY' from 'websockets.frames'`** (first chat after a container start)
Caused by langgraph 1.x: its langgraph-sdk 0.4.x pins `websockets<17`, so Open WebUI's startup pip run
downgrades websockets under the already-running server. Keep the pins above (langgraph 0.6.x,
langgraph-sdk <0.4), which leave websockets alone.

**`ImportError: no pq wrapper available`**
```bash
pip install "psycopg[binary]"
```

**`ModuleNotFoundError: No module named 'psycopg_pool'`**
```bash
pip install psycopg-pool
```

**`TypeError: expected string or bytes-like object, got 'list'`** in `_update_user_memory_state`, or
**`request (N tokens) exceeds the available context size`** from the relevance model
Happened when a chat message had an image attached: Open WebUI then sends `content` as a list of
parts (text + base64 `image_url`) instead of a string, which broke the PII regex scrub and pushed the
whole image into the relevance prompt. Fixed in the filter (2026-10-14): `content_to_text()` keeps only
the text parts and drops image data. Each message is also capped by a character budget derived
from the relevance/extraction model's context size, read from Open WebUI's model registry (llama.cpp
reports `meta.n_ctx`; presets are followed to their base model). Half the context at 3 chars/token is
split across the messages in the prompt. When no context size is reported, the `max_message_chars`
valve (default 12000, 0 = off) is used instead.

**Existing facts disappear after a chat** (e.g. 2 facts in, 2 facts out, but one old fact replaced)
The extraction model is asked to return the merged fact list and a small model sometimes omits facts it
was given. Since 2026-10-14 the filter upserts the model's output onto the stored facts (`merge_mode`
valve, default `upsert`): same type+subject updates the fact and keeps `first_mentioned`, new
type+subject is appended, and facts the model did not return are kept. A fact is dropped only when
the model returns it with `"remove": true` or `"status": "removed"`. Set `merge_mode` to `replace`
for the old trust-the-model behaviour. The memory-manager preset's system prompt must match this
contract (it is tracked in [prompt/MERGE_PROMPT_memory-manager.md](prompt/MERGE_PROMPT_memory-manager.md)
and installed in the `model` table): removals are explicit `"remove": true` entries, never omissions.

**Forget commands.** Both "forget everything you know about me" and targeted forgets such as
"forget my cars" go through the extraction model, which must return each matching fact (or every fact)
with `"remove": true`. The log line `Merge upsert: ... removed` shows what it did. Memory extraction for that message
was skipped, so re-send the fact in a new message.

See [DEPENDENCY_RESOLUTION.md](DEPENDENCY_RESOLUTION.md) for detailed troubleshooting.

## 📝 License

MIT License

## 🙏 Acknowledgments

- [Open WebUI](https://github.com/open-webui/open-webui) - The amazing AI chat interface
- [LangGraph](https://github.com/langchain-ai/langgraph) - State machine for LLM applications
- [LangChain](https://github.com/langchain-ai/langchain) - LLM application framework
