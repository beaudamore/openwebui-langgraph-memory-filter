# Memory Extraction Setup Instructions

## Overview

The LangGraph Memory Filter uses an LLM to extract and merge facts about users from conversations. The extraction model receives existing facts plus new conversation, and returns a merged fact list.

## Setup

### Step 1: Create Custom Model in OpenWebUI

1. Go to **Admin Panel** → **Settings** → **Models**
2. Click **"+ Add Model"**
3. Configure:
   - **Model Name:** `Memory Extractor`
   - **Model ID:** `memory-extractor`
   - **Base Model:** (choose one)
     - `gpt-4o-mini` - recommended, best balance
     - `llama3.2:latest` - good for local
     - `qwen2.5:7b` - excellent JSON output
     - `gemini/gemini-1.5-flash` - fast and cheap
   - **System Prompt:** Copy entire contents of `EXTRACTION_PROMPT.md`

4. **Advanced Settings:**
   - Temperature: `0.1`
   - Max Tokens: `2000` (needs room for existing facts + new)
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
   - Max Tokens: `1000`
   - Top P: `0.9`

5. Save

> **Tip:** This model can use a smaller/cheaper base than the extraction model since it only selects from existing facts rather than generating new JSON structures.

### Step 3: Configure Filter

1. Go to **Workspace** → **Functions** → **LangGraph Memory Graph**
2. Set valves:
   - `extraction_model_id`: `memory-extractor`
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
3. **Relevance filter** — call relevance model to select only facts relevant to the current conversation (identity facts always pass through)
4. **Inject relevant facts** into system message for main model
5. **Call extraction model** with existing facts + conversation
6. **Model returns merged facts** (handles updates, removals, additions)
7. **Store merged facts** back to PostgreSQL

## Troubleshooting

### Model returns markdown code blocks
The filter auto-cleans `\`\`\`json` blocks, but for best results use a model that follows instructions well.

### Empty extractions
Check that conversations contain actual personal information to extract.

### Facts not merging properly
- Increase `extraction_model_max_tokens` valve (needs room for all existing facts)
- Try a smarter model (gpt-4o-mini works well)
- Check logs: `docker logs open-webui 2>&1 | grep "LangGraph"`

### PostgreSQL connection errors
```bash
# Check if postgres is running
docker ps | grep langgraph-postgres

# Test connection
docker exec -it langgraph-postgres psql -U langgraph -d langgraph_memory -c "SELECT 1"
```

### View stored facts
```sql
docker exec -it langgraph-postgres psql -U langgraph -d langgraph_memory

-- Get latest checkpoint for a user
SELECT 
    thread_id,
    checkpoint->'channel_values'->>'facts' as facts,
    checkpoint->'channel_values'->>'total_facts' as count
FROM checkpoints 
ORDER BY checkpoint->>'ts' DESC 
LIMIT 5;
```

### Clear all memories (reset)
```sql
TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes CASCADE;
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
  - Input: all facts + current conversation
  - Output: relevant facts only
  - Identity facts bypass this step
     ↓
Inject Relevant Facts into System Message
     ↓
Main Model (with focused memory context)
     ↓
Response to User

— in parallel during inlet —

Call Extraction Model
  - Input: ALL existing facts + conversation
  - Output: merged fact list
     ↓
Store Merged Facts (PostgreSQL)
```

### Relevance Filter Configuration

| Valve | Default | Description |
|-------|---------|-------------|
| `relevance_filter_enabled` | `true` | Toggle relevance filtering on/off |
| `relevance_model_id` | `memory-relevance` | OpenWebUI model for relevance selection |
| `relevance_model_temperature` | `0.1` | Lower = more consistent selections |
| `relevance_model_max_tokens` | `1000` | Token budget for relevance response |
| `always_inject_types` | `["identity"]` | Fact types that always get injected |

To disable relevance filtering and go back to full-dump mode, set `relevance_filter_enabled` to `false`.
