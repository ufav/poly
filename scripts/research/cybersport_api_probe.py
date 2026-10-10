import re
from collections import Counter

import httpx

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"}
BASE = "https://www.cybersport.ru"
c = httpx.Client(headers=UA, timeout=30)

html = c.get(f"{BASE}/matches/cs2", params={"date": "2026-10-09"}).text
scripts = sorted(set(re.findall(r"src=\"(/_nuxt/[^\"]+\.js)\"", html)))
hits = Counter()
for s in scripts:
    js = c.get(BASE + s).text
    for m in re.findall(r"[\"'`]((?:/api)?/v\d/[a-zA-Z0-9_\-/{}$.]+)", js):
        hits[m] += 1
    for m in re.findall(r"[\"'`]([a-zA-Z0-9_\-]*matches[a-zA-Z0-9_\-/]*)[\"'`]", js):
        hits["str:" + m] += 1
    for m in re.findall(r"filter\[[a-zA-Z.]+\]", js):
        hits[m] += 1
for k, v in sorted(hits.items()):
    print(v, k)

print("\n=== Nuxt state hints")
for m in sorted(set(re.findall(r"(filter\[[a-zA-Z.]+\][^\"',]{0,40})", html)))[:30]:
    print(m)
