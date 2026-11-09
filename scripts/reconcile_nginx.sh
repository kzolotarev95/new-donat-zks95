#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="${APP_NAME:-new-donat-zks95}"
APP_DIR="${APP_DIR:-/opt/${APP_NAME}}"
APP_PORT="${APP_PORT:-8015}"
SITE_DOMAIN="${SITE_DOMAIN:?SITE_DOMAIN is required}"
DOMAIN_RE="$(printf '%s' "${SITE_DOMAIN}" | sed 's/[][\\.^$*+?(){}|]/\\&/g')"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/${APP_NAME}}"
NGINX_AVAILABLE="/etc/nginx/sites-available/${APP_NAME}.conf"
NGINX_ENABLED="/etc/nginx/sites-enabled/${APP_NAME}.conf"
TEMPLATE="${APP_DIR}/config/nginx/new-donat-zks95.conf.template"

log(){ printf '\033[1;33m[nginx]\033[0m %s\n' "$*"; }
fatal(){ printf '\033[1;31m[nginx error]\033[0m %s\n' "$*" >&2; exit 1; }

[[ "${EUID}" -eq 0 ]] || fatal "Запустите через sudo."
[[ -f "${TEMPLATE}" ]] || fatal "Не найден шаблон Nginx: ${TEMPLATE}"
mkdir -p "${BACKUP_DIR}"
STAMP="$(date +%Y%m%d-%H%M%S)"
ROLLBACK_DIR="$(mktemp -d)"
TMP_CONFIG="$(mktemp)"
trap 'rm -rf "${ROLLBACK_DIR}" "${TMP_CONFIG}"' EXIT

matches_domain(){
  grep -Eiq "server_name[[:space:]].*${DOMAIN_RE}([;[:space:]]|$)" "$1"
}

owned_config(){
  local file="$1" entry="${2:-$1}"
  grep -Eiq 'Managed by new-donat-zks95|new-donat-zks95|ZKS95 Developer' "${file}" || \
    basename "${entry}" | grep -Eiq '^(byzks95-developer-li|new-donat-zks95)(\.conf)?$'
}

save_entry(){
  local entry="$1" index="$2"
  cp -a "$entry" "${BACKUP_DIR}/nginx-${STAMP}-${index}-$(basename "$entry")" 2>/dev/null || true
  cp -a "$entry" "${ROLLBACK_DIR}/entry-${index}" 2>/dev/null || true
}

restore_rollback(){
  local entry index
  rm -f "${NGINX_ENABLED}" "${NGINX_AVAILABLE}"
  if [[ -f "${ROLLBACK_DIR}/available" ]]; then
    install -m 0644 "${ROLLBACK_DIR}/available" "${NGINX_AVAILABLE}"
  fi
  if [[ -e "${ROLLBACK_DIR}/enabled" || -L "${ROLLBACK_DIR}/enabled" ]]; then
    cp -a "${ROLLBACK_DIR}/enabled" "${NGINX_ENABLED}"
  fi
  while IFS='|' read -r entry index; do
    [[ -n "${entry}" ]] || continue
    rm -rf "$entry"
    mkdir -p "$(dirname "$entry")"
    cp -a "${ROLLBACK_DIR}/entry-${index}" "$entry"
  done < "${ROLLBACK_DIR}/removed.txt"
}

: > "${ROLLBACK_DIR}/removed.txt"
if [[ -e "${NGINX_AVAILABLE}" || -L "${NGINX_AVAILABLE}" ]]; then
  cp -a "${NGINX_AVAILABLE}" "${BACKUP_DIR}/nginx-${STAMP}-current.conf"
  cp -a "${NGINX_AVAILABLE}" "${ROLLBACK_DIR}/available"
fi
if [[ -e "${NGINX_ENABLED}" || -L "${NGINX_ENABLED}" ]]; then
  cp -a "${NGINX_ENABLED}" "${ROLLBACK_DIR}/enabled"
fi

