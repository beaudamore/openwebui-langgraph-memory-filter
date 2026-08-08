"""Tests for pgvector-backed fact storage SQL and fact helpers."""

from unittest.mock import MagicMock

from filter.vector_storage import VectorStore


def make_store(dimension=3):
    pool = MagicMock()
    connection = pool.connection.return_value.__enter__.return_value
    cursor = connection.cursor.return_value.__enter__.return_value
    return VectorStore(pool, dimension), pool, connection, cursor


def test_fact_key_is_stable_and_ignores_case_and_whitespace():
    fact_a = {"type": "Preference", "subject": " Coffee ", "value": "Dark Roast"}
    fact_b = {"type": "preference", "subject": "coffee", "value": "dark roast"}
    assert VectorStore.fact_key(fact_a) == VectorStore.fact_key(fact_b)


def test_upsert_writes_vector_literal_and_commits():
    store, _, connection, cursor = make_store()
    fact = {"type": "preference", "subject": "coffee", "value": "dark roast", "confidence": 0.9}

    store.upsert("user-1", fact, [0.1, 0.2, 0.3], "ollama", "nomic-embed-text")

    cursor.execute.assert_called_once()
    assert "ON CONFLICT (user_id, fact_key)" in cursor.execute.call_args.args[0]
    assert "[0.1,0.2,0.3]" in cursor.execute.call_args.args[1]
    connection.commit.assert_called_once()


def test_search_returns_only_database_matches():
    store, _, _, cursor = make_store()
    cursor.fetchall.return_value = [
        ("preference", "coffee", "dark roast", "positive", 0.9, 0.88),
    ]

    result = store.search("user-1", [0.1, 0.2, 0.3], limit=5, similarity_threshold=0.6)

    assert result == [{
        "type": "preference",
        "subject": "coffee",
        "value": "dark roast",
        "sentiment": "positive",
        "confidence": 0.9,
        "similarity": 0.88,
    }]
    assert "vector" in cursor.execute.call_args.args[0]
