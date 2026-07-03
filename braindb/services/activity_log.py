"""
Activity log — append-only log of every operation on the memory database.
Karpathy-inspired: traceability of what happened, when, and in what context.

The log_activity function is fire-and-forget: it must never fail the main operation.

`prune_activity_log` bounds the table's growth (see `Settings.activity_log_max_age_days`
/ `activity_log_max_size_mb` in braindb/config.py) — the table is pure observability
(never read by ranking or source-finding), so deleting old rows is safe. It is invoked
externally via `python -m braindb.tools.prune_activity_log` (no in-process scheduler
exists in this repo for this cadence; wire it to a k8s CronJob or host cron).
"""
import logging

import psycopg2.extras

logger = logging.getLogger(__name__)


def log_activity(
    conn,
    operation: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    details: dict | None = None,
    context_note: str | None = None,
) -> None:
    """Write an activity log entry. Swallows errors so it never breaks the caller."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO activity_log (operation, entity_type, entity_id, details, context_note)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    operation,
                    entity_type,
                    str(entity_id) if entity_id else None,
                    psycopg2.extras.Json(details or {}),
                    context_note,
                ),
            )
    except Exception:
        pass


def query_log(
    conn,
    operation: str | None = None,
    entity_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query the activity log with optional filters."""
    conditions = []
    params: list = []

    if operation:
        conditions.append("operation = %s")
        params.append(operation)
    if entity_id:
        conditions.append("entity_id = %s")
        params.append(str(entity_id))
    if since:
        conditions.append("timestamp >= %s")
        params.append(since)
    if until:
        conditions.append("timestamp <= %s")
        params.append(until)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT id, timestamp, operation, entity_type, entity_id, details, context_note
            FROM activity_log
            {where}
            ORDER BY timestamp DESC
            LIMIT %s
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]


# Batch size for the size-cap sweep below. Deleting by a bounded id range (not
# a single unbounded DELETE) keeps each statement's row-lock footprint small
# so a prune run never holds a long exclusive lock over the whole table —
# consistent with how `access_tracker.py` avoids one giant contended write.
_SIZE_PRUNE_BATCH_ROWS = 5000
# Hard ceiling on batches per run so a pathological backlog (e.g. the cap was
# lowered a lot, or a run was skipped for a long time) can't turn one prune
# invocation into an unbounded loop. 200 batches * 5000 rows = 1M rows/run,
# comfortably above the ~600k/day peak this is sized for.
_SIZE_PRUNE_MAX_BATCHES = 200

# Bytes-per-row assumptions used only to *estimate* live data volume for the
# size cap below (see `_estimate_bytes_per_row` and the loop docstring for
# why this can't be `pg_total_relation_size`). `_ROW_HEAP_OVERHEAD_BYTES`
# approximates the per-tuple heap overhead that `pg_stats.avg_width` doesn't
# capture (23-byte tuple header + 4-byte line pointer, rounded up for
# alignment). `_INDEX_OVERHEAD_FACTOR` is a rough multiplier to account for
# the table's three btree indexes (timestamp, operation, entity_id), which
# also consume disk and therefore matter to the *intent* of the cap ("keep
# this table's total on-disk footprint bounded") even though the decision
# itself must be driven by live rows, not Postgres's dead-tuple-inclusive
# physical size. `_FALLBACK_BYTES_PER_ROW` is the prod-observed average
# (~276 B/row incl. indexes — see `Settings.activity_log_max_size_mb`'s
# comment in config.py) used only if `pg_stats` has no stats yet (e.g. a
# freshly created / never-analyzed table), so the cap still means something
# on a cold start instead of silently no-op'ing.
_ROW_HEAP_OVERHEAD_BYTES = 28
_INDEX_OVERHEAD_FACTOR = 1.5
_FALLBACK_BYTES_PER_ROW = 276.0


def _estimate_bytes_per_row(cur) -> float:
    """Estimate average on-disk bytes per live row from planner statistics.

    Deliberately does NOT use `pg_total_relation_size`: that counts the
    physical file size, which includes dead tuples left behind by the
    age-delete above until the next autovacuum reclaims them. On a table
    that just had a large DELETE run against it, the physical size stays
    at (roughly) its pre-delete size — reading that number here would make
    every prune run see a table that looks just as big as before its own
    age-delete, and keep deleting *live, in-window* rows to "compensate"
    for space that was already freed logically (just not yet physically).
    That was the actual production incident this function is patching:
    the age-delete correctly dropped ~5.7M expired rows, but the old size
    loop then read the still-bloated physical size and deleted the
    remaining ~845k in-window rows too, emptying the table on its first run.

    `ANALYZE` is cheap (it samples, not a full scan) and keeps `pg_stats`
    fresh so `avg_width` reflects the current row shape.
    """
    cur.execute("ANALYZE activity_log")
    cur.execute(
        """
        SELECT sum(avg_width) FROM pg_stats
        WHERE schemaname = current_schema() AND tablename = 'activity_log'
        """
    )
    row = cur.fetchone()
    avg_width = row[0] if row else None
    if not avg_width:
        return _FALLBACK_BYTES_PER_ROW
    return (avg_width + _ROW_HEAP_OVERHEAD_BYTES) * _INDEX_OVERHEAD_FACTOR


