"""
Memory intelligence endpoints.
"""
import re
import time
from uuid import UUID

import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from braindb.db import get_conn
from braindb.schemas.search import ContextRequest, ContextResponse, SearchRequest, SearchResultItem
from braindb.services.activity_log import log_activity, query_log
from braindb.services.embedding_service import get_embedding_service
from braindb.services.keyword_service import generate_missing_embeddings
from braindb.services.context import (
    assemble_context,
    effective_importance,
    fetch_always_on_rules,
    fetch_ext,
    track_access,
)
from braindb.services.graph import graph_expand
from braindb.services.search import fuzzy_search

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])


@router.post("/search", response_model=list[SearchResultItem])
def search(body: SearchRequest):
    with get_conn() as conn:
        rows = fuzzy_search(conn, body.query, body.entity_types, body.min_importance, body.limit)
        items = []
        for r in rows:
            eff = effective_importance(r["importance"], r["created_at"], r["access_count"], r["entity_type"])
            items.append(SearchResultItem(
                id=r["id"], entity_type=r["entity_type"], title=r.get("title"),
                content=r["content"], summary=r.get("summary"),
                keywords=r.get("keywords") or [], importance=r["importance"],
                source=r.get("source"),
                notes=r.get("notes"),
                created_at=r.get("created_at"), updated_at=r.get("updated_at"),
                accessed_at=r.get("accessed_at"), access_count=r.get("access_count", 0),
                search_score=r["score"],
                effective_importance=eff, depth=0, accumulated_relevance=1.0,
                final_rank=r["score"] * eff, ext={},
            ))
        track_access(conn, [item.id for item in items])
        log_activity(conn, "search", details={"query": body.query, "results": len(items)})
        return items


@router.post("/context", response_model=ContextResponse)
def context(body: ContextRequest):
    with get_conn() as conn:
        result = assemble_context(conn, body)
        log_activity(conn, "context", details={
            "queries": result.queries,
            "results": result.total_found,
        })
        return result


@router.get("/rules")
def get_rules():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT e.id, e.title, e.content, e.summary, e.keywords,
                       e.importance, e.notes, e.created_at, e.updated_at,
                       r.always_on, r.category, r.priority, r.is_active
                FROM entities e
                JOIN rules_ext r ON r.entity_id = e.id
                WHERE r.is_active = TRUE
                ORDER BY r.always_on DESC, r.priority DESC
            """)
            return [dict(row) for row in cur.fetchall()]


@router.get("/tree/{entity_id}")
def entity_tree(
    entity_id: UUID,
    max_depth: int = Query(default=2, ge=1, le=3),
    include_keywords: bool = Query(default=False),
    top_k: int = Query(default=40, ge=1, le=500),
    min_path_score: float = Query(default=0.0, ge=0.0, le=1.0),
):
    """Return an entity and its graph neighbourhood as a nested JSON tree.

    Round-2f shape: root keyed by ``entity_type``; ``children`` array of
    typed nodes (each keyed by its own ``entity_type`` and labelled to
    its type — wiki=title, fact/thought=content, source=filename, etc.);
    multi-path first-wins by accumulated path score; ``tagged_with``
    keyword edges skipped by default (root's ``keywords`` array is the
    one-liner instead). Top-``top_k`` connections are kept; the rest are
    summarised with a single ``_truncated`` marker.
    """
    from braindb.services.tree import build_entity_tree
    with get_conn() as conn:
        tree = build_entity_tree(
            conn,
            str(entity_id),
            max_depth=max_depth,
            include_keywords=include_keywords,
            top_k=top_k,
            min_path_score=min_path_score,
        )
    if tree is None:
        raise HTTPException(404, "Entity not found")
    return tree


@router.get("/log")
def get_activity_log(
    operation: str | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
):
    """Query the activity log. Operations: create, update, delete, search, context, ingest, sql_query."""
    with get_conn() as conn:
        return query_log(
            conn,
            operation=operation,
            entity_id=str(entity_id) if entity_id else None,
            since=since,
            until=until,
            limit=limit,
        )


class SqlRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=5000)


_SAFE_SQL_RE = re.compile(r"^\s*(SELECT|WITH)\s", re.IGNORECASE)


@router.post("/sql")
def read_only_sql(body: SqlRequest):
    """
    Execute a read-only SQL query against the database.
    Only SELECT and WITH queries are allowed. 5 second timeout, 1000 row limit.
    """
    if not _SAFE_SQL_RE.match(body.query):
        raise HTTPException(400, "Only SELECT or WITH queries are allowed")

    start = time.perf_counter()
    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("SET LOCAL statement_timeout = '5s'")
                cur.execute("SET LOCAL transaction_read_only = on")
                cur.execute(body.query)
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchmany(1000)
                # Convert rows to JSON-safe format
                safe_rows = [[_to_safe(v) for v in row] for row in rows]
            except Exception as e:
                raise HTTPException(400, f"Query error: {e}")

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        log_activity(conn, "sql_query", details={
            "query": body.query[:500],
            "rows": len(safe_rows),
            "elapsed_ms": elapsed_ms,
        })
        return {
            "columns": columns,
            "rows": safe_rows,
            "row_count": len(safe_rows),
            "elapsed_ms": elapsed_ms,
        }


def _to_safe(value):
    """Convert a DB value to a JSON-serializable form."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    # UUID, datetime, Decimal, etc.
    return str(value)


@router.post("/generate-embeddings")
def generate_embeddings(
    force: bool = Query(
        False,
        description="Regenerate ALL keyword embeddings, not just missing ones. "
        "Use after switching the embedding model.",
    )
):
    """Generate keyword embeddings. With ``force=true`` regenerates all of them."""
    emb_svc = get_embedding_service()
    if not emb_svc.is_available():
        raise HTTPException(503, "Embedding service not available — is EMBED_MODEL set?")
    with get_conn() as conn:
        result = generate_missing_embeddings(conn, emb_svc, force=force)
        log_activity(conn, "generate_embeddings", details=result)
        return result


@router.get("/stats")
def stats():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT entity_type, COUNT(*) AS cnt FROM entities GROUP BY entity_type")
            counts = {r["entity_type"]: r["cnt"] for r in cur.fetchall()}

            cur.execute("SELECT COUNT(*) AS cnt FROM relations")
            rel_count = cur.fetchone()["cnt"]

            cur.execute("""
                SELECT id, entity_type, title, created_at
                FROM entities ORDER BY created_at DESC LIMIT 5
            """)
            recent = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT id, entity_type, title, access_count
                FROM entities ORDER BY access_count DESC LIMIT 5
            """)
            top = [dict(r) for r in cur.fetchall()]

        return {
            "entity_counts": counts,
            "total_entities": sum(counts.values()),
            "total_relations": rel_count,
            "recent_entities": recent,
            "most_accessed": top,
        }
