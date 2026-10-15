# LangGraph Memory Filter - Dependency Resolution

## ✅ CURRENT PINS (Updated 2026-10-13, Open WebUI 0.12.0)

```bash
pip install "langgraph~=0.6.11" "langgraph-sdk<0.4" "langgraph-checkpoint-postgres<3" "psycopg[binary]" psycopg-pool
```

Frontmatter line (Open WebUI splits it on commas, so no commas inside a single spec):

```
requirements: langgraph~=0.6.11, langgraph-sdk<0.4, langgraph-checkpoint-postgres<3, psycopg[binary], psycopg-pool
```

### Why not langgraph 1.x any more

- Every langgraph 1.x release requires `langgraph-sdk 0.4.x`, and every sdk 0.4.x pins `websockets<17`.
- The Open WebUI 0.12.0 image ships `websockets 17.2` (used by uvicorn). Open WebUI installs all plugin
  requirements with pip at container start, *after* uvicorn has already imported websockets 17.2, so pip
  downgraded websockets to 16.1.1 on disk under the running process.
- websockets 17 removed the `OP_BINARY` constant from `websockets.frames`. The first import of this filter
  after the swap loaded 16.1.1 modules against the cached 17.2 `frames` module and failed with
  `cannot import name 'OP_BINARY' from 'websockets.frames'`, which killed the first chat after every restart.
- `langgraph-sdk` 0.2.x/0.3.x has no websockets dependency, and langgraph 0.6.11 imports cleanly against
  langchain-core 1.4.8 (verified in a venv; `pip check` clean; websockets stays at 17.2). The filter only uses
  `PostgresSaver`, `StateGraph` and `END`, all present in 0.6.
- Open WebUI 0.12.0 resolves this set to: langgraph 0.6.11, langgraph-sdk 0.2.15, langgraph-checkpoint 2.1.2,
  langgraph-checkpoint-postgres 2.0.25.

Re-check these pins whenever the Open WebUI image bumps websockets or langgraph-sdk 0.4+ drops its websockets cap.

## Previous solution (June 2025, superseded)

After extensive testing in isolated virtual environment, the working dependency configuration was:

```bash
pip install "langgraph>=1.0.0" langgraph-checkpoint-postgres "psycopg[binary]" psycopg-pool
```

---

## 🔍 Root Cause Analysis

### The Problem

The error `module 'psycopg_binary.pq' has no attribute 'PGcancelConn'` was a **red herring**. The REAL issue:

**`psycopg` (v3) is a meta-package that requires a backend implementation.**

When you install `psycopg` alone (even via `langgraph-checkpoint-postgres`), it installs the **core library only** - NO backend driver!

### LangGraph Version Update (IMPORTANT)

**OLD:** `langgraph==0.2.45` had compatibility issues with `langchain-core>=0.3.45`

**NEW:** `langgraph>=1.0.0` is required for modern langchain-core compatibility

The error message `cannot import name 'RunnableSerializable' from 'langchain_core.runnables'` indicates a langgraph/langchain-core version mismatch. Upgrading to langgraph 1.0+ resolves this.

### What `psycopg` v3 Needs

`psycopg` v3 requires ONE of these backends:

1. **`psycopg-c`** - C implementation (fastest)
   - ❌ Requires PostgreSQL development libraries (`pg_config`)
   - ❌ Needs compilation
   - ✅ Best performance

2. **`psycopg[binary]`** - Pre-compiled binary wheels
   - ✅ No compilation required
   - ✅ No system dependencies
   - ✅ **THIS IS WHAT WORKS**

3. **System `libpq`** - Pure Python using system PostgreSQL client library
   - ❌ Requires PostgreSQL client installed on system
   - ❌ Not portable across Docker containers

### Why Previous Attempts Failed

| Attempt | Command | Why It Failed |
|---------|---------|---------------|
| 1 | `pip install langgraph-checkpoint-postgres` | Only installs `psycopg` core, missing backend |
| 2 | `pip install langgraph==0.2.45` | Old version, incompatible with modern langchain-core |
| 3 | `pip install 'psycopg[binary]>=3.1.0'` | Missing psycopg-pool for connection pooling |

### The Working Solution

**Install all dependencies in one command:**

```bash
pip install "langgraph>=1.0.0" langgraph-checkpoint-postgres "psycopg[binary]" psycopg-pool
```

**This installs:**
- `langgraph>=1.0.0` - Core LangGraph with modern langchain-core compatibility
- `langgraph-checkpoint-postgres` - PostgreSQL checkpointer
- `psycopg[binary]` - PostgreSQL adapter with pre-compiled binary backend
- `psycopg-pool` - Connection pooling (required by the filter)

---

## ✅ Verification Tests

All tests passed in isolated virtual environment:

