# Hybrid lexical + vector search for wiki articles

## Problem

`fuzzy_search` (tsquery + trigram) only finds rows sharing tokens or
trigrams with the query. It misses paraphrase queries against wiki
articles — e.g. "cold espresso drinks" won't find an article titled
"iced latte": no shared word, no shared trigram, zero recall regardless
of relevance. Wiki articles are exactly the content type where this
matters most: they're long-form, synthesised prose, and users query them
conversationally rather than by exact title/keyword.

## Why wiki-only, not all entities

Adding a per-row embedding is not free: it's a write on every create/
update, a backfill job, and an index to keep warm. Wiki-article
datasources (`entity_type='datasource' AND source='wiki'`) are ~10k rows
in prod vs 1.7M entities total — small enough that the embedding
write/backfill/index cost is negligible, and they're also the one type
big and stable enough (long-form, edited occasionally, not a firehose
like facts/thoughts) to justify it. Everything else stays lexical-only;
`hybrid_search` degrades to `fuzzy_search` verbatim whenever the entity
types requested don't include `datasource`.

## Retrieval design

Two independent arms, fused rather than blended:

- **Lexical arm**: existing `fuzzy_search`, over-fetched at
  `max(limit*5, 100)` candidates so there's enough depth for fusion to
  work with.
- **Vector arm**: `embedding_service.embed(query)` → pgvector cosine
  search (`embedding <=> query_vector`) restricted to
  `entity_type='datasource' AND source='wiki' AND embedding IS NOT NULL`,
  optionally further restricted by `metadata->>'locale'`, capped at 100
  rows.

### RRF fusion instead of a weighted blend

`ts_rank`/trigram similarity and cosine similarity are not on the same
numeric scale, and any hand-picked blend weight between them would be a
guess with no principled way to validate it. Reciprocal Rank Fusion
(RRF) sidesteps that: it only needs each arm's *rank order*, not
comparable scores. Each row's fused score is
`sum(1 / (k + rank + 1))` over the arms it appears in (rank is
0-indexed), so a row that shows up early in *both* arms naturally
outranks one that only shows up in one. `k=60` is the standard constant
from the original RRF paper — it damps the influence of a single very-
high-rank hit from one arm without needing per-deployment tuning.

### Degradation contract

`hybrid_search` degrades to `fuzzy_search`'s exact row shape (so callers
never need to branch on mode) whenever the vector arm can't meaningfully
run:

- `entity_types` given and it doesn't include `'datasource'` — there
  can be no wiki rows in the result set, so skip the embed call entirely
  (see `test_hybrid_search_skips_vector_arm_when_entity_types_excludes_datasource`).
- No `embedding_service` passed, or `embedding_service.is_available()`
  is `False` (no `EMBED_MODEL` configured) — return the lexical rows
  unchanged.
- `embedding_service.embed(query)` returns `None` — `EmbeddingService`
  swallows provider errors and returns `None` rather than raising (see
  `embedding_service.py`), so hybrid search treats that as "no vector
  arm available", not a failure.

`locale`, when given, restricts to wiki articles: pushed into the vector
arm's SQL `WHERE` (free, since that query is already wiki-only) and
applied as a Python post-filter on the lexical arm's rows (`fuzzy_search`
has no locale parameter). This means a locale-scoped hybrid search
narrows to wiki articles even if `entity_types` didn't explicitly say so
— intentional, since locale-scoped search only makes sense for wiki
(cookpadia's per-locale article pages).

## Embedded text

`embed_text_for_wiki(title, content)` = title (unclipped — carries the
most signal per token) + first 2000 chars of content (long enough for
the lead/summary without paying to re-embed an entire long-form body on
every edit). Shared by the write path (`_maybe_embed_wiki_datasource` in
`routers/entities.py`) and the backfill path
(`generate_missing_wiki_embeddings` in `services/search.py`), so the
embedding space is guaranteed to be built the same way whichever path
wrote it.

## Embed-on-write

`create_datasource`, `datasources/ingest`, and `update_datasource` all
call `_maybe_embed_wiki_datasource` after the row lands, gated on
`source == 'wiki'`. On update, the gate checks the **post**-update
source (not the pre-update snapshot): a PATCH can flip `source` to/from
`'wiki'` in the same request, and gating on the stale value would either
skip embedding a row that just became wiki-eligible or embed one that
just left. Embedding failure (`is_available()` false or `embed()`
returns `None`) is a silent skip, not a request failure — hybrid search
already degrades to lexical for rows with no embedding, so a missed
write here is a transparent quality note, never something that should
break a create/update call.

## Backfill

`POST /memory/generate-wiki-embeddings` mirrors the existing
`POST /memory/generate-embeddings` (keyword backfill): 503 if no
`EMBED_MODEL` configured, `force=false` by default (fills only NULL
embeddings), `force=true` regenerates every wiki embedding (required
after switching the embedding model — vectors from a different model
live in an incompatible space and must never be mixed in the same
cosine index).

## Migration 012 — HNSW vs ivfflat

The existing keyword-embedding index (migration 004) uses `ivfflat`.
Wiki embeddings use `HNSW` instead, for one specific reason: **cold
start**. `ivfflat`'s `lists` clustering is computed from whatever data
exists at *build* time. Keyword embeddings are written at creation time,
so by the time their ivfflat index is built the column is already
populated — clustering has real data to key off. Wiki embeddings start
at zero (this migration ships before the backfill runs) and get filled
by a separate backfill call plus incremental per-write updates after
that. Building an ivfflat index over an empty column bakes in
meaningless clusters that only get fixed by a manual `REINDEX` after the
backfill completes — an operational step nobody would remember to run.
HNSW builds its graph incrementally as rows are inserted/updated, so an
index built over an empty (or partially backfilled) column stays correct
through the backfill and every subsequent write, with no reindex step
required.

The index is partial (`WHERE entity_type='datasource' AND source='wiki'`)
so it only ever covers wiki rows — keyword embeddings (a different
semantic space, already served by `entities_embedding_idx`) never enter
its scan. Built `CONCURRENTLY` inside an `autocommit_block`, the same
pattern as migration 011: holds no long write lock and can't be killed
mid-transaction by the liveness probe.

## Deploy sequence

1. Merge + deploy this branch. `alembic upgrade head` runs migration 012
   automatically as part of the existing `docker compose` startup
   command — no manual migration step.
2. Run `POST /memory/generate-wiki-embeddings` once, against the ~10k
   existing wiki rows, **before** any consumer starts sending
   `mode="hybrid"` traffic. Until the backfill completes, hybrid search
   still works (falls back to lexical per-row for anything still
   missing an embedding) but recall quality for paraphrase queries is
   only as good as backfill progress.
3. Downstream consumer: cookpad/global-search-cookpadia#81 flips the
   flag that switches cookpadia's wiki search calls to
   `mode="hybrid"`. That flag should not flip until step 2 is done.
