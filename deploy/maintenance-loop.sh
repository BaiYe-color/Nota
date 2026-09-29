#!/usr/bin/env sh
# Runs inside the maintenance container. It never deletes uploads or notes.
set -eu

interval="${NOTA_MAINTENANCE_INTERVAL_SECONDS:-86400}"
cache_days="${NOTA_CACHE_RETENTION_DAYS:-14}"
artifact_days="${NOTA_ARTIFACT_RETENTION_DAYS:-30}"
quota_mb="${NOTA_CACHE_ARTIFACT_MAX_MB:-4096}"

clean() {
  python /app/object/maintenance.py --data-dir "${NOTA_DATA_DIR:-/data}" clean \
    --cache-days "$cache_days" --artifact-days "$artifact_days" \
    --max-cache-artifacts-mb "$quota_mb"
}

clean
while :; do
  sleep "$interval"
  clean
done
