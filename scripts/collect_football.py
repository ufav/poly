"""Football results, match stats and odds from football-data.co.uk -> data/football.sqlite (fd_matches).

22 main leagues per season since 1993/94 + 16 extra countries; a re-run fetches only what is missing, plus the
current season and the extra files again (they grow during the season). Progress line per file.

  python -u scripts/collect_football.py [--since 2005] [--no-extra] [--refresh]
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import footballdata as fd  # noqa: E402
from pmbot.http import HttpClient  # noqa: E402


def bar(done: int, total: int, width: int = 30) -> str:
    filled = int(width * done / total) if total else width
    return f"[{'#' * filled}{'.' * (width - filled)}] {100 * done / total if total else 100:5.1f}%"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=fd.FIRST_SEASON, help="first season start year (default 1993)")
    ap.add_argument("--no-extra", action="store_true", help="skip the 16 extra countries")
    ap.add_argument("--refresh", action="store_true", help="re-download files already done")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between requests")
    args = ap.parse_args()

    conn = fd.connect()
    done = {p for (p,) in conn.execute("SELECT path FROM fd_files WHERE status IN ('done', 'missing')")}
    current = fd.current_season()
    todo = [f for f in fd.files(args.since, not args.no_extra)
            if args.refresh or f[0] not in done or f[2] is None or f[2] == current]
    total = len(todo)
    print(f"football-data.co.uk -> {fd.DB}: {total} files to fetch "
          f"({len(fd.files(args.since, not args.no_extra)) - total} already done)", flush=True)

    http = HttpClient(min_interval=args.interval, retries=4, timeout=60, headers={"Accept": "text/csv,*/*"})
    t0, matches, missing, errors = time.time(), 0, 0, 0
    for i, (path, div, season) in enumerate(todo, 1):
        try:
            r = http.get(f"{fd.BASE}/{path}")
            if r.status_code == 404 or not r.content.lstrip(b"\xef\xbb\xbf").startswith((b"Div", b"Country")):
                rows, status = [], "missing"  # league not covered that season
            elif r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            else:
                rows, status = fd.parse_csv(r.content, div, season), "done"
            fd.save(conn, path, rows, status, time.time())
            matches += len(rows)
            missing += status == "missing"
            note = f"{len(rows):5d} matches" if status == "done" else "  — not covered"
        except Exception as e:  # keep going; the file is retried next run
            errors += 1
            note = f"  ERROR {e!r:.80}"
        elapsed = time.time() - t0
        eta = elapsed / i * (total - i)
        print(f"{bar(i, total)} {i:4d}/{total}  {path:<24}{note}   total {matches:7d}  "
              f"ETA {int(eta // 60):3d}m{int(eta % 60):02d}s", flush=True)

    n, leagues, first, last = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT div), MIN(date), MAX(date) FROM fd_matches").fetchone()
    print(f"\ndone in {(time.time() - t0) / 60:.1f} min: +{matches} rows this run, {missing} not covered, "
          f"{errors} errors (re-run to retry)")
    print(f"database: {n} matches, {leagues} leagues, {first} .. {last}")


if __name__ == "__main__":
    main()
