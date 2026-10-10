"""How well does the name+date join reproduce the exact external_id join, and how many matches it adds.
Aliases are learned from the same matches, so 'agree' is optimistic for teams never seen with stats."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot import storage  # noqa: E402
from pmbot.join import link_matches, norm_team  # noqa: E402

conn = storage.connect()
by_game = {g: e for e, g in conn.execute("SELECT id, game_id FROM pm_events WHERE game_id IS NOT NULL")}
exact = {cs: by_game[ext] for cs, ext in conn.execute("SELECT id, external_id FROM cs_matches WHERE external_id IS NOT NULL")
         if ext in by_game}

by_name = link_matches(conn, use_external=False)

agree = sum(1 for cs, e in exact.items() if by_name.get(cs) == e)
wrong = [(cs, e, by_name[cs]) for cs, e in exact.items() if cs in by_name and by_name[cs] != e]
print(f"exact links: {len(exact)}  name join found: {sum(cs in by_name for cs in exact)}  agree: {agree}  wrong: {len(wrong)}")
total = conn.execute("SELECT COUNT(*) FROM cs_matches").fetchone()[0]
print(f"all cs matches: {total}  linked by names: {len(by_name)}  combined: {len(link_matches(conn))}")

for cs, e, got in wrong[:12]:
    t1, t2, d = conn.execute("SELECT team1, team2, date FROM cs_matches WHERE id=?", (cs,)).fetchone()
    exp = conn.execute("SELECT team_a, team_b, start_time FROM pm_events WHERE id=?", (e,)).fetchone()
    bad = conn.execute("SELECT team_a, team_b, start_time FROM pm_events WHERE id=?", (got,)).fetchone()
    print(f"  wrong: cs [{t1} | {t2} {d}]  exact {exp}  got {bad}")
missed = [cs for cs in exact if cs not in by_name][:15]
for cs in missed:
    t1, t2 = conn.execute("SELECT team1, team2 FROM cs_matches WHERE id=?", (cs,)).fetchone()
    a, b = conn.execute("SELECT team_a, team_b FROM pm_events WHERE id=?", (exact[cs],)).fetchone()
    print(f"  miss: cs [{t1} | {t2}] -> {norm_team(t1)}/{norm_team(t2)}   pm [{a} | {b}] -> {norm_team(a)}/{norm_team(b)}")
