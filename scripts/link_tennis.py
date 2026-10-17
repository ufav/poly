"""Link tennis sources in data/tennis.sqlite: Polymarket ↔ Olimpbet ↔ tennis-data (pmbot/tennis_join.py).

Rebuilds tennis_links every run (features: surface, home/away, jetlag, fatigue, ranks, odds).

  python scripts/link_tennis.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot import storage, tennis_join  # noqa: E402


def main() -> None:
    conn = storage.connect(storage.db_path("tennis"))
    print("linking…", flush=True)
    stats = tennis_join.save_links(conn)
    n_pm = conn.execute("SELECT COUNT(*) FROM pm_events").fetchone()[0]
    n_pm1k = conn.execute("SELECT COUNT(*) FROM pm_events WHERE volume >= 1000").fetchone()[0]
    n_ob = conn.execute(
        "SELECT COUNT(*) FROM tennis_links WHERE ob_event_id IS NOT NULL"
    ).fetchone()[0]
    n_ob1k = conn.execute(
        "SELECT COUNT(*) FROM tennis_links WHERE ob_event_id IS NOT NULL AND volume >= 1000"
    ).fetchone()[0]
    n_td = stats["with_td"]
    n_both = stats["with_both"]
    print(f"Polymarket events: {n_pm:,}  (≥$1k: {n_pm1k:,})", flush=True)
    print(f"linked: {stats['links']:,}  with Olimpbet: {n_ob:,} ({n_ob1k:,} ≥$1k)  "
          f"with tennis-data: {n_td:,}  both: {n_both:,}", flush=True)
    for label, where in (
        ("exact/soft OB methods", "method LIKE 'exact%' OR method LIKE 'soft%'"),
        ("with surface", "surface IS NOT NULL"),
        ("with jetlag (A)", "a_jetlag IS NOT NULL"),
        ("with ranks", "a_rank IS NOT NULL"),
        ("home player A", "a_home = 1"),
    ):
        c = conn.execute(f"SELECT COUNT(*) FROM tennis_links WHERE {where}").fetchone()[0]
        print(f"  {label}: {c:,}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
