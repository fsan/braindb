# Hybrid search — review notes

Full review pass over all uncommitted code (`braindb/services/search.py`,
`braindb/schemas/search.py`, `braindb/routers/memory.py`,
`braindb/routers/entities.py`, `alembic/versions/012_wiki_hybrid_search.py`,
`tests/test_search_hybrid.py`, `tests/test_search.py`) before shipping.

## Bugs found

None. The implementation was already correct going into this pass:

- **SQL param order** in `hybrid_search`'s vector-arm query
  (`e.embedding <=> %s::vector` appears twice — once in the SELECT's
  `1 - (...)` and once in `ORDER BY`) matches the existing
  `keyword_service.find_similar_keywords` pattern exactly: the same
  `str(vec)` string is passed once per placeholder occurrence, and the
  `where_params` (min_importance, optional locale) are correctly
  interposed between the two `vec_str` occurrences in `full_params`.
- **RRF math** in `_rrf_fuse` — reciprocal rank uses `1.0 / (k + rank + 1)`
  with `rank` 0-indexed via `enumerate`, which is the correct RRF
  formula (`1/(k + rank)` for 1-indexed rank == `1/(k + rank + 1)` for
  0-indexed). Cross-checked against the unit test's explicit expected
  value (`1/61` for rank 0, k=60) — matches.
- **Migration revision chain** — `012`'s `down_revision = "011"` is
  correct; no gap or fork against the `001...011` chain (verified with
  `grep -n "revision\|down_revision" alembic/versions/*.py`).
- **Degradation paths** — all four (entity_types excludes datasource /
  embedding_service is None / unavailable / embed() returns None) are
  implemented and independently covered by
  `tests/test_search_hybrid.py`, including the case that specifically
  asserts the embedding service is **never touched** when
  `entity_types` excludes `'datasource'` (`_ExplodingEmbeddingService`
  raises if `is_available()` is called).
- **`source`/`entity_type` filter convention** — `entity_type='datasource'
  AND source='wiki'` matches the exact convention already established
  in migrations 010/011 and used throughout `wiki_jobs.py` for
  identifying wiki-article rows; this new code didn't invent a new
  filter shape.
- **Update-gate edge case** — `update_datasource` re-checks the
  post-update `source` (not the pre-update snapshot) before deciding
  whether to embed, so a PATCH that flips `source` to/from `'wiki'` in
  the same request is handled correctly in both directions. This is
  already documented inline in `entities.py`.

## Notable implementation gotchas (for future maintainers)

- **`locale` filtering asymmetry**: `fuzzy_search` has no `locale`
  parameter, so `hybrid_search` applies the locale filter as a *Python
  post-filter* on the lexical arm's already-fetched rows, but as a
  *SQL WHERE clause* on the vector arm's query (since that query is
  already wiki-only, adding it there is free). If `fuzzy_search` ever
  grows a native locale parameter, this asymmetry should be revisited —
  currently it's harmless because the lexical arm is over-fetched
  (`max(limit*5, 100)`) specifically to leave headroom for filtering.
- **Row shape uniformity after fusion**: `_rrf_fuse` intentionally
  applies `preview()` to vector-only rows (rows found only by the vector
  arm) since the lexical arm's rows already went through `preview()`
  inside `fuzzy_search`. Skipping this would leak full, un-truncated
  `content` for vector-only hits — easy to miss since nothing would
  fail loudly, results would just occasionally be oversized.
- **`generate_missing_wiki_embeddings` "failed" count**: on a batch
  failure, `embedding_service.embed_batch` returns `None` for the whole
  call (not partial results), so `{"failed": len(rows)}` in that branch
  is correct — it's an all-or-nothing per call to `embed_batch`, not a
  per-row count.
- **HNSW cold-start rationale is the crux of migration 012**: this is
  the one design decision that's easy to get backwards. ivfflat would
  have been the "obvious" choice (it's what migration 004 already uses)
  but it needs data present at build time to cluster meaningfully.
  Wiki embeddings deliberately start at zero (migration ships before
  the backfill runs), which is exactly the case ivfflat handles badly
  and HNSW handles natively. See `plan.md` for the full comparison.
