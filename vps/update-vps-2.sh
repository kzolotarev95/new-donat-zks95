#!/bin/sh
set -eu
echo "new-donat-zks95 update wrapper v2"
APP_DIR="/opt/new-donat-zks95"
RAW="https://raw.githubusercontent.com/kzolotarev95/new-donat-zks95/main/update.sh"
if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo: curl ... | sudo sh" >&2; exit 1; fi
test -d "$APP_DIR" || { echo "Application is not installed: $APP_DIR" >&2; exit 1; }
TMP="$(mktemp)"
trap 'rm -f "$TMP"' 0 HUP INT TERM
curl -fsSL "$RAW?v=$(date +%s)" -o "$TMP"
STATUS="$(git -c safe.directory="$APP_DIR" -C "$APP_DIR" status --porcelain -- update.sh)"
if [ -n "$STATUS" ]; then
  BACKUP_DIR="/var/backups/new-donat-zks95"
  mkdir -p "$BACKUP_DIR"
  cp -a "$APP_DIR/update.sh" "$BACKUP_DIR/update-wrapper-$(date +%Y%m%d-%H%M%S).sh" 2>/dev/null || true
  git -c safe.directory="$APP_DIR" -C "$APP_DIR" restore --staged --worktree -- update.sh 2>/dev/null || git -c safe.directory="$APP_DIR" -C "$APP_DIR" checkout HEAD -- update.sh
fi
chmod 0755 "$TMP"
exec "$TMP" </dev/tty
