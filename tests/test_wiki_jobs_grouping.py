"""Per-wiki cooldown on attach claims (across-tick batching).

Exercises `braindb.services.wiki_jobs.next_write_bucket()` directly against
the live Postgres instance (port 5433, the docker-compose mapping). Each
test seeds a minimal wiki entity + N wiki_job rows with controlled
`created_at` values, calls `next_write_bucket(conn)`, asserts the result,
and cleans up its rows in `try/finally`.

The cooldown contract under test (see
`braindb/services/wiki_jobs.py::ATTACH_COOLDOWN_SEC`):

  An `attach` bucket is claimable ONLY when the OLDEST pending attach for
  that target_wiki_id is at least ATTACH_COOLDOWN_SEC seconds old. Once
  eligible, the existing per-wiki batching scoops up ALL pending attaches
  for that wiki. `consolidate` and `create` paths are unaffected.
"""
from __future__ import annotations

import uuid
from typing import Iterator

import psycopg2
import pytest

from braindb.services import wiki_jobs


DB_URL = "postgresql://postgres:password@localhost:5433/braindb"

# Tests run against the real database which may already contain pending
# wiki_job rows from the running scheduler. To make our test rows the
# unambiguous winner in FIFO ordering (the seed query orders by created_at
# inside each job_type), we use timestamps far older than any realistic
# production row — 10 days. The cooldown is satisfied (cooldown_seconds
# is 5 min by default; 10 days is much greater) and our row beats anything
# the scheduler may have left pending.
ANCIENT_AGE_SECONDS = 10 * 24 * 3600  # 10 days


# ---------------------------------------------------------------- helpers --


def _insert_test_wiki(conn, label: str) -> str:
    """Insert a minimal wiki entity + its keyword + wikis_ext row. Returns
    the wiki entity UUID as text. The keyword is required because wikis_ext
    expects member_keyword_ids non-empty."""
    wid = uuid.uuid4()
    kw_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO entities (id, entity_type, content, keywords, source, importance)
               VALUES (%s, 'keyword', %s, %s, 'agent-inference', 0.5)""",
            (str(kw_id), f"_pytest_grouping_kw_{label}", [f"_pytest_grouping_{label}"]),
        )
        cur.execute(
            """INSERT INTO entities (id, entity_type, content, keywords, source, importance)
               VALUES (%s, 'wiki', %s, %s, 'agent-inference', 0.5)""",
            (str(wid),
             f"# Test wiki ({label})\n\nPlaceholder body.",
             [f"_pytest_grouping_{label}"]),
        )
        cur.execute(
            """INSERT INTO wikis_ext (entity_id, canonical_name, language, member_keyword_ids, revision)
               VALUES (%s, %s, 'en', %s::uuid[], 1)""",
            (str(wid), f"PytestGrouping_{label}", [str(kw_id)]),
        )
    return str(wid)


def _insert_job(
    conn,
    *,
    job_type: str,
    target_wiki_id: str | None,
    entity_ids: list[str] | None = None,
    age_seconds: int = 0,
    status: str = "pending",
    dedupe_suffix: str | None = None,
) -> str:
    """Insert a wiki_job row with controlled created_at (now() - age_seconds).
    Returns job id as text."""
    jid = uuid.uuid4()
    dedupe = f"_pytest_grouping_{job_type}_{target_wiki_id}_{dedupe_suffix or uuid.uuid4().hex}"
    eids = entity_ids if entity_ids is not None else []
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO wiki_job
               (id, job_type, status, target_wiki_id, entity_ids, dedupe_key,
                created_at, rationale)
               VALUES (%s, %s, %s, %s, %s::uuid[], %s,
                       now() - make_interval(secs => %s),
                       'pytest grouping')""",
            (str(jid), job_type, status, target_wiki_id, eids, dedupe, age_seconds),
        )
    return str(jid)


def _cleanup(conn, *, job_ids: list[str], wiki_ids: list[str]) -> None:
    with conn.cursor() as cur:
        if job_ids:
            cur.execute("DELETE FROM wiki_job WHERE id = ANY(%s::uuid[])", (job_ids,))
        if wiki_ids:
            cur.execute("DELETE FROM entities WHERE id = ANY(%s::uuid[])", (wiki_ids,))
        cur.execute(
            "DELETE FROM entities WHERE entity_type='keyword' "
            "AND content LIKE '_pytest_grouping_kw_%'"
        )


