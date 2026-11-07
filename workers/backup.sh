#!/bin/bash
# Nightly pg_dump of the sports database (the `backup` service of docker-compose.yml, image postgres:16 - the
# same pg_dump version as the server). Dumps to /backups (host /srv/sports/backups) at BACKUP_AT UTC, keeps the
# last BACKUP_KEEP; a dump is written as .part and renamed only when complete. On start, dumps at once if the
# newest backup is older than a day.
set -u
KEEP=${BACKUP_KEEP:-7}
AT=${BACKUP_AT:-03:30}

dump() {
  local f
  f=/backups/sports_$(date -u +%Y%m%d_%H%M).dump
  if pg_dump -h postgres -U sa -d sports -Fc -Z 6 -f "$f.part"; then
    mv "$f.part" "$f"
    echo "$(date -u '+%F %T') backup $f $(du -h "$f" | cut -f1)"
  else
    rm -f "$f.part"
    echo "$(date -u '+%F %T') backup FAILED"
  fi
  ls -1t /backups/sports_*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
}

newest=$(ls -1t /backups/sports_*.dump 2>/dev/null | head -1)
if [ -z "$newest" ] || [ $(( $(date +%s) - $(stat -c %Y "$newest") )) -gt 86400 ]; then
  dump
fi
while true; do
  now=$(date -u +%s)
  next=$(date -u -d "today $AT" +%s)
  [ "$next" -le "$now" ] && next=$(date -u -d "tomorrow $AT" +%s)
  echo "$(date -u '+%F %T') next backup at $(date -u -d "@$next" '+%F %T') UTC"
  sleep $((next - now))
  dump
done
