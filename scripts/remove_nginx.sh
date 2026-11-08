#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="${APP_NAME:-new-donat-zks95}"
SITE_DOMAIN="${SITE_DOMAIN:?SITE_DOMAIN is required}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/${APP_NAME}}"
NGINX_AVAILABLE="/etc/nginx/sites-available/${APP_NAME}.conf"
NGINX_ENABLED="/etc/nginx/sites-enabled/${APP_NAME}.conf"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "${BACKUP_DIR}"
matches_domain(){ grep -Eiq "server_name[[:space:]].*${SITE_DOMAIN}([;[:space:]]|$)" "$1"; }
owned_config(){ grep -Eiq 'Managed by new-donat-zks95|new-donat-zks95|ZKS95 Developer|proxy_pass[[:space:]]+http://127\.0\.0\.1:(8000|8015)' "$1"; }

shopt -s nullglob
for entry in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*; do
  [[ -e "${entry}" || -L "${entry}" ]] || continue
  target="$(readlink -f "${entry}" 2>/dev/null || printf '%s' "${entry}")"
  [[ -f "${target}" ]] || continue
  if matches_domain "${target}" && owned_config "${target}"; then
    cp -a "${entry}" "${BACKUP_DIR}/nginx-uninstall-${STAMP}-$(basename "${entry}")" 2>/dev/null || true
    rm -rf "${entry}"
  fi
done
shopt -u nullglob

if [[ -f "${NGINX_AVAILABLE}" ]] && owned_config "${NGINX_AVAILABLE}"; then
  cp -a "${NGINX_AVAILABLE}" "${BACKUP_DIR}/nginx-uninstall-${STAMP}-available.conf"
  rm -f "${NGINX_AVAILABLE}"
fi
if nginx -t >/dev/null 2>&1; then
  systemctl reload nginx || true
else
  printf '%s\n' "Nginx после удаления конфигурации не прошёл проверку; резервные копии: ${BACKUP_DIR}" >&2
  exit 1
fi
