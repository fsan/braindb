"""
Context assembly:
  1. Fuzzy search → seed entities
  2. Graph expand → up to 3 hops
  3. Temporal decay + reinforcement → effective_importance
  4. Final rank = search_score * effective_importance * accumulated_relevance
  5. Fetch always-on rules
  6. Increment access_count / accessed_at for all returned entities
"""
import math
from datetime import UTC, datetime, timezone
from uuid import UUID

import psycopg2.extras

from braindb.config import settings
from braindb.schemas.search import ContextRequest, ContextResponse, SearchResultItem
from braindb.services.embedding_service import get_embedding_service
from braindb.services.graph import graph_expand
from braindb.services.keyword_service import (
    find_entities_for_keywords,
    find_fuzzy_keywords,
    find_similar_keywords,
)
from braindb.services.search import fuzzy_search, preview

DECAY_RATES = {
    "thought":    settings.decay_rate_thought,
    "fact":       settings.decay_rate_fact,
    "source":     settings.decay_rate_source,
    "datasource": settings.decay_rate_datasource,
    "rule":       settings.decay_rate_rule,
    "wiki":       settings.decay_rate_wiki,
}


def effective_importance(importance: float, created_at: datetime, access_count: int, entity_type: str) -> float:
    rate = DECAY_RATES.get(entity_type, 0.003)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    age_days = max(0, (datetime.now(UTC) - created_at).days)
    decay = math.exp(-rate * age_days)
    reinforce = 1.0 + 0.05 * math.log(1 + access_count)
    return min(1.0, importance * decay * reinforce)


# ------------------------------------------------------------------ #
# Extension fields                                                    #
# ------------------------------------------------------------------ #

EXT_QUERIES = {
    "thought":    ("thoughts_ext",    "entity_id, certainty, context, emotional_valence"),
    "fact":       ("facts_ext",       "entity_id, certainty, is_verified, source_entity_id"),
    "source":     ("sources_ext",     "entity_id, url, domain, http_status, last_checked_at"),
    "datasource": ("datasources_ext", "entity_id, file_path, url, content_hash, word_count, language"),
    "rule":       ("rules_ext",       "entity_id, always_on, category, priority, is_active"),
    "wiki":       ("wikis_ext",       "entity_id, canonical_name, disambiguation, language, member_keyword_ids::text[] AS member_keyword_ids, revision, last_synthesised_at, retired_at, redirect_to"),
}


def fetch_ext(conn, rows: list[dict]) -> dict:
    by_type: dict[str, list] = {}
    for r in rows:
        by_type.setdefault(r["entity_type"], []).append(str(r["id"]))

    ext_map = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for etype, ids in by_type.items():
            if etype not in EXT_QUERIES:
                continue
            table, cols = EXT_QUERIES[etype]
            cur.execute(f"SELECT {cols} FROM {table} WHERE entity_id = ANY(%s::uuid[])", (ids,))
            for row in cur.fetchall():
                eid = row["entity_id"]
                ext_map[eid] = {k: v for k, v in row.items() if k != "entity_id"}
    return ext_map


# ------------------------------------------------------------------ #
# Always-on rules                                                     #
# ------------------------------------------------------------------ #

