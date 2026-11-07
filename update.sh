#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
APP_NAME="new-donat-zks95"; APP_DIR="/opt/${APP_NAME}"; DATA_DIR="/var/lib/${APP_NAME}"; ETC_DIR="/etc/${APP_NAME}"; BACKUP_DIR="/var/backups/${APP_NAME}"; SERVICE_NAME="${APP_NAME}.service"; HEALTH_URL="http://127.0.0.1:8015/healthz"
log(){ printf '\033[1;33m[update]\033[0m %s\n' "$*"; }
fatal(){ printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
[[ "${EUID}" -eq 0 ]] || fatal "Запустите через sudo."
[[ -d "${APP_DIR}/.git" ]] || fatal "${APP_DIR} не является Git checkout. Переустановите приложение."
cd "${APP_DIR}"
mkdir -p "${BACKUP_DIR}"; chmod 700 "${BACKUP_DIR}"
STAMP="$(date +%Y%m%d-%H%M%S)"; BACKUP="${BACKUP_DIR}/update-${STAMP}"; mkdir -p "${BACKUP}"
cp -a "${DATA_DIR}/site.db" "${BACKUP}/site.db" 2>/dev/null || true; cp -a "${ETC_DIR}/.env" "${BACKUP}/.env" 2>/dev/null || true
OLD_REV="$(git rev-parse HEAD)"; git fetch --prune origin
git archive --format=tar --output="${BACKUP}/current-${OLD_REV:0:8}.tar" "${OLD_REV}"
BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo main)"; REMOTE_REF="origin/${BRANCH}"
if ! git rev-parse --verify "${REMOTE_REF}" >/dev/null 2>&1; then REMOTE_REF="origin/main"; fi
NEW_REV="$(git rev-parse "${REMOTE_REF}")"
if [[ "${OLD_REV}" == "${NEW_REV}" ]]; then log "Обновлений нет (${OLD_REV:0:8})."; exit 0; fi
log "Обновляю ${OLD_REV:0:8} → ${NEW_REV:0:8}"
if ! git pull --ff-only origin "${BRANCH}"; then fatal "Fast-forward невозможен; локальные изменения сохранены."; fi
if ! runuser -u zks95 -- "${APP_DIR}/.venv/bin/pip" install -r requirements.txt; then git reset --hard "${OLD_REV}"; fatal "Зависимости не установились, выполнен откат."; fi
install -d -o zks95 -g zks95 -m 0755 "/var/log/${APP_NAME}"
DB_PATH="$(awk -F= '/^DATABASE_PATH=/{print substr($0, index($0,$2))}' "${ETC_DIR}/.env")"
if ! runuser -u zks95 -- env DATABASE_PATH="${DB_PATH}" "${APP_DIR}/.venv/bin/python" -c 'from app import create_app; app=create_app(); print("database ready")'; then git reset --hard "${OLD_REV}"; fatal "Миграция базы не прошла, выполнен откат коду."; fi
install -m 0644 "${APP_DIR}/config/systemd/${SERVICE_NAME}" "/etc/systemd/system/${SERVICE_NAME}"
systemctl daemon-reload
if ! systemctl restart "${SERVICE_NAME}"; then git reset --hard "${OLD_REV}"; systemctl restart "${SERVICE_NAME}" || true; fatal "Сервис не запустился, выполнен откат."; fi
sleep 2
if ! curl --fail --silent "${HEALTH_URL}" >/dev/null; then git reset --hard "${OLD_REV}"; systemctl restart "${SERVICE_NAME}" || true; fatal "Health check не пройден, выполнен откат коду."; fi
log "Готово. Резервная копия: ${BACKUP}"
