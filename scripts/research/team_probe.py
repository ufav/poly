"""Does cybersport.ru expose team country / region via players?"""

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pmbot.cybersport import API
from pmbot.http import HttpClient

http = HttpClient(min_interval=0.3)
d = http.get_json(f"{API}/teams/10006924")
print(Counter(x["type"] for x in d.get("included", [])))
for t in ("team_player", "player", "country", "organization"):
    for x in [x for x in d.get("included", []) if x["type"] == t][:2]:
        print(t, json.dumps(x.get("attributes"), ensure_ascii=False)[:500])
        print("   rels:", json.dumps(x.get("relationships"), ensure_ascii=False)[:300])
