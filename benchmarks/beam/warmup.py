"""Warmup barrier — wait for BrainDB's async pipeline to settle.

After a conversation .md file is dropped in ``data_bench/sources/beam/``,
BEFORE the runner asks any of that conversation's questions, this module
blocks until BrainDB has finished thinking:

* the watcher has consumed the file (sources/ is empty)
* no new entities have been created in the last ``settle_seconds`` (extraction done)
* the wiki_job queue is fully drained (status NOT IN pending/assigned)

These three conditions must hold for ``consecutive_clear_required``
consecutive polls before the barrier returns, to avoid flapping on a
single quiet poll that happened to fall between events.

Asking questions before the barrier returns would evaluate a half-formed
memory state and produce a number that does not represent BrainDB's true
performance. This is the standard two-phase eval pattern.

CLI usage:

    python -m benchmarks.beam.warmup
    python -m benchmarks.beam.warmup --timeout 600 --settle-seconds 60
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import psycopg2

from benchmarks.beam.config import (
    BENCH_DATABASE_URL,
    DATA_BENCH_SOURCES,
    assert_bench_database_url,
)


def _query_one(conn, sql: str):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()


def _entity_count(conn) -> int:
    row = _query_one(conn, "SELECT COUNT(*) FROM entities")
    return int(row[0]) if row else 0


def _seconds_since_last_activity(conn) -> float | None:
    """Seconds since the last INSERT in EITHER the entities OR the relations
    table. Tracking both is necessary because the per-chunk extraction agent
    (after the big-chunks change) does its save_fact() bursts up front, then
    spends 2-4 minutes calling create_relation() for cross-fact edges and
    optionally delegate_to_subagent() / recall_memory(). During that tail
    the entities table is quiet but the agent IS still working. If warmup
    only watched entities, it would falsely declare "extraction done" while
    chunk 1's agent is still doing its relation work and chunks 2..N have
    not started.

    GREATEST in Postgres ignores NULL arguments, so an empty relations
    table (early in the run) falls back gracefully to the entities max.
    Both NULL (truly empty DB) → returns NULL → caller treats as "no data
    yet".
    """
    row = _query_one(
        conn,
        """SELECT EXTRACT(EPOCH FROM (NOW() - GREATEST(
            (SELECT MAX(created_at) FROM entities),
            (SELECT MAX(created_at) FROM relations)
        )))""",
    )
    return float(row[0]) if row and row[0] is not None else None


def _pending_wiki_jobs(conn) -> int:
    row = _query_one(
        conn,
        "SELECT COUNT(*) FROM wiki_job WHERE status IN ('pending','assigned')",
    )
    return int(row[0]) if row else 0


def _unprocessed_files() -> list[Path]:
    if not DATA_BENCH_SOURCES.exists():
        return []
    return sorted(p for p in DATA_BENCH_SOURCES.glob("*.md") if p.is_file())


def wait_for_warmup(
    *,
    settle_seconds: float = 600.0,
    consecutive_clear_required: int = 2,
    poll_interval: float = 5.0,
    timeout_seconds: float = 43200.0,
    log_interval: float = 10.0,
    verbose: bool = True,
    block_on_wiki_queue: bool = False,
    database_url: str | None = None,
) -> dict:
    """Block until ingest is done (and optionally wiki pipeline drained).

    By default (``block_on_wiki_queue=False``) the barrier waits only for
    extraction to settle (no new entity INSERTs for ``settle_seconds`` AND
    no files remaining in ``data_bench/sources/``). Wiki processing runs
    asynchronously in the background while Phase C asks questions; that is
    correct behaviour because recall queries on existing entities don't
    need the wiki queue to be empty.

    Set ``block_on_wiki_queue=True`` (CLI ``--wait-for-wikis``) for the old
    strict behaviour that also waits for ``wiki_job`` to be fully drained.
    That convention was a bench-side mistake that turned the wiki layer
    into a single-point-of-failure for large documents where the maintainer
    queues candidates faster than the writer drains them.

    Returns a small stats dict (wall_clock_s, entities, relations, wikis).
    Raises TimeoutError if convergence does not happen within timeout.
    """
    url = database_url or BENCH_DATABASE_URL
    assert_bench_database_url(url)

    conn = psycopg2.connect(url)
    conn.autocommit = True

    start = time.monotonic()
    deadline = start + timeout_seconds
    consecutive_clear = 0
    last_log = -log_interval  # force first iteration to log

    try:
        while time.monotonic() < deadline:
            files_remaining = len(_unprocessed_files())
            activity_age = _seconds_since_last_activity(conn)
            pending = _pending_wiki_jobs(conn)
            entity_count = _entity_count(conn)

            if activity_age is None:
                # No entities or relations yet. If files are still in
                # sources/, watcher hasn't started; if not, it may have
                # just finished — give it a tiny grace period before
                # declaring "no work to do".
                clear = files_remaining == 0 and (time.monotonic() - start) > 10
            else:
                clear = (
                    files_remaining == 0
                    and activity_age >= settle_seconds
                    and (not block_on_wiki_queue or pending == 0)
                )

            elapsed = time.monotonic() - start
            if verbose and (elapsed - last_log >= log_interval):
                age_str = f"{activity_age:.0f}s" if activity_age is not None else "n/a"
                print(
                    f"[warmup t={elapsed:6.0f}s] files_left={files_remaining} "
                    f"entities={entity_count} last_entity_age={age_str} "
                    f"pending_wiki={pending} clear={clear} "
                    f"({consecutive_clear}/{consecutive_clear_required})",
                    flush=True,
                )
                last_log = elapsed

            if clear:
                consecutive_clear += 1
                if consecutive_clear >= consecutive_clear_required:
                    return _final_stats(conn, start)
            else:
                consecutive_clear = 0

            time.sleep(poll_interval)

        raise TimeoutError(
            f"warmup did not converge within {timeout_seconds:.0f}s "
            f"(files_left={len(_unprocessed_files())}, "
            f"last_activity_age={_seconds_since_last_activity(conn)}, "
            f"pending_wiki={_pending_wiki_jobs(conn)})"
        )
    finally:
        conn.close()


def _final_stats(conn, start: float) -> dict:
    return {
        "wall_clock_s": round(time.monotonic() - start, 1),
        "entities": _entity_count(conn),
        "relations": int(_query_one(conn, "SELECT COUNT(*) FROM relations")[0]),
        "wikis": int(
            _query_one(conn, "SELECT COUNT(*) FROM entities WHERE entity_type='wiki'")[0]
        ),
        "wiki_jobs_done": int(
            _query_one(conn, "SELECT COUNT(*) FROM wiki_job WHERE status='done'")[0]
        ),
    }


def _cli() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--timeout", type=float, default=43200,
                   help="seconds before warmup gives up on convergence (default 43200 = 12h, "
                        "covers a ~6h 100K-extraction with 2x slack; settle_seconds catches "
                        "genuine stalls inside this window).")
    p.add_argument("--settle-seconds", type=float, default=600,
                   help="seconds of no INSERT on entities OR relations before declaring "
                        "extraction settled (default 600). Per-chunk extraction agents do "
                        "save_fact bursts up front, then 2-4 minutes of create_relation + "
                        "subagent / recall_memory work; tracking both tables prevents the "
                        "barrier from falsely converging during a chunk's relation tail. "
                        "10 min gives comfortable slack over the worst observed quiet "
                        "stretch (~3 min) plus chunk-transition gaps.")
    p.add_argument("--poll-interval", type=float, default=5)
    p.add_argument("--consecutive-clear", type=int, default=2)
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--wait-for-wikis", action="store_true",
                   help="Strict mode: also require wiki_job queue empty before "
                        "declaring warmup done. Off by default because the wiki "
                        "writer can be slower than the maintainer queues, so the "
                        "queue may never converge for large documents.")
    args = p.parse_args()

    stats = wait_for_warmup(
        settle_seconds=args.settle_seconds,
        consecutive_clear_required=args.consecutive_clear,
        poll_interval=args.poll_interval,
        timeout_seconds=args.timeout,
        verbose=not args.quiet,
        block_on_wiki_queue=args.wait_for_wikis,
    )
    print(f"\nwarmup complete: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
