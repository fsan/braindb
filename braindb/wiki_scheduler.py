"""
Always-on wiki scheduler — ONE loop, like ingest_watcher.py (one interval).

Per tick:
  1. POST /wiki/cron               — cheap, pure SQL, no LLM.
  2. GET  /wiki/jobs?status=pending — cheap, pure SQL, no LLM. The gate.
  3. if a pending `triage` job exists  -> POST /wiki/maintain  (one case, C1)
  4. if pending suggestion jobs exist  -> POST /wiki/write, repeated to DRAIN
       them (bounded) so consolidate/attach keep up instead of trickling.
  5. nothing pending  -> NO LLM call this tick (idle == free).

The expensive LLM endpoints are never called speculatively: a tick with
empty queues costs nothing. No multi-timer staggering, one env var.
"""
import logging
import os
import sys
import time

import requests

API_URL = os.getenv("BRAINDB_API_URL", "http://localhost:8000")
INTERVAL = int(os.getenv("WIKI_INTERVAL", "60"))          # one cadence, like the watcher
DRAIN_MAX = int(os.getenv("WIKI_DRAIN_MAX", "20"))        # safety bound on /write per tick
AGENT_TIMEOUT = int(os.getenv("WIKI_AGENT_TIMEOUT", "600"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [wiki-scheduler] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("wiki-scheduler")

_SUGGESTION_TYPES = {"create", "attach", "consolidate"}


def wait_for_api(timeout: int = 90) -> bool:
    deadline = time.time() + timeout
    url = f"{API_URL}/health"
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=3).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(2)
    return False


def _post(path: str, timeout: int) -> dict | None:
    try:
        r = requests.post(f"{API_URL}{path}", timeout=timeout)
        if r.status_code == 200:
            return r.json()
        log.warning("%s -> %s: %s", path, r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.warning("%s request error: %s", path, e)
    return None


def _pending_kinds() -> tuple[bool, bool]:
    """(has_triage, has_suggestion) from ONE cheap SQL-only read. On error,
    return (False, False) so we never fire LLM calls on uncertain state."""
    try:
        r = requests.get(
            f"{API_URL}/api/v1/wiki/jobs",
            params={"status": "pending", "limit": 500},
            timeout=15,
        )
        if r.status_code != 200:
            log.warning("/jobs -> %s: %s", r.status_code, r.text[:200])
            return (False, False)
        jobs = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("/jobs read error: %s", e)
        return (False, False)
    has_triage = any(j.get("job_type") == "triage" for j in jobs)
    has_sugg = any(j.get("job_type") in _SUGGESTION_TYPES for j in jobs)
    return (has_triage, has_sugg)


def main() -> None:
    log.info("waiting for API at %s ...", API_URL)
    if not wait_for_api():
        log.error("API never came up; exiting")
        sys.exit(1)
    log.info("wiki scheduler ready (single loop, interval=%ss)", INTERVAL)

    while True:
        try:
            # 1. cron — cheap SQL, safe to run every tick.
            res = _post("/api/v1/wiki/cron", timeout=60)
            if res and res.get("triage_jobs_enqueued"):
                log.info("cron: enqueued=%s pending_triage=%s",
                         res.get("triage_jobs_enqueued"), res.get("pending_triage_total"))

            # 2. cheap gate — decide whether any LLM work is warranted.
            has_triage, has_sugg = _pending_kinds()

            # 3. one maintain case (C1) only if there is triage to do.
            if has_triage:
                res = _post("/api/v1/wiki/maintain", timeout=AGENT_TIMEOUT)
                if res and res.get("claimed"):
                    log.info("maintain: %s", res.get("result"))

            # 4. drain the write queue (bounded) only if suggestions exist.
            if has_sugg:
                for _ in range(DRAIN_MAX):
                    res = _post("/api/v1/wiki/write", timeout=AGENT_TIMEOUT)
                    if not res or not res.get("written"):
                        break
                    log.info("write: wiki=%s mode=%s rev=%s",
                             res.get("wiki_id"), res.get("mode"), res.get("revision"))

            # 5. nothing pending -> no LLM call happened this tick (free).
        except Exception as e:
            log.exception("loop error: %s", e)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
