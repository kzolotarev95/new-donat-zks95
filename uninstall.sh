#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
APP_NAME="new-donat-zks95"; APP_USER="zks95"; APP_DIR="/opt/${APP_NAME}"; DATA_DIR="/var/lib/${APP_NAME}"; ETC_DIR="/etc/${APP_NAME}"; BACKUP_DIR="/var/backups/${APP_NAME}"; SERVICE_NAME="${APP_NAME}.service"; ALERT_SERVICE_NAME="${APP_NAME}-alert.service"
fatal(){ printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
[[ "${EUID}" -eq 0 ]] || fatal "Запустите через sudo."
read -r -p "Удалить ZKS95 Developer с этого VPS? База и .env будут сохранены. Введите REMOVE для подтверждения: " CONFIRM
[[ "${CONFIRM}" == "REMOVE" ]] || { echo "Отменено."; exit 0; }
exec 9>/run/lock/new-donat-zks95-maintenance.lock
flock -n 9 || fatal "Другая установка, проверка или обновление проекта уже выполняется."
STAMP="$(date +%Y%m%d-%H%M%S)"; install -d -o root -g root -m 0700 "${BACKUP_DIR}"; BACKUP="${BACKUP_DIR}/uninstall-${STAMP}"; install -d -m 0700 "${BACKUP}"
cp -a "${DATA_DIR}/site.db" "${BACKUP}/site.db" 2>/dev/null || true
cp -a "${ETC_DIR}/.env" "${BACKUP}/.env" 2>/dev/null || true
chmod 0600 "${BACKUP}/.env" 2>/dev/null || true
REMOVE_APP_USER=0; [[ -f "${ETC_DIR}/.user-created" ]] && REMOVE_APP_USER=1
SITE_DOMAIN=""
if [[ -r "${ETC_DIR}/.env" ]]; then SITE_DOMAIN="$(awk -F= '/^SITE_DOMAIN=/{print substr($0,index($0,$2)); exit}' "${ETC_DIR}/.env" || true)"; fi
systemctl disable --now "${APP_NAME}-health.timer" "${APP_NAME}-health.service" 2>/dev/null || true
systemctl disable --now "${APP_NAME}-login-push.timer" "${APP_NAME}-login-push.service" 2>/dev/null || true
systemctl disable --now "${SERVICE_NAME}" 2>/dev/null || true; systemctl disable --now "${ALERT_SERVICE_NAME}" 2>/dev/null || true
rm -f "/etc/systemd/system/${SERVICE_NAME}" "/etc/systemd/system/${ALERT_SERVICE_NAME}" "/etc/systemd/system/${APP_NAME}-health.service" "/etc/systemd/system/${APP_NAME}-health.timer" "/etc/systemd/system/${APP_NAME}-login-push.service" "/etc/systemd/system/${APP_NAME}-login-push.timer"
systemctl daemon-reload
if [[ -n "${SITE_DOMAIN}" && -x "${APP_DIR}/scripts/remove_nginx.sh" ]]; then
  SITE_DOMAIN="${SITE_DOMAIN}" BACKUP_DIR="${BACKUP}/nginx" "${APP_DIR}/scripts/remove_nginx.sh" || echo "Nginx-конфиг не удалён автоматически; резервная копия: ${BACKUP}/nginx" >&2
fi
rm -rf "${APP_DIR}" "${DATA_DIR}" "${ETC_DIR}"
if [[ "${REMOVE_APP_USER}" -eq 1 ]] && id -u "${APP_USER}" >/dev/null 2>&1; then userdel "${APP_USER}" 2>/dev/null || true; fi
echo "Удалены только компоненты ${APP_NAME}. Бэкап сохранён: ${BACKUP}. Nginx, сертификаты и системные пакеты не удалялись."
