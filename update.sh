#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
APP_NAME="new-donat-zks95"; APP_USER="zks95"; APP_DIR="/opt/${APP_NAME}"; DATA_DIR="/var/lib/${APP_NAME}"; ETC_DIR="/etc/${APP_NAME}"; BACKUP_DIR="/var/backups/${APP_NAME}"; SERVICE_NAME="${APP_NAME}.service"; APP_PORT="8015"; HEALTH_URL="http://127.0.0.1:${APP_PORT}/healthz"
log(){ printf '\033[1;33m[update]\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m[warning]\033[0m %s\n' "$*" >&2; }
fatal(){ printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
[[ "${EUID}" -eq 0 ]] || fatal "Запустите через sudo."
exec 9>/run/lock/new-donat-zks95-maintenance.lock
flock -n 9 || fatal "Другая установка, проверка или обновление проекта уже выполняется."
[[ -d "${APP_DIR}/.git" ]] || fatal "${APP_DIR} не является Git checkout. Переустановите приложение."
git config --global --add safe.directory "${APP_DIR}" 2>/dev/null || true
mkdir -p "${BACKUP_DIR}"; chmod 700 "${BACKUP_DIR}"
STAMP="$(date +%Y%m%d-%H%M%S)"; BACKUP="${BACKUP_DIR}/update-${STAMP}"; mkdir -p "${BACKUP}"
cp -a "${DATA_DIR}/site.db" "${BACKUP}/site.db" 2>/dev/null || true
cp -a "${ETC_DIR}/.env" "${BACKUP}/.env" 2>/dev/null || true
chmod 0600 "${BACKUP}/.env" 2>/dev/null || true
cd "${APP_DIR}"
OLD_REV="$(git rev-parse HEAD)"; git fetch --prune origin
BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo main)"; REMOTE_REF="origin/${BRANCH}"
git rev-parse --verify "${REMOTE_REF}" >/dev/null 2>&1 || REMOTE_REF="origin/main"
NEW_REV="$(git rev-parse "${REMOTE_REF}")"
if [[ "${OLD_REV}" != "${NEW_REV}" ]]; then
  log "Обновляю ${OLD_REV:0:8} → ${NEW_REV:0:8}"
  git archive --format=tar --output="${BACKUP}/current-${OLD_REV:0:8}.tar" "${OLD_REV}"
  if ! git pull --ff-only origin "${BRANCH}"; then fatal "Fast-forward невозможен; локальные изменения не перезаписаны."; fi
else
  log "Обновлений нет (${OLD_REV:0:8}); выполняю проверку и ремонт runtime."
fi
SERVICE_CHANGED=0
repair_runtime(){
  [[ -r "${ETC_DIR}/.env" ]] || fatal "Не найден ${ETC_DIR}/.env; настройки сохранены, ремонт остановлен."
  chown root:root "${ETC_DIR}/.env"; chmod 0600 "${ETC_DIR}/.env"
  install -d -o "${APP_USER}" -g "${APP_USER}" -m 0750 "${DATA_DIR}" "/var/log/${APP_NAME}"
  runuser -u "${APP_USER}" -- "${APP_DIR}/.venv/bin/pip" install -r requirements.txt || return
  DB_PATH="$(awk -F= '/^DATABASE_PATH=/{print substr($0,index($0,$2)); exit}' "${ETC_DIR}/.env")"; [[ -n "${DB_PATH}" ]] || DB_PATH="${DATA_DIR}/site.db"
  runuser -u "${APP_USER}" -- env PYTHONPATH="${APP_DIR}" DATABASE_PATH="${DB_PATH}" "${APP_DIR}/.venv/bin/python" - <<'PY' || return
from app import create_app
from app.db import get_db
app = create_app()
with app.app_context():
    get_db().execute("SELECT 1").fetchone()
print("database ready")
PY
  cmp -s "${APP_DIR}/config/systemd/${SERVICE_NAME}" "/etc/systemd/system/${SERVICE_NAME}" || SERVICE_CHANGED=1
  for UNIT_FILE in "${APP_DIR}"/config/systemd/*.service "${APP_DIR}"/config/systemd/*.timer; do
    [[ -f "${UNIT_FILE}" ]] || continue
    install -m 0644 "${UNIT_FILE}" "/etc/systemd/system/$(basename "${UNIT_FILE}")" || return
  done
  systemctl daemon-reload || return
  systemctl enable "${SERVICE_NAME}" || return
}
if ! repair_runtime; then
  if [[ "${OLD_REV}" != "${NEW_REV}" ]]; then git reset --hard "${OLD_REV}" || true; fi
  fatal "Подготовка runtime не прошла; откат кода выполнен при необходимости. Бэкап: ${BACKUP}"
fi
SITE_DOMAIN="$(awk -F= '/^SITE_DOMAIN=/{print substr($0,index($0,$2)); exit}' "${ETC_DIR}/.env")"
if [[ -z "${SITE_DOMAIN}" ]]; then fatal "SITE_DOMAIN отсутствует в .env."; fi
NGINX_BACKUP="${BACKUP}/nginx"
if ! env SITE_DOMAIN="${SITE_DOMAIN}" APP_DIR="${APP_DIR}" APP_PORT="${APP_PORT}" BACKUP_DIR="${NGINX_BACKUP}" "${APP_DIR}/scripts/reconcile_nginx.sh"; then
  if [[ "${OLD_REV}" != "${NEW_REV}" ]]; then git reset --hard "${OLD_REV}"; repair_runtime || true; fi
  fatal "Согласование Nginx не прошло; чужие конфигурации не изменены. Бэкап: ${BACKUP}"
fi
SYSTEMD_ACTION=reload-or-restart
if [[ "${SERVICE_CHANGED}" -eq 1 ]]; then SYSTEMD_ACTION=restart; fi
if ! systemctl "${SYSTEMD_ACTION}" "${SERVICE_NAME}"; then
  if [[ "${OLD_REV}" != "${NEW_REV}" ]]; then git reset --hard "${OLD_REV}"; repair_runtime || true; systemctl restart "${SERVICE_NAME}" || true; fi
  fatal "Сервис не запустился; выполнен откат при необходимости."
fi
sleep 2
if ! curl --fail --silent --connect-timeout 2 --max-time 5 --retry 4 --retry-delay 1 --retry-connrefused "${HEALTH_URL}" >/dev/null || ! ss -lnt | grep -q ":${APP_PORT}[[:space:]]"; then
  if [[ "${OLD_REV}" != "${NEW_REV}" ]]; then git reset --hard "${OLD_REV}"; repair_runtime || true; systemctl restart "${SERVICE_NAME}" || true; fi
  fatal "Health check/порт не прошёл; выполнен откат кода при необходимости."
fi
if [[ -f "${APP_DIR}/config/systemd/${APP_NAME}-health.timer" ]]; then
  systemctl enable --now "${APP_NAME}-health.timer"
fi
log "Готово. Сервис слушает 127.0.0.1:${APP_PORT}; бэкап: ${BACKUP}"
