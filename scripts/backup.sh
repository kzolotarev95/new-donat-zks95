#!/usr/bin/env bash
set -Eeuo pipefail
DATA_DIR=/var/lib/new-donat-zks95
BACKUP_DIR=/var/backups/new-donat-zks95/manual
install -d -m 700 "$BACKUP_DIR"
STAMP=$(date +%Y%m%d-%H%M%S)
sqlite3 "$DATA_DIR/site.db" ".backup '$BACKUP_DIR/site-$STAMP.db'"
echo "Backup: $BACKUP_DIR/site-$STAMP.db"
