"""Batched access-count tracking.

Every `/memory/search` and `/memory/context` call used to fire an immediate
`UPDATE entities SET access_count = access_count + 1` on every returned row.
Under concurrent load (e.g. an ingestion pipeline issuing many searches) the
same popular rows were UPDATEd over and over, producing multi-minute row-lock
chains that jammed the whole database — every other query, including the
human-facing pages, then timed out.

`access_count` only feeds `effective_importance()`, a gentle logarithmic
ranking nudge (~30% boost at 100 accesses). It is a soft signal, not
correctness, so it does NOT need to be durable or immediate. This module
buffers increments in memory and flushes them as ONE de-duplicated UPDATE on a
fixed interval — collapsing thousands of contended single-row writes into a
handful of batched ones (~1000x fewer under load).

Each uvicorn worker process owns its own buffer + flush thread; counts are
merged per-id within a flush window. A small amount of cross-worker
under-counting is acceptable for a soft ranking signal.
"""
import atexit
import threading

from braindb.config import settings
from braindb.db import get_conn

# id (str) -> pending increment count. Guarded by _lock.
_buffer: dict[str, int] = {}
_lock = threading.Lock()
_flusher_started = False
_start_lock = threading.Lock()


def track_access(conn, ids: list) -> None:
    """Record an access for each id. Non-blocking: increments an in-memory
    buffer that a background thread flushes to Postgres periodically.

    The `conn` argument is accepted for call-site compatibility but unused —
    the flush owns its own pooled connection so a buffered write never rides
    (or blocks) the request's transaction.
    """
    if not ids or not settings.track_access_enabled:
        return
    with _lock:
        for i in ids:
            key = str(i)
            _buffer[key] = _buffer.get(key, 0) + 1
        overflow = len(_buffer) >= settings.track_access_max_buffer
    _ensure_flusher()
    if overflow:
        flush()


def _drain() -> dict[str, int]:
    """Atomically take and clear the current buffer."""
    with _lock:
        if not _buffer:
            return {}
        batch = dict(_buffer)
        _buffer.clear()
        return batch


def flush() -> int:
    """Flush buffered increments as one de-duplicated UPDATE. Returns the
    number of rows targeted. Best-effort: on any DB error the batch is dropped
    (a soft ranking stat is not worth retrying or crashing a request over)."""
    batch = _drain()
    if not batch:
        return 0
    items = list(batch.items())
    values_sql = ",".join(["(%s::uuid, %s)"] * len(items))
    params: list = []
    for entity_id, count in items:
        params.extend((entity_id, count))
    sql = (
        "UPDATE entities e "
        "SET access_count = e.access_count + v.cnt, accessed_at = now() "
        f"FROM (VALUES {values_sql}) AS v(id, cnt) "
        "WHERE e.id = v.id"
    )
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Fail fast instead of joining a lock queue: a contended flush
                # must never recreate the multi-minute pileup this module
                # exists to prevent. Dropped increments are harmless.
                cur.execute(
                    "SET LOCAL lock_timeout = %s",
                    (settings.track_access_lock_timeout,),
                )
                cur.execute(
                    "SET LOCAL statement_timeout = %s",
                    (settings.track_access_statement_timeout,),
                )
                cur.execute(sql, params)
    except Exception:
        # Swallow: the increments are a soft signal. Re-buffering risks
        # unbounded growth if the DB is unhealthy; dropping is the safe choice.
        return 0
    return len(items)


def _flush_loop() -> None:
    interval = settings.track_access_flush_interval_seconds
    while True:
        _stop.wait(interval)
        flush()
        if _stop.is_set():
            return


_stop = threading.Event()


def _ensure_flusher() -> None:
    global _flusher_started
    if _flusher_started:
        return
    with _start_lock:
        if _flusher_started:
            return
        t = threading.Thread(target=_flush_loop, name="access-tracker-flush", daemon=True)
        t.start()
        _flusher_started = True
        atexit.register(_shutdown)


def _shutdown() -> None:
    """Flush whatever is buffered on process exit (best-effort)."""
    _stop.set()
    flush()
