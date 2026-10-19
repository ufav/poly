#!/usr/bin/env bash
# Snapshot all paper live DBs and send .gz to Telegram.
# On the server:  cd ~/polymarket && bash scripts/dump_live_tg.sh
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose exec -T paper-bot python - <<'PY'
import sqlite3
from pathlib import Path

pairs = [
    ("data/live.sqlite", "data/live_copy.sqlite"),
    ("data/live_dota2.sqlite", "data/live_dota2_copy.sqlite"),
    ("data/live_tennis.sqlite", "data/live_tennis_copy.sqlite"),
]
for src, dst in pairs:
    sp, dp = Path(src), Path(dst)
    if not sp.exists():
        print(f"skip {src} (missing)")
        continue
    if dp.exists():
        dp.unlink()
    dst_conn = sqlite3.connect(dst)
    sqlite3.connect(f"file:{src}?mode=ro", uri=True).backup(dst_conn)
    dst_conn.close()
    print(f"ok {dst} ({sp.stat().st_size} bytes)")
PY

TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2- | tr -d '\r')
CHAT=$(grep '^TELEGRAM_CHAT_ID=' .env | cut -d= -f2- | tr -d '\r')
if [ -z "$TOKEN" ] || [ -z "$CHAT" ]; then
  echo "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env" >&2
  exit 1
fi
for f in data/live_copy.sqlite data/live_dota2_copy.sqlite data/live_tennis_copy.sqlite; do
  [ -f "$f" ] || continue
  gzip -f "$f"
  gz="${f}.gz"
  echo -n "$(basename "$gz") ($(du -h "$gz" | cut -f1)): "
  resp=$(curl -s -F chat_id="$CHAT" -F document=@"$gz" -F caption="$(basename "$gz")" \
    "https://api.telegram.org/bot${TOKEN}/sendDocument")
  echo "$resp" | grep -q '"ok":true' && echo OK || echo "FAIL $resp"
done
