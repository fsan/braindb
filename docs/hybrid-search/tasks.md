# Hybrid search — tasks

- [x] `embed_text_for_wiki(title, content)` — shared embed-text builder
      (title unclipped + content capped at 2000 chars).
      `braindb/services/search.py`
- [x] `_rrf_fuse(lexical_rows, vector_rows, k=60)` — pure RRF fusion,
      row data taken from the lexical copy when present.
      `braindb/services/search.py`
- [x] `hybrid_search(...)` — lexical + vector arms, locale post-filter/
      SQL-filter, all degradation branches (entity_types excludes
      datasource / no embedding_service / unavailable / embed()→None).
      `braindb/services/search.py`
- [x] `generate_missing_wiki_embeddings(...)` — backfill for wiki
      datasources, mirrors `keyword_service.generate_missing_embeddings`,
      `force` flag for full regeneration after a model switch.
      `braindb/services/search.py`
- [x] `SearchRequest.mode` (`"lexical"` default / `"hybrid"`) +
      `SearchRequest.locale`. `braindb/schemas/search.py`
- [x] `POST /memory/search` — branches on `body.mode`, calls
      `hybrid_search` with `get_embedding_service()`.
      `braindb/routers/memory.py`
- [x] `POST /memory/generate-wiki-embeddings` — backfill endpoint, 503
      guard when no `EMBED_MODEL` configured, mirrors
      `/memory/generate-embeddings`. `braindb/routers/memory.py`
- [x] Embed-on-write hooks: `create_datasource`, `datasources/ingest`,
      `update_datasource` (gated on the post-update `source`, handles
      source flipping to/from `'wiki'` mid-PATCH).
      `braindb/routers/entities.py`
- [x] Migration `012_wiki_hybrid_search.py` — partial HNSW index over
      wiki embeddings, `CONCURRENTLY` in an autocommit block, chained
      off `011` (revision chain verified: 001→...→011→012, no gaps/
      forks).
- [x] `tests/test_search_hybrid.py` — pure unit coverage, no live stack:
      `_rrf_fuse` fusion behaviour (both-arms-outranks-single-arm,
      empty-vector-arm-preserves-order, lexical-row-data-wins,
      fused-score-value), all four `hybrid_search` degradation branches,
      locale post-filter (both "restricts to wiki+locale" and
      "no-op when locale=None"), `embed_text_for_wiki` (title+content
      clipping, `None`-field handling). **12/12 passing** — run with
      `.venv/bin/python -m pytest tests/test_search_hybrid.py -x -q`.
- [x] `tests/test_search.py` — `test_search_hybrid_mode_returns_valid_shape`,
      an integration smoke test against the real `/memory/search`
      endpoint proving the lexical-degradation path end-to-end (this
      dev environment has no `EMBED_MODEL`, so it can't exercise the
      vector arm itself — that's covered by the unit tests above).
      Requires the live stack (`docker compose up -d`); verified by
      syntax check + fixture-usage check only in this pass, not by
      execution.
- [x] Review pass over all uncommitted code for correctness (SQL param
      order, RRF math, degradation paths, migration revision chain,
      `source`/`entity_type` filter conventions vs. the rest of the
      codebase). No bugs found — see `review-notes.md`.
- [x] Lint — no linter configured in `pyproject.toml` (no ruff/black/
      flake8 section); skipped, nothing to run.
- [x] `docs/hybrid-search/{plan,tasks,review-notes}.md`
- [x] Commit + push `feat/wiki-hybrid-search` + open draft PR.
