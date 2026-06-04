"""BrainDB on BEAM bench runner.

Per-conversation lifecycle (zero interleaving within a conversation):

    Phase A — reset bench DB + write conversation .md to data_bench/sources/beam/
    Phase B — warmup wait (extraction + wiki pipeline drains)
    Phase C — answer all of this conversation's probing questions via /agent/query
    Phase D — record answers, probing_questions, warmup_stats, meta to runs/<run_id>/conv_<NNN>/

Strict safety: every destructive op (DB reset, file write to data_bench/)
is gated on the literal substring ``braindb_bench`` appearing in the active
``DATABASE_URL`` — never touches the personal braindb database.

CLI:

    python -m benchmarks.beam.bench --split 1M --limit 3      # smoke (3 convs)
    python -m benchmarks.beam.bench --split 1M                 # full (35 convs)
    python -m benchmarks.beam.bench --split 1M --limit 3 --fail-fast
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

import psycopg2
import requests

from benchmarks.beam.adapter import (
    Conversation,
    iter_conversations,
    load_probing_questions,
    write_conversation_md,
)
from benchmarks.beam.config import (
    BENCH_API_BASE,
    BENCH_DATABASE_URL,
    DATA_BENCH_SOURCES,
    RUNS_DIR,
    assert_bench_database_url,
)
from benchmarks.beam.warmup import wait_for_warmup

# Tables that get truncated per conversation. alembic_version is NOT in this
# list — we want migrations to stay applied. Everything else is wiped.
_TRUNCATE_SQL = """
TRUNCATE TABLE
    entities,
    relations,
    wikis_ext,
    wiki_job,
    facts_ext,
    thoughts_ext,
    sources_ext,
    datasources_ext,
    rules_ext,
    activity_log
