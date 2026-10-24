# Memory Extraction Setup Instructions

## Overview

The User Memory filter (v7.0, 2026-10-24) uses one workspace model, `memory-manager`: it receives the stored facts
nearest to the latest user message, a little context and that message, and returns only the facts that change.
Which facts are injected into a chat is decided by vector search (pgvector) over the facts' embeddings, ordered by
Open WebUI's reranker when one is configured. There is no relevance model.

Embeddings are not configured in the filter. It uses the embedding model Open WebUI has under
**Admin Panel > Settings > Documents** (and the reranking model there, if any). Change them whenever you like: the
filter notices on its next start and re-embeds every stored fact.

## Setup

### Step 1: Create the Memory Manager model in Open WebUI

1. Go to **Admin Panel** → **Settings** → **Models**
2. Click **"+ Add Model"**
3. Configure:
   - **Model Name:** `Memory Manager`
   - **Model ID:** `memory-manager`
   - **Base Model:** a small instruction-following model with reliable JSON output
   - **System Prompt:** Copy entire contents of `MERGE_PROMPT_memory-manager.md`

4. **Advanced Settings:**
   - Temperature: `0.1`
   - Max Tokens: leave high (the reply lists only changed facts; the filter sets no cap)
   - Top P: `0.9`

5. Save

### Step 2: Embedding and reranking

**Admin Panel → Settings → Documents**: make sure an embedding model is set (it is required; the filter cannot work
without one). Optionally set a reranking model: when present, the retrieved facts are ordered by it instead of by raw
cosine similarity, which matters because short facts cluster closely in embedding space.

### Step 3: Configure Filter

1. Go to **Admin Panel** → **Functions** → **User Memory**
2. Set valves:
   - `postgres_url`: `scheme://host` or `scheme://host:port`, e.g. `http://langgraph-pgvector` or `http://10.5.10.100:5432`
   - `postgres_password`: your password
   - `extraction_model_id`: `memory-manager` (default)
   - `always_inject_types`: `["identity"]` (fact types injected on every turn)
   - `query_window_messages`, `relevance_threshold`, `max_memories`, `dedupe_threshold`: see the README valve table;
     the defaults are a working start
   - `pii_engine`: `none`, `regex` or `pii_tracer` (with `pii_tracer_url`)

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
2. **Embed** the last `query_window_messages` user/assistant messages as one search text
3. **Retrieve**: `always_inject_types` facts, plus the nearest other facts with similarity ≥ `relevance_threshold`,
   reranked when Open WebUI has a reranker, top `max_memories` kept
4. **Inject** the facts, grouped by type, appended to the latest user message
5. **Reply streams** to the user
6. **Outlet schedules a background task**: the extraction model gets the always-inject facts + the `max_memories`
   nearest existing facts + 3 earlier user messages + the latest user message and returns only changes
   (new, updated, `"remove": true`)
7. **Apply and store**: each change is embedded (`subject: value`) and becomes one row insert, update or delete in
   `user_facts`. A new fact ≥ `dedupe_threshold` similar to an existing fact of the same type updates that row
   instead of adding one. Identical facts are not written.

## Troubleshooting

### Model returns markdown code blocks
The filter auto-cleans `\`\`\`json` blocks, but for best results use a model that follows instructions well.

### Empty extractions
Check that conversations contain actual personal information to extract.

### Memory update skipped after a very long paste
The filter sends the raw message text to the helper model with no length cap. Keep **Paste Large Text as File** enabled in Settings > Interface so long pastes become files (the filter never reads files). A raw paste larger than the helper model's context fails that helper call and the turn's memory update is skipped.

### "Open WebUI has no embedding function configured"
Set an embedding model under Admin Panel → Settings → Documents. The filter cannot retrieve or store without it.

### Unrelated memories injected, or relevant ones missed
Raise or lower `relevance_threshold` (its scale depends on the embedding model; the log at `debug_mode` prints every
candidate's similarity and rerank score), or lower `max_memories`. Configure a reranker in Open WebUI if you have none.

### Facts not merging properly
- Try a smarter model for `memory-manager`
- Lower `dedupe_threshold` a little if obvious re-wordings still become separate rows; raise it if different facts get merged
- Check logs: `docker logs open-webui 2>&1 | grep "User Memory"`

### PostgreSQL connection errors
```bash
# Check if postgres is running
docker ps | grep langgraph-pgvector

# Test connection
docker exec -it langgraph-pgvector psql -U langgraph -d langgraph_memory -c "SELECT 1"
```

### View stored facts

```bash
docker exec -it langgraph-pgvector psql -U langgraph -d langgraph_memory -c \
  "SELECT user_id, type, subject, value, last_updated FROM user_facts ORDER BY user_id, first_mentioned;"
```

### Clear all memories (reset)

```bash
docker exec -it langgraph-pgvector psql -U langgraph -d langgraph_memory -c "TRUNCATE user_facts;"
```

## Architecture

```
User Message
     ↓
[Inlet Filter]  (no model call)
     ↓
Embed last N messages (Open WebUI's embedding model)
     ↓
pgvector search in user_facts: always-inject types + nearest facts ≥ threshold
     ↓
Rerank (Open WebUI's reranker, if configured) → top max_memories
     ↓
Append Selected Facts to the latest user message
     ↓
Main Model (with focused memory context)
     ↓
Response to User
     ↓
[Outlet Filter] -> background task (reply is not delayed)
     ↓
Extraction Model (memory-manager)
  - Input: always-inject facts + nearest existing facts + 3 earlier user messages + latest user message
  - Output: only the facts that change (new / updated / remove)
     ↓
Embed each change; merge into a near-duplicate row if one exists (dedupe_threshold)
     ↓
Apply each change to its own row in PostgreSQL table user_facts (insert / update / delete)
```