### Import Tests
```python
from langgraph.checkpoint.postgres import PostgresSaver  # ✅
from langgraph.graph import StateGraph, START, END       # ✅
from langchain_core.messages import BaseMessage          # ✅
from psycopg_pool import ConnectionPool                  # ✅
```

### Initialization Test
```python
PostgresSaver.from_conn_string  # ✅ Method exists and callable
# In langgraph 1.0+, use as context manager
```

### Graph Compilation Test
```python
workflow = StateGraph(SomeState)
workflow.add_node("test", test_fn)
graph = workflow.compile()  # ✅ Compiles successfully
```

---

## 📦 Recommended Versions (June 2025)

**Installation command:**
```bash
pip install "langgraph>=1.0.0" langgraph-checkpoint-postgres "psycopg[binary]" psycopg-pool
```

**This will install approximately:**
```
langgraph>=1.0.0                    # Core LangGraph framework
langgraph-checkpoint>=2.0.0         # Auto-installed by langgraph
langgraph-checkpoint-postgres>=2.0  # PostgreSQL checkpointer
psycopg>=3.1.0                      # PostgreSQL adapter (with binary backend)
psycopg-pool>=3.1.0                 # Connection pooling
langchain-core>=0.3.0               # LangChain core (auto-installed)
```

**Key Compatibility:**
- LangGraph 1.0+ requires langchain-core 0.3+
- psycopg[binary] includes the binary backend driver
- psycopg-pool is needed for connection pooling in the filter

---

## 🐋 Docker Installation

### In OpenWebUI Container

```bash
# Find container name
docker ps | grep open-webui

# One-liner install
docker exec -it open-webui-beaudamore pip install \
  "langgraph>=1.0.0" \
  langgraph-checkpoint-postgres \
  "psycopg[binary]" \
  psycopg-pool
```

### In Custom Dockerfile

```dockerfile
FROM ghcr.io/open-webui/open-webui:main

RUN pip install --no-cache-dir \
    "langgraph>=1.0.0" \
    langgraph-checkpoint-postgres \
    "psycopg[binary]" \
    psycopg-pool
```

---

## 🔬 Testing Methodology

1. **Created isolated venv:** `/Users/beaudamore/Source/filters/memory_langgraph/test_env`
2. **Installed minimal deps:** Only langgraph packages
3. **Reproduced error:** Same `ImportError: no pq wrapper available`
4. **Tested backends:**
   - ❌ `psycopg[c]` - Failed (needs `pg_config`)
   - ✅ `psycopg[binary]` - **SUCCESS**
5. **Tested langgraph versions:**
   - ❌ `langgraph==0.2.45` - Fails with langchain-core 0.3.45+ (`RunnableSerializable` error)
   - ✅ `langgraph>=1.0.0` - Works with modern langchain-core
6. **Verified imports:** All filter imports work
7. **Documented solution:** Updated requirements and docs

---

## ⚠️ Common Error Messages

### Error: `cannot import name 'RunnableSerializable'`
```
ImportError: cannot import name 'RunnableSerializable' from 'langchain_core.runnables'
```
**Solution:** Upgrade langgraph to 1.0+: `pip install "langgraph>=1.0.0"`

### Error: `ImportError: no pq wrapper available`
```
ImportError: no pq wrapper available.
Attempts made:
- couldn't import psycopg 'c' implementation: No module named 'psycopg_c'
- couldn't import psycopg 'binary' implementation: No module named 'psycopg_binary'
```
**Solution:** Install psycopg with binary backend: `pip install "psycopg[binary]"`

### Error: `Connection pool requires psycopg_pool`
```
ModuleNotFoundError: No module named 'psycopg_pool'
```
**Solution:** Install psycopg-pool: `pip install psycopg-pool`

---

## 📚 Reference

- **psycopg v3 docs:** https://www.psycopg.org/psycopg3/docs/basic/install.html
- **langgraph-checkpoint-postgres:** https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-postgres
- **LangGraph docs:** https://langchain-ai.github.io/langgraph/
- **GitHub Issue (similar):** https://github.com/psycopg/psycopg/issues/

---

## 🎯 Summary for Future Debugging

**TL;DR:** 
1. `psycopg` v3 is split into:
   - `psycopg` - Core library (types, API)
   - `psycopg[binary]` - Backend driver (required!)
   - `psycopg-pool` - Connection pooling (required by filter)

2. `langgraph` must be version 1.0+ for modern langchain-core compatibility

**Always install ALL of these when using langgraph-checkpoint-postgres:**

```bash
pip install "langgraph>=1.0.0" langgraph-checkpoint-postgres "psycopg[binary]" psycopg-pool
```

---

**Original Resolution Date:** January 9, 2026  
**Last Updated:** June 2025 (langgraph 1.0+ update)  
**Tested On:** macOS (ARM64), Python 3.13  
**Status:** ✅ VERIFIED WORKING
