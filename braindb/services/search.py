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