def prune_activity_log(conn, max_age_days: int, max_size_mb: int) -> dict:
    """Bound `activity_log` growth. Two independent, composable steps:

    1. Age: DELETE everything older than `max_age_days`. One statement — safe
       because it's index-driven (`activity_log_timestamp_idx`) and normal
       operation only ever needs to catch up a few days' worth of rows.
    2. Size: if the *estimated live data volume* is still over `max_size_mb`
       after step 1, delete the oldest remaining rows in bounded batches (by
       id, ascending — `id` is a BIGSERIAL PK so ascending id order tracks
       insertion/timestamp order) until under the cap or the batch ceiling
       is hit.

       Crucially, this is measured against live rows (`COUNT(*)` up front,
       decremented in Python by each batch's `rowcount` — cheap, no extra
       full-table scans), multiplied by an estimated bytes-per-row from
       `pg_stats` (see `_estimate_bytes_per_row`), NOT against
       `pg_total_relation_size`. That function counts physical file size
       *including dead tuples*, which the age-delete above just created a
       lot of — reading it here would make the size loop think the table is
       still huge and keep deleting live, in-window rows to compensate for
       space that's already been logically freed (reclaiming that space
       physically is autovacuum's job, not this pruner's). This was a real
       production bug: the very first run reported
       `age_deleted=5733588 size_deleted=817424 (165 batches)` and emptied
       the table to 0 rows, deleting the ~845k rows that were still inside
       the retention window.

    Commits after the age-delete and after each size batch (rather than
    leaving everything to the caller's outer transaction) so progress is
    durable if the process is interrupted mid-run, and so no single
    transaction accumulates locks/WAL for the full prune — each committed
    batch is a fully independent unit of work. Safe to call with a
    `get_conn()`-managed connection: the outer commit-on-exit becomes a
    no-op once this function has already committed everything itself.

    Idempotent and safe to run repeatedly (e.g. from a periodic job): a run
    with nothing to delete is a fast no-op. Never raises — errors are logged
    and swallowed, matching `log_activity`'s fire-and-forget posture, since a
    failed prune should not be treated as a service outage.
    """
    result = {
        "age_deleted": 0,
        "size_deleted": 0,
        "size_batches": 0,
        "final_size_mb": None,
        "final_live_mb": None,
        "error": None,
    }
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM activity_log WHERE timestamp < now() - %s::interval",
                (f"{max_age_days} days",),
            )
            result["age_deleted"] = cur.rowcount
        conn.commit()

        with conn.cursor() as cur:
            bytes_per_row = _estimate_bytes_per_row(cur)

        max_size_bytes = max_size_mb * 1024 * 1024
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM activity_log")
            live_row_count = cur.fetchone()[0]

        for _ in range(_SIZE_PRUNE_MAX_BATCHES):
            if live_row_count * bytes_per_row <= max_size_bytes:
                break
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM activity_log
                    WHERE id IN (
                        SELECT id FROM activity_log
                        ORDER BY id ASC
                        LIMIT %s
                    )
                    """,
                    (_SIZE_PRUNE_BATCH_ROWS,),
                )
                deleted = cur.rowcount
            conn.commit()
            live_row_count -= deleted
            result["size_deleted"] += deleted
            result["size_batches"] += 1
            if deleted == 0:
                # Nothing left to delete even though the estimate says we're
                # still over cap (e.g. bytes_per_row overestimated) — stop
                # rather than spin.
                break

        result["final_live_mb"] = round(live_row_count * bytes_per_row / (1024 * 1024), 1)
        with conn.cursor() as cur:
            # Kept for observability only — NOT used to drive any decision
            # above, since it includes dead-tuple bloat (see
            # `_estimate_bytes_per_row`'s docstring). Compare against
            # `final_live_mb` to see how much of the physical size is bloat
            # an autovacuum still needs to reclaim.
            cur.execute("SELECT pg_total_relation_size('activity_log')")
            result["final_size_mb"] = round(cur.fetchone()[0] / (1024 * 1024), 1)
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        result["error"] = str(exc)
        logger.exception("prune_activity_log failed")
        return result

    logger.info(
        "activity_log prune: age_deleted=%d size_deleted=%d (%d batches) "
        "final_live_mb=%s final_size_mb=%s (live estimate drives deletion; "
        "size incl. physical/dead-tuple bloat is observability-only)",
        result["age_deleted"], result["size_deleted"], result["size_batches"],
        result["final_live_mb"], result["final_size_mb"],
    )
    return result
