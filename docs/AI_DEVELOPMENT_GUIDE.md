# LangGraph Memory Filter - AI Development Guide

> **For AI Assistants:** This document is specifically written for you to understand, modify, and maintain the LangGraph Memory Filter codebase. It contains critical architectural decisions, schema migration procedures, and patterns you MUST follow.

---

## 🎯 Quick Reference for AI Assistants

### Before Making Changes, Ask Yourself:

1. **Will this change affect stored data structure?** → See [Schema Migration](#schema-migration-system)
2. **Am I adding/removing/renaming fields?** → Increment `SCHEMA_VERSION` and add migration entry
3. **Does this change affect backward compatibility?** → Add fallback logic
4. **Am I changing extraction behavior?** → Document in migration changes

---

## 📁 File Structure

```
filters/memory_langgraph/
├── filter/
│   └── langgraph_memory_filter.py   # Main filter (1100+ lines) - THE CODE
├── docs/
│   ├── AI_DEVELOPMENT_GUIDE.md      # THIS FILE - for AI assistants
│   ├── LANGGRAPH_SETUP.md           # Human setup/installation guide
│   └── EXTRACTION_MODEL_SETUP.md    # Extraction model configuration
├── docker/
│   └── docker-compose.langgraph.yml # PostgreSQL + PgAdmin stack
└── prompt/
    └── EXTRACTION_PROMPT.md         # Reference extraction prompts
```

---

## 🏗️ Architecture Overview

### Data Flow

```
User Message → Inlet Filter → Memory State Retrieval → Context Injection
                    ↓
              [Background Task]
                    ↓
            Extraction LLM Call → JSON Parsing → Graph State Update
                    ↓
            PostgreSQL Checkpoint (via LangGraph)
```

### Core Components

| Component | Purpose | Location |
|-----------|---------|----------|
| `MemoryGraphState` | TypedDict defining all stored data | Lines ~60-130 |
| `SCHEMA_VERSION` | Current schema version number | Line ~42 |
| `SCHEMA_MIGRATIONS` | Migration history with field changes | Lines ~44-60 |
| `PII_PATTERNS` | Compiled regex patterns for PII detection | PII section |
| `detect_pii()` | Scan text for PII matches | PII section |
| `scrub_pii()` | Replace PII in text with [REDACTED] | PII section |
| `filter_facts_pii()` | Validate/filter extracted facts for PII | PII section |
| `Filter.inlet()` | Entry point - processes incoming messages | ~Lines 1150+ |
| `_update_user_memory_state()` | LLM merge + PII scrub + graph invoke | ~Lines 950+ |
| `_format_memory_context()` | Formats memories for injection | ~Lines 1060+ |

---

## 🔄 Schema Migration System

### **CRITICAL: Read This Before Modifying Data Structures**

The filter uses a schema versioning system to track data structure changes. This allows:
- Future code to understand what version data was created with
- Documentation of all breaking changes
- Potential for automated data migrations

### Current Schema Version

```python
SCHEMA_VERSION = 2  # As of 2026-01-09
```

### Migration Table (PostgreSQL)

```sql
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT NOW(),
    description TEXT,
    changes JSONB  -- Detailed field-level changes
);
```

### How to Add a New Migration

**Step 1:** Increment `SCHEMA_VERSION`

```python
SCHEMA_VERSION = 3  # Was 2
```

**Step 2:** Add migration entry to `SCHEMA_MIGRATIONS`

```python
SCHEMA_MIGRATIONS = [
    # ... existing migrations ...
    {
        "version": 3,
        "date": "2026-01-15",  # Use current date
        "description": "Brief description of changes",
        "changes": [
            {"type": "field_add", "entity": "Preference", "field": "new_field", "default": None},
            {"type": "field_remove", "entity": "Relationship", "field": "old_field"},
            {"type": "field_rename", "entity": "Goal", "old": "goal_text", "new": "description"},
            {"type": "behavior_change", "entity": "Interest", "description": "Now deduplicates by name+proficiency"}
        ]
    }
]
```

### Change Types

| Type | When to Use | Required Fields |
|------|-------------|-----------------|
| `field_add` | Adding new field to TypedDict | `entity`, `field`, `default` |
| `field_remove` | Removing field from TypedDict | `entity`, `field` |
| `field_rename` | Renaming a field | `entity`, `old`, `new` |
| `type_change` | Changing field type | `entity`, `field`, `old_type`, `new_type` |
| `behavior_change` | Logic change (dedup, sorting, etc.) | `entity`, `description` |

### Backward Compatibility Pattern

When renaming fields, ALWAYS add fallback logic:

```python
# CORRECT - handles both old and new field names
timestamp = pref.get("mentioned_at") or pref.get("first_mentioned") or pref.get("last_updated") or ""

# WRONG - breaks with old data
timestamp = pref["mentioned_at"]  # KeyError on old data!
```

---

## � PII Protection System

The filter includes a 3-layer PII protection system. Understanding this is critical before modifying extraction or storage logic.

### Architecture

```
User Message
    │
    ▼
[Layer 2: Regex Pre-Scrub] ─── scrub_pii() replaces PII with [REDACTED]
    │                          before messages reach the extraction LLM
    ▼
[Layer 1: Prompt Guardrails] ── Extraction prompt tells LLM to never extract PII
    │
    ▼
[Layer 3: Post-Validation] ─── filter_facts_pii() scans extracted facts and
    │                          drops/redacts any with PII patterns or blocked subjects
    ▼
Clean facts stored in PostgreSQL
```

### Key Functions

| Function | Purpose |
|----------|---------|
| `detect_pii(text)` | Scan text, return list of PII matches with type/label/match |
| `scrub_pii(text)` | Replace PII in text with `[REDACTED]` |
| `validate_fact_no_pii(fact)` | Check one fact for PII; returns `{clean, blocked_reasons, scrubbed_fact}` |
| `filter_facts_pii(facts, mode)` | Filter list of facts; mode `"remove"` or `"redact"` |

### PII Pattern List

Defined in `PII_PATTERNS` (top of file). Each entry has:
- `name`: Short ID (e.g., `"ssn"`, `"phone"`)
- `label`: Human-readable label
- `pattern`: Compiled regex
- `validator` (optional): Lambda for additional validation (e.g., SSN can't start with 000)

Current patterns: `ssn`, `credit_card`, `phone`, `email`, `street_address`, `passport`, `drivers_license`, `bank_account`, `dob`, `ip_address`

### Adding a New PII Pattern

```python
# Add to PII_PATTERNS list:
{
    "name": "vehicle_vin",
    "label": "Vehicle VIN",
    "pattern": re.compile(r'\b[A-HJ-NPR-Z0-9]{17}\b'),
    # Optional validator:
    "validator": lambda m: not m.group(0).isdigit(),  # VINs aren't all digits
}
```

Then add `"vehicle_vin"` to the default list in `pii_patterns_enabled` valve.

### Valve Configuration

| Valve | Default | Description |
|-------|---------|-------------|
| `pii_filter_enabled` | `True` | Master on/off for all PII filtering |
| `pii_filter_mode` | `"remove"` | `"remove"` drops facts, `"redact"` keeps with `[REDACTED]` |
| `pii_scrub_input` | `True` | Pre-scrub messages before extraction LLM |
| `pii_patterns_enabled` | all 10 | List of pattern names to detect |

### Subject Blocklist

`PII_SUBJECT_BLOCKLIST` contains fact subjects that are always blocked regardless of regex:
`ssn`, `social security`, `credit card`, `bank account`, `passport`, `drivers license`, `ip address`, etc.

### When Modifying Extraction Logic

- **Always** ensure PII pre-scrub runs before the merge prompt is built
- **Always** ensure post-validation runs on `new_facts` before the `facts_equal()` comparison
- If adding new fact types, check if they could contain PII and add to blocklist if needed
- The extraction prompt in `prompt/EXTRACTION_PROMPT.md` contains a PII POLICY section — keep it in sync

---

## �📊 Data Structures (TypedDicts)

### Preference (Schema v2)

```python
class Preference(TypedDict):
    """User preference - each entry is a timestamped data point for evolution tracking"""
    category: str           # food, color, activity, etc.
    value: str              # The specific thing (e.g., "pizza", "blue")
    sentiment: Literal["like", "dislike", "neutral", "love", "hate"]
    confidence: float       # 0.0 to 1.0
    mentioned_at: str       # ISO datetime - when this specific mention occurred
    context: Optional[str]  # Optional quote/context from conversation
```

**Key Design Decision:** Preferences are NOT deduplicated by (category, value). Every mention is kept as a separate timestamped entry to track sentiment evolution over time.

### Relationship

```python
class Relationship(TypedDict):
    entity_name: str         # "Sarah", "John", etc.
    relationship_type: str   # friend, family, colleague, partner
    details: Optional[str]   # Additional context
    first_mentioned: str     # ISO datetime
    last_updated: str        # ISO datetime - updated on re-mention
```

**Key Design Decision:** Relationships ARE deduplicated by (entity_name, relationship_type). Only one entry per relationship.

### Interest

```python
class Interest(TypedDict):
    interest_name: str           # "Python", "cooking", etc.
    proficiency: Optional[str]   # beginner, intermediate, expert
    frequency: Optional[str]     # daily, weekly, occasionally
    first_mentioned: str
    last_updated: str
```

**Key Design Decision:** Interests ARE deduplicated by interest_name (lowercase).

---

## 🔌 Key Methods Reference

### `_extract_information_node(state)`

**Purpose:** Calls extraction LLM to parse user information from conversation.

**Runs:** Synchronously (uses `asyncio.new_event_loop()` internally)

**Input:** MemoryGraphState with `conversation_history`

**Output:** MemoryGraphState with new entries appended to lists

**Important:** 
- Only processes last 10 messages (`recent_messages = state["conversation_history"][-10:]`)
- Requires `_user` and `_request` in state for LLM call
- Handles JSON cleaning (removes markdown code blocks)

### `_deduplicate_memories_node(state)`

**Purpose:** Removes exact duplicates while preserving evolution history.

**Key Logic:**
```python
# Preferences: Only dedupe exact same-minute extractions
key = (category, value, sentiment, timestamp[:16])

# Relationships: Dedupe by entity+type
key = (entity_name.lower(), relationship_type.lower())

# Interests: Dedupe by name only
key = interest_name.lower()
```

### `_format_memory_context(state)`

**Purpose:** Formats stored memories for injection into model context.

**Output Formats:**
- `structured` - Detailed with sections (default)
- `natural` - Conversational prose
- `bullet` - Simple bullet points

**Evolution Display:**
```
Preference Evolution:
  - Likes pizza (since 2026-01-01)
  - Oranges: liked (2026-01-01) → loved (2026-01-05) → hated (2026-01-09)
```

---

## ⚠️ Common Pitfalls for AI Assistants

### 1. Forgetting Backward Compatibility

```python
# ❌ WRONG - will crash on old data
date = pref["mentioned_at"]

# ✅ CORRECT - handles schema evolution
date = pref.get("mentioned_at") or pref.get("first_mentioned") or ""
```

### 2. Not Updating Schema Version

If you add/remove/rename ANY field in a TypedDict, you MUST:
1. Increment `SCHEMA_VERSION`
2. Add entry to `SCHEMA_MIGRATIONS`
3. Add backward compat fallbacks

### 3. Breaking the Annotation Pattern

LangGraph uses `Annotated[List[...], operator.add]` for automatic list merging:

```python
# ✅ CORRECT - LangGraph will auto-merge lists
preferences: Annotated[List[Preference], operator.add]

# ❌ WRONG - will overwrite instead of append
preferences: List[Preference]
```

### 4. Blocking Async Event Loop

The extraction node runs sync, but is called from async context:

```python
# ✅ CORRECT - creates new event loop for sync->async bridge
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
try:
    result = loop.run_until_complete(async_func())
finally:
    loop.close()

# ❌ WRONG - would block or crash
result = asyncio.run(async_func())  # Can't nest event loops
```

### 5. Not Using run_in_executor for Blocking Calls

```python
# ✅ CORRECT - doesn't block async loop
loop = asyncio.get_event_loop()
result = await loop.run_in_executor(None, blocking_function)

# ❌ WRONG - blocks entire async loop
result = blocking_function()
```

---

## 🧪 Testing Changes

### Clear Test Data

```bash
docker exec -i langgraph-postgres psql -U langgraph -d langgraph_memory \
  -c "TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes CASCADE;"
```

### View Current Schema Version

```bash
docker exec -it langgraph-postgres psql -U langgraph -d langgraph_memory \
  -c "SELECT * FROM schema_migrations ORDER BY version;"
```

### Check Stored Preferences

```bash
docker exec -it langgraph-postgres psql -U langgraph -d langgraph_memory \
  -c "SELECT thread_id, checkpoint FROM checkpoints ORDER BY created_at DESC LIMIT 1;"
```

### Trigger Extraction (Set Low Threshold)

In filter valves, set `extraction_threshold: 1` for immediate extraction on every message.

---

## 📝 Modification Checklist

When making changes to this filter, verify:

- [ ] Schema version incremented if data structure changed
- [ ] Migration entry added with all field changes documented
- [ ] Backward compatibility fallbacks added
- [ ] TypedDict updated with correct types
- [ ] `_format_memory_context()` updated to display new data
- [ ] `_create_summary_node()` updated for memory summary
- [ ] PII implications reviewed (new fact types checked for PII risk)
- [ ] PII patterns updated if new sensitive data types introduced
- [ ] Extraction prompt PII POLICY section updated if needed
- [ ] Test data cleared and fresh test performed
- [ ] Docs updated (this file, LANGGRAPH_SETUP.md if needed)

---

## 🔮 Future Enhancement Ideas

For AI assistants looking to extend this system:

1. **Actual Data Migration** - Currently just tracks versions. Could add `migrate_data(from_version, to_version)` that transforms existing records.

2. **Embedding-based Retrieval** - Add vector embeddings for semantic memory search instead of just injecting everything.

3. **Memory Decay** - Confidence scores that decrease over time if not reinforced.

4. **Cross-conversation Aggregation** - Merge memories across conversations for user-level profile.

5. **Privacy Controls** - Allow users to view/delete specific memories.

6. **Entity Resolution** - "Sarah", "my wife Sarah", "she" → same entity.

7. **Enhanced PII Detection** - Consider integrating Microsoft Presidio or spaCy NER for ML-based PII detection alongside regex patterns.

---

## 📚 Related Documentation

- [LANGGRAPH_SETUP.md](LANGGRAPH_SETUP.md) - Installation and configuration
- [EXTRACTION_MODEL_SETUP.md](EXTRACTION_MODEL_SETUP.md) - Extraction model setup
- [LangGraph Documentation](https://langchain-ai.github.io/langgraph/)
- [PostgreSQL Checkpoint](https://langchain-ai.github.io/langgraph/reference/checkpoints/)

---

## Version History

| Date | Schema | Changes |
|------|--------|---------|
| 2026-01-09 | v1 | Initial schema with first_mentioned/last_updated |
| 2026-01-09 | v2 | Preference evolution tracking - keep all data points, added mentioned_at and context fields |
| 2026-01-10 | v3 | Simplified to flexible fact-based schema |
| 2026-01-10 | v4 | LLM-powered semantic merge replaces code-based deduplication |
| 2026-02-17 | v4+ | 3-layer PII protection (prompt guardrails, regex pre-scrub, post-extraction validation) |

---

*Last updated: 2026-02-17 by AI Assistant (Claude)*
