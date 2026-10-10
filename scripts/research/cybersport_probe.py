import re
import sys

import httpx

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
}

match_id = sys.argv[1] if len(sys.argv) > 1 else "10099240"
html = httpx.get(f"https://www.cybersport.ru/matches/cs2/{match_id}", headers=UA, timeout=30).text

i = html.find("winner_side")
print(re.sub(r"\s+", " ", html[max(0, i - 1500) : i + 700]))
print("======")
for m in sorted(set(re.findall(r"[\"'](/api/[^\"']{3,120})[\"']", html)))[:30]:
    print("path:", m)
for m in sorted(set(re.findall(r"https?://[a-zA-Z0-9.\-]+", html)))[:60]:
    print("host:", m)

print("====== bundles")
scripts = sorted(set(re.findall(r"src=\"(/_nuxt/[^\"]+\.js)\"", html)))
print(len(scripts), "scripts")
hits = set()
for s in scripts:
    js = httpx.get("https://www.cybersport.ru" + s, headers=UA, timeout=30).text
    for m in re.findall(r"[\"'`](/api/[^\"'`]{2,100}|/v\d/[^\"'`]{2,100})[\"'`]", js):
        hits.add(m)
    for m in re.findall(r"[\"'`]([^\"'`]{0,60}(?:csgo|matchGames|winner_side|match-details)[^\"'`]{0,60})[\"'`]", js):
        hits.add("ctx: " + m)
for h in sorted(hits)[:80]:
    print(h)