RESTART IDENTITY CASCADE;
"""

# Per-question agent timeout. BrainDB's /agent/query can take a while on
# local Qwen — multi-turn agent loops + extraction can be 30s to several
# minutes per question. 10 minutes hard ceiling per question.
QUESTION_TIMEOUT_SECONDS = 600


# ----------------------------- helpers ---------------------------------------

def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short_ts() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")


def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            cwd=Path(__file__).resolve().parents[2],
        )
        return out.decode().strip()
    except Exception:
        return "unknown"


def _git_dirty() -> bool:
    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
            cwd=Path(__file__).resolve().parents[2],
        )
        return bool(out.strip())
    except Exception:
        return False


# ----------------------------- preflight -------------------------------------

def check_bench_api_healthy(base: str = BENCH_API_BASE, timeout: float = 5) -> None:
    try:
        r = requests.get(f"{base}/health", timeout=timeout)
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        raise RuntimeError(
            f"bench API not reachable at {base} ({e}). "
            f"Did you run: docker compose -f docker-compose.bench.yml up -d ?"
        )
    if body.get("status") != "ok":
        raise RuntimeError(f"bench API unhealthy: {body}")


# ----------------------------- DB reset --------------------------------------

def reset_bench_db() -> None:
    """TRUNCATE all data tables in the bench DB. Schema (alembic_version)
    is preserved so the API stays connected and migrations stay applied.
    """
    assert_bench_database_url()
    conn = psycopg2.connect(BENCH_DATABASE_URL)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(_TRUNCATE_SQL)
    finally:
        conn.close()


def _clear_sources_dir() -> None:
    """Remove any stale .md files (and their .error.txt sidecars) left in
    the bench watcher's source dir and its ingested/ + failed/ siblings.
    Without this, files from previous conversations would either be
    re-ingested by the next warmup OR poison the warmup barrier by leaving
    pending content in the watch dir.
    """
    DATA_BENCH_SOURCES.mkdir(parents=True, exist_ok=True)
    for f in DATA_BENCH_SOURCES.glob("*.md"):
        try:
            f.unlink()
        except OSError:
            pass
    # The watcher creates ingested/ + failed/ as siblings of WATCH_DIR
    # (see ingest_watcher.py lines 39-40). Clean both so cumulative runs
    # don't pile up on disk and so a previous failed file doesn't trick
    # the next run into thinking ingest already happened.
    for sibling in ("ingested", "failed"):
        d = DATA_BENCH_SOURCES / sibling
        if d.exists():
            for f in d.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                    except OSError:
                        pass


# ----------------------------- ask agent -------------------------------------

def answer_one_question(
    question_text: str,
    base: str = BENCH_API_BASE,
    timeout: float = QUESTION_TIMEOUT_SECONDS,
) -> tuple[str, dict]:
    """POST /agent/query with the question; return (answer_text, raw_payload).

    Returns the answer text and the full payload (so we can capture tool
    counts, latency, etc.) for the per-question record. Errors get caught
    in the caller and recorded as `[ERROR: ...]` so the run continues.
    """
    started = time.monotonic()
    r = requests.post(
        f"{base}/api/v1/agent/query",
        json={"query": question_text},
        timeout=timeout,
    )
    elapsed = time.monotonic() - started
    r.raise_for_status()
    payload = r.json()
    payload["_elapsed_seconds"] = round(elapsed, 2)
    # BrainDB's /agent/query response shape: {"answer": "..."} at minimum.
    return payload.get("answer", ""), payload


# ----------------------------- per-conv lifecycle ----------------------------

def run_one_conversation(
    conv: Conversation,
    run_dir: Path,
    *,
    warmup_timeout: float = 1800,
    warmup_settle_seconds: float = 180,
    question_timeout: float = QUESTION_TIMEOUT_SECONDS,
) -> dict:
    """Full A-B-C-D lifecycle for one conversation. Returns a small stats dict."""
    conv_dir = run_dir / f"conv_{int(conv.conversation_id):03d}"
    conv_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()

    # ---- Phase A: reset + ingest ----
    print(f"[A] reset bench DB + write conversation markdown ...", flush=True)
    reset_bench_db()
    _clear_sources_dir()
    md_path = write_conversation_md(conv, DATA_BENCH_SOURCES)
    md_kb = md_path.stat().st_size / 1024
    print(f"[A done] {md_path.name} ({md_kb:.0f} KB) dropped at {_now_iso()}", flush=True)

    # ---- Phase B: warmup wait ----
    print(f"[B] warmup wait ...", flush=True)
    warmup_stats = wait_for_warmup(
        settle_seconds=warmup_settle_seconds,
        timeout_seconds=warmup_timeout,
        verbose=True,
    )
    print(f"[B done] {warmup_stats}", flush=True)

    # ---- Phase C: answer all questions ----
    probing = load_probing_questions(conv)
    print(f"[C] answering {sum(len(v) for v in probing.values())} questions "
          f"across {len(probing)} categories ...", flush=True)
    answers_by_category: dict[str, list[dict]] = {}
    raw_payloads_by_category: dict[str, list[dict]] = {}
    question_errors = 0

    for category, questions in probing.items():
        category_answers: list[dict] = []
        category_payloads: list[dict] = []
        for i, q in enumerate(questions, start=1):
            question_text = q["question"]
            try:
                ans, payload = answer_one_question(
                    question_text, timeout=question_timeout
                )
                print(
                    f"  [{category} {i}/{len(questions)}] OK "
                    f"({payload.get('_elapsed_seconds', '?')}s, "
                    f"{len(ans)} chars)",
                    flush=True,
                )
            except Exception as e:
                ans = f"[ERROR: {type(e).__name__}: {e}]"
                payload = {"_error": str(e)}
                question_errors += 1
                print(f"  [{category} {i}/{len(questions)}] ERROR: {e}", flush=True)
            category_answers.append({"question": question_text, "llm_response": ans})
            category_payloads.append({"question": question_text, "payload": payload})
        answers_by_category[category] = category_answers
        raw_payloads_by_category[category] = category_payloads

    # ---- Phase D: record artefacts ----
    print(f"[D] recording artefacts to {conv_dir} ...", flush=True)
    conv_dir.joinpath("answers.json").write_text(
        json.dumps(answers_by_category, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    conv_dir.joinpath("probing_questions.json").write_text(
        json.dumps(probing, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    conv_dir.joinpath("warmup_stats.json").write_text(
        json.dumps(warmup_stats, indent=2), encoding="utf-8"
    )
    conv_dir.joinpath("raw_payloads.json").write_text(
        json.dumps(raw_payloads_by_category, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    conv_meta = {
        "conversation_id": conv.conversation_id,
        "split": conv.split,
        "slug": conv.slug,
        "category": (conv.raw.get("conversation_seed") or {}).get("category"),
        "ingested_md_size_kb": int(md_kb),
        "question_errors": question_errors,
        "wall_clock_s": round(time.monotonic() - started, 1),
        "started_at": _now_iso(),
    }
    conv_dir.joinpath("conversation_meta.json").write_text(
        json.dumps(conv_meta, indent=2), encoding="utf-8"
    )

    print(f"[done] conv {conv.slug} in {conv_meta['wall_clock_s']:.0f}s, "
          f"errors={question_errors}\n", flush=True)
    return conv_meta


# ----------------------------- run-level -------------------------------------

def _generate_run_id(split: str, limit: int | None) -> str:
    sha = _git_sha()
    dirty = "+dirty" if _git_dirty() else ""
    n = f"_n{limit}" if limit is not None else ""
    return f"{_short_ts()}_{split}{n}_{sha}{dirty}"


def _save_run_config(run_dir: Path, args: argparse.Namespace) -> None:
    cfg = {
        "split": args.split,
        "limit": args.limit,
        "fail_fast": args.fail_fast,
        "warmup_timeout": args.warmup_timeout,
        "warmup_settle_seconds": args.warmup_settle_seconds,
        "question_timeout": args.question_timeout,
        "started_at": _now_iso(),
        "git_sha": _git_sha(),
        "git_dirty": _git_dirty(),
        "bench_api_base": BENCH_API_BASE,
        "python_version": sys.version,
    }
    run_dir.joinpath("run_config.json").write_text(
        json.dumps(cfg, indent=2), encoding="utf-8"
    )


def _conversations_to_run(args: argparse.Namespace) -> Iterable[Conversation]:
    convs = list(iter_conversations(args.split))
    if args.limit is not None:
        convs = convs[: args.limit]
    if args.only_ids:
        wanted = set(s.strip() for s in args.only_ids.split(","))
        convs = [c for c in convs if c.conversation_id in wanted]
    return convs


# ----------------------------- CLI -------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--split", default="1M", choices=["100K", "500K", "1M"])
    p.add_argument("--limit", type=int, default=None,
                   help="run only the first N conversations from the split")
    p.add_argument("--only-ids", default=None,
                   help="comma-separated list of conversation_ids to run (overrides --limit ordering)")
    p.add_argument("--fail-fast", action="store_true",
                   help="abort the whole run if any single conversation raises")
    p.add_argument("--warmup-timeout", type=float, default=1800,
                   help="seconds before warmup gives up on convergence")
    p.add_argument("--warmup-settle-seconds", type=float, default=180,
                   help="seconds of quiet on entities before declaring warmup clear "
                        "(default 180; big enough to span the gap between datasource "
                        "creation and the first extracted fact for slow chunk processing)")
    p.add_argument("--question-timeout", type=float, default=QUESTION_TIMEOUT_SECONDS,
                   help="per-question HTTP timeout on /agent/query")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    assert_bench_database_url()
    check_bench_api_healthy()

    convs = list(_conversations_to_run(args))
    if not convs:
        print("no conversations selected; exiting")
        return 1

    run_id = _generate_run_id(args.split, args.limit if not args.only_ids else None)
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _save_run_config(run_dir, args)

    print(f"=== BEAM bench run: {run_id} ===")
    print(f"split={args.split}  conversations={len(convs)}  run_dir={run_dir}")
    print(f"bench API: {BENCH_API_BASE}")
    print(f"git SHA:   {_git_sha()}{'  (dirty)' if _git_dirty() else ''}")
    print()

    metas: list[dict] = []
    run_started = time.monotonic()
    for i, conv in enumerate(convs, start=1):
        print(f"--- Conversation {i}/{len(convs)} ({conv.slug}, "
              f"category={(conv.raw.get('conversation_seed') or {}).get('category')}) ---")
        try:
            meta = run_one_conversation(
                conv,
                run_dir,
                warmup_timeout=args.warmup_timeout,
                warmup_settle_seconds=args.warmup_settle_seconds,
                question_timeout=args.question_timeout,
            )
            metas.append(meta)
        except Exception as e:
            print(f"!!! conversation {conv.slug} failed: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            metas.append({
                "conversation_id": conv.conversation_id,
                "slug": conv.slug,
                "fatal_error": f"{type(e).__name__}: {e}",
            })
            if args.fail_fast:
                _write_run_summary(run_dir, metas, time.monotonic() - run_started)
                return 1

    _write_run_summary(run_dir, metas, time.monotonic() - run_started)
    print(f"\n=== run complete: {run_id} ===")
    return 0


def _write_run_summary(run_dir: Path, metas: list[dict], wall_clock_s: float) -> None:
    summary = {
        "finished_at": _now_iso(),
        "wall_clock_s": round(wall_clock_s, 1),
        "conversations_attempted": len(metas),
        "conversations_succeeded": sum(1 for m in metas if "fatal_error" not in m),
        "total_question_errors": sum(int(m.get("question_errors", 0)) for m in metas),
        "per_conv_meta": metas,
    }
    run_dir.joinpath("run_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    raise SystemExit(main())
