# LangGraph Memory Graph v4.0

Source: `filter/langgraph_memory_graph_v4.py`. This is a verbatim copy of the
function `langgraph_memory_graph` in the Open WebUI function table as of
2026-09-18. It supersedes `filter/langgraph_memory_filter.py` (2.1.0).

## What changed from earlier versions

| | 2.x / 3.0 | 4.0 |
|---|---|---|
| Where facts live | One fact list per user, msgpack-serialised in the LangGraph checkpoint | One row per fact in the `memory_facts` table, with a pgvector embedding |
| How facts change | The manager model rewrote the whole list every turn | The manager returns `add` / `update` / `delete` ops against fact ids; the filter applies them |
| What the manager sees | Every stored fact | Identity facts plus the `retrieval_top_k` nearest facts to the new messages |
| Recall | The relevance model read every stored fact | Vector search on the last user turn, then optionally the relevance model over that bounded set |
| History | None; a bad merge overwrote everything | Updates insert a new row and mark the old one `superseded`; deletes set `status='deleted'`; rows are never physically removed |
| Persona-name protection | None | Code guard refuses an identity name that matches the workspace model's name or a "You are X" system prompt |

The rewrite was triggered by a real failure: a 9B manager overwrote the user's
name with the persona's name ("Tony") because the user addressed the persona by
name, and overwrote the user's location from a sentence about someone else. With
whole-list rewrites, one bad output replaced every fact. See
`docs/IMPROVEMENTS.md` for what is still open.

## Runtime dependencies

| Component | Where | Notes |
|---|---|---|
| PostgreSQL with pgvector | container `langgraph-pgvector`, image `pgvector/pgvector:pg16`, on `webui-network` | Compose reference: `/home/spark/projects/compose/langgraph-postgres-pgvector.yml`. Valve `postgres_host` must be this container name; port is 5432 inside the network (5435 is only the host mapping). |
| Embeddings | bge-m3 on vLLM, OpenAI-compatible `POST {base}/embeddings`, 1024 dims | Same endpoint Open WebUI's RAG uses. Valves `embedding_api_base_url`, `embedding_model`, `embedding_dim`. |
| memory-manager | Open WebUI workspace model on a served base (qwen3.8:27b-nvfp4-mtp) | System prompt below. Thinking disabled via `chat_template_kwargs`. |
| memory-relevance | Open WebUI workspace model on the same base | Prompt unchanged from `prompt/RELEVANCE_PROMPT.md`. Only used when `relevance_filter_enabled` is on. |
| Python packages in the Open WebUI container | langgraph, langgraph-checkpoint-postgres, psycopg[binary], psycopg-pool, aiohttp | `pgvector` Python package is not needed; vectors are passed as text literals and cast with `::vector`. |

## Data model

