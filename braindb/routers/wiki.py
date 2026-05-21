"""
Wiki pipeline endpoints: cron / maintain / write / jobs.

Stage 1 is manual (no scheduler) — these endpoints are driven by hand or by
the Stage-2 `wiki_scheduler` sidecar. `/cron` and `/jobs` are pure SQL and
non-destructive; `/maintain` and `/write` (later steps) drive the existing
agent endpoint.
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Query

from braindb.agent.agent import run_typed, get_maintainer_agent, get_writer_agent
from braindb.agent.schemas import MaintainerDecision, WikiWriteResult
from braindb.db import get_conn
from braindb.services.activity_log import log_activity
from braindb.services import wiki_jobs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/wiki", tags=["wiki"])

_PROMPTS = Path(__file__).parent.parent / "agent" / "prompts"
_MAINTAINER_PROMPT = (_PROMPTS / "wiki_maintainer_prompt.md").read_text(encoding="utf-8")
_WRITER_PROMPT = (_PROMPTS / "wiki_writer_prompt.md").read_text(encoding="utf-8")


@router.post("/cron")
def wiki_cron():
    """Read-only orphan scan; enqueues one `triage` job per orphan. Idempotent."""
    with get_conn() as conn:
        result = wiki_jobs.run_cron(conn)
        log_activity(conn, "wiki_cron", None, None, details=result)
        return result


@router.post("/maintain")
async def wiki_maintain():
    """
    Process EXACTLY ONE triage case (C1). Claims one pending triage job,
    asks the existing agent to decide attach/create/consolidate/skip for that
    single orphan, persists the resulting suggestion job, closes the triage.
    """
    # 1. Claim one case + staleness guard, atomically (one transaction).
    with get_conn() as conn:
        job = wiki_jobs.claim_one_triage(conn)
        if not job:
            return {"claimed": 0, "message": "no pending triage jobs"}
        orphan_id = job["entity_ids"][0]
        job_id = str(job["id"])
        batch_id = str(job["batch_id"]) if job["batch_id"] else None
        orphan = wiki_jobs.fetch_entity_brief(conn, orphan_id)

        if not orphan:
            wiki_jobs.finish_job(conn, job_id, "failed", "orphan entity not found")
            return {"claimed": 1, "job_id": job_id, "result": "failed",
                    "reason": "orphan missing"}

        # Stale-skip: a prior writer run may have already absorbed/linked this
        # entity (or it's already in an active suggestion). If so, close the
        # triage with NO LLM call — the writer's broad research retired it.
        if not wiki_jobs.is_orphan(conn, orphan_id, exclude_triage_job_id=job_id):
            wiki_jobs.finish_job(conn, job_id, "done",
                                 "already covered — absorbed by a wiki")
            return {"claimed": 1, "job_id": job_id, "result": "skipped_stale"}

        # Catalog of existing wikis the model will reference BY NUMBER (never
        # by uuid). This in-request list IS the numbering used to resolve the
        # model's chosen number(s) back to ids below.
        cat = wiki_jobs.list_active_wikis(conn)

    # 2. One agent call. The prompt directs it to RESEARCH the neighbourhood
    #    with its own tools (recall_memory / view_tree / delegate_to_subagent)
    #    before deciding — we give the seed, the LLM gathers the context.
    #    Generous turns so it can actually investigate / delegate.
    catalog_txt = (
        "\n".join(f"{i}. {w['canonical_name']}" for i, w in enumerate(cat, 1))
        or "(no existing wikis yet — attach/consolidate are impossible; "
           "use create/skip/ambiguous)"
    )
    prompt = _MAINTAINER_PROMPT.format(
        entity_id=orphan_id,
        entity_type=orphan["entity_type"],
        keywords=orphan.get("keywords") or [],
        summary=orphan.get("summary"),
        content=(orphan.get("content") or "")[:4000],
        wiki_catalog=catalog_txt,
    )
    # `run_typed` returns a SDK-validated MaintainerDecision, or raises if
    # the model never submitted (e.g. max_turns hit) — that error path
    # below treats it like any other agent failure (release + log + 5xx).
    try:
        res: MaintainerDecision = await run_typed(
            prompt, get_maintainer_agent(), MaintainerDecision, max_turns=30
        )
    except Exception as e:
        logger.exception("maintainer agent failed")
        with get_conn() as conn:
            wiki_jobs.finish_job(conn, job_id, "failed", f"agent error: {e}"[:500])
        return {"claimed": 1, "job_id": job_id, "result": "failed", "reason": str(e)}

    # Schema-validated; expose as a dict so the action handlers below are
    # unchanged.
    decision = res.model_dump()
    action = decision.get("action")
    rationale = decision.get("rationale")

    # 3. Persist the suggestion + close the triage, in one transaction.
    with get_conn() as conn:
        try:
            if action in ("skip", "ambiguous"):
                # 'ambiguous' = the data cannot disambiguate identity/scope;
                # the LLM correctly refuses to mint a confident page. Treated
                # as a deliberate skip (self-clears via run_cron).
                wiki_jobs.finish_job(conn, job_id, "rejected", rationale)
                outcome = {"action": action}

            elif action == "attach":
                no = decision.get("target_wiki_no")
                target = (cat[no - 1]["id"]
                          if isinstance(no, int) and 1 <= no <= len(cat)
                          else None)
                if not target or not _is_wiki(conn, target):
                    wiki_jobs.finish_job(
                        conn, job_id, "failed",
                        f"attach: target_wiki_no {no!r} not a valid catalog number (1..{len(cat)})")
                    outcome = {"action": "attach", "error": "invalid target_wiki_no"}
                else:
                    key = wiki_jobs.suggestion_dedupe_key("attach", target, [orphan_id], [])
                    sid = wiki_jobs.insert_suggestion(
                        conn, job_type="attach", target_wiki_id=target,
                        entity_ids=[orphan_id], dedupe_key=key, rationale=rationale,
                        proposed_name=None, batch_id=batch_id)
                    wiki_jobs.finish_job(conn, job_id, "done", rationale)
                    outcome = {"action": "attach", "suggestion_id": sid, "target_wiki_id": target}

            elif action == "create":
                name = decision.get("proposed_name")
                if not name:
                    wiki_jobs.finish_job(conn, job_id, "failed", "create missing proposed_name")
                    outcome = {"action": "create", "error": "missing proposed_name"}
                else:
                    key = wiki_jobs.suggestion_dedupe_key("create", None, [orphan_id], [])
                    sid = wiki_jobs.insert_suggestion(
                        conn, job_type="create", target_wiki_id=None,
                        entity_ids=[orphan_id], dedupe_key=key, rationale=rationale,
                        proposed_name=name, batch_id=batch_id)
                    wiki_jobs.finish_job(conn, job_id, "done", rationale)
                    outcome = {"action": "create", "suggestion_id": sid, "proposed_name": name}

            elif action == "consolidate":
                nos = decision.get("consolidate_nos") or []
                ids = [cat[n - 1]["id"] for n in nos
                       if isinstance(n, int) and 1 <= n <= len(cat)]
                wiki_ids = list(dict.fromkeys(ids))  # dedupe, keep order
                if len(wiki_ids) < 2:
                    wiki_jobs.finish_job(
                        conn, job_id, "failed",
                        f"consolidate: need >=2 valid catalog numbers, got {nos!r} (1..{len(cat)})")
                    outcome = {"action": "consolidate", "error": "need >=2 valid catalog numbers"}
                else:
                    key = wiki_jobs.suggestion_dedupe_key("consolidate", None, [], wiki_ids)
                    sid = wiki_jobs.insert_suggestion(
                        conn, job_type="consolidate", target_wiki_id=None,
                        entity_ids=wiki_ids, dedupe_key=key, rationale=rationale,
                        proposed_name=None, batch_id=batch_id)
                    # The orphan itself is still unconnected; closing 'done'
                    # lets the next cron re-triage it after the merge.
                    wiki_jobs.finish_job(conn, job_id, "done", rationale)
                    outcome = {"action": "consolidate", "suggestion_id": sid, "wiki_ids": wiki_ids}

            else:
                wiki_jobs.finish_job(conn, job_id, "failed", f"unknown action {action!r}")
                outcome = {"action": action, "error": "unknown action"}

            log_activity(conn, "wiki_maintain", orphan["entity_type"], orphan_id,
                         details={"job_id": job_id, **outcome})
        except Exception as e:
            logger.exception("maintainer persistence failed")
            raise

    return {"claimed": 1, "job_id": job_id, "result": outcome}


def _members_block(members: list[dict]) -> str:
    if not members:
        return "(none)"
    out = []
    for m in members:
        out.append(
            f"- id: {m['id']}\n  type: {m['entity_type']}\n"
            f"  keywords: {m.get('keywords') or []}\n"
            f"  content: {(m.get('content') or '')[:1200]}"
        )
    return "\n".join(out)


@router.post("/write")
async def wiki_write():
    """
    Write/update ONE wiki (one target per call). The LLM authors the entire
    body and may freely revise summary/disambiguation/scope/any section. No
    content gate, no manifest, no code-built ledger. The only guarantees are
    process/bookkeeping: the prior version is snapshotted (reversible) and
    `summarises` relations are reconciled *additively* from the LLM's inline
    refs. The LLM researches with its own tools before writing.
    """
    # 1. Pick + claim a bucket.
    with get_conn() as conn:
        bucket = wiki_jobs.next_write_bucket(conn)
        if not bucket:
            return {"written": 0, "message": "no pending create/attach jobs"}
        mode = bucket["mode"]
        jobs = bucket["jobs"]
        job_ids = [str(j["id"]) for j in jobs]
        lock_key = bucket["target_wiki_id"] or f"create:{job_ids[0]}"
        if not wiki_jobs.try_wiki_lock(conn, lock_key):
            return {"written": 0, "message": "target locked by another writer; retry later"}
        claimed = wiki_jobs.claim_jobs(conn, job_ids)
        if not claimed:
            return {"written": 0, "message": "jobs no longer claimable"}

        member_ids: list[str] = []
        for j in jobs:
            member_ids.extend(j["entity_ids"])
        dupes: list[dict] = []
        if mode == "attach":
            members = wiki_jobs.fetch_members(conn, member_ids)
            wiki = wiki_jobs.fetch_wiki(conn, bucket["target_wiki_id"])
            if not wiki:
                wiki_jobs.finish_jobs(conn, job_ids, "failed", "target wiki missing")
                return {"written": 0, "result": "failed", "reason": "target wiki missing"}
            canonical = wiki["canonical_name"]
            old_body = wiki["content"] or ""
        elif mode == "consolidate":
            members = []
            dupes = wiki_jobs.fetch_wikis_for_merge(conn, bucket["wiki_ids"])
            if len(dupes) < 2:
                wiki_jobs.finish_jobs(conn, job_ids, "failed",
                                      "fewer than 2 live wikis to consolidate")
                return {"written": 0, "result": "failed", "reason": "nothing to merge"}
            canonical = "(decide among duplicates)"
            wiki = None
            old_body = "\n\n".join(d["content"] or "" for d in dupes)
        else:  # create
            members = wiki_jobs.fetch_members(conn, member_ids)
            canonical = bucket["proposed_name"] or "Untitled"
            wiki = None
            old_body = ""
        batch_id = str(jobs[0].get("batch_id")) if jobs[0].get("batch_id") else None

    def _dupes_block(ds: list[dict]) -> str:
        if not ds:
            return "(n/a)"
        # Numbered; the writer picks the survivor by NUMBER (canonical_no),
        # never by id. This order IS the numbering resolved below.
        return "\n".join(
            f"{i}. {d['canonical_name']} "
            f"(importance: {d['importance']}  revision: {d['revision']})\n"
            f"  body:\n{(d['content'] or '')[:3000]}"
            for i, d in enumerate(ds, 1)
        )

    # 2. One focused agent call.
    prompt = (
        _WRITER_PROMPT
        .replace("%%MODE%%", mode)
        .replace("%%CANONICAL%%", canonical)
        .replace("%%WIKI_ID%%", bucket["target_wiki_id"] or "(assigned after write)")
        .replace("%%MEMBERS%%", _members_block(members))
        .replace("%%CURRENT_BODY%%", old_body or "(none — create mode)")
        .replace("%%DUPLICATES%%", _dupes_block(dupes))
    )
    # Capture pre-run revision on the target wiki for `attach` mode so we
    # can detect whether the writer used the section-edit tools (each
    # bumps `wikis_ext.revision` directly). The writer may then submit an
    # empty `body` — section edits are the authoritative persistence
    # path in that case. `create`/`consolidate` modes don't have a
    # pre-determined target, so empty body is rejected there.
    pre_revision: int | None = None
    if mode == "attach" and bucket.get("target_wiki_id"):
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT revision FROM wikis_ext WHERE entity_id = %s::uuid",
                    (bucket["target_wiki_id"],),
                )
                row = cur.fetchone()
                if row:
                    pre_revision = row[0]

    # Generous turns so the writer can recall_memory / view_tree / delegate a
    # subagent to research and verify before writing.
    # `run_typed` returns a SDK-validated WikiWriteResult, or raises if the
    # model never submitted — handled below like any agent failure
    # (release + log + 5xx). The only extra guard is "non-empty body OR
    # section edits happened"; everything else is the model's job (and
    # validated by Pydantic).
    try:
        res: WikiWriteResult = await run_typed(
            prompt, get_writer_agent(), WikiWriteResult, max_turns=30
        )
    except Exception as e:
        logger.exception("writer agent failed")
        with get_conn() as conn:
            disp = wiki_jobs.release_or_fail_jobs(conn, job_ids, f"agent error: {e}")
        return {"written": 0, "result": disp, "reason": str(e)}

    used_section_edits = False
    if not (res.body or "").strip():
        # Empty body — only valid in attach mode if section edits bumped
        # the revision during the run. Otherwise the agent did nothing
        # persistable and we fail the jobs.
        if mode != "attach" or pre_revision is None:
            with get_conn() as conn:
                disp = wiki_jobs.release_or_fail_jobs(
                    conn, job_ids,
                    f"empty body returned in {mode} mode: "
                    f"{res.model_dump_json()[:300]}",
                )
            return {"written": 0, "result": disp, "reason": "no body returned"}
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT e.content, w.revision
                       FROM entities e JOIN wikis_ext w ON w.entity_id = e.id
                       WHERE e.id = %s::uuid""",
                    (bucket["target_wiki_id"],),
                )
                row = cur.fetchone()
        if not row or row[1] == pre_revision:
            with get_conn() as conn:
                disp = wiki_jobs.release_or_fail_jobs(
                    conn, job_ids,
                    "empty body AND no section edits — agent did nothing",
                )
            return {"written": 0, "result": disp, "reason": "no edits"}
        new_body = row[0]
        used_section_edits = True
        logger.info(
            "writer used section-edit path: pre_rev=%s post_rev=%s body=%dch",
            pre_revision, row[1], len(new_body),
        )
    else:
        new_body = res.body

    # 3. Persist (one transaction). No content gate — the LLM's body is
    #    authoritative; we only snapshot (reversible) and reconcile additively.
    with get_conn() as conn:
        summary, disambig = wiki_jobs.extract_summary_disambig(new_body)
        kw = wiki_jobs.keywords_from_meta(new_body)
        retired: list[str] = []
        if mode == "create":
            wiki_id = wiki_jobs.create_wiki_entity(
                conn, canonical, new_body, summary, disambig, member_ids,
                keywords=kw)
            revision = 1
        elif mode == "consolidate":
            no = res.canonical_no
            if not (isinstance(no, int) and 1 <= no <= len(dupes)):
                disp = wiki_jobs.release_or_fail_jobs(
                    conn, job_ids,
                    f"canonical_no {no!r} not a valid duplicates number (1..{len(dupes)})")
                return {"written": 0, "result": disp,
                        "reason": "invalid canonical_no"}
            canonical_id = dupes[no - 1]["id"]
            wiki_id = canonical_id
            for d in dupes:
                wiki_jobs.snapshot_revision(
                    conn, d["id"], d["content"] or "",
                    wiki_jobs.parse_refs(d["content"] or ""), d["revision"])
            revision = wiki_jobs.finalize_wiki_write(
                conn, wiki_id, new_body, summary, disambig, member_ids)
            for d in dupes:
                if d["id"] != canonical_id:
                    wiki_jobs.soft_retire_wiki(conn, d["id"], canonical_id, None)
                    retired.append(d["id"])
        else:  # attach
            wiki_id = bucket["target_wiki_id"]
            wiki_jobs.snapshot_revision(
                conn, wiki_id, old_body, wiki_jobs.parse_refs(old_body),
                wiki["revision"])
            revision = wiki_jobs.finalize_wiki_write(
                conn, wiki_id, new_body, summary, disambig, member_ids)

        rel = wiki_jobs.reconcile_summarises_additive(conn, wiki_id, new_body)

        wiki_jobs.finish_jobs(conn, job_ids, "done")
        log_activity(conn, "wiki_write", "wiki", wiki_id, details={
            "mode": mode, "revision": revision, "jobs": job_ids,
            "members": len(member_ids), "retired": retired, **rel,
        })

    return {"written": 1, "wiki_id": wiki_id, "mode": mode,
            "revision": revision, "jobs": job_ids, "retired": retired, **rel}


def _is_wiki(conn, entity_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM entities WHERE id = %s AND entity_type = 'wiki'", (str(entity_id),))
        return cur.fetchone() is not None


@router.get("/jobs")
def wiki_jobs_list(
    status: str | None = Query(default=None),
    job_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
):
    with get_conn() as conn:
        return wiki_jobs.list_jobs(conn, status, job_type, limit)
