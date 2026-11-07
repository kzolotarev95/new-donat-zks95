#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="/opt/new-donat-zks95"
RAW="https://raw.githubusercontent.com/kzolotarev95/new-donat-zks95/main/uninstall.sh"
[[ "${EUID}" -eq 0 ]] || { echo "Run with sudo: curl ... | sudo sh" >&2; exit 1; }
test -d "$APP_DIR" || { echo "Application is not installed: $APP_DIR" >&2; exit 1; }
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
curl -fsSL "$RAW?v=$(date +%s)" -o "$TMP"
install -m 0755 "$TMP" "$APP_DIR/uninstall.sh"
exec "$APP_DIR/uninstall.sh" </dev/tty