@pytest.fixture
def db() -> Iterator[psycopg2.extensions.connection]:
    """One autocommit psycopg2 connection per test, closed at teardown."""
    c = psycopg2.connect(DB_URL)
    c.autocommit = True
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def cooldown() -> int:
    return wiki_jobs.ATTACH_COOLDOWN_SEC


# ---------------------------------------------------------------- tests --


class TestCoreCooldown:

    def test_fresh_attach_under_cooldown_not_claimed(self, db, cooldown):
        wid = _insert_test_wiki(db, "core_a")
        jid = _insert_job(db, job_type="attach", target_wiki_id=wid, age_seconds=1)
        try:
            bucket = wiki_jobs.next_write_bucket(db)
            if bucket is not None:
                assert bucket.get("target_wiki_id") != wid, (
                    f"fresh attach should NOT be claimable yet; got bucket={bucket!r}"
                )
        finally:
            _cleanup(db, job_ids=[jid], wiki_ids=[wid])

    def test_old_attach_past_cooldown_claimed(self, db, cooldown):
        wid = _insert_test_wiki(db, "core_b")
        # ANCIENT timestamp so our row wins FIFO against any production attach
        jid = _insert_job(
            db, job_type="attach", target_wiki_id=wid,
            age_seconds=ANCIENT_AGE_SECONDS,
        )
        try:
            bucket = wiki_jobs.next_write_bucket(db)
            assert bucket is not None
            assert bucket["mode"] == "attach"
            assert bucket["target_wiki_id"] == wid
            assert len(bucket["jobs"]) == 1
            assert bucket["jobs"][0]["id"] == jid
        finally:
            _cleanup(db, job_ids=[jid], wiki_ids=[wid])


class TestBatchingSemantics:
    """The actual point of the change: when one attach becomes eligible, the
    bucket scoops up the WHOLE pending queue for that wiki."""

    def test_multiple_attaches_batched_when_oldest_past_cooldown(self, db, cooldown):
        wid = _insert_test_wiki(db, "batch_a")
        # The "old" row uses ANCIENT timestamp so it wins FIFO against
        # production rows; the "fresh" rows are recent (their own age <
        # cooldown). Once `old` is eligible, the bucket should scoop them
        # ALL up because they share target_wiki_id.
        old = _insert_job(db, job_type="attach", target_wiki_id=wid,
                          age_seconds=ANCIENT_AGE_SECONDS, dedupe_suffix="0")
        fresh = [
            _insert_job(db, job_type="attach", target_wiki_id=wid,
                        age_seconds=10, dedupe_suffix=str(i))
            for i in range(1, 5)
        ]
        try:
            bucket = wiki_jobs.next_write_bucket(db)
            assert bucket is not None
            assert bucket["target_wiki_id"] == wid
            ids_in_bucket = {j["id"] for j in bucket["jobs"]}
            assert old in ids_in_bucket
            for fid in fresh:
                assert fid in ids_in_bucket, (
                    f"once the bucket is eligible, all 5 attaches for this wiki "
                    f"should batch — fresh job {fid} missing from bucket"
                )
            assert len(bucket["jobs"]) == 5
        finally:
            _cleanup(db, job_ids=[old, *fresh], wiki_ids=[wid])

    def test_multiple_wikis_only_eligible_one_claimed(self, db, cooldown):
        wid_a = _insert_test_wiki(db, "ma_a")  # fresh
        wid_b = _insert_test_wiki(db, "ma_b")  # past cooldown (ANCIENT)
        ja = _insert_job(db, job_type="attach", target_wiki_id=wid_a, age_seconds=10)
        jb = _insert_job(db, job_type="attach", target_wiki_id=wid_b,
                          age_seconds=ANCIENT_AGE_SECONDS)
        try:
            bucket = wiki_jobs.next_write_bucket(db)
            assert bucket is not None
            assert bucket["target_wiki_id"] == wid_b
            assert {j["id"] for j in bucket["jobs"]} == {jb}
        finally:
            _cleanup(db, job_ids=[ja, jb], wiki_ids=[wid_a, wid_b])

    def test_fifo_within_eligible_wikis(self, db, cooldown):
        """Both wikis past cooldown → older oldest-attach wins FIFO.
        Both rows are ANCIENT (older than any production row); wiki_old is
        even older so it beats wiki_new in created_at order."""
        wid_old = _insert_test_wiki(db, "fifo_old")
        wid_new = _insert_test_wiki(db, "fifo_new")
        jold = _insert_job(db, job_type="attach", target_wiki_id=wid_old,
                            age_seconds=ANCIENT_AGE_SECONDS + 300)
        jnew = _insert_job(db, job_type="attach", target_wiki_id=wid_new,
                            age_seconds=ANCIENT_AGE_SECONDS)
        try:
            bucket = wiki_jobs.next_write_bucket(db)
            assert bucket is not None
            assert bucket["target_wiki_id"] == wid_old
        finally:
            _cleanup(db, job_ids=[jold, jnew], wiki_ids=[wid_old, wid_new])


