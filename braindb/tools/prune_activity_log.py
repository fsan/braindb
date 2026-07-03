"""
Activity-log retention sweep.

Run in the container (or via a k8s CronJob calling the same command inside
the `api` image):

    docker compose exec -T api python -m braindb.tools.prune_activity_log

`activity_log` is an append-only audit table (see alembic/versions/003) with
no upper bound of its own — on a shared Postgres instance it can eventually
fill the disk on volume alone. This tool is the only thing that deletes from
it; there's no in-process scheduler in this repo for this cadence, so it's
meant to be invoked externally on a schedule (daily is plenty given the
default 3-day age window).

Delegates all the logic to `braindb.services.activity_log.prune_activity_log`
so the HTTP-facing code and this CLI can't drift. Exits non-zero on error so
a CronJob run shows as failed.
"""
import sys

from braindb.config import settings
from braindb.db import get_conn
from braindb.services.activity_log import prune_activity_log


def main() -> int:
    with get_conn() as conn:
        result = prune_activity_log(
            conn,
            max_age_days=settings.activity_log_max_age_days,
            max_size_mb=settings.activity_log_max_size_mb,
        )

    if result["error"]:
        print(f"activity_log prune FAILED: {result['error']}", file=sys.stderr)
        return 1

    print(
        "activity_log prune OK: "
        f"age_deleted={result['age_deleted']} "
        f"size_deleted={result['size_deleted']} ({result['size_batches']} batches) "
        f"final_size_mb={result['final_size_mb']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
