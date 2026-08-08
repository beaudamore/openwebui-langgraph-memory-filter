"""Vector storage and pgvector semantic retrieval for LangGraph user facts."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg_pool

logger = logging.getLogger("openwebui.filters.vector_storage")


class VectorStore:
    """Persist denormalized fact embeddings and retrieve them by cosine similarity."""

    def __init__(
        self,
        pool: psycopg_pool.ConnectionPool,
        embedding_dimension: int,
        logger_fn=None,
    ):
        self.pool = pool
        self.embedding_dimension = embedding_dimension
        self._log = logger_fn or (lambda message, level="info": getattr(logger, level)(message))

    def initialize(self) -> None:
        """Create the extension, fact-embedding table, and indexes if absent."""
        if self.embedding_dimension <= 0:
            raise ValueError("embedding_dimension must be positive")

        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS memory_fact_embeddings (
                        id BIGSERIAL PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        fact_key TEXT NOT NULL,
                        fact_type TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        value TEXT NOT NULL,
                        sentiment TEXT,
                        confidence DOUBLE PRECISION NOT NULL DEFAULT 0.8,
                        embedding vector({self.embedding_dimension}) NOT NULL,
                        embedding_provider TEXT NOT NULL,
                        embedding_model TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (user_id, fact_key)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_memory_fact_embeddings_user
                    ON memory_fact_embeddings (user_id, updated_at DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_memory_fact_embeddings_user_type
                    ON memory_fact_embeddings (user_id, fact_type)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_memory_fact_embeddings_hnsw
                    ON memory_fact_embeddings USING hnsw (embedding vector_cosine_ops)
                    """
                )
            conn.commit()
        self._log("pgvector fact-embedding storage initialized", "info")

    @staticmethod
    def fact_text(fact: Dict[str, Any]) -> str:
        """Produce a stable, meaningful text representation for an embedding."""
        pieces = [
            f"Type: {fact.get('type', 'other')}",
            f"Subject: {fact.get('subject', '')}",
            f"Value: {fact.get('value', '')}",
        ]
        sentiment = fact.get("sentiment")
        if sentiment:
            pieces.append(f"Sentiment: {sentiment}")
        return "\n".join(pieces)

    @staticmethod
    def fact_key(fact: Dict[str, Any]) -> str:
        """Generate a deterministic key for upserting one logical fact."""
        canonical = "\x1f".join(
            str(fact.get(field, "")).strip().lower()
            for field in ("type", "subject", "value")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def upsert(
        self,
        user_id: str,
        fact: Dict[str, Any],
        embedding: List[float],
        provider: str,
        model: str,
    ) -> None:
        """Insert or update an embedding for a user fact."""
        if len(embedding) != self.embedding_dimension:
            raise ValueError(
                f"Embedding dimension mismatch: got {len(embedding)}, "
                f"expected {self.embedding_dimension}"
            )

        now = datetime.now(timezone.utc)
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory_fact_embeddings (
                        user_id, fact_key, fact_type, subject, value, sentiment,
                        confidence, embedding, embedding_provider, embedding_model,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s::vector, %s, %s, %s, %s
                    )
                    ON CONFLICT (user_id, fact_key) DO UPDATE SET
                        fact_type = EXCLUDED.fact_type,
                        subject = EXCLUDED.subject,
                        value = EXCLUDED.value,
                        sentiment = EXCLUDED.sentiment,
                        confidence = EXCLUDED.confidence,
                        embedding = EXCLUDED.embedding,
                        embedding_provider = EXCLUDED.embedding_provider,
                        embedding_model = EXCLUDED.embedding_model,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        user_id,
                        self.fact_key(fact),
                        fact.get("type", "other"),
                        fact.get("subject", ""),
                        fact.get("value", ""),
                        fact.get("sentiment"),
                        float(fact.get("confidence", 0.8)),
                        self._vector_literal(embedding),
                        provider,
                        model,
                        now,
                        now,
                    ),
                )
            conn.commit()

    def search(
        self,
        user_id: str,
        query_embedding: List[float],
        *,
        limit: int,
        similarity_threshold: float,
        exclude_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Find this user's facts with cosine similarity at or above the threshold."""
        if len(query_embedding) != self.embedding_dimension:
            raise ValueError(
                f"Query dimension mismatch: got {len(query_embedding)}, "
                f"expected {self.embedding_dimension}"
            )

        clauses = ["user_id = %s"]
        params: List[Any] = [user_id, self._vector_literal(query_embedding)]
        if exclude_types:
            clauses.append("NOT (fact_type = ANY(%s))")
            params.append(exclude_types)
        params.extend([similarity_threshold, limit])

        sql = f"""
            SELECT fact_type, subject, value, sentiment, confidence,
                   1 - (embedding <=> %s::vector) AS similarity
            FROM memory_fact_embeddings
            WHERE {' AND '.join(clauses)}
              AND 1 - (embedding <=> %s::vector) >= %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """
        # query embedding occurs in SELECT, WHERE, and ORDER BY.
        query_literal = self._vector_literal(query_embedding)
        actual_params: List[Any] = [
            query_literal,
            user_id,
        ]
        if exclude_types:
            actual_params.append(exclude_types)
        actual_params.extend([query_literal, similarity_threshold, query_literal, limit])

        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, actual_params)
                rows = cur.fetchall()

        return [
            {
                "type": row[0],
                "subject": row[1],
                "value": row[2],
                "sentiment": row[3],
                "confidence": float(row[4]),
                "similarity": float(row[5]),
            }
            for row in rows
        ]

    def remove_stale(self, user_id: str, facts: List[Dict[str, Any]]) -> None:
        """Remove embeddings whose facts no longer occur in the current checkpoint state."""
        keys = [self.fact_key(fact) for fact in facts]
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                if keys:
                    cur.execute(
                        """
                        DELETE FROM memory_fact_embeddings
                        WHERE user_id = %s AND NOT (fact_key = ANY(%s))
                        """,
                        (user_id, keys),
                    )
                else:
                    cur.execute("DELETE FROM memory_fact_embeddings WHERE user_id = %s", (user_id,))
            conn.commit()

    @staticmethod
    def _vector_literal(values: List[float]) -> str:
        return "[" + ",".join(str(float(value)) for value in values) + "]"
