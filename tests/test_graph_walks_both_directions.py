"""
Locks in that the graph CTE walks relations in BOTH directions
regardless of the `is_bidirectional` flag.

Before this round, the CTE only walked `from -> to` unless
`is_bidirectional=true` was set on the edge. That diverged from
`services/tree.py` (which always walks both ways) and meant recall
seeded from the `to_entity_id` could not reach the `from_entity_id`
through a unidirectional edge — the bloat the user explicitly flagged.

Test approach: drive `graph_expand` directly. Avoids the noise of the
full recall pipeline's discovery fallback that complicates HTTP-based
tests with arbitrary DB state.
"""
import uuid

import requests
from braindb.db import get_conn
from braindb.services.graph import graph_expand


def test_graph_expand_walks_unidirectional_edge_backwards(api, make_fact):
    tag = uuid.uuid4().hex

    # A is the "from" side. B is the "to" side and is the SEED.
    # Edge: A -> B with is_bidirectional=false (default).
    # graph_expand seeded from [B] must still reach A by walking the
    # edge backwards.
    a = make_fact(f"Anchor A for {tag}")
    b = make_fact(f"Anchor B for {tag}", keywords=[tag])

    body = {
        "from_entity_id": a["id"],
        "to_entity_id": b["id"],
        "relation_type": "supports",
        "relevance_score": 0.8,
        "importance_score": 0.6,
        "is_bidirectional": False,
        "description": "Unidirectional A -> B for bidirectional-walk test",
    }
    r = requests.post(f"{api}/api/v1/relations", json=body, timeout=30)
    assert r.status_code == 201, r.text

    with get_conn() as conn:
        rows = graph_expand(conn, [b["id"]], max_depth=1, min_relevance=0.05)

    by_id = {str(r["id"]): r for r in rows}
    assert b["id"] in by_id, "B (the seed) is missing from graph_expand result"
    assert a["id"] in by_id, (
        "A (the from-side of a unidirectional edge) was NOT reached by "
        "graph_expand seeded from B. The CTE is not walking edges backwards."
    )
    a_row = by_id[a["id"]]
    assert a_row["min_depth"] == 1, (
        f"A should be reached at depth=1, got depth={a_row['min_depth']}"
    )
    assert a_row["via_relation_type"] == "supports"
