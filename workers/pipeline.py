"""Hourly collection pipeline: the steps run in order (fetch -> normalize -> link -> dedupe), each as a separate
process with its own timeout; a failed step is logged and the next ones still run (the next hour retries it).
Every source is incremental, so an hour without news costs a few requests:

  - Olimpbet results of today + yesterday (5 sports) -> matches;
  - ESPN today + yesterday (NBA, WNBA, NHL, European cups, top football leagues): box scores, lineups and player
    stats of the games that just ended; NBA / WNBA / NHL injury reports (changes only);
  - football-data files by ETag (304 when the site did not update);
  - tennis-data only when the download page's 'Updated' date changed;
  - Polymarket price history of the games that ended in the last 2 days;
  - Polymarket matches / teams linked to the results, duplicates of the last week merged;
  - the paper bot (decision_engine/paper_bot.py): settle, predict, news check, bets, Telegram.

  python -m workers.pipeline
"""

from __future__ import annotations

import datetime as dt
import logging
import subprocess
import sys
import time

from workers import alerts

log = logging.getLogger("pipeline")


def steps() -> list[tuple[str, list[str], int]]:
    week_ago = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    return [  # (name, python -m args, timeout minutes)
        ("olimpbet", ["data_engine.providers.olimpbet_results", "--recent", "2"], 20),
        ("olimpbet_normalize", ["data_engine.normalization.olimpbet_results", "--days", "2", "--no-links"], 15),
        ("espn", ["data_engine.providers.espn", "--recent", "2"], 20),
        ("espn_injuries", ["data_engine.providers.espn_injuries"], 5),
        ("football_data", ["data_engine.providers.football_data"], 10),
        ("tennis_data", ["data_engine.providers.tennis_data", "--if-updated"], 15),
        ("polymarket_history", ["data_engine.providers.polymarket_history", "--days", "2"], 45),
        ("polymarket_links", ["data_engine.normalization.polymarket_links"], 10),
        ("dedupe", ["data_engine.normalization.dedupe", "--since", week_ago, "--vote-source", "9"], 10),
        # paper bets: settle, predict the next 24 h (tennis), news check of the shortlist, parlays / singles, Telegram
        ("paper_bot", ["decision_engine.paper_bot"], 30),
    ]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    failed = []
    t_all = time.time()
    for name, args, timeout in steps():
        t0 = time.time()
        try:
            ok = subprocess.run([sys.executable, "-u", "-m", *args], timeout=timeout * 60).returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
            log.error("%s killed after %d min", name, timeout)
        log.info("%s %s in %.1f min", name, "done" if ok else "FAILED", (time.time() - t0) / 60)
        alerts.track(f"шаг {name}", ok)
        if not ok:
            failed.append(name)
    log.info("pipeline %s in %.1f min", f"failed steps: {failed}" if failed else "ok", (time.time() - t_all) / 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
