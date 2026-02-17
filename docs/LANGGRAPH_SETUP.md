# LangGraph Memory Filter - Enterprise Setup Guide

> **For AI Assistants:** If you're modifying this filter, read [AI_DEVELOPMENT_GUIDE.md](AI_DEVELOPMENT_GUIDE.md) first!

## Overview

Enterprise-grade graph-based memory system using LangGraph with PostgreSQL for:
- **Relationship tracking** - "John's wife Sarah likes Italian food"
- **Temporal reasoning** - "What did user say about their job before the promotion?"
- **Preference evolution** - Track how user opinions change over time ("used to like → now loves → now hates")
- **Schema versioning** - Built-in migration tracking for data structure changes
- **Complex queries** - "Get all friends who share common interests"
- **Scalability** - Handle millions of users with PostgreSQL

### Current Versions
- **Filter Schema:** v2 (see `SCHEMA_VERSION` in filter)
- **LangGraph:** 1.0.5 (upgraded from 0.2.45)
- **PostgreSQL:** 16-alpine

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Open WebUI Container                     │
│                                                               │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  LangGraph Memory Filter (Python)                     │  │
│  │  • Extracts user info from conversations              │  │
│  │  • Manages graph state (relationships, prefs, etc.)   │  │
│  │  • Injects memories into model context                │  │
│  └───────────────────┬───────────────────────────────────┘  │
│                      │                                        │
│                      │ PostgreSQL Protocol                    │
│                      ▼                                        │
└──────────────────────┼────────────────────────────────────────┘
                       │
                       │ Port 5432
                       ▼
┌─────────────────────────────────────────────────────────────┐
│              PostgreSQL Container (postgres:16)              │
│                                                               │
│  • LangGraph checkpoints (graph states)                      │
│  • User memory metadata                                      │
│  • Conversation tracking                                     │
│  • Memory analytics                                          │
│                                                               │
│  Volume: langgraph_postgres_data (persistent)                │
└─────────────────────────────────────────────────────────────┘
```

---

## Installation

### 1. Start PostgreSQL

#### Option A: Using Docker Compose (Command Line)

```bash
cd /path/to/filters/memory_langgraph/docker

# Start PostgreSQL and PgAdmin
docker-compose -f docker-compose.langgraph.yml up -d

# Check status
docker-compose -f docker-compose.langgraph.yml ps

# View logs
docker-compose -f docker-compose.langgraph.yml logs -f langgraph-postgres
```

#### Option B: Using Portainer (Recommended for GUI)

1. **Access Portainer**
   - Navigate to your Portainer instance (e.g., `https://your-server:9443`)

2. **Add Stack**
   - Go to **Stacks** → **+ Add stack**
   - Name: `langgraph-memory`

3. **Upload or Paste docker-compose.yml**
   
   **Method 1: Web Editor**
   - Select **Web editor**
   - Copy the contents of `docker-compose.langgraph.yml`
   - Paste into the editor
   
   **Method 2: Upload**
   - Select **Upload**
   - Click **Select file**
   - Upload `docker-compose.langgraph.yml`

   **Method 3: Git Repository** (if using git)
   - Select **Repository**
   - Enter your git URL
   - Set compose file path: `filters/memory_langgraph/docker/docker-compose.langgraph.yml`

4. **Deploy Stack**
   - Scroll down and click **Deploy the stack**
   - Wait for containers to start (check status in Containers view)

5. **Verify Deployment**
   - Go to **Containers**
   - Look for `langgraph-postgres` and `langgraph-pgadmin`
   - Both should show status: **running** with green indicator

**Note:** LangGraph creates all required checkpoint and schema tables automatically on first run — no manual SQL setup is needed.

**Services:**
- PostgreSQL: `localhost:5432`
- PgAdmin (web UI): `http://localhost:5050`
  - Email: `admin@langgraph.local`
  - Password: `admin` (CHANGE IN PRODUCTION!)

### 2. Install Python Dependencies in OpenWebUI Container

**CRITICAL: These dependencies MUST be installed inside your OpenWebUI container.**

```bash
# Find your OpenWebUI container name
docker ps | grep open-webui

# Example output:
# abc123  open-webui  "bash start.sh"  open-webui-beaudamore

# Install dependencies (replace container name with yours)
docker exec -it open-webui-beaudamore bash

# Inside container, run:
pip install langgraph>=1.0.0 langgraph-checkpoint-postgres psycopg[binary] psycopg-pool

# Exit container
exit
```

