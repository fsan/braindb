"""Unit tests for `activity_log.prune_activity_log`.

Like `test_access_tracker.py`, these do NOT need a live DB: they drive the
function against a fake psycopg2-shaped connection/cursor that simulates the
age delete, the `pg_total_relation_size` checks, and the batched size-cap
delete, and assert on the resulting behaviour (rows deleted, batch count,
early stop, error handling) rather than real bytes on disk.
"""
import pytest

from braindb.services.activity_log import (
    _SIZE_PRUNE_BATCH_ROWS,
    _SIZE_PRUNE_MAX_BATCHES,
    prune_activity_log,
)


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
        elif "pg_total_relation_size" in sql:
            self._last = ("size_check", s.size_bytes)
        elif "DELETE FROM activity_log" in sql and "id IN" in sql:
            deleted = s.next_batch_deleted()
            self._last = ("size_delete", deleted)
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    @property
    def rowcount(self):
        kind, val = self._last
        assert kind in ("age_delete", "size_delete")
        return val

    def fetchone(self):
        kind, val = self._last
        assert kind == "size_check"
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
    """Simulates a table whose size shrinks by `bytes_per_batch` per size
    batch, down to a floor, so the caller's re-check loop has something
    realistic to converge against."""

    def __init__(self, age_delete_rowcount, start_size_bytes, bytes_per_batch, floor_bytes=0):
        self.age_delete_rowcount = age_delete_rowcount
        self.size_bytes = start_size_bytes
        self._bytes_per_batch = bytes_per_batch
        self._floor_bytes = floor_bytes

    def next_batch_deleted(self):
        if self.size_bytes <= self._floor_bytes:
            return 0
        self.size_bytes = max(self._floor_bytes, self.size_bytes - self._bytes_per_batch)
        return _SIZE_PRUNE_BATCH_ROWS


def test_age_delete_only_when_already_under_size_cap():
    state = _FakeState(age_delete_rowcount=1234, start_size_bytes=100 * 1024 * 1024, bytes_per_batch=10 * 1024 * 1024)
    conn = _FakeConn(state)

    result = prune_activity_log(conn, max_age_days=3, max_size_mb=500)

    assert result["error"] is None
    assert result["age_deleted"] == 1234
    assert result["size_deleted"] == 0
    assert result["size_batches"] == 0
    assert result["final_size_mb"] == pytest.approx(100.0)
    assert conn.rolled_back == 0
    assert conn.committed == 1  # just the age delete; no size batches ran


def test_size_cap_deletes_in_batches_until_under_cap():
    # 600MB start, cap 500MB, shrinks 50MB/batch -> 600->550 (still over) ->
    # 500 (<=500, stop before a 3rd delete): 2 batches land exactly at the cap.
    state = _FakeState(age_delete_rowcount=0, start_size_bytes=600 * 1024 * 1024, bytes_per_batch=50 * 1024 * 1024)
    conn = _FakeConn(state)

    result = prune_activity_log(conn, max_age_days=3, max_size_mb=500)

    assert result["error"] is None
    assert result["size_batches"] == 2
    assert result["size_deleted"] == 2 * _SIZE_PRUNE_BATCH_ROWS
    assert result["final_size_mb"] <= 500
    # One commit for the age delete, one per size batch. The final size read
    # is a plain SELECT (no commit needed).
    assert conn.committed == 1 + result["size_batches"]


def test_size_cap_stops_when_batch_deletes_nothing():
    # Size accounting never drops below the cap (e.g. index bloat) and the
    # batch delete finds nothing left — must stop instead of spinning to
    # _SIZE_PRUNE_MAX_BATCHES.
    state = _FakeState(age_delete_rowcount=0, start_size_bytes=600 * 1024 * 1024, bytes_per_batch=0, floor_bytes=600 * 1024 * 1024)
    conn = _FakeConn(state)

    result = prune_activity_log(conn, max_age_days=3, max_size_mb=500)

    assert result["error"] is None
    assert result["size_batches"] == 1
    assert result["size_deleted"] == 0


def test_size_cap_respects_max_batches_ceiling():
    # Shrinks far too slowly to ever land under cap within the batch ceiling.
    state = _FakeState(age_delete_rowcount=0, start_size_bytes=10_000 * 1024 * 1024, bytes_per_batch=1024)
    conn = _FakeConn(state)

    result = prune_activity_log(conn, max_age_days=3, max_size_mb=500)

    assert result["error"] is None
    assert result["size_batches"] == _SIZE_PRUNE_MAX_BATCHES


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
