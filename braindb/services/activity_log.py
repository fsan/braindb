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


def prune_activity_log(conn, max_age_days: int, max_size_mb: int) -> dict:
    """Bound `activity_log` growth. Two independent, composable steps:

    1. Age: DELETE everything older than `max_age_days`. One statement — safe
       because it's index-driven (`activity_log_timestamp_idx`) and normal
       operation only ever needs to catch up a few days' worth of rows.
    2. Size: if `pg_total_relation_size('activity_log')` (heap + indexes) is
       still over `max_size_mb` after step 1, delete the oldest remaining
       rows in bounded batches (by id, ascending — `id` is a BIGSERIAL PK so
       ascending id order tracks insertion/timestamp order) until under the
       cap or the batch ceiling is hit. Re-checking the real relation size
       between batches (rather than estimating rows-to-delete up front) means
       we don't need to reproduce Postgres's own size accounting.

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

        max_size_bytes = max_size_mb * 1024 * 1024
        for _ in range(_SIZE_PRUNE_MAX_BATCHES):
            with conn.cursor() as cur:
                cur.execute("SELECT pg_total_relation_size('activity_log')")
                size_bytes = cur.fetchone()[0]
                if size_bytes <= max_size_bytes:
                    break
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
            result["size_deleted"] += deleted
            result["size_batches"] += 1
            if deleted == 0:
                # Table is smaller than the size accounting implied
                # (e.g. index bloat inflating pg_total_relation_size with
                # no rows left to reclaim it) — stop rather than spin.
                break

        with conn.cursor() as cur:
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
        "activity_log prune: age_deleted=%d size_deleted=%d (%d batches) final_size_mb=%s",
        result["age_deleted"], result["size_deleted"], result["size_batches"],
        result["final_size_mb"],
    )
    return result
