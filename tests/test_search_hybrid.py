"""Pure unit coverage for hybrid (lexical + vector) search.

No live stack needed: `_rrf_fuse` is a pure function tested directly, and
`hybrid_search`'s degradation branches are tested by monkeypatching
`fuzzy_search` so no real DB connection is touched. Run standalone with
`python -m pytest tests/test_search_hybrid.py`.
"""
from braindb.services import search as search_mod
from braindb.services.search import _rrf_fuse, embed_text_for_wiki, hybrid_search


def _row(id_, **extra):
    base = {
        "id": id_, "entity_type": "datasource", "title": f"title-{id_}",
        "content": f"content-{id_}", "summary": None, "keywords": [],
        "importance": 0.5, "source": "wiki", "notes": None,
        "created_at": None, "updated_at": None, "accessed_at": None,
        "access_count": 0, "metadata": {},
    }
    base.update(extra)
    return base


# ------------------------------------------------------------------ #
# _rrf_fuse                                                          #
# ------------------------------------------------------------------ #

def test_fusion_item_in_both_arms_outranks_single_arm_item():
    """A row that shows up (at any rank) in both arms must outrank a row
    that only shows up in one arm — that's the entire point of fusing
    instead of just concatenating lists."""
    lexical = [_row("a"), _row("b")]
    vector = [_row("b"), _row("c")]

    fused = _rrf_fuse(lexical, vector)
    ids = [r["id"] for r in fused]

    assert ids[0] == "b", f"row in both arms should rank first, got {ids}"
    assert set(ids) == {"a", "b", "c"}


def test_fusion_empty_vector_arm_preserves_lexical_order():
    """An empty vector arm must not perturb the lexical arm's relative order
    — this is the RRF-level analogue of hybrid_search degrading to lexical
    when there's nothing to fuse against."""
    lexical = [_row("x"), _row("y"), _row("z")]

    fused = _rrf_fuse(lexical, [])
    ids = [r["id"] for r in fused]

    assert ids == ["x", "y", "z"]


def test_fusion_prefers_lexical_row_data_when_present_in_both():
    """When a row is in both arms, the merged row's data should come from
    the lexical copy (already preview()-applied), not get clobbered by the
    vector copy's raw content."""
    lexical = [_row("a", content="lexical-preview")]
    vector = [_row("a", content="raw-vector-content")]

    fused = _rrf_fuse(lexical, vector)

    assert fused[0]["content"] == "lexical-preview"


def test_fusion_sets_score_to_fused_rrf_value():
    lexical = [_row("a")]
    fused = _rrf_fuse(lexical, [])
    # rank 0 in one arm only: 1 / (60 + 0 + 1)
    assert abs(fused[0]["score"] - 1.0 / 61) < 1e-9


# ------------------------------------------------------------------ #
# hybrid_search degradation branches                                 #
# ------------------------------------------------------------------ #

def test_hybrid_search_skips_vector_arm_when_entity_types_excludes_datasource(monkeypatch):
    """If the caller filters to types that don't include 'datasource', there
    can be no wiki articles in the result set at all — hybrid_search must
    not bother calling the embedding service, it should just delegate
    straight to fuzzy_search."""
    calls = {"fuzzy": 0}

    def fake_fuzzy_search(conn, query, entity_types, min_importance, limit):
        calls["fuzzy"] += 1
        assert limit == 10, "entity_types-without-datasource path must pass the caller's own limit through"
        return [_row("a", entity_type="fact", source="user-stated")]

    monkeypatch.setattr(search_mod, "fuzzy_search", fake_fuzzy_search)

    class _ExplodingEmbeddingService:
        def is_available(self):
            raise AssertionError("embedding_service must not be touched when datasource is excluded")

    result = hybrid_search(
        conn=None, query="q", entity_types=["fact"], min_importance=0.0, limit=10,
        embedding_service=_ExplodingEmbeddingService(),
    )

    assert calls["fuzzy"] == 1
    assert [r["id"] for r in result] == ["a"]


