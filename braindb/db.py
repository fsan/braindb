import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from braindb.config import settings

# Module-global pool, created lazily on first use so importing this module
# (e.g. in tooling or tests) never forces a DB connection. The lock guards
# the one-time initialisation against the threaded request workers.
_pool: ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadedConnectionPool(
                    minconn=settings.db_pool_min,
                    maxconn=settings.db_pool_max,
                    dsn=settings.database_url,
                )
    return _pool


@contextmanager
def get_conn():
    """Yield a pooled connection, committing on success and rolling back on error.

    Drop-in replacement for the previous connect-per-request implementation:
    same `with get_conn() as conn:` contract. The connection is returned to the
    pool (not closed) on exit. A connection broken mid-transaction is discarded
    from the pool via `putconn(close=True)` so a poisoned socket is never reused.
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        # Best-effort rollback: a connection broken mid-statement can't roll
        # back, and letting that secondary error propagate would mask the real
        # failure. Swallow it here; the broken conn is dropped in `finally`.
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        # If the connection died (e.g. server restart), drop it from the pool
        # rather than recycling a dead socket.
        broken = conn.closed != 0
        pool.putconn(conn, close=broken)