**One-liner (no need to enter container):**
```bash
docker exec -it open-webui-beaudamore pip install langgraph>=1.0.0 langgraph-checkpoint-postgres psycopg[binary] psycopg-pool
```

**Note:** `langgraph` 1.0+ will automatically install compatible versions of `langchain-core` as dependencies.

**Important Notes:**
- ⚠️ Dependencies installed this way are **lost on container restart/recreation**
- ✅ For persistent installation, use a custom Dockerfile (see "Production Deployment" section below)
- ✅ Verify installation: `docker exec -it open-webui-beaudamore pip list | grep langgraph`

### 3. Deploy Filter

```bash
# Copy filter to Open WebUI functions directory
# Replace <your-openwebui-container-name> with your actual container name

docker cp /path/to/langgraph_memory_filter.py <your-openwebui-container-name>:/app/backend/data/functions/

# Verify it's there
docker exec -it <your-openwebui-container-name> ls -la /app/backend/data/functions/
```

**Alternative:** If you have a volume mount for functions, copy directly to the mounted directory.

### 4. Configure Filter in Open WebUI

1. Navigate to **Settings** → **Functions**
2. Find **LangGraph Memory Graph**
3. Enable the filter
4. **IMPORTANT: Configure Extraction Model** (see [EXTRACTION_MODEL_SETUP.md](EXTRACTION_MODEL_SETUP.md))
   - Quick start: Set `extraction_model_id` to an existing model (e.g., `llama3.2:latest`)
   - Recommended: Create a custom model with extraction system prompt
5. Configure other valves:
   - `postgres_host`: `langgraph-postgres` (if using docker-compose network)
   - `postgres_password`: Change from default!
   - Other settings as desired

### 5. Verify Installation

```bash
# Check PostgreSQL connection
docker exec -it langgraph-postgres psql -U langgraph -d langgraph_memory -c "\dt"

# Should show LangGraph checkpoint tables (created automatically)
# Tables: checkpoints, checkpoint_writes, checkpoint_blobs, checkpoint_migrations, schema_migrations

# Check schema version
docker exec -it langgraph-postgres psql -U langgraph -d langgraph_memory \
  -c "SELECT * FROM schema_migrations ORDER BY version;"
```

---

## Schema Versioning

The filter includes a built-in schema migration system to track data structure changes over time.

### Check Current Version

```bash
docker exec -it langgraph-postgres psql -U langgraph -d langgraph_memory \
  -c "SELECT version, applied_at, description FROM schema_migrations ORDER BY version;"
```

### Migration Table

| Version | Date | Description |
|---------|------|-------------|
| 1 | 2026-01-09 | Initial schema with first_mentioned/last_updated fields |
| 2 | 2026-01-09 | Preference evolution tracking - keep all data points |

### How It Works

1. On startup, the filter checks `schema_migrations` table
2. If current `SCHEMA_VERSION` > database version, new migrations are recorded
3. Each migration documents field changes (adds, removes, renames)
4. This allows future code to understand data compatibility

### For Developers

See [AI_DEVELOPMENT_GUIDE.md](AI_DEVELOPMENT_GUIDE.md) for detailed instructions on:
- Adding new schema versions
- Documenting field changes
- Implementing backward compatibility

---

## Configuration

### PostgreSQL Valves

| Valve | Default | Description |
|-------|---------|-------------|
| `postgres_host` | `langgraph-postgres` | PostgreSQL hostname |
| `postgres_port` | `5432` | PostgreSQL port |
| `postgres_database` | `langgraph_memory` | Database name |
| `postgres_user` | `langgraph` | Database user |
| `postgres_password` | ⚠️ **CHANGE ME** | Database password |

### Memory Processing Valves

| Valve | Default | Description |
|-------|---------|-------------|
| `enable_memory_extraction` | `true` | Auto-extract memories from conversations |
| `extraction_threshold` | `3` | Messages before triggering extraction |
| `enable_conversation_memory` | `true` | Store per-conversation vs user-wide |
| `max_conversation_history` | `50` | Max messages to keep in graph state |

### Injection Valves

