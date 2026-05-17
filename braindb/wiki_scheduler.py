"""
Always-on wiki scheduler. Runs as a sidecar docker service (Stage 2).

Structural clone of ingest_watcher.py: wait_for_api, then an infinite loop
with independent interval timers. It only POSTs the existing Stage-1 wiki
endpoints — it contains no pipeline logic of its own:

  * cron     — every WIKI_CRON_INTERVAL    -> POST /api/v1/wiki/cron
               (read-only orphan scan, enqueues one triage job per orphan)
  * maintain — every WIKI_MAINTAIN_INTERVAL -> POST /api/v1/wiki/maintain
               (drains ONE triage case per tick — C1, per-case)
  * write    — every WIKI_WRITE_INTERVAL    -> POST /api/v1/wiki/write
               (writes ONE wiki per tick)

The api and ingest watcher are untouched; a wiki run can never block file
ingestion because this is an isolated process.
"""
import logging
import os
import sys
import time

import requests

API_URL = os.getenv("BRAINDB_API_URL", "http://localhost:8000")
CRON_INTERVAL = int(os.getenv("WIKI_CRON_INTERVAL", "120"))        # ~2m: cheap continuous scan; settling is enforced by the created_at freshness gate in _orphan_conditions(), not by this interval
MAINTAIN_INTERVAL = int(os.getenv("WIKI_MAINTAIN_INTERVAL", "45"))  # one case / 45s
WRITE_INTERVAL = int(os.getenv("WIKI_WRITE_INTERVAL", "60"))        # one wiki / 60s
TICK = int(os.getenv("WIKI_SCHEDULER_TICK", "5"))
AGENT_TIMEOUT = int(os.getenv("WIKI_AGENT_TIMEOUT", "600"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [wiki-scheduler] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("wiki-scheduler")


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


def main() -> None:
    log.info("waiting for API at %s ...", API_URL)
    if not wait_for_api():
        log.error("API never came up; exiting")
        sys.exit(1)
    log.info(
        "wiki scheduler ready (cron=%ss maintain=%ss write=%ss)",
        CRON_INTERVAL, MAINTAIN_INTERVAL, WRITE_INTERVAL,
    )

    next_cron = 0.0
    next_maintain = 0.0
    next_write = 0.0

    while True:
        now = time.time()
        try:
            if now >= next_cron:
                res = _post("/api/v1/wiki/cron", timeout=60)
                if res:
                    log.info("cron: %s", res)
                next_cron = now + CRON_INTERVAL

            if now >= next_maintain:
                res = _post("/api/v1/wiki/maintain", timeout=AGENT_TIMEOUT)
                if res and res.get("claimed"):
                    log.info("maintain: %s", res.get("result"))
                next_maintain = now + MAINTAIN_INTERVAL

            if now >= next_write:
                res = _post("/api/v1/wiki/write", timeout=AGENT_TIMEOUT)
                if res and res.get("written"):
                    log.info("write: wiki=%s mode=%s rev=%s",
                             res.get("wiki_id"), res.get("mode"), res.get("revision"))
                next_write = now + WRITE_INTERVAL
        except Exception as e:
            log.exception("loop error: %s", e)
        time.sleep(TICK)


if __name__ == "__main__":
    main()
