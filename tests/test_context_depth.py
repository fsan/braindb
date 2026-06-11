"""Unit coverage for the configurable graph-depth cap.

These tests don't need a live stack: they stub every DB-touching helper in
``assemble_context`` and only assert the depth passed to ``graph_expand``.
"""
from datetime import UTC, datetime

from braindb.config import settings
from braindb.schemas.search import ContextRequest
from braindb.services import context as ctx


def _stub_pipeline(monkeypatch, captured: dict):
    """Neutralise every DB/LLM call in assemble_context except graph_expand,
    which records the depth it was handed."""
    seed = {"id": "11111111-1111-1111-1111-111111111111", "score": 0.9,
            "entity_type": "fact", "content": "x", "title": None,
            "summary": None, "keywords": [], "importance": 0.5, "notes": None,
            "created_at": datetime.now(UTC), "updated_at": None,
            "accessed_at": None, "access_count": 0}

    # Keyword-mediated pathway (step 1) — return one fuzzy keyword that fans
    # out to the seed entity, so seed_scores is non-empty and graph_expand runs.
    monkeypatch.setattr(ctx, "find_fuzzy_keywords",
                        lambda *a, **k: [{"id": "kw-1", "similarity": 0.9}])
    monkeypatch.setattr(ctx, "find_entities_for_keywords",
                        lambda *a, **k: [{**seed, "matched_keyword_ids": ["kw-1"]}])
    monkeypatch.setattr(ctx, "fuzzy_search", lambda *a, **k: [seed])

    class _NoEmbed:
        def is_available(self) -> bool:
            return False

    monkeypatch.setattr(ctx, "get_embedding_service", lambda: _NoEmbed())

    def _spy_graph_expand(conn, seed_ids, max_depth, min_relevance):
        captured["depth"] = max_depth
        return [{**seed, "min_depth": 0, "relevance": 1.0}]

    monkeypatch.setattr(ctx, "graph_expand", _spy_graph_expand)
    monkeypatch.setattr(ctx, "fetch_ext", lambda conn, rows: {})
    monkeypatch.setattr(ctx, "fetch_always_on_rules", lambda conn: [])
    monkeypatch.setattr(ctx, "track_access", lambda conn, ids: None)


def test_depth_capped_by_settings(monkeypatch):
    captured: dict = {}
    _stub_pipeline(monkeypatch, captured)
    monkeypatch.setattr(settings, "max_graph_depth", 1)

    ctx.assemble_context(conn=None, req=ContextRequest(query="anything", max_depth=3))

    assert captured["depth"] == 1, "request max_depth=3 must be capped to settings.max_graph_depth=1"


def test_request_depth_used_when_below_cap(monkeypatch):
    captured: dict = {}
    _stub_pipeline(monkeypatch, captured)
    monkeypatch.setattr(settings, "max_graph_depth", 3)

    ctx.assemble_context(conn=None, req=ContextRequest(query="anything", max_depth=2))

    assert captured["depth"] == 2, "request below the cap must pass through unchanged"
