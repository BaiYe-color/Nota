#!/usr/bin/env bash
# Launch Nota as a local desktop-style application on macOS or Linux.
set -euo pipefail

cd "$(cd -- "$(dirname -- "$0")" && pwd)"
url="http://127.0.0.1:7860/"
python=".venv/bin/python"

if [[ ! -x "$python" ]] || ! "$python" -V >/dev/null 2>&1; then
  ./setup.sh
fi

if ! curl --silent --fail --max-time 1 "$url/api/health" >/dev/null 2>&1; then
  mkdir -p object/data
  nohup "$python" object/server.py >object/data/nota.log 2>&1 &
  for _ in {1..67}; do
    if curl --silent --fail --max-time 1 "$url/api/health" >/dev/null 2>&1; then
      break
    fi
    sleep 0.3
  done
fi

if ! curl --silent --fail --max-time 1 "$url/api/health" >/dev/null 2>&1; then
  echo "Nota service did not start within 20 seconds."
  echo "Run .venv/bin/python object/server.py to see the error."
  exit 1
fi

if command -v open >/dev/null 2>&1; then
  open "$url"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$url" >/dev/null 2>&1 &
else
  echo "Nota is running at $url"
fi
