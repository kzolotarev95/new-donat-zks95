#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo "Virtual environment not found. Run ./install.sh first." >&2
  exit 1
fi
exec .venv/bin/python -m gunicorn --bind "${BIND:-127.0.0.1:8000}" --workers "${WORKERS:-2}" --access-logfile - --error-logfile - wsgi:app