| Valve | Default | Description |
|-------|---------|-------------|
| `enable_memory_injection` | `true` | Inject memories into model context |
| `max_injected_memories` | `10` | Max facts to inject |
| `memory_injection_format` | `structured` | Format: structured/natural/bullet |

---

## Usage Examples

### Basic Usage

Once enabled, the filter automatically:

1. **Extracts** information as users chat
2. **Stores** it in graph format in PostgreSQL
3. **Retrieves** relevant memories for context
4. **Injects** memories into model prompts

### Example Conversation

```
User: "My name is Alex and I work at Microsoft as a Senior Engineer"
→ Extracted: personal_info.name="Alex", personal_info.occupation="Senior Engineer", 
             personal_info.company="Microsoft"

User: "My friend Sarah loves Italian food"
→ Extracted: relationship(entity="Sarah", type="friend"), 
             preference(category="food", value="Italian food", sentiment="love")

User: "I'm planning to learn Python this year"
→ Extracted: goal(text="learn Python", category="skill", status="active")

--- Later, sentiment changes ---

User: "Actually I don't like Italian food anymore"
→ Extracted: NEW preference entry (category="food", value="Italian food", sentiment="dislike")
→ Both entries kept for evolution tracking!

--- Later conversation ---

User: "What restaurants should I recommend to my friend?"
→ Injected: "Your friend Sarah loves Italian food"
→ Injected: "Italian Food: loved (2026-01-01) → disliked (2026-01-09)"
→ Model can see the evolution and respond appropriately
```

### Graph Queries (Advanced)

The graph structure enables complex queries:

```python
# Example: Get all friends' preferences
# Query: "What are my friends' favorite foods?"
# System can traverse: user → relationships (friends) → preferences (food)

# Example: Temporal query
# Query: "What did I say about my job before I mentioned the promotion?"
# System can filter by timestamp and relationship
```

---

## Database Management

### Access PgAdmin

1. Open `http://localhost:5050`
2. Login with credentials
3. Add server:
   - Name: `LangGraph Memory`
   - Host: `langgraph-postgres`
   - Port: `5432`
   - Username: `langgraph`
   - Password: (your password)
   - Database: `langgraph_memory`

### Direct SQL Access

```bash
# Connect to PostgreSQL
docker exec -it langgraph-postgres psql -U langgraph -d langgraph_memory

# View checkpoint tables
\dt

# View memory metadata
SELECT * FROM user_memory_metadata;

# View conversation tracking
SELECT * FROM conversation_metadata;

# View memory analytics
SELECT * FROM memory_analytics;

# Exit
\q
```

### Backup Database

```bash
# Create backup
docker exec langgraph-postgres pg_dump -U langgraph langgraph_memory > backup.sql

# Restore backup
docker exec -i langgraph-postgres psql -U langgraph langgraph_memory < backup.sql
```

---

## Monitoring

### Memory Statistics

```sql
-- Total users with memories
SELECT COUNT(*) FROM user_memory_metadata;

-- Memory breakdown by type
SELECT memory_type, SUM(count) as total 
FROM memory_analytics 
GROUP BY memory_type;

-- Most active users
SELECT user_id, total_facts, last_updated 
FROM user_memory_metadata 
ORDER BY total_facts DESC 
LIMIT 10;

-- Recent conversations
SELECT c.conversation_id, c.user_id, c.message_count, c.last_message_at
FROM conversation_metadata c
ORDER BY c.last_message_at DESC
LIMIT 20;
```

### Performance Monitoring

```sql
-- Database size
SELECT pg_size_pretty(pg_database_size('langgraph_memory'));

-- Table sizes
SELECT 
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;

-- Active connections
SELECT count(*) FROM pg_stat_activity WHERE datname = 'langgraph_memory';
```

---

## Security Best Practices

### 🔒 Production Deployment

1. **Change default passwords:**
   ```yaml
   # In docker-compose.langgraph.yml
   POSTGRES_PASSWORD: your_strong_password_here
   PGADMIN_DEFAULT_PASSWORD: your_admin_password_here
   ```

2. **Restrict network access:**
   ```yaml
   # Remove port exposure if not needed externally
   # ports:
   #   - "5432:5432"
   ```

3. **Enable SSL:**
   ```sql
   -- Generate SSL certificates
   -- Configure postgresql.conf for SSL
   ```