def fetch_always_on_rules(conn) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT e.*, r.always_on, r.category, r.priority, r.is_active
            FROM entities e
            JOIN rules_ext r ON r.entity_id = e.id
            WHERE r.always_on = TRUE AND r.is_active = TRUE
            ORDER BY r.priority DESC
            LIMIT %s
        """, (settings.max_always_on_rules,))
        return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------------ #
# Access tracking                                                     #
# ------------------------------------------------------------------ #

def track_access(conn, ids: list) -> None:
    if not ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE entities SET access_count = access_count + 1, accessed_at = now() WHERE id = ANY(%s::uuid[])",
            ([str(i) for i in ids],),
        )


# ------------------------------------------------------------------ #
# Row → SearchResultItem                                              #
# ------------------------------------------------------------------ #

def _to_item(row: dict, search_score: float, depth: int, relevance: float, ext: dict) -> SearchResultItem:
    eff = effective_importance(row["importance"], row["created_at"], row["access_count"], row["entity_type"])
    return SearchResultItem(
        id=row["id"],
        entity_type=row["entity_type"],
        title=row.get("title"),
        content=preview(row.get("content"), row.get("id")),
        summary=row.get("summary"),
        keywords=row.get("keywords") or [],
        importance=row["importance"],
        source=row.get("source"),
        notes=row.get("notes"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        accessed_at=row.get("accessed_at"),
        access_count=row.get("access_count", 0),
        search_score=search_score,
        effective_importance=eff,
        depth=depth,
        accumulated_relevance=relevance,
        final_rank=search_score * eff * relevance,
        ext=ext,
    )


# ------------------------------------------------------------------ #
# Two-level diversity quota (per-search-term + per-keyword)            #
# ------------------------------------------------------------------ #

def _apply_two_level_quota(
    items: list,
    dominant_kw_by_id: dict[str, str],
    per_query_top_ids: list[list[str]],
    max_results: int,
    per_query_share: float,
    halving: float,
) -> list:
    """Re-rank `items` (sorted by `final_rank` desc) under two
    complementary diversity quotas. Both run in ONE pass so they can
    never conflict.

    Level 1 — per-search-term (the user's outer quota):
      Each query in `per_query_top_ids` gets a reserved share of the
      output. Walking the per-query top-K lists first guarantees each
      angle of the multi-query recall surfaces something in the
      result, even if its absolute scores would be outranked globally.

    Level 2 — per-keyword (the inner quota):
      Walking the remaining items in `final_rank`-desc order, each
      new dominant matched keyword gets a halving slot allowance
      (`ceil(max_results × halving^n)`, floor 1). Stops a single
      popular keyword (e.g. `user-profile` tagging 100 biographical
      facts) from monopolising the open portion of the result.

    HOW THE TWO LEVELS COEXIST WITHOUT CONFLICT (this is the crucial
    bit, please read before changing this function):

      Both levels share ONE counter dict (`seen`: kw_id → remaining).
      Level 1 places reserved items first and decrements their
      dominant keyword's allowance. Level 2 then walks the open items
      and respects what L1 already consumed. So:

      - A reserved item is added unconditionally (L1 wins). Its
        keyword's L2 quota shrinks accordingly — no double spending
        in the open phase.
      - If a popular keyword's allowance is exhausted purely by L1
        reservations, L2 will skip further entities tagged dominantly
        with it. That's the intended hard cap.
      - Items without a dominant keyword (graph-expansion finds, the
        discoverability backup) pass through both phases freely;
        they're not counted against any keyword's allowance.

    `per_query_share`=0 disables L1 (only L2 runs). `halving`>=1.0
    disables L2 (only L1 + raw top-N for the rest). Both at extremes
    = raw top-N.
    """
    seen: dict[str, int] = {}   # kw_id → remaining slots (SHARED across L1 + L2)
    n_new = 0                    # number of distinct keywords met so far (drives the halving sequence)
    taken: set[str] = set()      # entity ids already placed (dedup across L1's per-query lists)
    out: list = []

    def _consume(item) -> bool:
        """Try to place `item` in `out`, respecting the per-keyword quota.
        Returns True if placed, False if blocked by L2."""
        nonlocal n_new
        if str(item.id) in taken:
            return False
        kw = dominant_kw_by_id.get(str(item.id))
        if kw is None:
            # No keyword to gate against (graph-expansion / discovery
            # fallback) — let it through.
            taken.add(str(item.id))
            out.append(item)
            return True
        if halving < 1.0:
            if kw not in seen:
                # Lazy-init this keyword's allowance using its position
                # in the geometric-decay sequence.
                seen[kw] = max(1, math.ceil(max_results * (halving ** n_new)))
                n_new += 1
            if seen[kw] <= 0:
                return False
            seen[kw] -= 1
        taken.add(str(item.id))
        out.append(item)
        return True

    # Map id → item so we can walk per-query lists in O(1).
    by_id: dict[str, object] = {str(it.id): it for it in items}

    # ---- LEVEL 1: per-search-term reservation phase --------------------
    # Walk each query's own top-K and place reserved items first.
    # `per_query_top_ids[q_index]` is already sorted by THIS query's
    # combined score, so we get the best-for-this-angle items first.
    if per_query_share > 0:
        for q_top in per_query_top_ids:
            for eid in q_top:
                item = by_id.get(eid)
                if item is None:
                    continue
                _consume(item)
                if len(out) >= max_results:
                    return out

    # ---- LEVEL 2: open phase with per-keyword quota --------------------
    # Walk remaining items in global final_rank-desc order. `_consume`
    # respects whatever L1 already used up in the `seen` counter, so
    # a keyword that filled its quota via L1 is correctly blocked here.
    for item in items:
        if len(out) >= max_results:
            break
        _consume(item)
    return out


# ------------------------------------------------------------------ #
# Main context assembly                                               #
# ------------------------------------------------------------------ #

def assemble_context(conn, req: ContextRequest) -> ContextResponse:
    # Normalize to list of queries
    query_list = req.queries if req.queries else [req.query]

    # ------------------------------------------------------------------ #
    # 1. TEXT SEARCH (keyword-mediated) — fuzzy on KEYWORD entities,      #
    #    then fan out via tagged_with. Symmetric to the embedding         #
    #    pathway below: both produce a per-entity score equal to the      #
    #    best match between the query and the entity's tagged keywords.   #
    #    This avoids the pg_trgm dilution that previously hit any short   #
    #    query against a long entity body — keywords are short, so the    #
    #    trigram intersection is meaningful, not diluted.                 #
    # ------------------------------------------------------------------ #
    text_scores: dict = {}       # entity_id → best keyword-fuzzy similarity (max across queries)
    text_dom_kw: dict = {}       # entity_id → keyword_id that yielded the text_scores max
    text_scores_by_q: list = []  # per-query: list of {entity_id → best_sim for THIS query}
    seed_rows_by_id: dict = {}   # entity_id → row data
    fuzzy_rows: dict = {}        # entity_id → row data (entities found only via fuzzy-keyword)

    for q in query_list:
        per_q_scores: dict = {}  # this query's text scores only — feeds Level-1 quota
        fuzzy_kw = find_fuzzy_keywords(
            conn, q, limit=settings.scoring_pool_fuzzy,
        )
        if fuzzy_kw:
            kw_sim = {str(kw["id"]): kw["similarity"] for kw in fuzzy_kw}
            entities = find_entities_for_keywords(conn, list(kw_sim.keys()))
            for ent in entities:
                eid = ent["id"]
                matched_ids = [str(mid) for mid in (ent.get("matched_keyword_ids") or [])]
                if matched_ids:
                    # Pick the matched keyword with the strongest similarity for this entity
                    best_kw_id = max(matched_ids, key=lambda m: kw_sim.get(m, 0))
                    best_sim = kw_sim.get(best_kw_id, 0)
                    per_q_scores[str(eid)] = best_sim
                    if eid not in text_scores or best_sim > text_scores[eid]:
                        text_scores[eid] = best_sim
                        text_dom_kw[eid] = best_kw_id
                        if eid not in seed_rows_by_id:
                            fuzzy_rows[eid] = ent
        text_scores_by_q.append(per_q_scores)

    # Discoverability backup — entities whose content matches the query
    # directly but aren't tagged with a matching keyword. Heavy discount
    # (`DISCOVERY_DISCOUNT`) keeps them weakly-ranked. Pure fallback: only
    # set text_scores for an entity if the keyword-mediated path didn't
    # already cover it (never override a real keyword match).
    DISCOVERY_DISCOUNT = 0.2
    for q in query_list:
        rows = fuzzy_search(
            conn, q, req.entity_types, req.min_importance,
            limit=settings.scoring_pool_fuzzy,
        )
        for r in rows:
            eid = r["id"]
            if eid in text_scores:
                continue   # keyword path already scored this entity; do not override
            text_scores[eid] = r["score"] * DISCOVERY_DISCOUNT
            if eid not in seed_rows_by_id and eid not in fuzzy_rows:
                fuzzy_rows[eid] = r

    # ------------------------------------------------------------------ #
    # 2. KEYWORD EMBEDDING SEARCH (new) — semantic via keyword vectors    #
    # ------------------------------------------------------------------ #
    embedding_scores: dict = {}  # entity_id → best keyword similarity (max across queries)
    embedding_dom_kw: dict = {}  # entity_id → keyword_id that yielded the embedding_scores max
    embedding_scores_by_q: list = []  # per-query embedding scores — feeds Level-1 quota
    embedding_rows: dict = {}    # entity_id → row data (for entities found only via embedding)

    emb_svc = get_embedding_service()
    if emb_svc.is_available():
        for q in query_list:
            per_q_scores: dict = {}
            query_emb = emb_svc.embed(q)
            if query_emb:
                # Scoring pool — same principle: wide candidate set for the
                # embedding pathway. A narrow keyword may rank far below 30 for
                # a sentence-shaped query even when it's an exact term match;
                # widening here keeps it visible to the rest of the pipeline.
                similar_kw = find_similar_keywords(
                    conn, query_emb, limit=settings.scoring_pool_keyword_neighbors,
                )
                if similar_kw:
                    kw_sim = {str(kw["id"]): kw["similarity"] for kw in similar_kw}
                    kw_ids = list(kw_sim.keys())
                    entities = find_entities_for_keywords(conn, kw_ids)
                    for ent in entities:
                        eid = ent["id"]
                        matched_ids = [str(mid) for mid in (ent.get("matched_keyword_ids") or [])]
                        if matched_ids:
                            best_kw_id = max(matched_ids, key=lambda m: kw_sim.get(m, 0))
                            best_sim = kw_sim.get(best_kw_id, 0)
                            per_q_scores[str(eid)] = best_sim
                            if eid not in embedding_scores or best_sim > embedding_scores[eid]:
                                embedding_scores[eid] = best_sim
                                embedding_dom_kw[eid] = best_kw_id
                                if eid not in seed_rows_by_id:
                                    embedding_rows[eid] = ent
            embedding_scores_by_q.append(per_q_scores)
    else:
        embedding_scores_by_q = [{} for _ in query_list]

    # ------------------------------------------------------------------ #
    # 3. MERGE — geometric mean when both, penalty when single signal     #
    # ------------------------------------------------------------------ #
    all_entity_ids = set(text_scores.keys()) | set(embedding_scores.keys())
    seed_scores: dict = {}
    penalty = settings.missing_signal_penalty
    for eid in all_entity_ids:
        text_s = text_scores.get(eid)
        emb_s = embedding_scores.get(eid)
        if text_s and emb_s:
            seed_scores[eid] = math.sqrt(text_s * emb_s)  # geometric mean — both agree
        elif text_s:
            seed_scores[eid] = text_s * penalty            # text only — penalized
        elif emb_s:
            seed_scores[eid] = emb_s * penalty             # embedding only — penalized
        # Ensure we have row data for entities that came in via either
        # of the two keyword-mediated pathways.
        if eid not in seed_rows_by_id and eid in fuzzy_rows:
            seed_rows_by_id[eid] = fuzzy_rows[eid]
        if eid not in seed_rows_by_id and eid in embedding_rows:
            seed_rows_by_id[eid] = embedding_rows[eid]

    # ------------------------------------------------------------------ #
    # 4. GRAPH EXPAND + RANK (existing pipeline)                          #
    # ------------------------------------------------------------------ #
    seed_ids = list(seed_scores.keys())

    if seed_ids and req.max_depth > 0:
        graph_rows = graph_expand(conn, seed_ids, req.max_depth, req.min_relevance)
    else:
        graph_rows = [{**r, "min_depth": 0, "relevance": 1.0} for r in seed_rows_by_id.values()]

    seen = {}
    for r in graph_rows:
        eid = r["id"]
        if eid not in seen or r.get("relevance", 1.0) > seen[eid].get("relevance", 0):
            seen[eid] = r

    # Filter out keyword entities from results — they're infrastructure, not content
    all_rows = [r for r in seen.values() if r.get("entity_type") != "keyword"]
    ext_map = fetch_ext(conn, all_rows)

    items = []
    for row in all_rows:
        eid = row["id"]
        # Score = the entity's own similarity if it was a seed; otherwise inherit
        # the score of the seed it descended from (carried by `seed_origin_id`
        # through the graph CTE). This propagates the real similarity signal
        # through depth-1+ hops instead of resetting to a literal fallback.
        score = seed_scores.get(eid)
        if score is None:
            origin = row.get("seed_origin_id")
            score = seed_scores.get(str(origin), 1.0) if origin else 1.0
        depth = row.get("min_depth", 0)
        relevance = row.get("relevance", 1.0)
        items.append(_to_item(row, score, depth, relevance, ext_map.get(eid, {})))

    items.sort(key=lambda x: x.final_rank, reverse=True)

    # Build the inputs the two-level diversity quota needs.
    #
    # `dominant_kw_by_id`: which matched keyword "won" for each entity
    # (used by Level 2 — per-keyword quota). Whichever pathway scored
    # the entity higher (text-fuzzy or embedding) supplies the keyword.
    dominant_kw_by_id: dict[str, str] = {}
    for eid in seed_scores:
        text_s = text_scores.get(eid, 0.0)
        emb_s = embedding_scores.get(eid, 0.0)
        if emb_s >= text_s and eid in embedding_dom_kw:
            dominant_kw_by_id[str(eid)] = embedding_dom_kw[eid]
        elif eid in text_dom_kw:
            dominant_kw_by_id[str(eid)] = text_dom_kw[eid]

    # `per_query_top_ids`: each query's top-K entities by THAT query's
    # own combined score (geometric-mean merge of text + embedding per
    # query, same formula the global merge uses). Used by Level 1 —
    # per-search-term reservation. Each query gets `K` reserved slots:
    # `K = ceil(max_results × per_query_share / num_queries)`. The
    # narrow-query-strategy nudge in `recall_memory`'s docstring is
    # what makes this useful: when the agent issues a focused
    # single-keyword query alongside broader ones, that focused query
    # is guaranteed a reserved share of the result.
    penalty = settings.missing_signal_penalty
    nq = max(1, len(query_list))
    per_q_reserved = max(
        0, math.ceil(req.max_results * settings.per_query_share / nq)
    )
    per_query_top_ids: list[list[str]] = []
    if per_q_reserved > 0 and settings.per_query_share > 0:
        for q_idx in range(nq):
            t_q = text_scores_by_q[q_idx] if q_idx < len(text_scores_by_q) else {}
            e_q = embedding_scores_by_q[q_idx] if q_idx < len(embedding_scores_by_q) else {}
            # Same merge math as the global seed_scores, but using
            # only THIS query's text and embedding signals.
            per_q_seed: dict[str, float] = {}
            for eid in set(t_q) | set(e_q):
                t = t_q.get(eid)
                e = e_q.get(eid)
                if t and e:
                    per_q_seed[eid] = math.sqrt(t * e)
                elif t:
                    per_q_seed[eid] = t * penalty
                elif e:
                    per_q_seed[eid] = e * penalty
            ordered = sorted(per_q_seed.items(), key=lambda kv: -kv[1])[:per_q_reserved]
            per_query_top_ids.append([eid for eid, _ in ordered])

    items = _apply_two_level_quota(
        items,
        dominant_kw_by_id,
        per_query_top_ids,
        req.max_results,
        per_query_share=settings.per_query_share,
        halving=settings.keyword_quota_halving,
    )

    always_on = []
    if req.include_always_on_rules:
        rule_rows = fetch_always_on_rules(conn)
        rule_ext = fetch_ext(conn, rule_rows)
        for row in rule_rows:
            eid = row["id"]
            always_on.append(_to_item(row, 1.0, 0, 1.0, rule_ext.get(eid, {})))

    track_access(conn, [item.id for item in items] + [item.id for item in always_on])

    return ContextResponse(
        query=req.query or (req.queries[0] if req.queries else ""),
        queries=query_list,
        items=items,
        always_on_rules=always_on,
        total_found=len(items),
    )
