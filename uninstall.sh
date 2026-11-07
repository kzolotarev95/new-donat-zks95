#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
APP_NAME="new-donat-zks95"; APP_DIR="/opt/${APP_NAME}"; DATA_DIR="/var/lib/${APP_NAME}"; ETC_DIR="/etc/${APP_NAME}"; BACKUP_DIR="/var/backups/${APP_NAME}"; SERVICE_NAME="${APP_NAME}.service"; NGINX_AVAILABLE="/etc/nginx/sites-available/${APP_NAME}.conf"; NGINX_ENABLED="/etc/nginx/sites-enabled/${APP_NAME}.conf"
[[ "${EUID}" -eq 0 ]] || { echo "Запустите через sudo." >&2; exit 1; }
read -r -p "Удалить ZKS95 Developer с этого VPS? Данные и .env будут сохранены в backup. Введите REMOVE для подтверждения: " CONFIRM
[[ "${CONFIRM}" == "REMOVE" ]] || { echo "Отменено."; exit 0; }
STAMP="$(date +%Y%m%d-%H%M%S)"; BACKUP="${BACKUP_DIR}/uninstall-${STAMP}"; install -d -m 700 "${BACKUP}"
cp -a "${DATA_DIR}/site.db" "${BACKUP}/site.db" 2>/dev/null || true
cp -a "${ETC_DIR}/.env" "${BACKUP}/.env" 2>/dev/null || true
systemctl disable --now "${SERVICE_NAME}" 2>/dev/null || true
rm -f "/etc/systemd/system/${SERVICE_NAME}"; systemctl daemon-reload
if [[ -L "${NGINX_ENABLED}" && "$(readlink -f "${NGINX_ENABLED}")" == "${NGINX_AVAILABLE}" ]]; then rm -f "${NGINX_ENABLED}"; fi
rm -f "${NGINX_AVAILABLE}"; nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
rm -rf "${APP_DIR}" "${DATA_DIR}" "${ETC_DIR}"
if id -u zks95 >/dev/null 2>&1; then userdel zks95 2>/dev/null || true; fi
echo "Удалены только компоненты ${APP_NAME}. Бэкап сохранён: ${BACKUP}. Nginx, сертификаты и пакеты не удалялись."
