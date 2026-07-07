"""
Fuzzy + full-text search against the entities table.
Uses a 3-tier scoring system:
  1. AND tsquery match (all words) — weight 1.0
  2. OR tsquery match (any word)  — weight 0.3
  3. Title trigram similarity      — weight 0.3

Retrieval is tiered and capped: candidates come from three independently
index-served subqueries (AND tsquery, title trigram, OR tsquery), each capped
at SEARCH_TIER_CAP rows, UNIONed by id. Only that bounded candidate set is
fetched from the heap, scored and sorted. Without the caps a single common
term can make one branch match a third of the corpus (measured: 500k+ title
candidates, 360k+ OR-tsquery candidates on the prod corpus) and the bitmap
heap recheck turns into a full-table I/O storm.

Content relevance is captured by the tsquery tiers; it is deliberately NOT a
scoring term. `similarity(e.content, q)` would detoast the full content body
of every candidate during ORDER BY — the exact cost that trips
statement_timeout once a popular query matches thousands of rows. Ranking by
tsquery rank + title similarity keeps scoring off the TOASTed column.
"""
import os

import psycopg2.extras

# ------------------------------------------------------------------ #
# Central content-preview helper (shared by recall/search/list/etc.)  #
# ------------------------------------------------------------------ #
# Lives here because search.py is a dependency-free leaf module that
# context.py and the agent tools already import — so this is reused, not
# a new module. The ONLY full-content read is get_entity(<id>); every
# multi-item path renders previews so big/polluted bodies never flood
# (or pollute) the caller's context.
PREVIEW_CAP = int(os.getenv("BRAINDB_PREVIEW_CAP", "1024"))  # <= 1K per item
SLICE_MAX = int(os.getenv("BRAINDB_SLICE_MAX", "8000"))      # max chars per get-by-id slice


def slice_content(text, offset: int = 0, limit: int | None = None) -> tuple[str, dict]:
    """Return (slice, meta) of a full content string for the by-id deep read.
    A slice is clamped to SLICE_MAX so one slice can never itself flood a
    caller — large bodies are read by paging `next_offset` (and/or handing
    each slice to a separate subagent). `meta.next_offset` is None at EOF.
    Used only when offset/limit are explicitly requested; default get-by-id
    behaviour is unchanged (full body)."""
    s = "" if text is None else str(text)
    total = len(s)
    offset = max(0, int(offset))
    eff = SLICE_MAX if limit is None else max(1, min(int(limit), SLICE_MAX))
    chunk = s[offset:offset + eff]
    nxt = offset + len(chunk)
    return chunk, {
        "total_chars": total,
        "offset": offset,
        "returned": len(chunk),
        "next_offset": nxt if nxt < total else None,
    }


def preview(text, entity_id=None, cap: int = PREVIEW_CAP) -> str:
    """Bound a content string to `cap` chars; if cut, append the standard
    marker + drill-down protocol so the LLM knows how to read the full body."""
    s = "" if text is None else str(text)
    if len(s) <= cap:
        return s
    extra = len(s) - cap
    how = f' full body: get_entity("{entity_id}").' if entity_id else "."
    return (
        s[:cap]
        + f"\n--truncated ({extra} more chars)--{how} If large, "
        "delegate_to_subagent to read/extract it without polluting this context."
    )


# Shared SQL fragments
_OR_TSQUERY = "to_tsquery('english', regexp_replace(plainto_tsquery('english', %s)::text, ' & ', ' | ', 'g'))"

_SCORE_EXPR = f"""
    COALESCE(
        CASE WHEN e.search_vector @@ plainto_tsquery('english', %s)
             THEN ts_rank(e.search_vector, plainto_tsquery('english', %s))
             ELSE 0 END, 0)
    + COALESCE(
        CASE WHEN e.search_vector @@ {_OR_TSQUERY}
             AND NOT (e.search_vector @@ plainto_tsquery('english', %s))
             THEN ts_rank(e.search_vector, {_OR_TSQUERY}) * 0.3
             ELSE 0 END, 0)
    + COALESCE(similarity(COALESCE(e.title, ''), %s), 0) * 0.3
    AS score
"""