Created on first connect by `_ensure_memory_tables()` (idempotent), after the
LangGraph checkpoint tables:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS memory_facts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    type TEXT NOT NULL,            -- identity, preference, ownership, relationship, goal, skill, event
    subject TEXT NOT NULL,
    value TEXT NOT NULL,
    sentiment TEXT,
    confidence REAL NOT NULL DEFAULT 0.8,
    source_chat_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',   -- active | superseded | deleted
    superseded_by UUID,
    first_mentioned TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    embedding vector(1024)
);
CREATE INDEX IF NOT EXISTS memory_facts_user_status_idx ON memory_facts (user_id, status);
CREATE INDEX IF NOT EXISTS memory_facts_embedding_idx ON memory_facts USING hnsw (embedding vector_cosine_ops);
```

The embedded text for a fact is `"{type} - {subject}: {value}"`.

The LangGraph checkpointer (thread id = user id) now stores only metadata:
`last_updated`, `total_facts`, `memory_summary`. The `facts` channel is gone.
`schema_migrations` records version 5 for this layout.

## Per-message flow (inlet)

1. **Load state.** Active facts for the user from `memory_facts`; summary from the checkpoint.
2. **Recall** (`_select_relevant_memories`), only if the user has any facts:
   - Facts whose type is in `always_inject_types` (default `identity`) are always included.
   - The last user message is embedded and the `retrieval_top_k` nearest other facts with cosine similarity at or above `similarity_threshold` are fetched.
   - If `relevance_filter_enabled`, memory-relevance re-ranks that candidate set. On any failure the vector candidates are used as-is.
   - The result is formatted by `_format_memory_context` and prepended to the system message.
3. **Upsert** (`_update_user_memory_state`), when the number of user messages reaches `extraction_threshold`:
   - The last 10 messages are reduced to user-role messages only. System and assistant turns never reach the manager.
   - PII pre-scrub (`scrub_pii`) if enabled.
   - The joined user text is embedded; candidates = all always-inject facts plus the `retrieval_top_k` nearest others, each with its id.
   - The manager receives `CANDIDATE FACTS` and `NEW CONVERSATION` and returns `{"ops": [...]}`.
   - `_validate_ops` checks each op (see below). Facts being written are embedded in one batch.
   - `_db_apply_ops` applies everything in one transaction.
   - The graph runs `update_memory -> summarize` to refresh the count and summary.

Every log line is prefixed `[LangGraph Memory]`. With `debug_mode` on, the guarded
assistant names are logged per turn.

## Ops contract

```json
{"ops": [
  {"op": "add",    "fact": {"type": "...", "subject": "...", "value": "...", "sentiment": "neutral", "confidence": 0.9}},
  {"op": "update", "id": "<candidate id>", "fact": {...}},
  {"op": "delete", "id": "<candidate id>", "reason": "..."}
]}
```

`_validate_ops` rejects, with a warning in the log:

- an op kind other than add / update / delete;
- an update or delete whose id was not among the candidates sent (the manager cannot touch what it was not shown);
- an add or update with a missing type, subject or value;
- **identity guard:** an add or update where `type == identity`, the subject contains "name", and the value equals, or consists only of tokens from, the current assistant's names. Assistant names come from the workspace model's `name` and `id` (tokens of three or more letters) and from `You are X` / `your name is X` in any system message;
- a fact that fails PII post-validation (`pii_filter_mode` remove or redact);
- an update identical to the candidate it targets (no-op).

Rejected ops are dropped individually. The rest still apply.

## Valves

| Valve | Default | Purpose |
|---|---|---|
| `postgres_host` / `postgres_port` / `postgres_database` / `postgres_user` / `postgres_password` | `langgraph-pgvector` / 5432 / `langgraph_memory` / `langgraph` / (change it) | Database |
| `embedding_api_base_url` | `http://100.79.200.70:8006/v1` | Embeddings endpoint |
| `embedding_api_key` | empty | Bearer token if the endpoint needs one |
| `embedding_model` | `bge-m3` | Model id sent to the endpoint |
| `embedding_dim` | 1024 | Must match the column. Changing it means a new table. |
| `embedding_timeout` | 30 | Seconds |
| `retrieval_top_k` | 30 | Nearest facts fetched, both for merge candidates and for recall |
| `similarity_threshold` | 0.35 | Cosine floor for recall only; merge candidates ignore it |
| `extraction_model_id` | `memory-manager` | Workspace model that returns ops |
| `extraction_model_max_tokens` | 4000 | Set to 10000 in production. The log warns on `finish_reason=length`. |
| `extraction_threshold` | 1 | User messages before an upsert runs |
| `relevance_filter_enabled` | true | Run memory-relevance over the vector candidates |
| `relevance_model_id` / `relevance_model_max_tokens` | `memory-relevance` / 4000 | |
| `always_inject_types` | `["identity"]` | Bypass retrieval, always candidates, always injected |
| `max_injected_memories`, `memory_injection_format` | 10, `structured` | Unchanged from 2.x |
| `pii_filter_enabled`, `pii_filter_mode`, `pii_scrub_input`, `pii_patterns_enabled` | true, remove, true, all ten patterns | Unchanged from 2.x |
| `show_status`, `show_injected_memories`, `debug_mode`, `priority` | true, false, false, 10 | Unchanged from 2.x |

Changing a valve takes effect on the next request. Changing the code in the
function table only takes effect after Save in the function editor; toggling the
function does not reload the module.

## memory-manager system prompt

Set this as the system prompt of the `memory-manager` workspace model. The same text
is in `prompt/EXTRACTION_PROMPT.md`.