class TestPriorityPreservation:
    """Cooldown is attach-only; consolidate and create are unaffected."""

    def test_consolidate_drains_before_fresh_attaches(self, db):
        wid_a = _insert_test_wiki(db, "prio_ca")
        wid_b = _insert_test_wiki(db, "prio_cb")
        ja = _insert_job(db, job_type="attach", target_wiki_id=wid_a, age_seconds=10)
        jc = _insert_job(
            db, job_type="consolidate", target_wiki_id=None,
            entity_ids=[wid_a, wid_b], age_seconds=0,
        )
        try:
            bucket = wiki_jobs.next_write_bucket(db)
            assert bucket is not None
            assert bucket["mode"] == "consolidate"
            assert bucket["jobs"][0]["id"] == jc
        finally:
            _cleanup(db, job_ids=[ja, jc], wiki_ids=[wid_a, wid_b])

    def test_consolidate_drains_before_eligible_attaches(self, db, cooldown):
        """The cooldown does NOT alter the consolidate > attach hierarchy.
        Attach is ANCIENT (eligible); consolidate is recent — consolidate
        still wins by priority, not by created_at."""
        wid_a = _insert_test_wiki(db, "prio_ea")
        wid_b = _insert_test_wiki(db, "prio_eb")
        ja = _insert_job(db, job_type="attach", target_wiki_id=wid_a,
                          age_seconds=ANCIENT_AGE_SECONDS)
        jc = _insert_job(
            db, job_type="consolidate", target_wiki_id=None,
            entity_ids=[wid_a, wid_b], age_seconds=ANCIENT_AGE_SECONDS + 60,
        )
        try:
            bucket = wiki_jobs.next_write_bucket(db)
            assert bucket is not None
            assert bucket["mode"] == "consolidate"
        finally:
            _cleanup(db, job_ids=[ja, jc], wiki_ids=[wid_a, wid_b])

    # Note on `create` jobs: by SQL inspection of next_write_bucket() the
    # cooldown filter is gated `job_type <> 'attach' OR ...`, so create jobs
    # bypass it entirely. An end-to-end test that asserts a fresh create is
    # claimed FIRST is not reliable against a live DB with any pending
    # higher-priority jobs (consolidate/attach), and forcibly draining
    # production jobs is out of scope. The SQL itself is the proof; the
    # other tests above transitively confirm non-attach paths are unaffected.


class TestEdgeCases:

    def test_assigned_jobs_excluded_from_cooldown_calc(self, db, cooldown):
        """An `assigned` attach for the same wiki does NOT count toward the
        cooldown's MIN(created_at). Only `pending` rows do."""
        wid = _insert_test_wiki(db, "edge_assigned")
        j_assigned = _insert_job(
            db, job_type="attach", target_wiki_id=wid,
            age_seconds=cooldown + 600,
            status="assigned",
        )
        j_pending = _insert_job(
            db, job_type="attach", target_wiki_id=wid,
            age_seconds=10,
        )
        try:
            bucket = wiki_jobs.next_write_bucket(db)
            if bucket is not None:
                assert bucket.get("target_wiki_id") != wid, (
                    f"fresh pending should NOT be claimable — assigned doesn't "
                    f"count toward cooldown MIN. Got {bucket!r}"
                )
        finally:
            _cleanup(db, job_ids=[j_assigned, j_pending], wiki_ids=[wid])
