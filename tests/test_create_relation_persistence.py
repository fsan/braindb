"""
Locks in the importance_score wiring on relations.

For agent-created rows the column was NULL until this round; now both
relevance_score and importance_score must persist whatever the caller
sets, and a fresh GET must return the same values.
"""
import requests


def test_relation_persists_both_scores(api, make_fact):
    a = make_fact("Anchor entity A for persistence check.")
    b = make_fact("Anchor entity B for persistence check.")

    body = {
        "from_entity_id": a["id"],
        "to_entity_id": b["id"],
        "relation_type": "supports",
        "relevance_score": 0.82,
        "importance_score": 0.71,
        "description": "Persistence round-trip",
    }
    r = requests.post(f"{api}/api/v1/relations", json=body, timeout=30)
    assert r.status_code == 201, f"create relation failed: {r.status_code} {r.text}"
    rel = r.json()

    # POST response carries both scores.
    assert rel["relevance_score"] == 0.82
    assert rel["importance_score"] == 0.71

    # And a fresh GET reads them back identically — proves the column is
    # actually written, not just echoed in the response.
    r = requests.get(f"{api}/api/v1/relations/{rel['id']}", timeout=10)
    assert r.status_code == 200
    fresh = r.json()
    assert fresh["relevance_score"] == 0.82
    assert fresh["importance_score"] == 0.71


def test_relation_importance_score_not_null_by_default(api, make_fact):
    """If the caller omits importance_score, it falls back to the schema
    default (0.5) — NOT NULL. This locks the schema/router contract."""
    a = make_fact("A for default-import.")
    b = make_fact("B for default-import.")

    body = {
        "from_entity_id": a["id"],
        "to_entity_id": b["id"],
        "relation_type": "supports",
    }
    r = requests.post(f"{api}/api/v1/relations", json=body, timeout=30)
    assert r.status_code == 201
    rel = r.json()

    assert rel["importance_score"] is not None
    # Default is the schema's neutral 0.5.
    assert rel["importance_score"] == 0.5
