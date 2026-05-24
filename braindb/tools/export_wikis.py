"""
Read-only wiki review export.

Run in the container:
    docker compose exec -T api python -m braindb.tools.export_wikis

Writes one markdown file per wiki to data/wiki_review/ (gitignored) plus an
INDEX.md, so the maintainer/writer output can be read and judged in the IDE.

STRICTLY READ-ONLY: only SELECT queries, never mutates the DB or the pipeline.
Reuses existing data (entities, relations, wiki_job, activity_log) and the
existing ref/section parsers in wiki_jobs (C3 — no new search/scoring).
"""
import json
import re
from pathlib import Path

import psycopg2.extras

from braindb.db import get_conn
from braindb.services.wiki_jobs import parse_refs

OUT_DIR = Path("data/wiki_review")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "wiki").lower()).strip("-")
    return s or "wiki"


def _fetch_all_wikis(conn) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT e.id::text AS id, e.content, e.summary, e.importance,
                      w.canonical_name, w.disambiguation, w.language, w.revision,
                      w.last_synthesised_at, w.retired_at, w.redirect_to::text AS redirect_to,
                      w.member_keyword_ids::text[] AS member_keyword_ids,
                      e.created_at
               FROM entities e JOIN wikis_ext w ON w.entity_id = e.id
               WHERE e.entity_type = 'wiki'
               ORDER BY e.created_at"""
        )
        return [dict(r) for r in cur.fetchall()]


def _summarises_targets(conn, wiki_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_entity_id::text FROM relations "
            "WHERE from_entity_id = %s AND relation_type = 'summarises'",
            (wiki_id,),
        )
        return [r[0] for r in cur.fetchall()]


def _entities(conn, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id::text AS id, entity_type, content FROM entities "
            "WHERE id = ANY(%s::uuid[])",
            (ids,),
        )
        return {r["id"]: dict(r) for r in cur.fetchall()}


def _decisions(conn, wiki_id: str, summarised_ids: list[str]) -> tuple[list[dict], list[dict]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT job_type, status, rationale, proposed_name,
                      entity_ids::text[] AS entity_ids, created_at
               FROM wiki_job
               WHERE target_wiki_id = %s
                  OR (entity_ids && %s::uuid[])
               ORDER BY created_at""",
            (wiki_id, summarised_ids or ["00000000-0000-0000-0000-000000000000"]),
        )
        jobs = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT operation, timestamp, details
               FROM activity_log
               WHERE entity_id = %s
                 AND operation IN ('wiki_write','wiki_revise','wiki_ref_removed','wiki_merge')
               ORDER BY timestamp""",
            (wiki_id,),
        )
        acts = [dict(r) for r in cur.fetchall()]
    return jobs, acts


def _consistency(body: str, summarises: set[str]) -> tuple[bool, list[str]]:
    """Provenance check: every entity the LLM cited inline must have a
    `summarises` relation (reconcile is additive, so that should always hold).
    Lingering relations (LLM dropped a ref but the edge remains, since code
    never deletes behind the LLM) are reported as info, not a failure."""
    inline = parse_refs(body or "")
    msgs: list[str] = []
    missing = sorted(inline - summarises)
    lingering = sorted(summarises - inline)
    if missing:
        msgs.append(f"cited inline but NO summarises relation: {missing}")
    if lingering:
        msgs.append(f"summarises relation but not cited inline (LLM-dropped, "
                    f"edge left for LLM to remove): {lingering}")
    # Pass = no missing relation for a cited ref. Lingering is informational.
    return (not missing), msgs


def _render(conn, w: dict) -> str:
    wid = w["id"]
    summarises = set(_summarises_targets(conn, wid))
    ok, issues = _consistency(w["content"] or "", summarises)
    all_refs = sorted(parse_refs(w["content"] or "") | summarises)
    ents = _entities(conn, all_refs)
    jobs, acts = _decisions(conn, wid, sorted(summarises))

    L = []
    L.append(f"# Wiki review — {w['canonical_name']}")
    L.append("")
    L.append(f"- **id:** `{wid}`")
    L.append(f"- **revision:** {w['revision']}   "
             f"**importance:** {w['importance']}   "
             f"**language:** {w['language']}")
    L.append(f"- **last_synthesised_at:** {w['last_synthesised_at']}")
    L.append(f"- **summary:** {w['summary']}")
    L.append(f"- **disambiguation:** {w['disambiguation']}")
    L.append("")
    L.append(f"## Consistency: {'CONSISTENT ✓' if ok else 'MISMATCH ✗'}")
    L.append(f"inline refs / ledger / summarises-relations "
             f"({len(parse_refs(w['content'] or ''))} body, {len(summarises)} relations)")
    for m in issues:
        L.append(f"- ⚠ {m}")
    L.append("")
    L.append("## Body (verbatim)")
    L.append("")
    L.append("```markdown")
    L.append(w["content"] or "(empty)")
    L.append("```")
    L.append("")
    L.append("## Provenance — cited source entities (judge grounding here)")
    for rid in all_refs:
        e = ents.get(rid)
        if e:
            L.append(f"- **`{rid}`** [{e['entity_type']}]: {e['content']}")
        else:
            L.append(f"- **`{rid}`**: ⚠ ENTITY NOT FOUND (dangling ref)")
    L.append("")
    L.append("## Decisions & history")
    L.append("")
    L.append("### Maintainer suggestion jobs")
    for j in jobs:
        L.append(f"- `{j['job_type']}` [{j['status']}] {j['created_at']:%Y-%m-%d %H:%M} "
                 f"name={j.get('proposed_name')}\n  rationale: {j.get('rationale')}")
    L.append("")
    L.append("### Writer activity")
    for a in acts:
        det = json.dumps(a["details"], default=str, indent=2)
        L.append(f"- **{a['operation']}** {a['timestamp']:%Y-%m-%d %H:%M}")
        L.append(f"```json\n{det}\n```")
    L.append("")
    return "\n".join(L)


def _render_retired(w: dict) -> str:
    return (f"# {w['canonical_name']} — RETIRED\n\n"
            f"- id: `{w['id']}`\n"
            f"- retired_at: {w['retired_at']}\n"
            f"- redirect_to: `{w['redirect_to']}`\n"
            f"- summary: {w['summary']}\n\n"
            f"This wiki was consolidated into its redirect target "
            f"(`duplicate_of` / `consolidated_into` relations record the merge). "
            f"It still resolves via GET /entities/{w['id']} but is dropped from ranking.\n")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        wikis = _fetch_all_wikis(conn)
        index = ["# Wiki review index", "",
                 f"{len(wikis)} wiki entities. Open each file below and judge against the checklist.",
                 "",
                 "| canonical_name | rev | refs | consistency | retired | file |",
                 "|---|---|---|---|---|---|"]
        for w in wikis:
            # id suffix keeps filenames unique (e.g. 'pytest' vs retired 'PyTest')
            slug = _slug(w["canonical_name"])
            fname = f"{slug}-{w['id'][:8]}.md"
            if w["retired_at"]:
                (OUT_DIR / fname).write_text(_render_retired(w), encoding="utf-8")
                index.append(f"| {w['canonical_name']} | {w['revision']} | - | - | YES | {fname} |")
                continue
            summarises = set(_summarises_targets(conn, w["id"]))
            ok, _ = _consistency(w["content"] or "", summarises)
            nrefs = len(parse_refs(w["content"] or ""))
            (OUT_DIR / fname).write_text(_render(conn, w), encoding="utf-8")
            index.append(f"| {w['canonical_name']} | {w['revision']} | {nrefs} | "
                         f"{'✓' if ok else '✗'} | no | {fname} |")

        index += ["",
                  "## Quality checklist (fill while reading each wiki)",
                  "",
                  "- [ ] **Grounded** — every claim traceable to a cited source entity (no hallucination)",
                  "- [ ] **Identity** — no third-party attribute transferred onto the subject; distinct people not fused",
                  "- [ ] **Honest uncertainty** — ambiguous data is represented as such, not fabricated into confidence",
                  "- [ ] **Summary/Disambiguation** — accurate; rewritten (not frozen) when better data exists",
                  "- [ ] **Consistency** — every cited inline ref has a summarises relation (column ✓)",
                  "- [ ] **Maintainer decision sane** — create/attach/skip/ambiguous rationale reasonable",
                  "- [ ] **No keyword-token sources** — cited refs are real fact/thought/source entities",
                  "- [ ] **Contradictions** — opposing sources reconciled or explicitly noted",
                  ""]
        (OUT_DIR / "INDEX.md").write_text("\n".join(index), encoding="utf-8")

    print(f"Exported {len(wikis)} wikis to {OUT_DIR.resolve()} (open INDEX.md)")


if __name__ == "__main__":
    main()
