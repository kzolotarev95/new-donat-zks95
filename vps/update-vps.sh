#!/bin/sh
set -eu
APP_DIR="/opt/new-donat-zks95"
RAW="https://raw.githubusercontent.com/kzolotarev95/new-donat-zks95/main/update.sh"
if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo: curl ... | sudo sh" >&2; exit 1; fi
test -d "$APP_DIR" || { echo "Application is not installed: $APP_DIR" >&2; exit 1; }
TMP="$(mktemp)"
trap 'rm -f "$TMP"' 0 HUP INT TERM
curl -fsSL "$RAW?v=$(date +%s)" -o "$TMP"
install -m 0755 "$TMP" "$APP_DIR/update.sh"
exec "$APP_DIR/update.sh" </dev/tty
