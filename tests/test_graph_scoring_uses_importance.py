"""
Locks in that the recall graph CTE multiplies BOTH per-edge scores
(relevance_score AND importance_score) into the per-hop accumulated
relevance. Before this round, importance_score sat in the column
unused; the LLM's judgment of edge importance didn't affect ranking.

Test approach: drive `graph_expand` directly (the function the recall
pipeline calls). This isolates the behavior we want to lock without
the noise of full-text discovery fallback + diversity-quota filtering
that /memory/context layers on top.
"""
import uuid

import requests
from braindb.db import get_conn
from braindb.services.graph import graph_expand


def test_importance_score_moves_per_hop_relevance(api, make_fact):
    """Two relations from the same seed, identical relation_type and
    relevance_score, ONLY differing in importance_score. The hop's
    accumulated_relevance from graph_expand must reflect the difference."""
    tag = uuid.uuid4().hex
    seed = make_fact(f"Seed for {tag}", keywords=[tag])
    hi_target = make_fact("Generic hi target.")
    lo_target = make_fact("Generic lo target.")

    for target_id, imp in ((hi_target["id"], 0.9), (lo_target["id"], 0.2)):
        body = {
            "from_entity_id": seed["id"],
            "to_entity_id": target_id,
            "relation_type": "elaborates",
            "relevance_score": 0.7,
            "importance_score": imp,
            "description": "Test edge",
        }
        r = requests.post(f"{api}/api/v1/relations", json=body, timeout=30)
        assert r.status_code == 201, r.text

    with get_conn() as conn:
        rows = graph_expand(conn, [seed["id"]], max_depth=1, min_relevance=0.01)

    by_id = {str(r["id"]): r for r in rows}
    assert hi_target["id"] in by_id, "hi_target not reached by graph_expand"
    assert lo_target["id"] in by_id, "lo_target not reached by graph_expand"

    hi_rel = by_id[hi_target["id"]]["relevance"]
    lo_rel = by_id[lo_target["id"]]["relevance"]

    # Both reached via the same one-hop path; only importance_score differs.
    # With the fix in graph.py the per-hop multiplier multiplies by
    # COALESCE(r.importance_score, 0.5), so hi (imp 0.9) > lo (imp 0.2).
    # Per-hop math: 1.0 * 0.7 * imp * depth_penalty(1.0)
    assert hi_rel > lo_rel, (
        f"importance_score does not move per-hop relevance: "
        f"hi={hi_rel:.4f} lo={lo_rel:.4f}"
    )
    # Sanity: ratio hi/lo ~= 0.9 / 0.2 = 4.5 (within rounding).
    ratio = hi_rel / lo_rel
    assert 4.0 < ratio < 5.0, (
        f"per-hop relevance ratio should track importance_score ratio "
        f"(0.9/0.2=4.5); got hi/lo={ratio:.3f}"
    )
