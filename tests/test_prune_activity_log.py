"""Unit tests for `activity_log.prune_activity_log`.

Like `test_access_tracker.py`, these do NOT need a live DB: they drive the
function against a fake psycopg2-shaped connection/cursor that simulates the
age delete, the live-row-count + `pg_stats` estimate used for the size cap,
and the batched size-cap delete, and assert on the resulting behaviour (rows
deleted, batch count, early stop, error handling) rather than real bytes on
disk.

`_FakeState.physical_size_bytes` simulates `pg_total_relation_size` — the
production bug this suite guards against. Postgres does NOT shrink that
number just because rows were deleted; it only drops once autovacuum
reclaims the dead tuples. Our fake reflects that reality by keeping
`physical_size_bytes` constant regardless of how many rows get deleted
(exactly like a real just-vacuumed-never table would behave). The old
implementation queried that number to decide whether to keep deleting —
which is precisely why it kept deleting live, in-window rows until the pool
ran dry or the batch ceiling hit, on every run, forever. The fix keys the
size cap off live row count * an estimated bytes-per-row instead.
"""
import pytest

from braindb.services.activity_log import (
    _FALLBACK_BYTES_PER_ROW,
    _INDEX_OVERHEAD_FACTOR,
    _ROW_HEAP_OVERHEAD_BYTES,
    _SIZE_PRUNE_BATCH_ROWS,
    _SIZE_PRUNE_MAX_BATCHES,
    prune_activity_log,
)


def _bytes_per_row(avg_width: float | None) -> float:
    """Mirror `_estimate_bytes_per_row`'s formula so tests can pick round
    inputs and derive expected outputs instead of hand-computing bytes."""
    if not avg_width:
        return _FALLBACK_BYTES_PER_ROW
    return (avg_width + _ROW_HEAP_OVERHEAD_BYTES) * _INDEX_OVERHEAD_FACTOR


class _FakeCursor:
    def __init__(self, state):
        self._state = state
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = self._state
        if "DELETE FROM activity_log WHERE timestamp" in sql:
            self._last = ("age_delete", s.age_delete_rowcount)
        elif "ANALYZE" in sql:
            self._last = ("analyze", None)
        elif "pg_stats" in sql:
            self._last = ("avg_width", s.avg_width)
        elif "count(*)" in sql:
            self._last = ("live_count", s.reported_live_rows)
        elif "DELETE FROM activity_log" in sql and "id IN" in sql:
            deleted = s.next_batch_deleted()
            self._last = ("size_delete", deleted)
        elif "pg_total_relation_size" in sql:
            self._last = ("size_check", s.physical_size_bytes)
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    @property
    def rowcount(self):
        kind, val = self._last
        assert kind in ("age_delete", "size_delete")
        return val

    def fetchone(self):
        kind, val = self._last
        assert kind in ("avg_width", "live_count", "size_check")
        return (val,)


class _FakeConn:
    def __init__(self, state):
        self._state = state
        self.committed = 0
        self.rolled_back = 0

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self._state)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


class _FakeState:
    """Simulates a table for the size-cap loop.

    `reported_live_rows` is what `SELECT count(*)` returns (read once, up
    front, by the real implementation — it never re-queries it, tracking
    remaining rows in Python from each batch's rowcount instead).

    `force_batch_deleted`, when set, makes every size-cap batch DELETE
    return that fixed rowcount regardless of `reported_live_rows` — used to
    model a mismatch between the live-count estimate and what's actually
    left to delete (e.g. the stop-on-zero-deleted safety valve). When unset,
    batches drain from a pool that starts at `reported_live_rows`, mirroring
    a real `DELETE ... LIMIT n` against a shrinking table.

    `physical_size_bytes` simulates `pg_total_relation_size` and is held
    constant — see module docstring for why that's the realistic behaviour
    and the whole point of this regression suite.
    """

    def __init__(
        self,
        age_delete_rowcount,
        reported_live_rows,
        avg_width,
        physical_size_bytes=0,
        force_batch_deleted=None,
    ):
        self.age_delete_rowcount = age_delete_rowcount
        self.reported_live_rows = reported_live_rows
        self.avg_width = avg_width
        self.physical_size_bytes = physical_size_bytes
        self._force_batch_deleted = force_batch_deleted
        self._pool = reported_live_rows

    def next_batch_deleted(self):
        if self._force_batch_deleted is not None:
            return self._force_batch_deleted
        deleted = min(_SIZE_PRUNE_BATCH_ROWS, self._pool)
        self._pool -= deleted
        return deleted


def test_age_delete_only_when_already_under_size_cap():
    # 1000 live rows at a modest bytes/row is nowhere near the 500MB cap.
    state = _FakeState(age_delete_rowcount=1234, reported_live_rows=1000, avg_width=100)
    conn = _FakeConn(state)

    result = prune_activity_log(conn, max_age_days=3, max_size_mb=500)

    assert result["error"] is None
    assert result["age_deleted"] == 1234
    assert result["size_deleted"] == 0
    assert result["size_batches"] == 0
    expected_mb = round(1000 * _bytes_per_row(100) / (1024 * 1024), 1)
    assert result["final_live_mb"] == pytest.approx(expected_mb, rel=1e-6)
    assert conn.rolled_back == 0
    assert conn.committed == 1  # just the age delete; no size batches ran