# Content matching is served entirely by the full-text `search_vector` (the AND
# + OR tsquery branches, GIN-indexed). The content trigram `%` branch was
# REMOVED: even though it used `entities_trgm_idx`, matching a common term built
# a huge candidate bitmap whose recheck DETOASTED every candidate's full content
# body — measured at 7-25s on the prod corpus, the sole component that kept
# /memory/search timing out. Its marginal recall was ~1 row per query that
# tsquery didn't already find. Title trigram (`e.title %`) stays: titles are
# short, never TOASTed, served cheaply by `entities_title_trgm_idx` (migration
# 008). It uses the `%` operator (honouring `pg_trgm.similarity_threshold`, which
# fuzzy_search pins to 0.15 via SET LOCAL); bare `e.title` (not COALESCE) so the
# index applies — `%` yields NULL→false for NULL titles, correctly excluding them.
_CONTENT_TRGM_THRESHOLD = 0.15

# Per-tier candidate cap. Each retrieval tier (AND tsquery / title trigram /
# OR tsquery) contributes at most this many ids before scoring. Bounds the
# heap fetch + recheck work for pathological terms; 5k is 100x a typical
# request limit while staying well under the corpus-scale bitmaps (500k+)
# that caused multi-minute searches. Benchmarked on the prod corpus (1.7M
# rows): worst-case term 95s uncapped -> 1.7s at 5k, top results identical.
SEARCH_TIER_CAP = int(os.getenv("BRAINDB_SEARCH_TIER_CAP", "5000"))


def fuzzy_search(conn, query: str, entity_types: list[str] | None, min_importance: float, limit: int) -> list[dict]:
    # Score: AND check + AND rank (2) + OR tsquery + NOT AND + OR tsquery rank (3) + title trigram (1) = 6
    score_params = (query,) * 6

    # Shared row filters are pushed into every tier so a cap can never be
    # consumed by rows the outer query would discard anyway.
    if entity_types:
        filters = "AND e.entity_type = ANY(%s) AND e.importance >= %s"
        filter_params: tuple = (entity_types, min_importance)
    else:
        filters = "AND e.importance >= %s"
        filter_params = (min_importance,)

    tier_and = f"""
        SELECT e.id FROM entities e
        WHERE e.search_vector @@ plainto_tsquery('english', %s) {filters}
        LIMIT %s
    """
    tier_title = f"""
        SELECT e.id FROM entities e
        WHERE e.title %% %s {filters}
        LIMIT %s
    """
    tier_or = f"""
        SELECT e.id FROM entities e
        WHERE e.search_vector @@ {_OR_TSQUERY}
          AND NOT (e.search_vector @@ plainto_tsquery('english', %s)) {filters}
        LIMIT %s
    """
    tier_params = (
        (query,) + filter_params + (SEARCH_TIER_CAP,)
        + (query,) + filter_params + (SEARCH_TIER_CAP,)
        + (query, query) + filter_params + (SEARCH_TIER_CAP,)
    )

    sql = f"""
        SELECT
            e.id, e.entity_type, e.title, e.content, e.summary,
            e.keywords, e.importance, e.source, e.notes,
            e.created_at, e.updated_at, e.accessed_at, e.access_count, e.metadata,
            {_SCORE_EXPR}
        FROM entities e
        JOIN (
            ({tier_and}) UNION ({tier_title}) UNION ({tier_or})
        ) candidates ON candidates.id = e.id
        ORDER BY score DESC
        LIMIT %s
    """
    params = score_params + tier_params + (limit,)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # The `%` operator in the title tier uses pg_trgm.similarity_threshold
        # (default 0.3). SET LOCAL pins it to the previous 0.15 content cutoff
        # for THIS transaction only — it auto-resets on commit/rollback, so a
        # pooled/reused connection never leaks the lowered threshold.
        cur.execute("SET LOCAL pg_trgm.similarity_threshold = %s", (_CONTENT_TRGM_THRESHOLD,))
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    # Central preview cap — covers /memory/search + quick_search (and the
    # text seeds feeding /memory/context). Real content is read only via
    # get_entity(<id>) (the full carve-out).
    for r in rows:
        r["content"] = preview(r.get("content"), r.get("id"))
    return rows


