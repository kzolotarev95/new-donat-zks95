#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo "Virtual environment not found. Run ./install.sh first." >&2
  exit 1
fi
exec .venv/bin/python -m gunicorn --config config/gunicorn.conf.py --bind "${BIND:-127.0.0.1:8015}" --workers "${WORKERS:-2}" wsgi:app
