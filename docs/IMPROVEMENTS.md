# LangGraph Memory Filter - Improvements Backlog

Status as of 2026-09-18. Note: `filter/` in this repo is still version 2.1.0. The
running code is version 4.0 in the Open WebUI function table (fact-level ops,
`memory_facts` pgvector table) and is not yet synced back here.

Applies to both variants in the Open WebUI function table:
`langgraph_memory_graph` (relevance-model recall) and `langgraph_memory_graph_rerank`
(bge-reranker-v2-m3 recall). Both share the `memory_facts` table in the
`langgraph-pgvector` container. Items are additive; none require changing the
`memory_facts` schema except where noted.

## 1. Consolidation pass

**Problem.** Every downstream step trusts what memory-manager wrote. The identity
guard catches one failure class (persona name stored as user name). Over months the
store will accumulate quiet errors that nothing flags, because no one reads the table.

**Design.**
- A scheduled job (or a valve-triggered pass on the Nth extraction) re-reads a user's
  active facts in batches of ~30, grouped by type, and asks memory-manager for ops
  against them with no new conversation: merge duplicates, split combined items,
  normalise subjects ("car" vs "vehicle"), drop facts contradicted by newer ones.
- Same ops path and validation as a normal turn, so the identity guard, PII filter
  and id checks all apply.
- Log every op the pass emits with a `consolidation` source_chat_id so its edits are
  distinguishable from conversation-driven ones.

## 2. Age-based decay

**History.** Listed as future work in `AI_DEVELOPMENT_GUIDE.md` ("Memory Decay -
confidence scores that decrease over time if not reinforced") and in
`MEMORY_FILTER_V2_PLAN.md` ("soft age penalty, not deletion"). Never implemented in
any shipped version (2.1.0 in this repo, 3.0 and 4.0 in the function table).

**Problem.** "Girlfriend moved back to New York" was true in the late 1990s. Without
decay, a 2030 conversation still injects it at full weight.

**Design.**
- Valves: `decay_after_days` (default 180) and `decay_factor` (default 0.5).
- At recall, multiply the ranking score (cosine similarity, or reranker score in the
  rerank variant) by `decay_factor` for facts whose `last_updated` is older than
  `decay_after_days`. Apply once per period elapsed if a steeper curve is wanted.
- Identity facts and any type in `always_inject_types` are exempt.
- Facts re-confirmed in conversation get a fresh `last_updated` through a normal
  update op, which resets the decay. That is the intended behaviour: mention keeps
  a fact young.
- Optional later step: a `valid_from` column so the manager can record when
  something was true ("worked as a tow truck driver 1994-1999") instead of when it
  was said.

## 3. "Clear everything" code path

**Problem.** The manager can only delete candidates, and candidates are identity
facts plus the `retrieval_top_k` nearest (30). "Forget what cars I own" works,
because the vehicle facts are the nearest candidates. "Clear everything" on a store
larger than the candidate set leaves facts behind. The pre-4.0 version handled this
trivially by rewriting the whole list, which is the same behaviour that overwrote the
user's name.

**Design.**
- Add an op kind `clear_all` to the manager prompt and to `_validate_ops`.
- When present, the filter itself marks every active fact for that user `deleted`
  in the same transaction, without relying on ids.
- Require the op to carry a `reason` quoting the user's words, and log it at warning
  level. A wrong clear is recoverable because rows are never physically deleted.

## 4. Episodic summaries per chat

**History.** Overlaps with the "discussion memory" and "round summaries" rows of
`MEMORY_FILTER_V2_PLAN.md`. This item is the single-assistant version of that plan.

**Problem.** Facts give the model what was said. They do not give it how a
conversation went: tone, what the user was working through, what was left open.
That is the missing half of coherence.

**Design.**
- New table `memory_episodes`: id, user_id, chat_id, started_at, last_updated,
  summary (text), mood/sentiment, open_threads (jsonb), embedding vector(1024).
  One row per chat, updated in place as the chat continues.
- A second extractor model (workspace model `memory-episode`) receives the last N
  turns including assistant messages, plus the existing episode row, and returns the
  updated summary. Unlike facts, assistant turns are in scope here, because the
  episode is about the exchange, not about the user.
- Runs in the outlet, not the inlet, so the assistant's reply is included and the
  user's next message is not delayed.
- Recall: embed the current user turn, retrieve the top few episodes by similarity
  (excluding the current chat), and inject them as a short "Recent conversations"
  block beneath the fact profile. Age decay from item 2 applies here as well.
- Build this only once item 1 has run at least once and the facts layer is stable.

## 5. User-facing memory view

**Problem.** A memory the user cannot see and edit is a memory they will eventually
distrust.

**Design.**
- Read view backed directly by `memory_facts` (active rows, grouped by type, with
  first_mentioned and last_updated) and by `memory_episodes` once item 4 exists.
- Per-fact delete and edit, applied as the same ops the manager would emit so
  validation and history are preserved.
- Simplest first cut: an Open WebUI tool or action button that returns the profile
  in chat, before building any UI.

## 6. Reranker threshold calibration

**Problem.** `rerank_score_threshold` defaults to 0.01 because reranker scores are
not calibrated like cosine. A first probe returned 0.06 for a relevant pair and
0.00002 for an irrelevant one.

**Design.**
- After a few days of use, read the per-candidate "Rerank score ... PASS/drop" lines
  from the `[LangGraph Memory Rerank]` log, pick the threshold from the gap between
  facts that should and should not have been injected, and set the valve.
- Also decide, from the same log, whether the wider-net variant (vector top 100 then
  rerank to 30) is worth adding for merge candidates at production scale.
