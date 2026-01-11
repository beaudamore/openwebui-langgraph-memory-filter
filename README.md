# LangGraph Memory Filter for Open WebUI

A sophisticated persistent memory system for [Open WebUI](https://github.com/open-webui/open-webui) that extracts and maintains user information across conversations using LangGraph and PostgreSQL.

## Author

**Beau D'Amore**  
[www.damore.ai](https://www.damore.ai)

## ✨ Features

- **Persistent Memory**: Remembers user preferences, relationships, goals, interests, and personal information
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
│   ├── docker-compose.langgraph.yml  # PostgreSQL container
│   └── init-postgres.sql             # Optional analytics tables
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
  "langgraph>=1.0.0" \
  langgraph-checkpoint-postgres \
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
| [DEPENDENCY_RESOLUTION.md](DEPENDENCY_RESOLUTION.md) | Dependency troubleshooting |

## 🔧 Requirements

- **Open WebUI**: Latest version
- **Python**: 3.10+
- **PostgreSQL**: 16+ (provided via Docker)
- **Dependencies**:
  ```
  langgraph>=1.0.0
  langgraph-checkpoint-postgres
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

### Optional: Analytics Tables

```bash
docker exec -i langgraph-postgres psql -U langgraph -d langgraph_memory < init-postgres.sql
```

## ⚙️ Configuration

The filter has configurable "valves" (settings):

| Valve | Default | Description |
|-------|---------|-------------|
| `enable_filter` | `true` | Enable/disable the filter |
| `postgres_connection_string` | `postgresql://...` | Database connection |
| `extraction_model_id` | `""` | Model ID for memory extraction |
| `debug_logging` | `false` | Enable verbose logging |
| `enable_memory_injection` | `true` | Inject memories into context |

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

## 📊 Schema Version

**Current: Schema v2** (Preference Evolution Tracking)

Schema v2 features:
- Preferences stored as timestamped data points
- Full evolution history tracking (sentiment changes over time)
- `mentioned_at` and `context` fields for each preference entry
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
pip install "langgraph>=1.0.0"
```

**`ImportError: no pq wrapper available`**
```bash
pip install "psycopg[binary]"
```

**`ModuleNotFoundError: No module named 'psycopg_pool'`**
```bash
pip install psycopg-pool
```

See [DEPENDENCY_RESOLUTION.md](DEPENDENCY_RESOLUTION.md) for detailed troubleshooting.

## 📝 License

MIT License

## 🙏 Acknowledgments

- [Open WebUI](https://github.com/open-webui/open-webui) - The amazing AI chat interface
- [LangGraph](https://github.com/langchain-ai/langgraph) - State machine for LLM applications
- [LangChain](https://github.com/langchain-ai/langchain) - LLM application framework
