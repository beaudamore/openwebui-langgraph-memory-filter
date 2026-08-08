"""Embedding Service Abstraction for Local-First Vectorization

Provides a unified interface for embedding providers with intelligent failover:
  1. Primary: Ollama (local, no API key required)
  2. Fallback: vLLM (alternative local option)
  3. Frontier: OpenRouter (external, API key required)

Automatic health checks and provider switching ensure robustness.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Literal, Optional, Tuple
from dataclasses import dataclass
import httpx

logger = logging.getLogger("openwebui.filters.embedding_service")


@dataclass
class EmbeddingResponse:
    """Standardized embedding response"""
    embedding: List[float]
    provider: str
    model: str
    dimension: int
    error: Optional[str] = None


class EmbeddingProvider:
    """Base class for embedding providers"""
    
    def __init__(self, base_url: str, model: str, dimension: int = 1536):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dimension = dimension
    
    async def embed(self, text: str) -> Optional[List[float]]:
        """Embed text and return vector. Return None on failure."""
        raise NotImplementedError
    
    async def health_check(self) -> bool:
        """Check if provider is available. Return True if healthy."""
        raise NotImplementedError


class OllamaEmbedding(EmbeddingProvider):
    """Ollama embedding provider (local-first)"""
    
    async def embed(self, text: str) -> Optional[List[float]]:
        """Call Ollama /api/embeddings endpoint"""
        if not text:
            return None
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.base_url}/api/embeddings",
                    json={"model": self.model, "prompt": text},
                )
                response.raise_for_status()
                data = response.json()
                embedding = data.get("embedding", [])
                
                if not embedding:
                    logger.warning(f"Ollama returned empty embedding for model {self.model}")
                    return None
                
                # Verify dimension matches
                if len(embedding) != self.dimension:
                    logger.warning(
                        f"Ollama embedding dimension mismatch: got {len(embedding)}, "
                        f"expected {self.dimension}. Truncating/padding."
                    )
                    embedding = embedding[:self.dimension] + [0.0] * max(0, self.dimension - len(embedding))
                
                return embedding
        except Exception as e:
            logger.warning(f"Ollama embedding failed: {type(e).__name__}: {e}")
            return None
    
    async def health_check(self) -> bool:
        """Check Ollama /api/tags endpoint"""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                return response.status_code == 200
        except Exception as e:
            logger.debug(f"Ollama health check failed: {e}")
            return False


class vLLMEmbedding(EmbeddingProvider):
    """vLLM embedding provider (local alternative)"""
    
    async def embed(self, text: str) -> Optional[List[float]]:
        """Call vLLM OpenAI-compatible embeddings endpoint"""
        if not text:
            return None
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.base_url}/v1/embeddings",
                    json={"model": self.model, "input": text},
                    headers={"Authorization": "Bearer fake-key"},  # vLLM doesn't validate in local mode
                )
                response.raise_for_status()
                data = response.json()
                embeddings = data.get("data", [])
                
                if not embeddings:
                    logger.warning(f"vLLM returned empty embeddings for model {self.model}")
                    return None
                
                embedding = embeddings[0].get("embedding", [])
                
                # Verify dimension matches
                if len(embedding) != self.dimension:
                    logger.warning(
                        f"vLLM embedding dimension mismatch: got {len(embedding)}, "
                        f"expected {self.dimension}. Truncating/padding."
                    )
                    embedding = embedding[:self.dimension] + [0.0] * max(0, self.dimension - len(embedding))
                
                return embedding
        except Exception as e:
            logger.warning(f"vLLM embedding failed: {type(e).__name__}: {e}")
            return None
    
    async def health_check(self) -> bool:
        """Check vLLM /v1/models endpoint"""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.base_url}/v1/models",
                    headers={"Authorization": "Bearer fake-key"},
                )
                return response.status_code == 200
        except Exception as e:
            logger.debug(f"vLLM health check failed: {e}")
            return False


class OpenRouterEmbedding(EmbeddingProvider):
    """OpenRouter embedding provider (frontier/external)"""
    
    def __init__(self, api_key: str, model: str = "openai/text-embedding-3-small", dimension: int = 1536):
        super().__init__("https://openrouter.ai/api/v1", model, dimension)
        self.api_key = api_key
    
    async def embed(self, text: str) -> Optional[List[float]]:
        """Call OpenRouter embeddings endpoint"""
        if not text or not self.api_key:
            return None
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.base_url}/embeddings",
                    json={"model": self.model, "input": text},
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                response.raise_for_status()
                data = response.json()
                embeddings = data.get("data", [])
                
                if not embeddings:
                    logger.warning(f"OpenRouter returned empty embeddings for model {self.model}")
                    return None
                
                embedding = embeddings[0].get("embedding", [])
                
                # Verify dimension matches
                if len(embedding) != self.dimension:
                    logger.warning(
                        f"OpenRouter embedding dimension mismatch: got {len(embedding)}, "
                        f"expected {self.dimension}"
                    )
                
                return embedding
        except Exception as e:
            logger.warning(f"OpenRouter embedding failed: {type(e).__name__}: {e}")
            return None
    
    async def health_check(self) -> bool:
        """Check OpenRouter API key validity"""
        if not self.api_key:
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    "https://openrouter.ai/api/v1/auth/key",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                return response.status_code == 200
        except Exception as e:
            logger.debug(f"OpenRouter health check failed: {e}")
            return False


class EmbeddingServiceRouter:
    """
    Intelligent routing between multiple embedding providers.
    
    Implements failover logic:
      1. Try primary provider
      2. If fails or not configured, try fallback
      3. If frontier enabled, use as last resort
      4. Cache healthy providers to avoid repeated health checks
    """
    
    def __init__(
        self,
        primary_provider: str = "ollama",
        ollama_url: str = "http://localhost:11434",
        ollama_model: str = "nomic-embed-text",
        vllm_url: str = "http://localhost:8000",
        vllm_model: str = "LM-7B",
        openrouter_key: Optional[str] = None,
        openrouter_model: str = "openai/text-embedding-3-small",
        embedding_dimension: int = 1536,
        fallback_to_frontier: bool = True,
        logger_fn=None,
    ):
        self.primary_provider = primary_provider
        self.embedding_dimension = embedding_dimension
        self.fallback_to_frontier = fallback_to_frontier
        self._log = logger_fn or logger.info
        self._health_cache: Dict[str, Tuple[bool, float]] = {}  # provider -> (healthy, timestamp)
        
        # Initialize providers
        self.providers: Dict[str, EmbeddingProvider] = {}
        
        if ollama_url:
            self.providers["ollama"] = OllamaEmbedding(ollama_url, ollama_model, embedding_dimension)
        
        if vllm_url:
            self.providers["vllm"] = vLLMEmbedding(vllm_url, vllm_model, embedding_dimension)
        
        if openrouter_key:
            self.providers["openrouter"] = OpenRouterEmbedding(openrouter_key, openrouter_model, embedding_dimension)
    
    async def embed(self, text: str) -> Optional[EmbeddingResponse]:
        """
        Embed text using available providers in failover order.
        
        Returns EmbeddingResponse with embedding + provider info, or None on total failure.
        """
        if not text:
            return None
        
        # Build provider order: primary first, then fallbacks
        provider_order = self._get_provider_order()
        
        for provider_name in provider_order:
            if provider_name not in self.providers:
                self._log(f"Provider {provider_name} not configured", "debug")
                continue
            
            provider = self.providers[provider_name]
            
            # Check health (with caching to avoid hammering endpoints)
            if not await self._is_healthy(provider_name):
                self._log(f"Provider {provider_name} is unhealthy, skipping", "debug")
                continue
            
            # Try embedding
            self._log(f"Attempting embedding with {provider_name}...", "debug")
            embedding = await provider.embed(text)
            
            if embedding:
                self._log(f"Successfully embedded with {provider_name}", "debug")
                return EmbeddingResponse(
                    embedding=embedding,
                    provider=provider_name,
                    model=provider.model,
                    dimension=len(embedding),
                )
        
        # All providers failed
        self._log("All embedding providers exhausted", "warning")
        return None
    
    def _get_provider_order(self) -> List[str]:
        """Get provider order based on configuration and health"""
        order = []
        
        # Add primary first
        if self.primary_provider in self.providers:
            order.append(self.primary_provider)
        
        # Add other local providers
        for provider_name in ["ollama", "vllm"]:
            if provider_name != self.primary_provider and provider_name in self.providers:
                order.append(provider_name)
        
        # Add frontier if fallback enabled
        if self.fallback_to_frontier and "openrouter" in self.providers:
            order.append("openrouter")
        
        return order
    
    async def _is_healthy(self, provider_name: str, cache_ttl_sec: int = 60) -> bool:
        """Check provider health with caching"""
        import time
        
        now = time.time()
        if provider_name in self._health_cache:
            healthy, timestamp = self._health_cache[provider_name]
            if now - timestamp < cache_ttl_sec:
                return healthy
        
        # Perform health check
        provider = self.providers[provider_name]
        healthy = await provider.health_check()
        self._health_cache[provider_name] = (healthy, now)
        
        return healthy


class BatchEmbedder:
    """Batch embedding with rate limiting and error handling"""
    
    def __init__(self, router: EmbeddingServiceRouter, max_batch_size: int = 10):
        self.router = router
        self.max_batch_size = max_batch_size
    
    async def embed_many(self, texts: List[str]) -> List[Optional[List[float]]]:
        """Embed multiple texts, returning list of embeddings or None for failures"""
        results = []
        
        for i in range(0, len(texts), self.max_batch_size):
            batch = texts[i:i + self.max_batch_size]
            
            # Embed sequentially within batch to avoid rate limiting
            batch_results = []
            for text in batch:
                response = await self.router.embed(text)
                batch_results.append(response.embedding if response else None)
            
            results.extend(batch_results)
            
            # Rate limiting: small delay between batches
            if i + self.max_batch_size < len(texts):
                await asyncio.sleep(0.1)
        
        return results
