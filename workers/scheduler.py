"""Worker: applies migrations, then runs scheduled jobs forever. Each job is a separate process (python -m ...), the
jobs run in parallel (a long night collection does not stop the 10-minute order book snapshots), one run of a job
at a time; a crash or a hang of one job does not take the worker down; a failed run is retried after RETRY_MIN
minutes, a run longer than its timeout is killed. Times are UTC.

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
    # those days -> matches of 5 sports, then Polymarket matches / teams linked to them by vote
    Job("olimpbet_normalize", ["data_engine.normalization.olimpbet_results", "--days", "5"], at=dt.time(4, 0),
        timeout_min=60),
    # Polymarket match markets of the target series + order book snapshots (the venue's price history)
    Job("polymarket_prices", ["data_engine.providers.polymarket_prices"], every_min=10, timeout_min=9),
    # minute price series of the games that ended (the venue's history: no bid / ask, all lines and in play)
    Job("polymarket_history", ["data_engine.providers.polymarket_history", "--days", "3"], at=dt.time(5, 0),
        timeout_min=120),
]


def start(job: Job) -> subprocess.Popen:
    log.info("start %s", job.name)
    return subprocess.Popen([sys.executable, "-u", "-m", *job.args])


def run(job: Job) -> bool:
    """Run one job to the end (--once)."""
    proc = start(job)
    try:
        return proc.wait(timeout=job.timeout_min * 60) == 0
    except subprocess.TimeoutExpired:
        proc.kill()
        log.error("%s killed after %d min", job.name, job.timeout_min)
        return False


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
    running: dict[str, tuple[subprocess.Popen, float]] = {}  # jobs run in parallel, one run of a job at a time
    while True:
        now = dt.datetime.now(dt.timezone.utc)
        for job in JOBS:
            if job.name in running:
                proc, t0 = running[job.name]
                minutes = (time.time() - t0) / 60
                if proc.poll() is None and minutes > job.timeout_min:
                    proc.kill()
                    proc.wait()
                    log.error("%s killed after %d min", job.name, job.timeout_min)
                if proc.poll() is None:
                    continue
                ok = proc.returncode == 0
                del running[job.name]
                log.info("%s %s in %.1f min", job.name, "done" if ok else "FAILED", minutes)
                due[job.name] = next_run(job, now) if ok else now + dt.timedelta(minutes=RETRY_MIN)
                log.info("next %s at %s", job.name, due[job.name].isoformat())
            if now >= due[job.name]:
                running[job.name] = (start(job), time.time())
        time.sleep(15)


if __name__ == "__main__":
    main()
