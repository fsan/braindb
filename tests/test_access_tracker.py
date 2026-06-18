"""Unit tests for the batched access tracker.

These do NOT need a live DB: they exercise the in-memory buffer + dedup logic
and stub out the flush's `get_conn`, asserting on the SQL/params produced.
"""
import contextlib

import pytest

from braindb.services import access_tracker as at


@pytest.fixture(autouse=True)
def _reset_buffer(monkeypatch):
    # Isolate each test from buffer/flusher global state.
    with at._lock:
        at._buffer.clear()
    monkeypatch.setattr(at, "_flusher_started", True)  # never spawn the real thread
    yield
    with at._lock:
        at._buffer.clear()


class _FakeCursor:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._sink.append((sql, params))


class _FakeConn:
    def __init__(self, sink):
        self._sink = sink

    def cursor(self):
        return _FakeCursor(self._sink)


def _fake_get_conn(sink):
    @contextlib.contextmanager
    def _cm():
        yield _FakeConn(sink)

    return _cm


def test_track_access_buffers_without_writing(monkeypatch):
    sink = []
    monkeypatch.setattr(at, "get_conn", _fake_get_conn(sink))
    at.track_access(None, ["a", "b", "a"])
    # Nothing written yet — only buffered.
    assert sink == []
    assert at._buffer == {"a": 2, "b": 1}


def test_flush_dedups_and_sums(monkeypatch):
    sink = []
    monkeypatch.setattr(at, "get_conn", _fake_get_conn(sink))
    at.track_access(None, ["a", "a", "b"])
    at.track_access(None, ["a", "c"])
    n = at.flush()
    assert n == 3  # three distinct ids
    assert at._buffer == {}  # drained
    # The last statement is the UPDATE; lock/statement timeouts set first.
    stmts = [s for s, _ in sink]
    assert any("lock_timeout" in s for s in stmts)
    assert any("statement_timeout" in s for s in stmts)
    update_sql, params = sink[-1]
    assert "UPDATE entities" in update_sql
    assert "access_count = e.access_count + v.cnt" in update_sql
    # params interleave (id, count) for each distinct id; a=3, b=1, c=1.
    pairs = dict(zip(params[0::2], params[1::2]))
    assert pairs == {"a": 3, "b": 1, "c": 1}


def test_flush_empty_is_noop(monkeypatch):
    sink = []
    monkeypatch.setattr(at, "get_conn", _fake_get_conn(sink))
    assert at.flush() == 0
    assert sink == []


def test_disabled_skips_buffering(monkeypatch):
    monkeypatch.setattr(at.settings, "track_access_enabled", False)
    at.track_access(None, ["a", "b"])
    assert at._buffer == {}


def test_flush_swallows_db_errors(monkeypatch):
    @contextlib.contextmanager
    def _boom():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(at, "get_conn", _boom)
    at.track_access(None, ["a"])
    # Must not raise — soft signal.
    assert at.flush() == 0