def test_hybrid_search_returns_lexical_verbatim_when_embed_returns_none(monkeypatch):
    """embed() swallows errors and returns None (documented contract in
    embedding_service.py) — hybrid_search must treat that as "no vector arm
    available" and hand back the lexical rows unchanged, not raise."""
    lexical_rows = [_row("a"), _row("b")]
    monkeypatch.setattr(
        search_mod, "fuzzy_search",
        lambda conn, query, entity_types, min_importance, limit: lexical_rows,
    )

    class _NoneEmbeddingService:
        def is_available(self):
            return True

        def embed(self, text):
            return None

    result = hybrid_search(
        conn=None, query="q", entity_types=None, min_importance=0.0, limit=10,
        embedding_service=_NoneEmbeddingService(),
    )

    assert result == lexical_rows[:10]


def test_hybrid_search_returns_lexical_when_embedding_service_none(monkeypatch):
    lexical_rows = [_row("a")]
    monkeypatch.setattr(
        search_mod, "fuzzy_search",
        lambda conn, query, entity_types, min_importance, limit: lexical_rows,
    )

    result = hybrid_search(
        conn=None, query="q", entity_types=None, min_importance=0.0, limit=10,
        embedding_service=None,
    )

    assert result == lexical_rows


def test_hybrid_search_returns_lexical_when_embedding_service_unavailable(monkeypatch):
    lexical_rows = [_row("a")]
    monkeypatch.setattr(
        search_mod, "fuzzy_search",
        lambda conn, query, entity_types, min_importance, limit: lexical_rows,
    )

    class _UnavailableEmbeddingService:
        def is_available(self):
            return False

    result = hybrid_search(
        conn=None, query="q", entity_types=None, min_importance=0.0, limit=10,
        embedding_service=_UnavailableEmbeddingService(),
    )

    assert result == lexical_rows


def test_hybrid_search_locale_post_filters_lexical_arm_to_wiki_only(monkeypatch):
    """When locale is given, ALL lexical rows are post-filtered down to
    (source == 'wiki' AND metadata.locale == locale) — a non-wiki row or a
    wiki row from a different locale must both be dropped, even though the
    vector arm is never reached in this test (embedding_service unavailable
    isolates the assertion to the lexical post-filter)."""
    lexical_rows = [
        _row("wiki-match", source="wiki", metadata={"locale": "en"}),
        _row("wiki-other-locale", source="wiki", metadata={"locale": "fr"}),
        _row("non-wiki", entity_type="fact", source="user-stated", metadata={}),
    ]
    monkeypatch.setattr(
        search_mod, "fuzzy_search",
        lambda conn, query, entity_types, min_importance, limit: lexical_rows,
    )

    class _UnavailableEmbeddingService:
        def is_available(self):
            return False

    result = hybrid_search(
        conn=None, query="q", entity_types=None, min_importance=0.0, limit=10,
        locale="en", embedding_service=_UnavailableEmbeddingService(),
    )

    assert [r["id"] for r in result] == ["wiki-match"]


def test_hybrid_search_no_locale_filter_when_locale_none(monkeypatch):
    lexical_rows = [
        _row("wiki-en", source="wiki", metadata={"locale": "en"}),
        _row("non-wiki", entity_type="fact", source="user-stated", metadata={}),
    ]
    monkeypatch.setattr(
        search_mod, "fuzzy_search",
        lambda conn, query, entity_types, min_importance, limit: lexical_rows,
    )

    class _UnavailableEmbeddingService:
        def is_available(self):
            return False

    result = hybrid_search(
        conn=None, query="q", entity_types=None, min_importance=0.0, limit=10,
        locale=None, embedding_service=_UnavailableEmbeddingService(),
    )

    assert [r["id"] for r in result] == ["wiki-en", "non-wiki"]


# ------------------------------------------------------------------ #
# embed_text_for_wiki                                                #
# ------------------------------------------------------------------ #

def test_embed_text_for_wiki_combines_title_and_clipped_content():
    text = embed_text_for_wiki("Title", "x" * 3000)
    assert text.startswith("Title")
    # 2000-char cap on content, no cap on title
    assert len(text) <= len("Title") + 2 + 2000


def test_embed_text_for_wiki_handles_none_fields():
    assert embed_text_for_wiki(None, None) == ""
    assert embed_text_for_wiki(None, "body") == "body"
    assert embed_text_for_wiki("Title", None) == "Title"
