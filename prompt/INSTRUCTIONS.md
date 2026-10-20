# Memory Extraction Setup Instructions

## Overview

The User Memory filter uses two workspace models: `memory-manager` receives the stored facts, a little context and the latest user message, and returns only the facts that change; `memory-relevance` receives the stored facts and the recent conversation and returns the keys of the facts worth injecting.

## Setup

### Step 1: Create Custom Model in OpenWebUI

1. Go to **Admin Panel** → **Settings** → **Models**
2. Click **"+ Add Model"**
3. Configure:
   - **Model Name:** `Memory Manager`
   - **Model ID:** `memory-manager`
   - **Base Model:** (choose one)
     - `gpt-4o-mini` - recommended, best balance
     - `llama3.2:latest` - good for local
     - `qwen2.5:7b` - excellent JSON output
     - `gemini/gemini-1.5-flash` - fast and cheap
   - **System Prompt:** Copy entire contents of `MERGE_PROMPT_memory-manager.md`

4. **Advanced Settings:**
   - Temperature: `0.1`
   - Max Tokens: leave high (the reply lists only changed facts; the filter sets no cap)
   - Top P: `0.9`

5. Save

### Step 2: Create Relevance Filter Model in OpenWebUI

This model selects only the memories relevant to the current conversation, avoiding a full data dump into context.

1. Go to **Admin Panel** → **Settings** → **Models**
2. Click **"+ Add Model"**
3. Configure:
   - **Model Name:** `Memory Relevance Filter`
   - **Model ID:** `memory-relevance`
   - **Base Model:** (choose one — can be lighter/cheaper than extraction)
     - `gpt-4o-mini` - recommended
     - `llama3.2:latest` - good for local
     - `qwen2.5:7b` - excellent JSON output
     - `gemini/gemini-1.5-flash` - fast and cheap
   - **System Prompt:** Copy entire contents of `RELEVANCE_PROMPT.md`

4. **Advanced Settings:**
   - Temperature: `0.1`
   - Max Tokens: leave high (the reply is a short list of keys; the filter sets no cap)
   - Top P: `0.9`

5. Save

> **Tip:** This model can use a smaller/cheaper base than the extraction model since it only selects from existing facts rather than generating new JSON structures.

### Step 3: Configure Filter

1. Go to **Admin Panel** → **Functions** → **User Memory**
2. Set valves:
   - `extraction_model_id`: `memory-manager`
   - `relevance_model_id`: `memory-relevance`
   - `relevance_filter_enabled`: `true`
   - `always_inject_types`: `["identity"]` (fact types that bypass filtering)
   - `postgres_host`: your postgres host
   - `postgres_password`: your password

### Step 4: Test

```
You: "Hi! I'm John and I own a 2006 Corvette Z06"
```

Then later:
```
You: "I sold my Corvette and bought a Tesla Model S"
```

The Tesla should replace the Corvette, not add alongside it.

## How It Works

1. **Inlet receives message**
2. **Load existing facts** from PostgreSQL
3. **Relevance filter**: the relevance model returns the `{type, subject}` keys of relevant facts (identity facts always pass; any failure injects all facts)
4. **Inject** the selected facts, grouped by type, appended to the latest user message
5. **Reply streams** to the user
6. **Outlet schedules a background task**: the extraction model gets existing facts + 3 earlier user messages + the latest user message and returns only changes (new, updated, `"remove": true`)
7. **Apply and store**: each change becomes one row insert, update or delete in `user_facts`; identical facts are not written

## Troubleshooting

### Model returns markdown code blocks
The filter auto-cleans `\`\`\`json` blocks, but for best results use a model that follows instructions well.

### Empty extractions
Check that conversations contain actual personal information to extract.

### Memory update skipped after a very long paste
The filter sends the raw message text to the helper models with no length cap. Keep **Paste Large Text as File** enabled in Settings > Interface so long pastes become files (the filter never reads files). A raw paste larger than the helper model's context fails that helper call and the turn's memory update is skipped.

### Facts not merging properly
- Raise `max_tokens` on the `memory-manager` model preset (the reply must hold every existing fact; no cap is set by the filter)
- Try a smarter model (gpt-4o-mini works well)
- Check logs: `docker logs open-webui 2>&1 | grep "User Memory"`

### PostgreSQL connection errors
```bash
# Check if postgres is running
docker ps | grep langgraph-postgres

# Test connection
docker exec -it langgraph-postgres psql -U langgraph -d langgraph_memory -c "SELECT 1"
```

### View stored facts

```bash
docker exec -it langgraph-postgres psql -U langgraph -d langgraph_memory -c \
  "SELECT user_id, type, subject, value, last_updated FROM user_facts ORDER BY user_id, first_mentioned;"
```

### Clear all memories (reset)

```bash
docker exec -it langgraph-postgres psql -U langgraph -d langgraph_memory -c "TRUNCATE user_facts;"
```

## Recommended Models

| Model | Pros | Cons |
|-------|------|------|
| gpt-4o-mini | Best JSON, smart merging | Cost |
| qwen2.5:7b | Great JSON, local | Slower |
| llama3.2:latest | Fast, local | Sometimes adds markdown |
| gemini-1.5-flash | Fast, cheap | Occasional format issues |

## Architecture

```
User Message
     ↓
[Inlet Filter]
     ↓
Load ALL Facts (PostgreSQL)
     ↓
Relevance Filter (LLM)
  - Input: non-identity facts as {type, subject, value} + last 6 messages
  - Output: {type, subject} keys of the relevant facts
     ↓
Append Selected Facts to the latest user message
     ↓
Main Model (with focused memory context)
     ↓
Response to User
     ↓
[Outlet Filter] -> background task (reply is not delayed)
     ↓
Extraction Model
  - Input: ALL existing facts + 3 earlier user messages + latest user message
  - Output: only the facts that change (new / updated / remove)
     ↓
Apply each change to its own row in PostgreSQL table user_facts (insert / update / delete)
```

### Relevance Filter Configuration

| Valve | Default | Description |
|-------|---------|-------------|
| `relevance_filter_enabled` | `true` | Toggle relevance filtering on/off |
| `relevance_model_id` | `memory-relevance` | OpenWebUI model for relevance selection |
| `always_inject_types` | `["identity"]` | Fact types that always get injected |

To disable relevance filtering and go back to full-dump mode, set `relevance_filter_enabled` to `false`.
