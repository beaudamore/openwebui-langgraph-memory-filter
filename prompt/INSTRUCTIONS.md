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

### Step 2: Configure Filter

1. Go to **Workspace** → **Functions** → **LangGraph Memory Graph**
2. Set valves:
   - `extraction_model_id`: `memory-extractor`
   - `postgres_host`: your postgres host
   - `postgres_password`: your password

### Step 3: Test

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
3. **Call extraction model** with existing facts + conversation
4. **Model returns merged facts** (handles updates, removals, additions)
5. **Store merged facts** back to PostgreSQL
6. **Inject facts** into system message for main model

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
Load Existing Facts (PostgreSQL)
     ↓
Call Extraction Model
  - Input: existing facts + conversation
  - Output: merged fact list
     ↓
Store Merged Facts (PostgreSQL)
     ↓
Inject Facts into System Message
     ↓
Main Model (with memory context)
     ↓
Response to User
```
