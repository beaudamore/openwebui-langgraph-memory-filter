# LangGraph Memory Filter v2 — All-Encompassing Conversation Memory

> **Status:** Planning  
> **Date:** March 12, 2026  
> **Author:** Beau D'Amore  
> **Supersedes:** LangGraph Memory Filter v1 (user facts only)

> **Not implemented:** This proposal is not part of `pipeline-public`. The active pipeline only consumes memory context already supplied in Open WebUI messages.

## TL;DR

Evolve the LangGraph memory filter into a single, all-encompassing memory system that handles **everything** in its inlet:

1. **User facts** (existing v1 — identity, preferences, etc.)
2. **Per-speaker round summaries** — when conversation history exceeds a threshold, older rounds are summarized by an LLM with individual summaries per speaker, stored in PostgreSQL as persistent, retrievable records
3. **Discussion/topic memory** — extracted from both user AND speaker messages
4. **Relevance-based injection with soft age penalty** — at query time, only facts and summaries relevant to the current message are injected. Relevance is primary; time is a soft tiebreaker, not a hard cutoff

The pipeline's `_truncate_history` remains as a dumb token safety net. All intelligent context management lives in the filter inlet. No pipeline changes required.

---

## Problem Statement

### Current Limitations (v1)

- **User facts only** — the filter extracts identity, preferences, ownership, goals, skills, events. It knows *who* the user is, but not *what was discussed*.
- **No conversation memory** — start a new chat and reference something from a prior chat → the model is lost. Context is myopic to the current conversation.
- **Truncation is lossy** — the pipeline's `_truncate_history` drops old rounds entirely. That context is gone forever for the current request.
- **No speaker awareness** — in Circle of Speakers, multiple speakers respond per round. The filter doesn't capture what individual speakers said.
- **Hard cutoffs everywhere** — both truncation and any max-items limit are just different flavors of "drop old stuff," which is the wrong approach.

### What v2 Solves

- **Cross-conversation awareness** — speakers know what was discussed in prior chats without the user repeating themselves
- **Per-speaker granularity** — "what did Augustine say about grace?" retrieves just that speaker's summary, not the entire round
- **Conversation-agnostic** — starting a new chat doesn't mean starting from scratch
- **Self-regulating memory** — relevance-based scoring with soft age penalty naturally deprioritizes stale, irrelevant items without deleting anything
- **No per-request summarization overhead** — summaries are computed once (when rounds overflow) and stored permanently; retrieval is just a PostgreSQL read + relevance scoring

---

## Architecture

### Inlet Flow (Single Pass)

```
User sends message
    ↓
┌─────────────────────────────────────────────────────────────┐
│  INLET (Memory Filter v2)                                   │
│                                                             │
│  1. LOAD from PostgreSQL                                     │
│     └─ User facts + round summaries + discussion memory     │
│                                                             │
│  2. EXTRACT facts FIRST (sees full history before trimming)  │
│     └─ User facts from user messages                        │
│     └─ Discussion/conclusion/interaction facts              │
│        from user + assistant messages                       │
│     └─ LLM merges with existing facts → PostgreSQL          │
│                                                             │
│  3. SUMMARIZE overflow rounds (only when threshold exceeded) │
│     └─ Count rounds in body["messages"]                     │
│     └─ If rounds > SUMMARY_THRESHOLD:                       │
│        • Identify overflow rounds (oldest beyond threshold) │
│        • Send to summarization LLM                          │
│        • Get per-speaker summaries for each round           │
│        • Store RoundSummary objects in PostgreSQL            │
│        • Remove summarized messages from body               │
│                                                             │
│  4. INJECT relevant context into system prompt               │
│     └─ Relevance model scores ALL stored items              │
│     └─ Apply soft age penalty to old items                  │
│     └─ Items below relevance_threshold excluded             │
│     └─ Surviving items injected into system message         │
│                                                             │
│  5. RETURN modified body to pipeline                         │
│     └─ System message enriched with memory context          │
│     └─ Messages trimmed to hard cap                         │
│     └─ Pipeline sees clean, right-sized messages            │
└─────────────────────────────────────────────────────────────┘
    ↓
Pipeline (_truncate_history is just a safety net now)
    ↓
Speakers respond with full cross-conversation awareness
```

### Why Extract Before Summarize

Extraction needs to see the **full history** (including rounds about to be summarized) so it captures discussion facts before those rounds are removed from the message list. The summary is for future retrieval; the extraction captures the structured facts while they're still in context.

