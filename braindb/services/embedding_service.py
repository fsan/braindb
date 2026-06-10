"""
Embedding service — generates keyword embeddings. HYBRID backend.

Two interchangeable backends, picked once in ``initialize()`` from the env:

  - **Remote / LiteLLM** — selected when ``EMBED_MODEL`` is set (non-empty).
    Routes the same way cook_wiki's llm_client does:
      * ``ollama/*`` models go directly through the litellm SDK
        (``OLLAMA_API_BASE`` / ``OLLAMA_EMBED_API_BASE`` / ``OLLAMA_API_KEY``).
      * everything else routes through a LiteLLM proxy (OpenAI-compatible),
        using ``LLM_PROXY_URL`` / ``LLM_PROXY_API_KEY``.
    For OpenAI ``text-embedding-3-*`` models we request ``dimensions=1024``
    explicitly; for Ollama pick a 1024-dim model (e.g. ``mxbai-embed-large``).

  - **Local / sentence-transformers** — the fallback when ``EMBED_MODEL`` is
    empty. Loads Qwen/Qwen3-Embedding-0.6B (or ``EMBEDDING_MODEL_PATH``) in
    process. Same behavior as the original fork.

Embeddings are stored in a fixed ``vector(1024)`` column, so every provider
must return 1024-dim vectors. A remote vector of the wrong length is rejected
so a misconfiguration can't silently poison the cosine index.

If neither backend can serve (no ``EMBED_MODEL`` and sentence-transformers
can't load) the service reports unavailable and the system runs text-only —
same contract as before.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Model identifier may be a HF model id OR a local directory path.
# SentenceTransformer(name) accepts a local path, so EMBEDDING_MODEL_PATH
# lets us load a model baked/synced into a local dir (e.g. from S3 in k8s)
# without touching the rest of the code. Falls back to the default HF id.
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL_PATH") or "Qwen/Qwen3-Embedding-0.6B"
EMBEDDING_DIM = 1024

_OLLAMA_PREFIX = "ollama/"
_DEFAULT_OLLAMA_BASE = "http://localhost:11434"
_DEFAULT_PROXY_URL = "http://localhost:4001"


def _embed_model() -> str:
    """The configured remote embedding model, or "" if the remote path is off."""
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
    """Dispatches to a remote (LiteLLM) or local (sentence-transformers) backend.

    The backend is chosen lazily in ``initialize()`` from the env: if
    ``EMBED_MODEL`` is set we use the remote path, otherwise we fall back to the
    in-process sentence-transformers model. ``model_name`` always reflects the
    active backend's model id.
    """

    def __init__(self, model_name: str | None = None):
        # If EMBED_MODEL is set, the remote model id wins; otherwise model_name
        # tracks the local sentence-transformers model id (default Qwen / path).
        remote = _embed_model()
        if model_name is not None:
            self.model_name = model_name
        elif remote:
            self.model_name = remote
        else:
            self.model_name = EMBEDDING_MODEL
        self._remote = bool(remote)
        self.model = None  # populated only for the local backend
        self._initialized = False
        self._available = False

    def initialize(self) -> bool:
        """Pick + ready a backend. Returns True if embeddings can be served."""
        if self._initialized:
            return self._available
        self._initialized = True

        if self._remote:
            # Remote calls are stateless — nothing to load, just report config.
            self._available = True
            logger.info(
                "Embedding model configured (remote/litellm): %s (%d-dim)",
                self.model_name,
                EMBEDDING_DIM,
            )
            return True

        # Local in-process sentence-transformers backend.
        try:
            from sentence_transformers import SentenceTransformer

            logger.info("Loading embedding model (local): %s ...", self.model_name)
            self.model = SentenceTransformer(self.model_name)
            self._available = True
            logger.info(
                "Embedding model loaded (local): %s (%d-dim)",
                self.model_name,
                EMBEDDING_DIM,
            )
            return True
        except ImportError:
            logger.warning("sentence-transformers not installed — embeddings disabled")
            return False
        except Exception as e:
            logger.warning("Failed to load embedding model: %s", e)
            return False

    def is_available(self) -> bool:
        return self._available

    # -- remote backend -----------------------------------------------------

    def _embed_many_remote(self, texts: list[str]) -> list[list[float]] | None:
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

    # -- public interface ---------------------------------------------------

    def embed(self, text: str) -> list[float] | None:
        """Embed a single text string. Returns list of floats or None."""
        if not self.initialize():
            return None
        if self._remote:
            result = self._embed_many_remote([text])
            return result[0] if result else None
        try:
            return self.model.encode([text], show_progress_bar=False)[0].tolist()
        except Exception as e:
            logger.warning("Embedding failed for text '%s': %s", text[:50], e)
            return None

    def embed_batch(
        self, texts: list[str], batch_size: int = 32
    ) -> list[list[float]] | None:
        """Embed multiple texts. Returns list of embedding vectors or None."""
        if not self.initialize() or not texts:
            return None
        if self._remote:
            out: list[list[float]] = []
            for start in range(0, len(texts), batch_size):
                chunk = self._embed_many_remote(texts[start : start + batch_size])
                if chunk is None:
                    return None
                out.extend(chunk)
            return out
        try:
            return self.model.encode(
                texts, batch_size=batch_size, show_progress_bar=False
            ).tolist()
        except Exception as e:
            logger.warning("Batch embedding failed: %s", e)
            return None


# Module-level singleton — initialized once, reused across requests
_instance: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    """Get the singleton EmbeddingService instance."""
    global _instance
    if _instance is None:
        _instance = EmbeddingService()
    return _instance
