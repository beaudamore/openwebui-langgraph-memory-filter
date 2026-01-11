-- ============================================================================
-- LangGraph Memory Database - Initialization Script
-- ============================================================================
-- This script provides OPTIONAL helper tables for analytics and monitoring.
-- LangGraph checkpoint tables (checkpoints, checkpoint_blobs, checkpoint_writes)
-- are created AUTOMATICALLY by the PostgresSaver.
--
-- Run this after first startup if you want analytics features:
--   docker exec -i langgraph-postgres psql -U langgraph -d langgraph_memory < init-postgres.sql
-- ============================================================================

-- Create extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- For text search

-- ============================================================================
-- Schema Migrations Table (created by filter, but we can ensure it exists)
-- ============================================================================
-- NOTE: The filter creates this table automatically on startup.
-- This is here for completeness and manual inspection.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT NOW(),
    description TEXT,
    changes JSONB
);

COMMENT ON TABLE schema_migrations IS 'Tracks filter schema versions for data structure evolution';

-- ============================================================================
-- User Memory Metadata (OPTIONAL - for analytics)
-- ============================================================================

CREATE TABLE IF NOT EXISTS user_memory_metadata (
    user_id VARCHAR(255) PRIMARY KEY,
    total_facts INTEGER DEFAULT 0,
    total_preferences INTEGER DEFAULT 0,
    total_relationships INTEGER DEFAULT 0,
    total_interests INTEGER DEFAULT 0,
    total_goals INTEGER DEFAULT 0,
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_user_memory_metadata_updated 
ON user_memory_metadata(last_updated DESC);

COMMENT ON TABLE user_memory_metadata IS 'Analytics: Aggregated user memory statistics';

-- ============================================================================
-- Conversation Tracking (OPTIONAL - for analytics)
-- ============================================================================

CREATE TABLE IF NOT EXISTS conversation_metadata (
    conversation_id VARCHAR(255) PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_message_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    message_count INTEGER DEFAULT 0,
    extraction_count INTEGER DEFAULT 0,  -- How many times memory extraction ran
    facts_extracted INTEGER DEFAULT 0     -- Total facts extracted from this conversation
);

CREATE INDEX IF NOT EXISTS idx_conversation_user 
ON conversation_metadata(user_id);

CREATE INDEX IF NOT EXISTS idx_conversation_last_message 
ON conversation_metadata(last_message_at DESC);

COMMENT ON TABLE conversation_metadata IS 'Analytics: Per-conversation tracking';

-- ============================================================================
-- Memory Analytics (OPTIONAL - for dashboards)
-- ============================================================================

CREATE TABLE IF NOT EXISTS memory_analytics (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    memory_type VARCHAR(50) NOT NULL,  -- preference, relationship, goal, interest, personal_info
    count INTEGER DEFAULT 0,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, memory_type)
);

CREATE INDEX IF NOT EXISTS idx_memory_analytics_user 
ON memory_analytics(user_id);

CREATE INDEX IF NOT EXISTS idx_memory_analytics_type 
ON memory_analytics(memory_type);

COMMENT ON TABLE memory_analytics IS 'Analytics: Breakdown of memory types per user';

-- ============================================================================
-- Preference Evolution Log (OPTIONAL - detailed preference tracking)
-- ============================================================================
-- This table can be populated by a future version of the filter
-- to provide detailed analytics on preference changes

CREATE TABLE IF NOT EXISTS preference_evolution_log (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    category VARCHAR(100) NOT NULL,
    value VARCHAR(500) NOT NULL,
    sentiment VARCHAR(20) NOT NULL,
    confidence FLOAT DEFAULT 0.8,
    mentioned_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    context TEXT,  -- Optional quote from conversation
    conversation_id VARCHAR(255)
);

CREATE INDEX IF NOT EXISTS idx_preference_evolution_user 
ON preference_evolution_log(user_id);

CREATE INDEX IF NOT EXISTS idx_preference_evolution_category 
ON preference_evolution_log(category, value);

CREATE INDEX IF NOT EXISTS idx_preference_evolution_time 
ON preference_evolution_log(mentioned_at DESC);

COMMENT ON TABLE preference_evolution_log IS 'Analytics: Detailed preference timeline for evolution tracking';

-- ============================================================================
-- Auto-update Timestamps
-- ============================================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.last_updated = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Trigger for user_memory_metadata
DROP TRIGGER IF EXISTS update_user_memory_metadata_updated_at 
ON user_memory_metadata;

CREATE TRIGGER update_user_memory_metadata_updated_at 
BEFORE UPDATE ON user_memory_metadata 
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- Useful Views (OPTIONAL)
-- ============================================================================

-- View: User memory summary
CREATE OR REPLACE VIEW v_user_memory_summary AS
SELECT 
    u.user_id,
    u.total_facts,
    u.total_preferences,
    u.total_relationships,
    u.last_updated,
    COUNT(DISTINCT c.conversation_id) as conversation_count,
    SUM(c.message_count) as total_messages
FROM user_memory_metadata u
LEFT JOIN conversation_metadata c ON u.user_id = c.user_id
GROUP BY u.user_id, u.total_facts, u.total_preferences, u.total_relationships, u.last_updated;

-- View: Recent preference changes (requires preference_evolution_log to be populated)
CREATE OR REPLACE VIEW v_recent_preference_changes AS
SELECT 
    user_id,
    category,
    value,
    sentiment,
    mentioned_at,
    LAG(sentiment) OVER (PARTITION BY user_id, category, value ORDER BY mentioned_at) as previous_sentiment
FROM preference_evolution_log
ORDER BY mentioned_at DESC;

-- ============================================================================
-- Grant Permissions
-- ============================================================================

GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO langgraph;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO langgraph;
GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public TO langgraph;

-- ============================================================================
-- Success Message
-- ============================================================================

DO $$
BEGIN
    RAISE NOTICE '============================================================';
    RAISE NOTICE 'LangGraph Memory Database - Analytics Tables Initialized';
    RAISE NOTICE '============================================================';
    RAISE NOTICE 'Core checkpoint tables are created automatically by filter.';
    RAISE NOTICE 'These analytics tables are OPTIONAL for monitoring/dashboards.';
    RAISE NOTICE '';
    RAISE NOTICE 'Tables created:';
    RAISE NOTICE '  - schema_migrations (version tracking)';
    RAISE NOTICE '  - user_memory_metadata (user analytics)';
    RAISE NOTICE '  - conversation_metadata (conversation tracking)';
    RAISE NOTICE '  - memory_analytics (type breakdown)';
    RAISE NOTICE '  - preference_evolution_log (detailed timeline)';
    RAISE NOTICE '============================================================';
END $$;
