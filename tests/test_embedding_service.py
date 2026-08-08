"""Tests for local-first embedding-provider failover."""

from unittest.mock import AsyncMock

import pytest

from filter.embedding_service import EmbeddingServiceRouter, OllamaEmbedding, vLLMEmbedding


@pytest.mark.asyncio
async def test_router_uses_primary_healthy_provider():
    router = EmbeddingServiceRouter(primary_provider="ollama", embedding_dimension=3)
    ollama = AsyncMock(spec=OllamaEmbedding)
    ollama.model = "nomic-embed-text"
    ollama.health_check.return_value = True
    ollama.embed.return_value = [0.1, 0.2, 0.3]
    router.providers = {"ollama": ollama}

    result = await router.embed("a memory fact")

    assert result is not None
    assert result.provider == "ollama"
    assert result.embedding == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_router_falls_back_from_failed_ollama_to_vllm():
    router = EmbeddingServiceRouter(primary_provider="ollama", embedding_dimension=3)
    ollama = AsyncMock(spec=OllamaEmbedding)
    ollama.model = "nomic-embed-text"
    ollama.health_check.return_value = True
    ollama.embed.return_value = None

    vllm = AsyncMock(spec=vLLMEmbedding)
    vllm.model = "bge-small"
    vllm.health_check.return_value = True
    vllm.embed.return_value = [0.3, 0.2, 0.1]
    router.providers = {"ollama": ollama, "vllm": vllm}

    result = await router.embed("a memory fact")

    assert result is not None
    assert result.provider == "vllm"
    ollama.embed.assert_awaited_once()
    vllm.embed.assert_awaited_once()


@pytest.mark.asyncio
async def test_router_skips_unhealthy_primary_provider():
    router = EmbeddingServiceRouter(primary_provider="ollama", embedding_dimension=3)
    ollama = AsyncMock(spec=OllamaEmbedding)
    ollama.model = "nomic-embed-text"
    ollama.health_check.return_value = False

    vllm = AsyncMock(spec=vLLMEmbedding)
    vllm.model = "bge-small"
    vllm.health_check.return_value = True
    vllm.embed.return_value = [0.3, 0.2, 0.1]
    router.providers = {"ollama": ollama, "vllm": vllm}

    result = await router.embed("a memory fact")

    assert result is not None
    assert result.provider == "vllm"
    ollama.embed.assert_not_awaited()