# ------------------------------------------------------------------ #
# Hybrid lexical + vector search (wiki articles only)                 #
# ------------------------------------------------------------------ #
# Lexical-only `fuzzy_search` misses paraphrase queries that share no
# tokens/trigrams with the target article (e.g. "how do birds find their
# way home" vs a title of "avian magnetoreception"). Wiki articles are the
# one content type big and stable enough to justify a per-row embedding
# (~10k rows vs 1.7M total entities) — everything else stays lexical-only.
# The two retrieval arms are fused with Reciprocal Rank Fusion (RRF) rather
# than a weighted score blend: RRF only needs each arm's *rank order*, not
# comparable score scales, which matters here because ts_rank/trigram
# similarity and cosine similarity are not on the same numeric scale and any
# hand-picked blend weight would be a guess. k=60 is the standard RRF
# constant from the original paper — it damps the influence of a single
# very-high-rank hit from one arm without needing per-deployment tuning.

RRF_K = 60
_VECTOR_ARM_LIMIT = 100


def embed_text_for_wiki(title: str | None, content: str | None) -> str:
    """Build the text embedded for a wiki article: title carries the most
    signal per token so it's unclipped; content is capped at 2000 chars —
    long enough to capture the lead/summary of an article without paying
    to embed (or re-embed on every edit) an entire long-form body."""
    return f"{(title or '').strip()}\n\n{(content or '')[:2000].strip()}".strip()


def _rrf_fuse(lexical_rows: list[dict], vector_rows: list[dict], k: int = RRF_K) -> list[dict]:
    """Fuse two ranked row lists by Reciprocal Rank Fusion.

    Each row must have an `id` key. A row present in both arms sums both
    reciprocal-rank contributions, so it naturally outranks a row found by
    only one arm. Row *data* is taken from the lexical copy when present
    (it already has `preview()` applied to `content`); vector-only rows are
    previewed here so the fused output has a uniform shape either way.
    Returns rows sorted by fused score desc, each with `score` set to the
    fused RRF value (replacing whatever `score`/`similarity` field the arm
    produced — callers must re-read `score` after fusion, not before).
    """
    fused: dict[str, float] = {}
    data: dict[str, dict] = {}

    for rank, row in enumerate(lexical_rows):
        rid = str(row["id"])
        fused[rid] = fused.get(rid, 0.0) + 1.0 / (k + rank + 1)
        data.setdefault(rid, row)

    for rank, row in enumerate(vector_rows):
        rid = str(row["id"])
        fused[rid] = fused.get(rid, 0.0) + 1.0 / (k + rank + 1)
        if rid not in data:
            row = dict(row)
            row["content"] = preview(row.get("content"), row.get("id"))
            data[rid] = row

    merged = []
    for rid, fscore in fused.items():
        row = dict(data[rid])
        row["score"] = fscore
        merged.append(row)

    merged.sort(key=lambda r: r["score"], reverse=True)
    return merged


