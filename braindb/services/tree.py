"""Shared tree-build service.

Single source of truth for "walk the relation graph outward from an entity".
Used by both the HTTP endpoint (`/api/v1/memory/tree/<id>`) and the agent's
`view_tree` tool. Walks bidirectionally and respects `max_depth`.
"""
from __future__ import annotations

import psycopg2.extras

from braindb.services.context import fetch_ext


_TREE_SQL = """
WITH RECURSIVE traversal AS (
    SELECT e.id, e.entity_type, e.title, e.content, e.keywords,
           e.importance, e.notes,
           0 AS depth,
           ARRAY[e.id] AS visited,
           NULL::TEXT  AS via_relation_type,
           NULL::TEXT  AS via_description,
           NULL::FLOAT AS relevance_score,
           NULL::TEXT  AS direction
    FROM entities e
    WHERE e.id = %s

    UNION ALL

    SELECT target.id, target.entity_type, target.title, target.content,
           target.keywords, target.importance, target.notes,
           t.depth + 1,
           t.visited || target.id,
           r.relation_type,
           r.description,
           r.relevance_score,
           CASE WHEN r.from_entity_id = t.id THEN 'outgoing' ELSE 'incoming' END
    FROM traversal t
    JOIN relations r ON r.from_entity_id = t.id OR r.to_entity_id = t.id
    JOIN entities target ON target.id = CASE
        WHEN r.from_entity_id = t.id THEN r.to_entity_id
        ELSE r.from_entity_id
    END
    WHERE t.depth < %s
      AND NOT (target.id = ANY(t.visited))
)
SELECT DISTINCT ON (id)
    id, entity_type, title, content, keywords, importance, notes,
    depth, via_relation_type, via_description, relevance_score, direction
FROM traversal
WHERE depth > 0
ORDER BY id, depth, relevance_score DESC NULLS LAST
"""


def build_entity_tree(conn, entity_id: str, max_depth: int = 2) -> dict | None:
    """Walk the relation graph bidirectionally from `entity_id` up to
    `max_depth` hops. Returns:

        {"root": <entity_dict>, "connections": [<connection_dict>, ...]}

    or ``None`` if the root entity is not found.

    Each connection dict has keys:
        entity, depth, relevance, via_relation_type, via_description, direction
    where `direction` is "outgoing" or "incoming" relative to the root path.
    """
    eid = str(entity_id)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM entities WHERE id = %s", (eid,))
        root_row = cur.fetchone()
        if not root_row:
            return None
        root_row = dict(root_row)

        cur.execute(_TREE_SQL, (eid, max_depth))
        rows = [dict(r) for r in cur.fetchall()]

    # Extension fields for root + all connection entities (single batched call)
    ext_map = fetch_ext(conn, [root_row] + rows)

    root_data = {
        "id": root_row["id"],
        "entity_type": root_row["entity_type"],
        "title": root_row.get("title"),
        "content": root_row["content"],
        "keywords": root_row.get("keywords") or [],
        "importance": root_row["importance"],
        "notes": root_row.get("notes"),
        "ext": ext_map.get(root_row["id"], {}),
    }

    connections = []
    for row in rows:
        rid = row["id"]
        connections.append({
            "entity": {
                "id": rid,
                "entity_type": row["entity_type"],
                "title": row.get("title"),
                "content": row["content"],
                "keywords": row.get("keywords") or [],
                "importance": row["importance"],
                "ext": ext_map.get(rid, {}),
            },
            "depth": row["depth"],
            "relevance": row.get("relevance_score", 1.0) if row.get("relevance_score") is not None else 1.0,
            "via_relation_type": row.get("via_relation_type"),
            "via_description": row.get("via_description"),
            "direction": row.get("direction"),
        })

    # Sort by depth asc, then relevance desc within depth
    connections.sort(key=lambda c: (c["depth"], -c["relevance"]))

    return {"root": root_data, "connections": connections}
