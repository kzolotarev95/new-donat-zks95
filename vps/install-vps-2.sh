#!/bin/sh
set -eu
REPO="https://github.com/kzolotarev95/new-donat-zks95.git"
TMP="$(mktemp -d /tmp/new-donat-zks95-install.XXXXXX)"
trap 'rm -rf "$TMP"' 0 HUP INT TERM
if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo: curl ... | sudo sh" >&2; exit 1; fi
export DEBIAN_FRONTEND=noninteractive
if ! command -v git >/dev/null 2>&1; then
  apt-get update
  apt-get install -y git ca-certificates curl
fi
git clone --depth 1 "$REPO" "$TMP/repo"
chmod +x "$TMP/repo/install.sh"
exec "$TMP/repo/install.sh" </dev/tty
