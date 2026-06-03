"""Shared tree-build service.

`build_entity_tree` walks the relation graph bidirectionally from a root
entity and returns a nested JSON tree. The same function is used by the
HTTP endpoint `/api/v1/memory/tree/<id>` and the agent's `view_tree`
tool — one shape, no behaviour drift.

Shape (root keyed by ``entity_type``):

    {
      "<entity_type>": "<label>",
      "keywords": ["k1", "k2", ...],   # only when root.keywords exist
      "children": [
          {"fact": "...", "children": [...]},
          {"wiki": "..."},
          {"source": "..."},
          {"_truncated": "<N> more — increase max_depth or filter"}
      ]
    }

Rules:
* Per-hop multiplier is the same formula as ``services/graph.py``:
  ``relevance_score × COALESCE(importance_score, 0.5) × depth_penalty``.
* Multi-path entities collapse to a single occurrence under the parent
  whose path scored highest (first-wins by accumulated score).
* ``tagged_with`` edges and ``keyword`` target entities are skipped by
  default (root's ``keywords[]`` column is the one-liner instead).
* Retired wikis (``wikis_ext.retired_at IS NOT NULL``) are skipped.
* ``top_k`` caps the rendered connections; a single ``_truncated`` marker
  is appended to root's ``children`` when anything was dropped.
"""
from __future__ import annotations

import psycopg2.extras


_TREE_SQL = """
WITH RECURSIVE traversal AS (
    SELECT e.id, e.entity_type, e.title, e.content, e.keywords,
           e.importance,
           0 AS depth,
           ARRAY[e.id] AS visited,
           NULL::UUID  AS parent_id,
           1.0::FLOAT  AS accumulated_score
    FROM entities e
    WHERE e.id = %s

    UNION ALL

    SELECT target.id, target.entity_type, target.title, target.content,
           target.keywords, target.importance,
           t.depth + 1,
           t.visited || target.id,
           t.id::UUID,
           (
               t.accumulated_score
               * COALESCE(r.relevance_score, 0.5)
               * COALESCE(r.importance_score, 0.5)
               * CASE t.depth + 1
                   WHEN 1 THEN 1.0
                   WHEN 2 THEN 0.8
                   ELSE        0.6
                 END
           )::FLOAT
    FROM traversal t
    JOIN relations r ON r.from_entity_id = t.id OR r.to_entity_id = t.id
    JOIN entities target ON target.id = CASE
        WHEN r.from_entity_id = t.id THEN r.to_entity_id
        ELSE r.from_entity_id
    END
    LEFT JOIN wikis_ext target_wiki ON target_wiki.entity_id = target.id
    WHERE t.depth < %s
      AND NOT (target.id = ANY(t.visited))
      AND (
            %s::boolean
            OR (r.relation_type <> 'tagged_with' AND target.entity_type <> 'keyword')
          )
      AND (target.entity_type <> 'wiki' OR target_wiki.retired_at IS NULL)
)
SELECT DISTINCT ON (id)
    id, entity_type, title, content, keywords, importance,
    depth, parent_id, accumulated_score
FROM traversal
WHERE depth > 0
  AND accumulated_score >= %s
ORDER BY id, accumulated_score DESC
"""


def _node_label(entity: dict) -> str:
    """Type-aware label for a node in the tree.

    * wikis → ``title`` (canonical_name) when present, else short content
    * facts / thoughts → up to ~50 words / ~300 chars of content
    * sources / datasources → ``title`` if set, else short content
    * anything else → first 80 chars of content
    """
    et = (entity.get("entity_type") or "").lower()
    content = (entity.get("content") or "").replace("\n", " ").strip()
    title = (entity.get("title") or "").strip() or None

    if et == "wiki":
        return title or content[:80]
    if et in ("source", "datasource"):
        return title or content[:120]
    if et in ("fact", "thought"):
        if len(content) <= 300:
            return content
        return content[:297].rstrip() + "..."
    return title or content[:80]


def build_entity_tree(
    conn,
    entity_id: str,
    max_depth: int = 2,
    include_keywords: bool = False,
    top_k: int = 40,
    min_path_score: float = 0.0,
) -> dict | None:
    """Walk the graph bidirectionally from ``entity_id`` and return a
    nested JSON tree. See module docstring for shape and rules.
    Returns ``None`` if the root entity doesn't exist.
    """
    eid = str(entity_id)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM entities WHERE id = %s", (eid,))
        root_row = cur.fetchone()
        if not root_row:
            return None
        root_row = dict(root_row)

        cur.execute(
            _TREE_SQL,
            (eid, max_depth, bool(include_keywords), float(min_path_score)),
        )
        rows = [dict(r) for r in cur.fetchall()]

    rows.sort(key=lambda r: -float(r["accumulated_score"]))
    kept = rows[:top_k]
    dropped = len(rows) - len(kept)

    children_by_parent: dict[str, list[dict]] = {}
    for r in kept:
        children_by_parent.setdefault(str(r["parent_id"]), []).append(r)
    for siblings in children_by_parent.values():
        siblings.sort(key=lambda r: -float(r["accumulated_score"]))

    def _build_node(row: dict) -> dict:
        et = row["entity_type"]
        node: dict = {et: _node_label(row)}
        cid = str(row["id"])
        if cid in children_by_parent:
            node["children"] = [_build_node(c) for c in children_by_parent[cid]]
        return node

    result: dict = {root_row["entity_type"]: _node_label(root_row)}
    if root_row.get("keywords"):
        result["keywords"] = list(root_row["keywords"])

    root_children: list[dict] = []
    if eid in children_by_parent:
        root_children = [_build_node(c) for c in children_by_parent[eid]]
    if dropped > 0:
        root_children.append({
            "_truncated": (
                f"{dropped} more — increase max_depth, "
                f"raise min_path_score, or narrow filter"
            )
        })

    if root_children:
        result["children"] = root_children

    return result
