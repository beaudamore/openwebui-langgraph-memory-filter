psql -U langgraph -d langgraph_memory << 'EOSQL'
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT NOW(),
    description TEXT,
    changes JSONB
);

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

CREATE INDEX IF NOT EXISTS idx_user_memory_metadata_updated ON user_memory_metadata(last_updated DESC);

CREATE TABLE IF NOT EXISTS conversation_metadata (
    conversation_id VARCHAR(255) PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_message_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    message_count INTEGER DEFAULT 0,
    extraction_count INTEGER DEFAULT 0,
    facts_extracted INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_conversation_user ON conversation_metadata(user_id);
CREATE INDEX IF NOT EXISTS idx_conversation_last_message ON conversation_metadata(last_message_at DESC);

CREATE TABLE IF NOT EXISTS memory_analytics (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    memory_type VARCHAR(50) NOT NULL,
    count INTEGER DEFAULT 0,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, memory_type)
);

CREATE INDEX IF NOT EXISTS idx_memory_analytics_user ON memory_analytics(user_id);
CREATE INDEX IF NOT EXISTS idx_memory_analytics_type ON memory_analytics(memory_type);

CREATE TABLE IF NOT EXISTS preference_evolution_log (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    category VARCHAR(100) NOT NULL,
    value VARCHAR(500) NOT NULL,
    sentiment VARCHAR(20) NOT NULL,
    confidence FLOAT DEFAULT 0.8,
    mentioned_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    context TEXT,
    conversation_id VARCHAR(255)
);

CREATE INDEX IF NOT EXISTS idx_preference_evolution_user ON preference_evolution_log(user_id);
CREATE INDEX IF NOT EXISTS idx_preference_evolution_category ON preference_evolution_log(category, value);
CREATE INDEX IF NOT EXISTS idx_preference_evolution_time ON preference_evolution_log(mentioned_at DESC);

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.last_updated = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

DROP TRIGGER IF EXISTS update_user_memory_metadata_updated_at ON user_memory_metadata;
CREATE TRIGGER update_user_memory_metadata_updated_at BEFORE UPDATE ON user_memory_metadata FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE OR REPLACE VIEW v_user_memory_summary AS
SELECT u.user_id, u.total_facts, u.total_preferences, u.total_relationships, u.last_updated,
COUNT(DISTINCT c.conversation_id) as conversation_count, SUM(c.message_count) as total_messages
FROM user_memory_metadata u LEFT JOIN conversation_metadata c ON u.user_id = c.user_id
GROUP BY u.user_id, u.total_facts, u.total_preferences, u.total_relationships, u.last_updated;

CREATE OR REPLACE VIEW v_recent_preference_changes AS
SELECT user_id, category, value, sentiment, mentioned_at,
LAG(sentiment) OVER (PARTITION BY user_id, category, value ORDER BY mentioned_at) as previous_sentiment
FROM preference_evolution_log ORDER BY mentioned_at DESC;

GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO langgraph;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO langgraph;
GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public TO langgraph;
EOSQL