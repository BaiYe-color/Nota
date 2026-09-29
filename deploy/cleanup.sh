#!/usr/bin/env sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
docker compose exec -T app python object/maintenance.py \
  --data-dir /data clean --cache-days "${NOTA_CACHE_RETENTION_DAYS:-14}" \
  --artifact-days "${NOTA_ARTIFACT_RETENTION_DAYS:-30}" \
  --max-cache-artifacts-mb "${NOTA_CACHE_ARTIFACT_MAX_MB:-4096}" "$@"
