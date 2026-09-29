#!/usr/bin/env sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
mkdir -p runtime/backups
docker compose exec -T app python object/maintenance.py \
  --data-dir /data backup --output /backups