### Why Everything Lives in the Inlet

- The inlet already intercepts `body["messages"]` before the pipeline
- No pipeline code changes needed — the filter modifies messages, pipeline inherits context automatically
- Single responsibility: all memory logic in one place
- Same pattern as v1 (inlet/outlet), just more capable

---

## Data Model

### Extended `Fact` TypedDict

```python
class Fact(TypedDict):
    type: str           # identity, preference, ownership, relationship, goal, skill, event,
                        # discussion, conclusion, speaker_interaction, topic_interest
    subject: str        # what this fact is about
    value: str          # the information
    sentiment: Optional[str]  # positive, negative, neutral
    confidence: float   # 0.0–1.0
    first_mentioned: str      # ISO datetime
    last_updated: str         # ISO datetime
    conversation_id: Optional[str]  # NEW: which chat this came from (None = cross-chat)
```

#### New Fact Types

| Type | Description | Subject Examples | Value Examples |
|------|-------------|-----------------|----------------|
| `discussion` | What was discussed in a conversation | "Book of Romans", "grace vs works" | "Paul and Augustine debated grace vs works — Paul emphasized faith, Augustine emphasized predestination" |
| `conclusion` | What the circle concluded | "free will debate" | "Circle concluded that free will and sovereignty can coexist" |
| `speaker_interaction` | Notable exchanges between speakers | "Paul-Augustine exchange" | "Paul challenged Augustine's view on predestination; they found common ground on grace" |
| `topic_interest` | Topics the user keeps returning to | "theology of suffering" | "User asked about suffering across multiple conversations — clearly important to them" |

### New `SpeakerSummary` and `RoundSummary` TypedDicts

```python
class SpeakerSummary(TypedDict):
    speaker_name: str        # e.g. "Paul", "Augustine"
    model_id: str            # the OpenWebUI model ID
    key_points: str          # concise summary of what this speaker said
    position: str            # their stance/perspective on the topic

class RoundSummary(TypedDict):
    conversation_id: str          # which chat this summary covers
    round_number: int             # which round (1-indexed)
    user_query: str               # what the user asked (truncated)
    speaker_summaries: List[SpeakerSummary]  # per-speaker breakdown
    topics: List[str]             # key topics discussed
    created_at: str               # ISO datetime
```

Per-speaker summaries within each round enable granular retrieval — "what did Augustine say about grace?" pulls just that speaker's summary, not the entire round. The relevance model can score individual `SpeakerSummary` entries independently.

### Extended `MemoryGraphState`

```python
class MemoryGraphState(TypedDict):
    user_id: str
    conversation_id: str
    facts: List[Fact]
    round_summaries: List[RoundSummary]  # NEW
    _messages_to_process: List[Dict[str, str]]
    last_updated: str
    total_facts: int
    memory_summary: str
```

---

## Relevance Scoring with Soft Age Penalty

### Core Principle

**Relevance is primary. Time is a soft tiebreaker, not a hard cutoff.**

A fact from 6 months ago that's relevant to the current question **gets injected**. A fact from 5 minutes ago that's irrelevant **gets excluded**. Hard cutoffs (`max_stored_summaries`, time-based deletion) are just truncation by another name — the system should be self-regulating.

### How It Works

1. The relevance model scores ALL stored items (facts + round summaries) against the current user message → **base relevance score** (0.0–1.0)
2. A **soft age penalty** is applied to older items:
   - Items newer than `age_penalty_threshold_days` (default: 180 / 6 months): **no penalty**
   - Items older than threshold: `final_score = base_score - age_penalty_amount` (default: 0.1)
   - Penalty is constant, not progressive — a 7-month-old fact gets the same -0.1 as a 2-year-old fact
3. `relevance_threshold` valve (float, default 0.3) sets the injection cutoff on the **final score**
4. `always_inject_types` (default: `["identity"]`) bypass filtering entirely
5. **Nothing is deleted** — items stay in the store permanently. They only disappear when the LLM merge decides they're contradicted or superseded ("I sold my Tesla")

### Why This Works

| Scenario | Base Score | Age Penalty | Final Score | Threshold (0.3) | Result |
|----------|-----------|-------------|-------------|------------------|--------|
| Highly relevant old fact (8 months) | 0.8 | -0.1 | 0.7 | ✅ above | **Injected** |
| Marginally relevant old fact (8 months) | 0.35 | -0.1 | 0.25 | ❌ below | **Excluded** |
| Highly relevant recent fact (2 months) | 0.8 | 0.0 | 0.8 | ✅ above | **Injected** |
| Irrelevant recent fact (2 months) | 0.15 | 0.0 | 0.15 | ❌ below | **Excluded** |
| Identity fact (any age) | — | — | — | — | **Always injected** |

