"""Import football from the local Olimp results archive (monthly xlsx) into data/football.sqlite (ol_matches,
ol_stats). Top-level *.xlsx of the folder in name order, errors_result.xlsx last (corrected rows win); subfolders and
total.xlsx (copies / merges) are skipped. A re-run skips files already imported unless their size changed.

  python -u scripts/import_olimp_football.py [--dir D:/PycharmProjects/olimp] [--refresh]
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import footballdata as fd  # noqa: E402
from pmbot import olimp_archive as oa  # noqa: E402
from pmbot.tennisdata import read_xlsx  # noqa: E402

SKIP = {"errors.xlsx"}
LAST = "errors_result.xlsx"


def bar(done: int, total: int, width: int = 30) -> str:
    filled = int(width * done / total) if total else width
    return f"[{'#' * filled}{'.' * (width - filled)}] {100 * done / total if total else 100:5.1f}%"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="D:/PycharmProjects/olimp")
    ap.add_argument("--refresh", action="store_true", help="re-import files already imported")
    args = ap.parse_args()

    folder = Path(args.dir)
    paths = sorted(p for p in folder.glob("*.xlsx") if p.name not in SKIP and p.name != LAST)
    if (folder / LAST).exists():
        paths.append(folder / LAST)
    conn = fd.connect()
    oa.connect_tables(conn)
    known = dict(conn.execute("SELECT name, size FROM ol_files"))
    todo = [p for p in paths if args.refresh or known.get(p.name) != p.stat().st_size]
    total, size_total = len(todo), sum(p.stat().st_size for p in todo)
    print(f"Olimp archive {folder} -> {fd.DB}: {total} files ({size_total / 1e6:.0f} MB), "
          f"{len(paths) - total} already imported", flush=True)

    t0, size_done, n_matches, n_stats = time.time(), 0, 0, 0
    for i, p in enumerate(todo, 1):
        try:
            matches, stats = [], []
            for row in read_xlsx(p.read_bytes()):
                got = oa.classify(row)
                if got:
                    (matches if got[0] == "match" else stats).append(got[1])
            oa.save(conn, matches, stats)
            with conn:
                conn.execute("INSERT OR REPLACE INTO ol_files VALUES (?,?,?,?,?)",
                             (p.name, p.stat().st_size, len(matches), len(stats), time.time()))
            n_matches, n_stats = n_matches + len(matches), n_stats + len(stats)
            note = f"{len(matches):6d} matches {len(stats):6d} stats"
        except Exception as e:  # keep going; the file is retried next run
            note = f"ERROR {e!r:.80}"
        size_done += p.stat().st_size
        elapsed = time.time() - t0
        eta = elapsed / size_done * (size_total - size_done) if size_done else 0
        print(f"{bar(size_done, size_total)} {i:3d}/{total}  {p.name:<20} {note}   "
              f"ETA {int(eta // 60):3d}m{int(eta % 60):02d}s", flush=True)

    m, s, first, last = conn.execute(
        "SELECT (SELECT COUNT(*) FROM ol_matches), (SELECT COUNT(*) FROM ol_stats), "
        "(SELECT MIN(dt) FROM ol_matches), (SELECT MAX(dt) FROM ol_matches)").fetchone()
    print(f"\ndone in {(time.time() - t0) / 60:.1f} min: {n_matches} match rows, {n_stats} stat rows read")
    print(f"database: {m} matches, {s} stats, {first} .. {last}")


if __name__ == "__main__":
    main()
