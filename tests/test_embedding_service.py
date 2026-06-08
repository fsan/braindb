"""
Unit tests for EmbeddingService backend dispatch.

These tests do NOT require a live BrainDB stack — they mock
`sentence_transformers.SentenceTransformer` and `litellm.embedding` so no real
model loads and no network call is made. They override the session-scoped
`_require_live_api` autouse fixture (from conftest.py) so the suite can run
without `docker compose up`.

Covers the HYBRID dispatch:
  - EMBED_MODEL set  -> remote/litellm backend
  - EMBED_MODEL unset -> local sentence-transformers backend, honoring the
    EMBEDDING_MODEL_PATH override (falls back to Qwen/Qwen3-Embedding-0.6B).
plus the remote-path dimension guard and the text-embedding-3-* dimensions arg.
"""
from __future__ import annotations

import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest import mock

import pytest


DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"
EMBEDDING_DIM = 1024


@pytest.fixture(autouse=True)
def _require_live_api():
    """Override conftest's live-API guard — these are pure unit tests."""
    return None


def _reload_service(monkeypatch, env_value, embed_model=None):
    """Reload embedding_service with EMBEDDING_MODEL_PATH / EMBED_MODEL set/unset.

    The module computes EMBEDDING_MODEL at import time, so reload after
    mutating the env to exercise the env-var resolution.
    """
    if env_value is None:
        monkeypatch.delenv("EMBEDDING_MODEL_PATH", raising=False)
    else:
        monkeypatch.setenv("EMBEDDING_MODEL_PATH", env_value)
    if embed_model is None:
        monkeypatch.delenv("EMBED_MODEL", raising=False)
    else:
        monkeypatch.setenv("EMBED_MODEL", embed_model)
    import braindb.services.embedding_service as svc

    return importlib.reload(svc)


def _fake_sentence_transformers():
    """A stub sentence_transformers module whose SentenceTransformer records calls."""
    fake_mod = ModuleType("sentence_transformers")
    fake_mod.SentenceTransformer = mock.MagicMock(name="SentenceTransformer")
    return fake_mod


def _fake_litellm(embedding_mock):
    """A stub litellm module whose embedding() is the given mock."""
    fake_mod = ModuleType("litellm")
    fake_mod.embedding = embedding_mock
    return fake_mod


def _embedding_response(vectors):
    """Mimic a litellm EmbeddingResponse: .data is a list of objects with .embedding."""
    return SimpleNamespace(data=[SimpleNamespace(embedding=v) for v in vectors])


def test_default_model_when_env_unset(monkeypatch):
    svc = _reload_service(monkeypatch, env_value=None)
    assert svc.EMBEDDING_MODEL == DEFAULT_MODEL
    assert svc.get_embedding_service().model_name == DEFAULT_MODEL


def test_env_overrides_model_path(monkeypatch):
    svc = _reload_service(monkeypatch, env_value="/models/qwen")
    assert svc.EMBEDDING_MODEL == "/models/qwen"
    # singleton is module-level; reload reset it, so a fresh instance picks up env
    assert svc.get_embedding_service().model_name == "/models/qwen"


def test_initialize_loads_from_env_path(monkeypatch):
    svc = _reload_service(monkeypatch, env_value="/models/qwen")
    fake = _fake_sentence_transformers()
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)

    service = svc.EmbeddingService()
    assert service.initialize() is True
    fake.SentenceTransformer.assert_called_once_with("/models/qwen")


def test_initialize_loads_default_when_env_unset(monkeypatch):
    svc = _reload_service(monkeypatch, env_value=None)
    fake = _fake_sentence_transformers()
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)

    service = svc.EmbeddingService()
    assert service.initialize() is True
    fake.SentenceTransformer.assert_called_once_with(DEFAULT_MODEL)


# --- HYBRID dispatch: remote (litellm) backend ---------------------------


def test_embed_model_selects_remote_litellm_path(monkeypatch):
    """EMBED_MODEL set => litellm.embedding is called, sentence-transformers is not."""
    svc = _reload_service(monkeypatch, env_value=None, embed_model="ollama/mxbai-embed-large")
    emb_mock = mock.MagicMock(
        return_value=_embedding_response([[0.1] * EMBEDDING_DIM])
    )
    monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(emb_mock))
    # If the local path were taken this would blow up on a missing model load.
    st = _fake_sentence_transformers()
    monkeypatch.setitem(sys.modules, "sentence_transformers", st)

    service = svc.EmbeddingService()
    assert service.initialize() is True
    assert service.model_name == "ollama/mxbai-embed-large"

    vec = service.embed("carbonara")
    assert vec == [0.1] * EMBEDDING_DIM
    emb_mock.assert_called_once()
    _, kwargs = emb_mock.call_args
    assert kwargs["model"] == "ollama/mxbai-embed-large"
    assert kwargs["input"] == ["carbonara"]
    # ollama path uses OLLAMA_* api_base, NOT the proxy /v1 and NOT dimensions
    assert "dimensions" not in kwargs
    # the remote path must never touch sentence-transformers
    st.SentenceTransformer.assert_not_called()


def test_remote_rejects_wrong_dim_vector(monkeypatch):
    """A non-1024 vector from litellm is rejected (embed returns None)."""
    svc = _reload_service(monkeypatch, env_value=None, embed_model="openai/some-model")
    emb_mock = mock.MagicMock(
        return_value=_embedding_response([[0.0] * 512])  # wrong length
    )
    monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(emb_mock))

    service = svc.EmbeddingService()
    assert service.initialize() is True
    assert service.embed("anything") is None


def test_remote_passes_dimensions_for_text_embedding_3(monkeypatch):
    """openai/text-embedding-3-* must request dimensions=1024 via the proxy."""
    svc = _reload_service(
        monkeypatch, env_value=None, embed_model="openai/text-embedding-3-small"
    )
    emb_mock = mock.MagicMock(
        return_value=_embedding_response([[0.2] * EMBEDDING_DIM])
    )
    monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(emb_mock))

    service = svc.EmbeddingService()
    assert service.initialize() is True
    assert service.embed("pasta") == [0.2] * EMBEDDING_DIM

    _, kwargs = emb_mock.call_args
    assert kwargs["dimensions"] == EMBEDDING_DIM
    assert kwargs["api_base"].endswith("/v1")


def test_embed_model_unset_with_path_uses_local(monkeypatch):
    """EMBED_MODEL unset + EMBEDDING_MODEL_PATH set => sentence-transformers path."""
    svc = _reload_service(monkeypatch, env_value="/models/qwen", embed_model=None)
    fake = _fake_sentence_transformers()
    # encode([text]) -> ndarray-like; emulate with an object exposing [0].tolist()
    encoded_row = mock.MagicMock()
    encoded_row.tolist.return_value = [0.5] * EMBEDDING_DIM
    fake.SentenceTransformer.return_value.encode.return_value = [encoded_row]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    # litellm must NOT be called on the local path
    emb_mock = mock.MagicMock()
    monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(emb_mock))

    service = svc.EmbeddingService()
    assert service.initialize() is True
    assert service.embed("local text") == [0.5] * EMBEDDING_DIM
    fake.SentenceTransformer.assert_called_once_with("/models/qwen")
    emb_mock.assert_not_called()