The soft penalty naturally deprioritizes stale marginally-relevant items without manual cleanup. Important old context still surfaces. Self-regulating.

### Relevance Valves

```python
relevance_threshold: float = 0.3           # minimum final score for injection
age_penalty_threshold_days: int = 180      # items older than this get the penalty
age_penalty_amount: float = 0.1            # how much to subtract for old items
always_inject_types: List[str] = ["identity"]  # bypass filtering entirely
```

---

## Implementation Steps

### Phase 1: Schema & State Extensions

**1. Extend `Fact` TypedDict**
- File: `filter/langgraph_memory_filter.py` ~line 92
- Add `conversation_id: Optional[str]` field
- Document new fact types: `discussion`, `conclusion`, `speaker_interaction`, `topic_interest`
- Backward compatible: existing facts get `conversation_id = None`

**2. Add `RoundSummary` and `SpeakerSummary` TypedDicts**
- File: `filter/langgraph_memory_filter.py` after `Fact` definition
- `SpeakerSummary`: `speaker_name`, `model_id`, `key_points`, `position`
- `RoundSummary`: `conversation_id`, `round_number`, `user_query`, `speaker_summaries: List[SpeakerSummary]`, `topics`, `created_at`

**3. Extend `MemoryGraphState`**
- File: `filter/langgraph_memory_filter.py` ~line 107
- Add `round_summaries: List[RoundSummary]` field
- Default empty list for backward compatibility with existing checkpoints

### Phase 2: Summarization Logic in Inlet

**4. Add summarization valves** to `Valves` class (~line 440)
- `summary_enabled: bool = True` — master switch
- `summary_threshold: int = 6` — summarize when rounds exceed this count
- `summary_keep_recent: int = 4` — how many recent rounds to keep verbatim
- `summary_model_id: str = "memory-manager"` — can reuse extraction model or separate
- `summary_max_tokens: int = 500` — cap on summary length

**5. Add `_summarize_overflow_rounds()` method**
- Input: full `messages` list from `body`, `conversation_id`
- Logic:
  1. Count conversation rounds (user+assistant pairs, skip system messages)
  2. If rounds ≤ `summary_threshold`: return messages unchanged, no summarization
  3. Identify rounds to summarize: everything except the most recent `summary_keep_recent` rounds
  4. Build a per-speaker summarization prompt for those older rounds
  5. Call LLM via `generate_chat_completion(bypass_filter=True)` (same pattern as extraction)
  6. Parse response into a `RoundSummary` with individual `SpeakerSummary` entries per speaker per round
  7. Store summaries in graph state (PostgreSQL checkpoint)
  8. Return trimmed messages (only recent rounds kept)
- Summarization prompt instructs: "For each round, summarize EACH SPEAKER individually. Capture their name, key points, and position/perspective. This enables granular retrieval of what specific speakers said."

**6. Wire summarization into `inlet()`** (~line 1310, after extraction, before injection return)
- Extraction runs FIRST (sees full history, captures structured facts)
- THEN summarization trims old rounds and stores per-speaker summaries
- Update `body["messages"]` with the trimmed result
- Summaries stored in PostgreSQL via graph checkpoint

### Phase 3: Fact Extraction Evolution

**7. Update extraction to include assistant messages** (~line 1360 in inlet)
- Currently: `if msg.get("role") == "user"` — only user messages extracted
- Change to: include both user and assistant messages (needed for discussion/speaker_interaction facts)
- Truncate assistant messages to ~500 chars per speaker section to keep extraction prompts manageable
- Tag extracted discussion facts with `conversation_id`

**8. Update `EXTRACTION_PROMPT.md`**
- File: `prompt/EXTRACTION_PROMPT.md`
- Add FACT TYPES: `discussion`, `conclusion`, `speaker_interaction`, `topic_interest`
- Add extraction rules for conversation context
- Add clear instruction: for multi-speaker conversations, extract discussion topics and notable interactions
- Add examples for new fact types
- Keep all existing PII rules

**9. Add `_summarize_rounds_prompt()` method**
- Builds the per-speaker summarization prompt
- Instructs LLM: "For each round, summarize EACH SPEAKER'S response individually. Return structured JSON with speaker_name, key_points, and position for each."
- The summarization model can be the same as extraction or a dedicated one via valve