def test_size_cap_deletes_in_batches_until_under_cap():
    # Pick a live-row count that lands exactly 2 batches above the cap
    # threshold, then just under it after a 2nd batch.
    avg_width = 100
    bpr = _bytes_per_row(avg_width)
    max_size_mb = 500
    threshold_rows = int((max_size_mb * 1024 * 1024) / bpr)
    initial_live_rows = threshold_rows + 2 * _SIZE_PRUNE_BATCH_ROWS - 3000

    state = _FakeState(age_delete_rowcount=0, reported_live_rows=initial_live_rows, avg_width=avg_width)
    conn = _FakeConn(state)

    result = prune_activity_log(conn, max_age_days=3, max_size_mb=max_size_mb)

    assert result["error"] is None
    assert result["size_batches"] == 2
    assert result["size_deleted"] == 2 * _SIZE_PRUNE_BATCH_ROWS
    remaining_rows = initial_live_rows - result["size_deleted"]
    assert remaining_rows * bpr <= max_size_mb * 1024 * 1024
    # One commit for the age delete, one per size batch. The final size read
    # is a plain SELECT (no commit needed).
    assert conn.committed == 1 + result["size_batches"]


def test_size_cap_stops_when_batch_deletes_nothing():
    # The live-row estimate says we're still over cap, but the batch delete
    # finds nothing left (e.g. the in-memory count drifted from reality) —
    # must stop instead of spinning to _SIZE_PRUNE_MAX_BATCHES.
    state = _FakeState(
        age_delete_rowcount=0,
        reported_live_rows=10_000_000,
        avg_width=100,
        force_batch_deleted=0,
    )
    conn = _FakeConn(state)

    result = prune_activity_log(conn, max_age_days=3, max_size_mb=500)

    assert result["error"] is None
    assert result["size_batches"] == 1
    assert result["size_deleted"] == 0


def test_size_cap_respects_max_batches_ceiling():
    # Shrinks far too slowly (1 row/batch) to ever land under cap within the
    # batch ceiling given a huge starting row count.
    state = _FakeState(
        age_delete_rowcount=0,
        reported_live_rows=10**9,
        avg_width=100,
        force_batch_deleted=1,
    )
    conn = _FakeConn(state)

    result = prune_activity_log(conn, max_age_days=3, max_size_mb=500)

    assert result["error"] is None
    assert result["size_batches"] == _SIZE_PRUNE_MAX_BATCHES


def test_size_cap_ignores_dead_tuple_bloat_and_keeps_inwindow_rows():
    """Regression test for the production incident: age-delete drops a huge
    batch of expired rows, leaving `pg_total_relation_size` bloated with
    dead tuples (Postgres won't shrink that number until autovacuum runs),
    while the live in-window data is comfortably under the cap.

    Numbers mirror the real prod run: age_deleted=5733588, ~845k live rows
    left (~222MB via the ~276 B/row fallback estimate), but the physical
    size still reads ~1723MB (over the 500MB cap) because of dead tuples.

    Against the OLD implementation (which loops on
    `pg_total_relation_size`), this constant, never-shrinking physical size
    would make the size loop keep deleting 5000-row batches until the pool
    of 845000 in-window rows was exhausted (169 batches) — i.e. it would
    empty the table, exactly as observed in production. Against the FIXED
    implementation, the loop never even starts because the live estimate is
    already under cap.
    """
    live_rows = 845_000
    state = _FakeState(
        age_delete_rowcount=5_733_588,
        reported_live_rows=live_rows,
        avg_width=None,  # no pg_stats yet -> falls back to the prod-observed ~276 B/row
        physical_size_bytes=1723.3 * 1024 * 1024,  # bloated by dead tuples; held constant
    )
    conn = _FakeConn(state)

    result = prune_activity_log(conn, max_age_days=3, max_size_mb=500)

    assert result["error"] is None
    assert result["age_deleted"] == 5_733_588
    # The whole point of the fix: the size loop must NOT touch the
    # comfortably-in-window live rows just because physical size reads high.
    assert result["size_deleted"] == 0
    assert result["size_batches"] == 0
    expected_live_mb = round(live_rows * _FALLBACK_BYTES_PER_ROW / (1024 * 1024), 1)
    assert result["final_live_mb"] == pytest.approx(expected_live_mb, rel=1e-6)
    assert result["final_live_mb"] < 500
    # Physical size is still reported for observability, and can legitimately
    # stay over the cap until autovacuum reclaims the dead tuples — that's
    # fine, because it no longer drives any deletion decision.
    assert result["final_size_mb"] == pytest.approx(1723.3, rel=1e-6)
    assert result["final_size_mb"] > 500


def test_db_error_is_swallowed_and_reported():
    class _BoomConn:
        def cursor(self, cursor_factory=None):
            raise RuntimeError("db down")

        def commit(self):
            pass

        def rollback(self):
            pass

    result = prune_activity_log(_BoomConn(), max_age_days=3, max_size_mb=500)

    assert result["error"] == "db down"
    assert result["age_deleted"] == 0
    assert result["size_deleted"] == 0