def hybrid_search(
    conn,
    query: str,
    entity_types: list[str] | None,
    min_importance: float,
    limit: int,
    *,
    locale: str | None = None,
    embedding_service=None,
) -> list[dict]:
    """Lexical + vector hybrid search, fused by RRF. Vector recall is scoped
    to wiki articles only (`entity_type='datasource' AND source='wiki'`) —
    see module docstring for why.

    `locale`: when given, restricts to wiki articles carrying that
    `metadata->>'locale'`. Applied as a post-filter on BOTH arms (the vector
    arm also has it pushed into its SQL WHERE, since that query is already
    wiki-only and it's free to add there). fuzzy_search has no locale
    parameter, so the lexical arm's rows are filtered in Python after the
    call; note this means a locale-filtered hybrid search narrows to wiki
    articles even though the lexical arm itself considers all entity types
    — this is intentional: locale-scoped search only exists for wiki
    (cookpadia's per-locale article pages).

    Degrades to `fuzzy_search` verbatim (same row shape) whenever the vector
    arm can't run: `entity_types` given without 'datasource' in it, no/
    unavailable embedding_service, or the embed call itself returns None.
    """
    if entity_types and "datasource" not in entity_types:
        return fuzzy_search(conn, query, entity_types, min_importance, limit)

    lexical_rows = fuzzy_search(conn, query, entity_types, min_importance, max(limit * 5, 100))

    if locale is not None:
        lexical_rows = [
            r for r in lexical_rows
            if r.get("source") == "wiki" and (r.get("metadata") or {}).get("locale") == locale
        ]

    if embedding_service is None or not embedding_service.is_available():
        return lexical_rows[:limit]

    vec = embedding_service.embed(query)
    if vec is None:
        return lexical_rows[:limit]

    where = "e.entity_type = 'datasource' AND e.source = 'wiki' AND e.embedding IS NOT NULL AND e.importance >= %s"
    where_params: list = [min_importance]
    if locale is not None:
        where += " AND e.metadata->>'locale' = %s"
        where_params.append(locale)

    sql = f"""
        SELECT
            e.id, e.entity_type, e.title, e.content, e.summary,
            e.keywords, e.importance, e.source, e.notes,
            e.created_at, e.updated_at, e.accessed_at, e.access_count, e.metadata,
            1 - (e.embedding <=> %s::vector) AS similarity
        FROM entities e
        WHERE {where}
        ORDER BY e.embedding <=> %s::vector
        LIMIT %s
    """
    # str(vec) passed once per `%s::vector` placeholder occurrence (SELECT,
    # then ORDER BY) — same pattern as keyword_service.find_similar_keywords.
    vec_str = str(vec)
    full_params = [vec_str, *where_params, vec_str, _VECTOR_ARM_LIMIT]
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, tuple(full_params))
        vector_rows = [dict(r) for r in cur.fetchall()]

    return _rrf_fuse(lexical_rows, vector_rows)[:limit]


def generate_missing_wiki_embeddings(
    conn, embedding_service, *, force: bool = False, batch_size: int = 32
) -> dict:
    """Backfill embeddings for wiki-article datasources. Mirrors
    `keyword_service.generate_missing_embeddings`; separate function because
    the source table filter and the embedded text (title+content, not a bare
    keyword string) differ.

    By default only fills rows with a NULL embedding. `force=True`
    regenerates all wiki embeddings — required after switching the embedding
    model, since vectors from a different model live in an incompatible
    space and must not be mixed in the cosine index.
    """
    where = "entity_type = 'datasource' AND source = 'wiki'"
    if not force:
        where += " AND embedding IS NULL"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SELECT id, title, content FROM entities WHERE {where}")
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        return {"scanned": 0, "embedded": 0, "failed": 0}

    texts = [embed_text_for_wiki(r.get("title"), r.get("content")) for r in rows]
    embeddings = embedding_service.embed_batch(texts, batch_size=batch_size)

    if not embeddings:
        return {"scanned": len(rows), "embedded": 0, "failed": len(rows)}

    embedded = 0
    with conn.cursor() as cur:
        for row, emb in zip(rows, embeddings):
            cur.execute(
                "UPDATE entities SET embedding = %s WHERE id = %s",
                (str(emb), str(row["id"])),
            )
            embedded += 1

    return {"scanned": len(rows), "embedded": embedded, "failed": len(rows) - embedded}
