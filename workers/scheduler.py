"""Worker: applies migrations, then runs scheduled jobs forever. Each job is a separate process (python -m ...), so a
crash or a hang of one job does not take the worker down; a failed run is retried after RETRY_MIN minutes, a run
longer than its timeout is killed. Times are UTC.

  python -m workers.scheduler            # in the worker container
  python -m workers.scheduler --once olimpbet_daily
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import subprocess
import sys
import time
from dataclasses import dataclass

log = logging.getLogger("worker")
RETRY_MIN = 30


@dataclass
class Job:
    name: str
    args: list[str]                  # python -m <args>
    at: dt.time | None = None        # daily start time (UTC), or
    every_min: int | None = None     # a run every N minutes
    timeout_min: int = 120


JOBS = [
    # results of the last days of 5 sports + any day missing in the ~14 months the site keeps scores for
    Job("olimpbet_daily", ["data_engine.providers.olimpbet_results", "--daily"], at=dt.time(2, 0), timeout_min=240),
    # Polymarket match markets of the target series + order book snapshots (the venue's price history)
    Job("polymarket_prices", ["data_engine.providers.polymarket_prices"], every_min=10, timeout_min=9),
]


def run(job: Job) -> bool:
    log.info("start %s", job.name)
    t0 = time.time()
    try:
        proc = subprocess.run([sys.executable, "-u", "-m", *job.args], timeout=job.timeout_min * 60)
        ok = proc.returncode == 0
    except subprocess.TimeoutExpired:
        log.error("%s killed after %d min", job.name, job.timeout_min)
        ok = False
    log.info("%s %s in %.1f min", job.name, "done" if ok else "FAILED", (time.time() - t0) / 60)
    return ok


def next_run(job: Job, now: dt.datetime) -> dt.datetime:
    if job.every_min:
        return now + dt.timedelta(minutes=job.every_min)
    at = dt.datetime.combine(now.date(), job.at, tzinfo=dt.timezone.utc)
    return at if at > now else at + dt.timedelta(days=1)


def migrate() -> None:
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    subprocess.run([sys.executable, "-m", "core.seed"], check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", help="run one job now and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    migrate()
    if args.once:
        job = next(j for j in JOBS if j.name == args.once)
        sys.exit(0 if run(job) else 1)
    now = dt.datetime.now(dt.timezone.utc)
    due = {j.name: now if j.every_min else next_run(j, now) for j in JOBS}
    log.info("jobs: %s", {n: t.isoformat() for n, t in due.items()})
    while True:
        now = dt.datetime.now(dt.timezone.utc)
        for job in JOBS:
            if now >= due[job.name]:
                ok = run(job)
                now = dt.datetime.now(dt.timezone.utc)
                due[job.name] = next_run(job, now) if ok else now + dt.timedelta(minutes=RETRY_MIN)
                log.info("next %s at %s", job.name, due[job.name].isoformat())
        time.sleep(30)


if __name__ == "__main__":
    main()