SOURCE_CONFIG=""
INDEX=0
declare -a OWNED_ENTRIES=()
shopt -s nullglob
# First inspect every candidate.  This prevents a foreign duplicate discovered
# later in the directory scan from leaving an earlier project symlink disabled.
for entry in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*; do
  [[ -e "${entry}" || -L "${entry}" ]] || continue
  target="$(readlink -f "${entry}" 2>/dev/null || printf '%s' "${entry}")"
  [[ -f "${target}" ]] || continue
  if matches_domain "${target}"; then
    if ! owned_config "${target}" "${entry}"; then
      fatal "Обнаружен чужой Nginx-конфиг с server_name ${SITE_DOMAIN}: ${entry}. Он не изменён."
    fi
    [[ -n "${SOURCE_CONFIG}" ]] || SOURCE_CONFIG="${target}"
    OWNED_ENTRIES+=("${entry}")
  fi
done
shopt -u nullglob

for entry in "${OWNED_ENTRIES[@]}"; do
  if [[ "${entry}" != "${NGINX_ENABLED}" ]]; then
    INDEX=$((INDEX + 1))
    save_entry "${entry}" "${INDEX}"
    printf '%s|%s\n' "${entry}" "${INDEX}" >> "${ROLLBACK_DIR}/removed.txt"
    rm -rf "${entry}"
    log "Отключён старый конфиг проекта: ${entry}"
  fi
done

if [[ -z "${SOURCE_CONFIG}" && -f "${NGINX_AVAILABLE}" ]] && matches_domain "${NGINX_AVAILABLE}" && owned_config "${NGINX_AVAILABLE}"; then
  SOURCE_CONFIG="${NGINX_AVAILABLE}"
fi

if [[ -n "${SOURCE_CONFIG}" ]]; then
  cp -f "${SOURCE_CONFIG}" "${TMP_CONFIG}"
  sed -E -i "s#127\\.0\\.0\\.1:(8000|8015)#127.0.0.1:${APP_PORT}#g" "${TMP_CONFIG}"
else
  sed -e "s|__SITE_DOMAIN__|${SITE_DOMAIN}|g" -e "s|__APP_PORT__|${APP_PORT}|g" "${TEMPLATE}" > "${TMP_CONFIG}"
fi

if [[ -e "${NGINX_ENABLED}" || -L "${NGINX_ENABLED}" ]]; then
  target="$(readlink -f "${NGINX_ENABLED}" 2>/dev/null || printf '%s' "${NGINX_ENABLED}")"
  if ! owned_config "${target}" "${NGINX_ENABLED}"; then
    fatal "${NGINX_ENABLED} существует и не принадлежит проекту."
  fi
  rm -rf "${NGINX_ENABLED}"
fi
if [[ -L "${NGINX_AVAILABLE}" ]]; then
  target="$(readlink -f "${NGINX_AVAILABLE}" 2>/dev/null || printf '%s' "${NGINX_AVAILABLE}")"
  owned_config "${target}" "${NGINX_AVAILABLE}" || fatal "${NGINX_AVAILABLE} является чужой ссылкой и не изменён."
  rm -f "${NGINX_AVAILABLE}"
fi
if [[ -e "${NGINX_AVAILABLE}" ]] && ! owned_config "${NGINX_AVAILABLE}"; then
  fatal "${NGINX_AVAILABLE} содержит чужую конфигурацию и не изменён."
fi
install -m 0644 "${TMP_CONFIG}" "${NGINX_AVAILABLE}"
ln -s "${NGINX_AVAILABLE}" "${NGINX_ENABLED}"

if ! nginx -t >/dev/null; then
  log "nginx -t не прошёл, возвращаю предыдущую конфигурацию."
  restore_rollback
  nginx -t >/dev/null 2>&1 || true
  fatal "Проверка Nginx не пройдена. Конфигурация восстановлена."
fi
if ! systemctl reload nginx; then
  log "Перезагрузка Nginx не прошла, возвращаю предыдущую конфигурацию."
  restore_rollback
  nginx -t >/dev/null 2>&1 || true
  fatal "Nginx не перезагрузился. Конфигурация восстановлена."
fi
log "Nginx настроен: ${SITE_DOMAIN} → 127.0.0.1:${APP_PORT}"
