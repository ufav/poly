"""Current order-book spreads of open CS2 markets: match vs map 1 / map 2, by volume.

  python scripts/research/spread_probe.py
"""

import sys
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot.http import HttpClient  # noqa: E402
from pmbot.live import feeds  # noqa: E402


def main() -> None:
    http = HttpClient(min_interval=0.1)
    spreads = {}
    for raw in feeds.open_events(http):
        ev = feeds.parse_event(raw)
        for m in ev["markets"]:
            kind = m["market_type"] if m["market_type"] == "moneyline" else f"map{m.get('map_number')}"
            if m["market_type"] not in ("moneyline", "child_moneyline"):
                continue
            try:
                book = feeds.fetch_book(http, m["token_a"])
            except Exception:
                continue
            if book["bid"] is None or book["ask"] is None:
                spreads.setdefault(kind, []).append(None)
                continue
            spread = book["ask"] - book["bid"]
            spreads.setdefault(kind, []).append(spread)
            print(f"{ev['title'][:55]:55} {kind:9} bid {book['bid']:.3f} ask {book['ask']:.3f} "
                  f"spread {spread:.3f} sizes {book['bid_size']:.0f}/{book['ask_size']:.0f}")
    print()
    for kind, xs in sorted(spreads.items()):
        ok = [x for x in xs if x is not None]
        if ok:
            print(f"{kind:9} n={len(xs):3d} empty side {xs.count(None):3d}  median spread {median(ok):.3f}  "
                  f"<=0.02: {sum(x <= 0.02 for x in ok) / len(ok):.0%}")


if __name__ == "__main__":
    main()