4. **Regular backups:**
   ```bash
   # Automated backup script
   0 2 * * * docker exec langgraph-postgres pg_dump -U langgraph langgraph_memory | gzip > /backups/langgraph_$(date +\%Y\%m\%d).sql.gz
   ```

5. **User isolation:**
   - Filter automatically isolates memories by user_id
   - PostgreSQL row-level security (optional)

---

## Troubleshooting

### Connection Issues

```bash
# Check if PostgreSQL is running
docker-compose -f docker-compose.langgraph.yml ps

# Check PostgreSQL logs
docker-compose -f docker-compose.langgraph.yml logs langgraph-postgres

# Test connection from Open WebUI container
docker exec -it open-webui bash
pip install psycopg2-binary
python -c "import psycopg2; conn = psycopg2.connect('host=langgraph-postgres port=5432 dbname=langgraph_memory user=langgraph password=langgraph_password_change_me'); print('Connected!')"
```

### Network Issues

If Open WebUI can't reach PostgreSQL:

```yaml
# Add Open WebUI to the LangGraph network
# In your Open WebUI docker-compose.yml:
networks:
  - langgraph_network

networks:
  langgraph_network:
    external: true
```

### Memory Not Extracting

1. Check debug logs:
   - Enable `debug_mode` in filter valves
   - Check Open WebUI logs: `docker logs -f open-webui`

2. Verify extraction model is accessible
3. Check `extraction_threshold` - might need more messages

### Performance Issues

```sql
-- Add indexes if queries are slow
CREATE INDEX idx_checkpoint_thread ON checkpoints(thread_id);
CREATE INDEX idx_checkpoint_timestamp ON checkpoints(checkpoint_ns);

-- Vacuum database
VACUUM ANALYZE;
```

---

## Scaling Considerations

### For Large Deployments (100k+ users)

1. **Connection Pooling:**
   ```python
   # Use pgbouncer for connection pooling
   postgres_host: "pgbouncer"  # Instead of direct postgres
   ```

2. **Read Replicas:**
   - Set up PostgreSQL streaming replication
   - Route read queries to replicas

3. **Partitioning:**
   ```sql
   -- Partition checkpoint tables by user_id ranges
   CREATE TABLE checkpoints_partition_1 PARTITION OF checkpoints
   FOR VALUES FROM (0) TO (100000);
   ```

4. **Sharding:**
   - Multiple PostgreSQL instances
   Production Deployment

### Custom Dockerfile with Dependencies

For production, bake dependencies into a custom OpenWebUI image:

```dockerfile
# Dockerfile
FROM ghcr.io/open-webui/open-webui:main

# Install LangGraph memory dependencies
RUN pip install --no-cache-dir \
    langgraph>=1.0.0 \
    langgraph-checkpoint-postgres \
    psycopg[binary] \
    psycopg-pool

# Optionally copy filter if you want it baked in
# COPY langgraph_memory_filter.py /app/backend/data/functions/
```

Build and run:
```bash
docker build -t open-webui-langgraph:latest .
docker run -d --name open-webui-custom \
  -p 3000:8080 \
  -v open-webui:/app/backend/data \
  --network langgraph_network \
  open-webui-langgraph:latest
```

---

## Uninstallation

```bash
# Stop and remove containers
cd /path/to/filters/memory_langgraph/docker
docker-compose -f docker-compose.langgraph.yml down

# Remove volumes (⚠️ DELETES ALL DATA!)
docker-compose -f docker-compose.langgraph.yml down -v

# Remove filter from Open WebUI
docker exec -it <your-openwebui-container-name> rm /app/backend/data/functions/langgraph_memory_filter.py

# Uninstall Python dependencies (optional)
docker exec -it <your-openwebui-container-name> pip uninstall -y langgraph langgraph-checkpoint-postgres langchain-core psycopg
docker-compose -f docker-compose.langgraph.yml down -v

# Remove filter from Open WebUI
docker exec -it open-webui rm /app/backend/data/functions/langgraph_memory_filter.py
```

---

## Support & Resources

- **AI Development Guide:** [AI_DEVELOPMENT_GUIDE.md](AI_DEVELOPMENT_GUIDE.md) - For AI assistants modifying this code
- **LangGraph Docs:** https://langchain-ai.github.io/langgraph/
- **PostgreSQL Docs:** https://www.postgresql.org/docs/
- **Open WebUI:** https://docs.openwebui.com/

---

## License

MIT License - See main filter file for details
