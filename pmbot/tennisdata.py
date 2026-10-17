"""tennis-data.co.uk: yearly ATP / WTA main-tour results with surface, court, round, rankings and bookmaker odds.

The yearly .xlsx links sit under a changing directory on /alldata.php ('<dir>/2026/2026.xlsx' ATP,
'<dir>/2026w/2026.xlsx' WTA). No Challengers / ITF. Read with the standard library (xlsx = zip of XML).
"""

import datetime as dt
import io
import json
import re
import sqlite3
import zipfile
from xml.etree import ElementTree as ET

from pmbot.http import HttpClient

SITE = "https://www.tennis-data.co.uk"
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
ODDS = ("B365W", "B365L", "PSW", "PSL", "MaxW", "MaxL", "AvgW", "AvgL", "BFEW", "BFEL")


def file_links(http: HttpClient) -> dict[tuple[str, int], str]:
    """(tour, year) -> absolute xlsx URL from the data page."""
    html = http.get(f"{SITE}/alldata.php").text
    out = {}
    for path, year, women in re.findall(r'href="([^"]*?/(\d{4})(w?)/\d{4}\.xlsx)"', html, re.I):
        out[("wta" if women else "atp", int(year))] = path if path.startswith("http") else f"{SITE}/{path.lstrip('/')}"
    return out


def read_xlsx(data: bytes) -> list[dict]:
    """First sheet -> list of {header: value} (strings; numbers as text)."""
    z = zipfile.ZipFile(io.BytesIO(data))
    shared = ["".join(t.text or "" for t in si.iter(f"{_NS}t"))
              for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(f"{_NS}si")]
    rows = []
    for row in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter(f"{_NS}row"):
        cells = {}
        for c in row.findall(f"{_NS}c"):
            idx = 0
            for ch in re.match(r"[A-Z]+", c.get("r")).group():
                idx = idx * 26 + ord(ch) - 64
            v = c.find(f"{_NS}v")
            if v is not None:
                cells[idx - 1] = shared[int(v.text)] if c.get("t") == "s" else v.text
        rows.append(cells)
    head = rows[0]
    return [{head[i]: (v.strip() if isinstance(v, str) else v) for i, v in r.items() if i in head}
            for r in rows[1:] if r]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def save_rows(conn: sqlite3.Connection, tour: str, rows: list[dict]) -> int:
    n = 0
    with conn:
        for r in rows:
            if not r.get("Date") or not r.get("Winner"):
                continue
            day = (dt.date(1899, 12, 30) + dt.timedelta(days=float(r["Date"]))).isoformat()
            sets = [(_num(r.get(f"W{i}")), _num(r.get(f"L{i}"))) for i in range(1, 6)]
            conn.execute(
                """INSERT OR REPLACE INTO td_matches (tour, day, location, tournament, series, court, surface, round,
                       best_of, winner, loser, wrank, lrank, wpts, lpts, set_scores, wsets, lsets, comment, odds)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tour, day, r.get("Location"), r.get("Tournament"), r.get("Series") or r.get("Tier"), r.get("Court"),
                 r.get("Surface"), r.get("Round"), _num(r.get("Best of")), r["Winner"], r.get("Loser"),
                 _num(r.get("WRank")), _num(r.get("LRank")), _num(r.get("WPts")), _num(r.get("LPts")),
                 json.dumps([s for s in sets if s[0] is not None]), _num(r.get("Wsets")), _num(r.get("Lsets")),
                 r.get("Comment"), json.dumps({k: _num(r.get(k)) for k in ODDS if _num(r.get(k)) is not None})))
            n += 1
    return n
