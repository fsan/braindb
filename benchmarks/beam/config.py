"""Bench-only configuration constants.

Reads from env vars with safe defaults that match ``docker-compose.bench.yml``.
The hard-coded safety sentinel (``braindb_bench``) is the load-bearing piece
here: every destructive op gates on it.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# Top-level sources dir, NOT a beam/ subdir. The watcher is non-recursive
# (line 318 of braindb/ingest_watcher.py uses WATCH_DIR.iterdir()) and the
# ingest endpoint hardcodes "data/sources/ingested/..." as the file path it
# expects (line 278), so a subdirectory triggers a 404 on ingest. Bench
# files at top-level are still uniquely named (e.g. beam_1m_conv_001.md)
# so they never collide with anything else in this dedicated dir.
DATA_BENCH_SOURCES = REPO_ROOT / "data_bench" / "sources"
ANSWERS_DIR = REPO_ROOT / "benchmarks" / "beam" / "answers"
RUNS_DIR = REPO_ROOT / "benchmarks" / "beam" / "runs"

# ---- Bench DB ----------------------------------------------------------------
# Hosted by docker-compose.bench.yml's `postgres_bench` service. From the host
# the bench Postgres is reachable on port 5434 (5433 is the personal Postgres
# in this environment). Inside the bench Docker network it's `postgres_bench:5432`.
BENCH_DATABASE_URL = os.getenv(
    "BENCH_DATABASE_URL",
    "postgresql://braindb_bench:bench_local_only@localhost:5434/braindb_bench",
)

# The literal substring every bench DATABASE_URL must contain. If a destructive
# op is about to fire and the active URL does NOT contain this string, the
# code refuses to proceed. This is the load-bearing safety check.
BENCH_DB_SENTINEL = "braindb_bench"

# ---- Bench BrainDB API ------------------------------------------------------
BENCH_API_BASE = os.getenv("BENCH_API_BASE", "http://localhost:8001")

# ---- Judge LLM (OpenAI-compatible endpoint) ---------------------------------
# Bench defaults to the workstation Qwen via vLLM on the SSH tunnel. The judge
# is fully outside BrainDB; the bench API is independent of which judge runs.
QWEN_BASE_URL = os.getenv("QWEN_BASE_URL", "http://localhost:8010/v1")
QWEN_MODEL = os.getenv("QWEN_MODEL", "")  # empty -> let vLLM's served model resolve
QWEN_API_KEY = os.getenv("QWEN_API_KEY", "EMPTY")  # vLLM defaults to no auth


def _mask_password(url: str) -> str:
    """Return a printable form of the URL with the password redacted."""
    if "@" not in url or "://" not in url:
        return url
    scheme_and_creds, host_path = url.split("@", 1)
    if ":" not in scheme_and_creds.split("//", 1)[1]:
        return url
    head, creds = scheme_and_creds.split("//", 1)
    user, _ = creds.split(":", 1)
    return f"{head}//{user}:***@{host_path}"


def assert_bench_database_url(url: str = BENCH_DATABASE_URL) -> None:
    """REFUSE to proceed unless URL contains the bench sentinel literally.

    This is intentionally a literal substring check, NOT a hostname/database
    parse, so that misconfigured env vars (typos, copy-paste mishaps) fail
    closed rather than falling through to the personal DB.
    """
    if BENCH_DB_SENTINEL not in url:
        raise RuntimeError(
            f"REFUSING to proceed: connection string must contain "
            f"{BENCH_DB_SENTINEL!r} as a safety check against operating on "
            f"the personal braindb database. Got: {_mask_password(url)}"
        )


__all__ = [
    "REPO_ROOT",
    "DATA_BENCH_SOURCES",
    "ANSWERS_DIR",
    "RUNS_DIR",
    "BENCH_DATABASE_URL",
    "BENCH_DB_SENTINEL",
    "BENCH_API_BASE",
    "QWEN_BASE_URL",
    "QWEN_MODEL",
    "QWEN_API_KEY",
    "assert_bench_database_url",
]
