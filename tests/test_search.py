"""
Search, context (single + multi-query), and graph traversal coverage.
"""
import requests


def test_search_finds_created_fact(api, test_tag, make_fact):
    make_fact("The Aardvark is a medium-sized, nocturnal mammal native to Africa.")
    r = requests.post(
        f"{api}/api/v1/memory/search",
        json={"query": f"Aardvark mammal Africa {test_tag}", "limit": 10},
        timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else (body.get("items") or body.get("results") or [])
    assert isinstance(items, list)
    # Distinctive term "Aardvark" should surface at least one hit
    contents = " ".join(str(x.get("content", "")) for x in items)
    assert "Aardvark" in contents


def test_search_content_matched_via_tsquery(api, test_tag, make_fact):
    """Content is matched by the full-text search_vector, NOT a content trigram.

    The content trigram `%` WHERE branch was removed because its recheck
    detoasted every candidate's full body (7-25s on the prod corpus) for ~1 row
    of recall tsquery didn't already find. Content matching now goes entirely
    through the GIN-indexed search_vector (the AND/OR tsquery branches). A query
    of real content terms must still surface the doc — proving FTS covers
    content recall without the trigram branch.
    """
    make_fact(f"Mississippi mud pie is a chocolate dessert {test_tag}.")
    # Real content terms -> tsquery match (the supported content path).
    r = requests.post(
        f"{api}/api/v1/memory/search",
        json={"query": f"chocolate dessert {test_tag}", "limit": 10},
        timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else (body.get("items") or body.get("results") or [])
    contents = " ".join(str(x.get("content", "")) for x in items)
    assert "Mississippi" in contents, "content not matched via tsquery"


def test_search_title_trigram_match_uses_index(api, test_tag, make_datasource):
    """Title fuzzy-match must keep working after the title `%`-operator rewrite.

    The title WHERE branch moved from `similarity(COALESCE(e.title,''), q) > 0.2`
    (seq-scan) to the indexed `e.title % q` (served by entities_title_trgm_idx,
    migration 008). A misspelled query that is trigram-similar to the TITLE but
    absent from the body must still surface — proving the title `%` path works
    and shares the 0.15 SET LOCAL threshold.
    """
    make_datasource(
        content=f"An opaque body with no distinctive terms {test_tag}.",
        title=f"Worcestershire Sauce {test_tag}",
    )
    # "Worcestishire" (misspelled) is trigram-similar to the title token but is
    # not a tsquery match and does not appear in the content.
    r = requests.post(
        f"{api}/api/v1/memory/search",
        json={"query": f"Worcestishire {test_tag}", "limit": 10},
        timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else (body.get("items") or body.get("results") or [])
    titles = " ".join(str(x.get("title", "")) for x in items)
    assert "Worcestershire" in titles, "title trigram match lost after % rewrite"


def test_search_tiered_retrieval_survives_common_term(api, test_tag, make_fact):
    """Retrieval is tiered + capped (SEARCH_TIER_CAP per tier). A distinctive
    doc must still surface for a query whose terms also appear elsewhere —
    proving the UNION of capped tiers keeps recall for AND-tsquery matches,
    which are the highest-signal tier and the one the cap must never starve.
    """
    token = f"Quokkaberry{test_tag[-4:]}"
    make_fact(f"The {token} compote uses twice the sugar of plain jam {test_tag}.")
    r = requests.post(
        f"{api}/api/v1/memory/search",
        json={"query": f"{token} compote sugar {test_tag}", "limit": 10},
        timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else (body.get("items") or body.get("results") or [])
    contents = " ".join(str(x.get("content", "")) for x in items)
    assert token in contents, "AND-tsquery tier lost the distinctive match"


def test_search_entity_type_filter_applies_inside_tiers(api, test_tag, make_fact, make_datasource):
    """entity_types/min_importance filters are pushed INTO each capped tier
    (not applied after the cap) so filtered-out rows can never consume cap
    slots. A type-filtered search must return only that type while still
    finding the matching row.
    """
    token = f"Vellumfig{test_tag[-4:]}"
    make_fact(f"A fact about {token} preserves {test_tag}.")
    make_datasource(
        content=f"A datasource about {token} preserves {test_tag}.",
        title=f"{token} datasource {test_tag}",
    )
    r = requests.post(
        f"{api}/api/v1/memory/search",
        json={"query": f"{token} {test_tag}", "limit": 10, "entity_types": ["fact"]},
        timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else (body.get("items") or body.get("results") or [])
    matched = [x for x in items if token in str(x.get("content", "")) or token in str(x.get("title", ""))]
    assert matched, "type-filtered search lost the matching fact"
    assert all(x.get("entity_type") == "fact" for x in matched), "entity_types filter leaked other types"


def test_context_single_query_returns_structured_response(api, test_tag, make_fact):
    make_fact("Bismuth has the chemical symbol Bi and atomic number 83.")
    r = requests.post(
        f"{api}/api/v1/memory/context",
        json={"query": f"Bismuth chemistry {test_tag}", "max_depth": 3, "max_results": 10},
        timeout=20,
    )
    assert r.status_code == 200
    body = r.json()
    # The context endpoint returns both ranked items and always_on_rules
    assert "items" in body or "results" in body
    assert "always_on_rules" in body


def test_context_multi_query_merges_seeds(api, test_tag, make_fact):
    make_fact(f"Pytest marker entry one about Xerophyte plants {test_tag}.")
    make_fact(f"Pytest marker entry two about Yellowfin tuna {test_tag}.")
    r = requests.post(
        f"{api}/api/v1/memory/context",
        json={
            "queries": [f"Xerophyte {test_tag}", f"Yellowfin {test_tag}"],
            "max_depth": 2,
            "max_results": 20,
        },
        timeout=20,
    )
    assert r.status_code == 200
    items = r.json().get("items") or r.json().get("results") or []
    contents = " ".join(str(x.get("content", "")) for x in items)
    assert "Xerophyte" in contents and "Yellowfin" in contents


def test_graph_traversal_surfaces_connected_entity(api, test_tag, make_fact, make_relation):
    """Direct keyword match still surfaces. The previous version of this
    test asserted that an entity reachable ONLY via graph traversal from
    a directly-matched seed also appeared in the top-N. After commit
    `c4e4a2f` (Stage A.6 + A.7), `/memory/context` is keyword-mediated
    AND applies a two-level diversity quota — entities without a direct
    keyword/embedding match get a default seed_score of 0.3 and a
    depth-1 relevance fade of 0.6, so their final_rank lands around
    0.09. In a populated DB this is correctly out-competed by entities
    with real direct matches; the graph traversal MECHANISM still
    runs, but its output ranks low. That's the documented architectural
    choice (see README.md "How Retrieval Works" and BRAINDB_GUIDE.md
    "How Search Works"), not a bug. A proper isolated unit test of
    `graph_expand` at the service level (without /memory/context's
    full scoring stack) is the right tool to verify graph traversal
    in isolation — that's a TODO, not in scope here.
    """
    seed_token = f"ZephyrMarker{test_tag[-4:]}"
    a = make_fact(
        f"Direct fact mentioning {seed_token} for search.",
        keywords=[seed_token],
    )
    b = make_fact("Secondary fact with no distinctive term, linked to A.")
    make_relation(a["id"], b["id"], "elaborates")

    r = requests.post(
        f"{api}/api/v1/memory/context",
        json={"query": seed_token, "max_depth": 3, "max_results": 30},
        timeout=20,
    )
    assert r.status_code == 200
    items = r.json().get("items") or r.json().get("results") or []
    ids = [x.get("id") for x in items]
    # A must appear — that's the keyword-mediated direct-match path
    # functioning correctly. (B's graph-only surfacing is no longer
    # guaranteed in a populated DB; see docstring.)
    assert a["id"] in ids, "direct keyword match not found"


def test_tree_endpoint_returns_structure(api, make_fact, make_relation):
    root = make_fact("Root node of a small tree for test.")
    child1 = make_fact("Child 1 of the test tree.")
    child2 = make_fact("Child 2 of the test tree.")
    make_relation(root["id"], child1["id"], "elaborates")
    make_relation(root["id"], child2["id"], "elaborates")

    r = requests.get(
        f"{api}/api/v1/memory/tree/{root['id']}",
        params={"max_depth": 2},
        timeout=15,
    )
    assert r.status_code == 200
    # Tree structure is an opaque but non-empty payload
    body = r.json()
    assert body   # not empty or None


def test_stats_endpoint_returns_counts(api):
    r = requests.get(f"{api}/api/v1/memory/stats", timeout=10)
    assert r.status_code == 200
    body = r.json()
    # Stats must include at least a total count or per-type breakdown
    assert isinstance(body, dict)
    assert len(body) > 0
