"""
Unit tests for EmbeddingService model-path resolution.

These tests do NOT require a live BrainDB stack — they mock
`sentence_transformers.SentenceTransformer` so no real model loads. They
override the session-scoped `_require_live_api` autouse fixture (from
conftest.py) so the suite can run without `docker compose up`.

Covers the EMBEDDING_MODEL_PATH env override added for local/S3 model loading:
the model id must come from EMBEDDING_MODEL_PATH when set, and fall back to the
default HF id (Qwen/Qwen3-Embedding-0.6B) when unset.
"""
from __future__ import annotations

import importlib
import sys
from types import ModuleType
from unittest import mock

import pytest


DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"


@pytest.fixture(autouse=True)
def _require_live_api():
    """Override conftest's live-API guard — these are pure unit tests."""
    return None


def _reload_service(monkeypatch, env_value):
    """Reload embedding_service with EMBEDDING_MODEL_PATH set/unset.

    The module computes EMBEDDING_MODEL at import time, so reload after
    mutating the env to exercise the env-var resolution.
    """
    if env_value is None:
        monkeypatch.delenv("EMBEDDING_MODEL_PATH", raising=False)
    else:
        monkeypatch.setenv("EMBEDDING_MODEL_PATH", env_value)
    import braindb.services.embedding_service as svc

    return importlib.reload(svc)


def _fake_sentence_transformers():
    """A stub sentence_transformers module whose SentenceTransformer records calls."""
    fake_mod = ModuleType("sentence_transformers")
    fake_mod.SentenceTransformer = mock.MagicMock(name="SentenceTransformer")
    return fake_mod


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
