"""
Embedding service — generates keyword embeddings via LiteLLM.

Routes the same way cook_wiki's llm_client does:
  - ``ollama/*`` models go directly through the litellm SDK
    (``OLLAMA_API_BASE`` / ``OLLAMA_EMBED_API_BASE`` / ``OLLAMA_API_KEY``).
  - everything else routes through a LiteLLM proxy (OpenAI-compatible),
    using ``LLM_PROXY_URL`` / ``LLM_PROXY_API_KEY``.

Embeddings are stored in a fixed ``vector(1024)`` column, so every provider
must return 1024-dim vectors. For OpenAI ``text-embedding-3-*`` models we
request ``dimensions=1024`` explicitly; for Ollama pick a 1024-dim model
(e.g. ``mxbai-embed-large``). A vector of the wrong length is rejected so a
misconfiguration can't silently poison the cosine index.

If no model is configured (``EMBED_MODEL`` empty) the service reports
unavailable and the system runs text-only — same contract as before.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 1024
_OLLAMA_PREFIX = "ollama/"
_DEFAULT_OLLAMA_BASE = "http://localhost:11434"
_DEFAULT_PROXY_URL = "http://localhost:4001"


def _embed_model() -> str:
    return os.getenv("EMBED_MODEL", "").strip()


def _ollama_kwargs() -> dict:
    return {
        "api_base": os.getenv("OLLAMA_EMBED_API_BASE")
        or os.getenv("OLLAMA_API_BASE")
        or _DEFAULT_OLLAMA_BASE,
        "api_key": os.getenv("OLLAMA_API_KEY"),
    }


def _proxy_kwargs(model: str) -> dict:
    kwargs: dict = {
        "api_base": (os.getenv("LLM_PROXY_URL") or _DEFAULT_PROXY_URL) + "/v1",
        "api_key": os.getenv("LLM_PROXY_API_KEY") or "proxy-handled",
    }
    # OpenAI text-embedding-3-* support a server-side dimensions cut so we can
    # match the fixed vector(1024) column regardless of the model's native size.
    if "text-embedding-3" in model:
        kwargs["dimensions"] = EMBEDDING_DIM
    return kwargs


class EmbeddingService:
    def __init__(self, model_name: str | None = None):
        self.model_name = model_name if model_name is not None else _embed_model()

    def initialize(self) -> bool:
        """No model to load (calls are remote) — just report config validity."""
        if self.is_available():
            logger.info("Embedding model configured: %s (%d-dim)", self.model_name, EMBEDDING_DIM)
        else:
            logger.warning("EMBED_MODEL not set — embeddings disabled, running text-only")
        return self.is_available()

    def is_available(self) -> bool:
        """A model must be configured for embeddings to work."""
        return bool(self.model_name)

    def _embed_many(self, texts: list[str]) -> list[list[float]] | None:
        if not self.is_available() or not texts:
            return None
        try:
            import litellm

            if self.model_name.startswith(_OLLAMA_PREFIX):
                resp = litellm.embedding(
                    model=self.model_name, input=texts, **_ollama_kwargs()
                )
            else:
                resp = litellm.embedding(
                    model=self.model_name, input=texts, **_proxy_kwargs(self.model_name)
                )
        except Exception as e:
            logger.warning("Embedding call failed (%s): %s", self.model_name, e)
            return None

        vectors = [
            d["embedding"] if isinstance(d, dict) else d.embedding for d in resp.data
        ]
        for vec in vectors:
            if len(vec) != EMBEDDING_DIM:
                logger.error(
                    "Embedding model %s returned %d-dim vector, expected %d — "
                    "refusing to store (would corrupt the vector(%d) index)",
                    self.model_name,
                    len(vec),
                    EMBEDDING_DIM,
                    EMBEDDING_DIM,
                )
                return None
        return vectors

    def embed(self, text: str) -> list[float] | None:
        """Embed a single text string. Returns list of floats or None."""
        result = self._embed_many([text])
        return result[0] if result else None

    def embed_batch(
        self, texts: list[str], batch_size: int = 32
    ) -> list[list[float]] | None:
        """Embed multiple texts. Returns list of embedding vectors or None."""
        if not self.is_available() or not texts:
            return None
        out: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            chunk = self._embed_many(texts[start : start + batch_size])
            if chunk is None:
                return None
            out.extend(chunk)
        return out


# Module-level singleton — initialized once, reused across requests
_instance: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    """Get the singleton EmbeddingService instance."""
    global _instance
    if _instance is None:
        _instance = EmbeddingService()
    return _instance