### Phase 4: Injection Evolution

**10. Update `_format_memory_context()`** (~line 1150)
- Add per-speaker round summary rendering:
  ```
  === PREVIOUS CONVERSATIONS ===
  [Chat Jan 5 — "Book of Romans"]:
    • Paul: Emphasized justification by faith alone, cited Romans 3:28
    • Augustine: Argued for predestination, referenced Romans 9
    • User focus: Most interested in the grace question
  [Chat Jan 12 — "Sermon on the Mount"]:
    • Jesus: Taught on righteousness exceeding the Pharisees'
    • Paul: Connected Sermon themes to Galatians
  === END PREVIOUS CONVERSATIONS ===
  ```
- Add section for discussion facts (separate from user identity)
- Keep existing structured/natural/bullet format options

**11. Update `_select_relevant_memories()`** (~line 1100)
- Extend to score `RoundSummary` entries alongside facts
- Score individual `SpeakerSummary` entries within rounds for granular retrieval
- Apply soft age penalty: items older than `age_penalty_threshold_days` get `age_penalty_amount` subtracted from relevance score
- Items below `relevance_threshold` (after penalty) excluded
- `always_inject_types` still bypasses (identity facts always included)
- Return both relevant facts AND relevant summaries

### Phase 5: Graph Workflow Updates

**12. Add `process_summaries` node** to the LangGraph workflow
- New node in `_create_memory_graph()` (~line 650)
- Processes and stores round summaries alongside facts
- Workflow becomes: `process_merged → process_summaries → update_memory → summarize → END`

**13. Update `_create_summary_node()`** (~line 780)
- Include discussion/conclusion facts in the natural language summary
- Include round summary topics in the summary
- This summary is used for the "natural" injection format

**14. Add migration entry** to `SCHEMA_MIGRATIONS` (~line 60)
- Version 5: "Added round summaries and conversation-level facts"
- Changes: `round_summaries` field added, new fact types, `conversation_id` on facts

### Phase 6: Pipeline Coordination

**15. No core pipeline changes** — the filter handles everything
- `_truncate_history` in `openwebui_streaming_blueprint.py` stays as dumb safety net
- The filter has already trimmed messages and injected context before the pipeline sees them
- Circle of Speakers `pipe()` receives right-sized, context-enriched messages automatically

**16. Optional: Add debug valve** to pipeline for visibility
- `MEMORY_FILTER_DEBUG: bool = False` — logs when memory context is detected in system message
- Just for debugging pipeline + filter coordination, not functional

---

## New Valves Summary

### Summarization Valves

```python
summary_enabled: bool = True                  # master switch for round summarization
summary_threshold: int = 6                    # summarize when rounds exceed this count
summary_keep_recent: int = 4                  # recent rounds kept verbatim
summary_model_id: str = "memory-manager"      # model for summarization (can reuse extraction model)
summary_max_tokens: int = 500                 # cap on summary response length
```

### Relevance & Age Penalty Valves

```python
relevance_threshold: float = 0.3              # minimum final score for injection
age_penalty_threshold_days: int = 180         # items older than 6 months get penalty
age_penalty_amount: float = 0.1               # subtracted from relevance score for old items
always_inject_types: List[str] = ["identity"] # bypass relevance filtering entirely
```

---

## Example: Injected Context

What a speaker's system prompt looks like after the filter enriches it:

```
=== USER MEMORY PROFILE ===

About You:
  - Name: Beau D'Amore
  - Location: Florida
  - Interests: Theology, philosophy, software engineering

Preferences:
  - Likes: Deep theological discussions, Pauline epistles
  - Dislikes: Surface-level takes on scripture

=== END MEMORY PROFILE ===

=== DISCUSSION CONTEXT ===

Topics You've Explored:
  - Grace vs. works (discussed across 3 conversations)
  - Book of Romans (deep dive with Paul and Augustine)
  - Eschatology (started with Revelation, progressing to Daniel)

Key Conclusions:
  - Free will and sovereignty can coexist (Biblical Circle consensus)
  - Paul and Augustine found common ground on grace despite predestination disagreement

=== END DISCUSSION CONTEXT ===

=== PREVIOUS CONVERSATIONS ===

[Chat Jan 5 — "Book of Romans"]:
  • Paul: Emphasized justification by faith alone, cited Romans 3:28
  • Augustine: Argued for predestination, referenced Romans 9
  • User focus: Most interested in the grace question

[Chat Jan 12 — "Sermon on the Mount"]:
  • Jesus: Taught on righteousness exceeding the Pharisees'
  • Paul: Connected Sermon themes to Galatians

=== END PREVIOUS CONVERSATIONS ===
```