```
You are a memory manager. You receive CANDIDATE FACTS already stored about a user (each with an id) and a NEW CONVERSATION made up of the user's own messages only. Your job is to return the smallest list of OPERATIONS that brings the stored facts up to date. You never rewrite the whole list.

OPERATIONS:
- add: a new fact about the user that no candidate already covers
- update: a candidate whose information changed or became more specific; give its id and the complete replacement fact
- delete: a candidate the user explicitly retracted or directly contradicted; give its id and a short reason

OUTPUT FORMAT (return ONLY this JSON, no markdown, no commentary):
{
  "ops": [
    {"op": "add", "fact": {"type": "identity|preference|ownership|relationship|goal|skill|event", "subject": "specific category", "value": "the information with relevant details", "sentiment": "positive|negative|neutral", "confidence": 0.9}},
    {"op": "update", "id": "<candidate id>", "fact": {"type": "...", "subject": "...", "value": "...", "sentiment": "...", "confidence": 0.9}},
    {"op": "delete", "id": "<candidate id>", "reason": "short reason"}
  ]
}
When the conversation adds nothing new, return {"ops": []}.

FACT TYPES:
- identity: name, age, location, job, company, education
- preference: likes, dislikes, favorites, opinions
- ownership: things they own (ONE FACT PER ITEM)
- relationship: people - family, friends, colleagues
- goal: wants, plans, aspirations
- skill: abilities, expertise, hobbies
- event: important dates, milestones, birthdays

WHO IS WHO:
1. The user wrote every message in NEW CONVERSATION. Facts are about the user only.
2. A name the user uses to ADDRESS someone ("Tony, how did you know...", "thanks Tony", "what do you know about me, Tony?") is the assistant's name. It is NEVER the user's name. Do not add or update the user's name from a form of address. Set the user's name only when the user states it about themselves ("my name is Beau", "I'm Beau").
3. Something that happens to another person ("my girlfriend moved back to New York", "my dad was a doctor") is a relationship fact about that person. It is not the user's location, job, or identity.

CHANGE RULES:
4. Only update a candidate when the conversation states something newer or more specific about the SAME subject. Only delete on explicit retraction ("I sold my X", "forget X", "that's wrong") or direct contradiction. Absence of mention is never a reason to update or delete.
5. A candidate whose meaning the conversation merely repeats gets no op at all.
6. Use ids exactly as given. Never invent an id. Never reference an id that is not in CANDIDATE FACTS.
7. ONE FACT PER DISTINCT ITEM. Never combine items: "Corvette and Tesla" is two ownership facts.
8. Include temporal context in value when mentioned: "bought Tesla in 2021" → value "Tesla Model 3, purchased 2021"; "married since 2010" → value "Sarah, married since 2010".
9. Use specific subjects: "vehicle" not "car", "spouse" not "family".

META-MEMORY COMMANDS (user editing their memory):
- "Forget X" / "Delete X" / "Remove X from memory" → delete that candidate
- "That's wrong" / "I don't actually own X" → delete that candidate
- "Update my age to Y" → update the age candidate
- "Clear everything" → delete every candidate

CONFIDENCE:
- 1.0: explicit first-person statement ("I am", "I own", "my name is")
- 0.8: clear implication
- 0.6: contextual inference

IGNORE:
- Hypotheticals, questions, temporary states
- Sarcasm, jokes

Return ONLY valid JSON. No markdown, no explanation.
```

## Operating it

Inspect a user's facts:

```sql
SELECT type, subject, value, status, confidence, first_mentioned, last_updated
FROM memory_facts WHERE user_id = '<id>' ORDER BY status, type, first_mentioned;
```

Follow a fact's history: each `update` leaves the old row with `status='superseded'`
and `superseded_by` pointing at the replacement, which inherits the original
`first_mentioned`.

Wipe one user without dropping the table:

```sql
DELETE FROM memory_facts WHERE user_id = '<id>';
DELETE FROM checkpoint_writes WHERE thread_id = '<id>';
DELETE FROM checkpoint_blobs  WHERE thread_id = '<id>';
DELETE FROM checkpoints       WHERE thread_id = '<id>';
```

Read the last upsert in the container log:

```
docker logs open-webui-beaudamore --since 10m 2>&1 | grep "\[LangGraph Memory\]" | grep -E "Step 1|Manager returned|Ops:|Applied ops|Identity guard|Rejected"
```

## Rerank variant

A second function, `langgraph_memory_graph_rerank`, is the same code with the
recall step changed: after the vector search, candidates are scored by
bge-reranker-v2-m3 (`POST {base}/rerank` on the vLLM node) and kept at or above
`rerank_score_threshold`. In that variant `relevance_filter_enabled` is only a
fallback for when the rerank call fails. Both variants share the same
`memory_facts` table so an A/B between them compares recall alone. Its log prefix
is `[LangGraph Memory Rerank]` and it logs every candidate's score. Source:
`filter/langgraph_memory_graph_v4_rerank.py`.
