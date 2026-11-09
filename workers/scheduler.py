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

from workers import alerts

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
    # hourly: Olimpbet / ESPN / football-data / tennis-data / Polymarket price history -> matches, links, dedupe
    Job("pipeline", ["workers.pipeline"], every_min=60, timeout_min=120),
    # Polymarket match markets of the target series + order book snapshots (bid / ask / size)
    Job("polymarket_prices", ["data_engine.providers.polymarket_prices"], every_min=10, timeout_min=9),
    # nightly repair: any Olimpbet day of the ~14 months the site keeps scores for that is still missing
    Job("olimpbet_backfill", ["data_engine.providers.olimpbet_results", "--backfill"], at=dt.time(2, 0),
        timeout_min=240),
    # the paper bets summary to Telegram (22:00 Almaty)
    Job("paper_report", ["decision_engine.paper_bot", "--send-report"], at=dt.time(17, 0), timeout_min=5),
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
                if job.name != "pipeline":  # the pipeline alerts per step itself
                    alerts.track(f"задача {job.name}", ok, threshold=6 if job.every_min and job.every_min < 30 else 3)
                log.info("%s %s in %.1f min", job.name, "done" if ok else "FAILED", minutes)
                due[job.name] = next_run(job, now) if ok else now + dt.timedelta(minutes=RETRY_MIN)
                log.info("next %s at %s", job.name, due[job.name].isoformat())
            if now >= due[job.name]:
                running[job.name] = (start(job), time.time())
        time.sleep(15)


if __name__ == "__main__":
    main()