---

## Relevant Files

| File | Role | Changes |
|------|------|---------|
| `filter/langgraph_memory_filter.py` | **Primary target** | Schema, inlet, summarization, injection, graph workflow |
| `prompt/EXTRACTION_PROMPT.md` | Extraction model prompt | Add new fact types and rules |
| `prompt/SUMMARIZATION_PROMPT.md` | **New file** | Per-speaker summarization model prompt |
| `circleofspeakers/pipeline-public/pipelines/blueprints/openwebui_streaming_blueprint.py` | Base pipeline | No changes (`_truncate_history` stays as safety net) |
| `circleofspeakers/pipeline-public/pipelines/blueprints/circle_of_speakers_blueprint.py` | Circle pipeline | No changes |

---

## Verification Plan

1. **Summarization trigger** — Send a conversation with 8+ rounds → verify filter summarizes rounds 1-4 with per-speaker breakdowns, keeps rounds 5-8 verbatim, stores summaries in PostgreSQL
2. **Cross-conversation memory** — Chat A discusses Romans → start Chat B → verify round summary from Chat A is injected when user asks about related topics
3. **Relevance threshold** — Store 20+ facts + 5 summaries → send unrelated query → verify only relevant items (above threshold) get injected
4. **Soft age penalty** — Store a fact from 8 months ago → send related query → verify it's still injected (relevance overcomes penalty). Store a marginally relevant 8-month fact → verify it drops below threshold
5. **Per-speaker retrieval** — Query "what did Augustine say about grace?" → verify only Augustine's speaker summary is injected, not the entire round
6. **Discussion fact extraction** — Send a multi-speaker conversation → verify `discussion`, `conclusion`, `speaker_interaction` facts are extracted from both user AND assistant messages
7. **PII scrubbing** — Verify PII scrubbing works on assistant messages and new fact types
8. **Backward compatibility** — Existing v1 checkpoints (user facts only) still load correctly with empty `round_summaries`
9. **Pipeline safety net** — Verify `_truncate_history` still works as final safety cap after filter has already trimmed

---

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| **All logic in the filter inlet** | Pipeline doesn't need to know about memory/summarization. Same inlet pattern as v1. |
| **Relevance-based with soft age penalty** | Hard cutoffs are truncation by another name. Soft penalty is self-regulating. |
| **Per-speaker summaries, not aggregate** | Granular retrieval: "what did Paul say?" not "what was discussed?" |
| **Single user thread (`thread_id = user_id`)** | Cross-conversation memory is the whole point. `conversation_id` tags on summaries for context. |
| **Extract FIRST, then summarize** | Extraction sees full history and captures structured facts before rounds are trimmed. |
| **Summarization LLM call only when threshold exceeded** | Not every request — only when history grows beyond the cap. |
| **Round summaries are append-only** | Old summaries persist forever. Relevance scoring handles what gets injected. No deletion. |
| **Assistant messages included in extraction** | Needed for speaker interaction facts. PII scrubbing still applies. |
| **Nothing is deleted from the store** | Facts only disappear when the LLM merge decides they're contradicted or superseded. |

---

## Comparison: v1 vs v2

| Capability | v1 (Current) | v2 (Planned) |
|-----------|-------------|-------------|
| User facts | ✅ identity, prefs, goals | ✅ same + conversation_id tag |
| Discussion memory | ❌ | ✅ discussion, conclusion, speaker_interaction, topic_interest |
| Round summaries | ❌ | ✅ per-speaker, stored in PostgreSQL |
| Cross-conversation awareness | ❌ (only user facts) | ✅ full — summaries + discussion facts |
| Relevance filtering | ✅ LLM-based | ✅ LLM-based + soft age penalty |
| Time-based decay | ❌ (none) | Soft penalty only (not deletion) |
| History management | ❌ (pipeline truncation) | ✅ intelligent summarization in inlet |
| Per-speaker granularity | ❌ | ✅ individual SpeakerSummary per round |
| Pipeline changes needed | — | None |
| Extraction scope | User messages only | User + assistant messages |
| PII protection | ✅ 3-layer | ✅ 3-layer (extended to assistant msgs) |
