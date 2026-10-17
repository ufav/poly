"""tennis-data.co.uk ATP / WTA yearly results -> data/tennis.sqlite (td_matches). Re-running refreshes the files.

  python scripts/collect_tennisdata.py [--years 2025 2026]
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import storage, tennisdata  # noqa: E402
from pmbot.http import HttpClient  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=[2025, 2026])
    args = ap.parse_args()
    conn = storage.connect(storage.db_path("tennis"))
    http = HttpClient(min_interval=2, retries=3, timeout=120)
    try:
        links = tennisdata.file_links(http)
        jobs = [(tour, y) for y in args.years for tour in ("atp", "wta")]
        for i, (tour, year) in enumerate(jobs, 1):
            url = links.get((tour, year))
            if not url:
                print(f"[{i}/{len(jobs)}] {tour} {year}: файла нет на сайте", flush=True)
                continue
            t0 = time.monotonic()
            r = http.get(url)
            r.raise_for_status()
            rows = tennisdata.read_xlsx(r.content)
            n = tennisdata.save_rows(conn, tour, rows)
            print(f"[{i}/{len(jobs)}] {tour} {year}: {len(r.content) / 1e3:,.0f} КБ, матчей {n} "
                  f"({time.monotonic() - t0:.0f} с)", flush=True)
    finally:
        http.close()
    for tour, n, first, last in conn.execute("SELECT tour, COUNT(*), MIN(day), MAX(day) FROM td_matches GROUP BY tour"):
        print(f"в базе {tour}: {n} матчей, {first} … {last}", flush=True)


if __name__ == "__main__":
    main()
