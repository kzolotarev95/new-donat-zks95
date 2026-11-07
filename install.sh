#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

APP_NAME="new-donat-zks95"
APP_USER="zks95"
APP_DIR="/opt/${APP_NAME}"
ETC_DIR="/etc/${APP_NAME}"
DATA_DIR="/var/lib/${APP_NAME}"
BACKUP_DIR="/var/backups/${APP_NAME}"
SERVICE_NAME="${APP_NAME}.service"
NGINX_AVAILABLE="/etc/nginx/sites-available/${APP_NAME}.conf"
NGINX_ENABLED="/etc/nginx/sites-enabled/${APP_NAME}.conf"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

log(){ printf '\033[1;33m[install]\033[0m %s\n' "$*"; }
fatal(){ printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
cleanup(){ rm -f "${TMP_NGINX:-}"; }
trap cleanup EXIT

[[ "${EUID}" -eq 0 ]] || fatal "Запустите установщик через sudo."
[[ -f /etc/os-release ]] || fatal "Не удалось определить операционную систему."
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || fatal "Поддерживается Ubuntu 24.04/26.04 LTS. Обнаружено: ${PRETTY_NAME:-unknown}."
[[ "${VERSION_ID%%.*}" == "24" || "${VERSION_ID%%.*}" == "26" ]] || log "Предупреждение: версия Ubuntu ${VERSION_ID:-unknown} не проверялась, продолжаю."

if ! command -v git >/dev/null 2>&1; then :; fi
if ! getent hosts github.com >/dev/null 2>&1; then fatal "GitHub недоступен из DNS. Проверьте сеть и повторите."; fi

read -r -p "Домен сайта (например donate.example.com): " SITE_DOMAIN
[[ "${SITE_DOMAIN}" =~ ^[A-Za-z0-9.-]+$ ]] || fatal "Некорректное доменное имя."
read -r -p "Настроить HTTPS через Let's Encrypt сейчас? [y/N] " SETUP_SSL
read -r -p "Email для Certbot (если HTTPS включён, можно оставить пустым): " CERTBOT_EMAIL

log "Устанавливаю системные зависимости"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip git nginx curl ca-certificates rsync sqlite3
if [[ "${SETUP_SSL,,}" == "y" || "${SETUP_SSL,,}" == "yes" ]]; then
  apt-get install -y certbot python3-certbot-nginx
fi

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
fi
install -d -o "${APP_USER}" -g "${APP_USER}" -m 0755 "${APP_DIR}" "${DATA_DIR}" "${BACKUP_DIR}"
install -d -o root -g root -m 0755 "${ETC_DIR}"
log "Копирую приложение в ${APP_DIR}"
rsync -a --exclude='.venv' --exclude='instance' --exclude='*.db' "${SCRIPT_DIR}/" "${APP_DIR}/"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}" "${DATA_DIR}"

if [[ ! -f "${ETC_DIR}/.env" ]]; then
  log "Создаю конфигурацию с безопасными правами"
  read -r -p "YooKassa shop ID (можно задать позже): " SHOP_ID
  read -r -s -p "YooKassa secret key (можно задать позже): " SECRET_KEY; printf '\n'
  APP_SECRET="$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(32))')"
  cat > "${ETC_DIR}/.env" <<EOF
APP_ENV=production
APP_SECRET_KEY=${APP_SECRET}
SITE_DOMAIN=${SITE_DOMAIN}
YOOKASSA_SHOP_ID=${SHOP_ID}
YOOKASSA_SECRET_KEY=${SECRET_KEY}
YOOKASSA_RETURN_URL=https://${SITE_DOMAIN}/payment/success
YOOKASSA_WEBHOOK_URL=https://${SITE_DOMAIN}/webhooks/yookassa
YOOKASSA_WEBHOOK_SECRET=
GITHUB_USERNAME=kzolotarev95
GITHUB_REPOS_URL=https://api.github.com/users/kzolotarev95/repos
DATABASE_PATH=${DATA_DIR}/site.db
PAYMENT_MIN_AMOUNT=100
PAYMENT_MAX_AMOUNT=1000000
PAYMENT_CURRENCY=RUB
EOF
  chown root:"${APP_USER}" "${ETC_DIR}/.env"
  chmod 0640 "${ETC_DIR}/.env"
else
  log "Сохраняю существующий ${ETC_DIR}/.env"
fi

log "Создаю виртуальное окружение и ставлю Python-зависимости"
runuser -u "${APP_USER}" -- python3 -m venv "${APP_DIR}/.venv"
runuser -u "${APP_USER}" -- "${APP_DIR}/.venv/bin/pip" install --upgrade pip
runuser -u "${APP_USER}" -- "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
DB_PATH="$(awk -F= '/^DATABASE_PATH=/{print substr($0, index($0,$2))}' "${ETC_DIR}/.env")"
runuser -u "${APP_USER:-zks95}" -- env DATABASE_PATH="${DB_PATH}" "${APP_DIR}/.venv/bin/python" -c 'from app import create_app; app=create_app(); print("database ready")'

log "Устанавливаю systemd service"
install -m 0644 "${APP_DIR}/config/systemd/${SERVICE_NAME}" "/etc/systemd/system/${SERVICE_NAME}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

TMP_NGINX="$(mktemp)"
sed "s/__SITE_DOMAIN__/${SITE_DOMAIN}/g" "${APP_DIR}/config/nginx/new-donat-zks95.conf.template" > "${TMP_NGINX}"
if [[ -e "${NGINX_AVAILABLE}" ]]; then
  cp -a "${NGINX_AVAILABLE}" "${BACKUP_DIR}/nginx-$(date +%Y%m%d-%H%M%S).conf"
fi
if [[ -L "${NGINX_ENABLED}" && "$(readlink -f "${NGINX_ENABLED}")" != "${NGINX_AVAILABLE}" ]]; then
  cp -a "${NGINX_ENABLED}" "${BACKUP_DIR}/nginx-enabled-$(date +%Y%m%d-%H%M%S).link"
  rm -f "${NGINX_ENABLED}"
elif [[ -e "${NGINX_ENABLED}" && ! -L "${NGINX_ENABLED}" ]]; then
  cp -a "${NGINX_ENABLED}" "${BACKUP_DIR}/nginx-enabled-$(date +%Y%m%d-%H%M%S).conf"
fi
if grep -RqsE "server_name[[:space:]].*${SITE_DOMAIN}([;[:space:]]|$)" /etc/nginx/sites-enabled 2>/dev/null; then
  log "Предупреждение: домен уже встречается в конфигурации Nginx; проверьте конфликт server_name."
fi
install -m 0644 "${TMP_NGINX}" "${NGINX_AVAILABLE}"
ln -sfn "${NGINX_AVAILABLE}" "${NGINX_ENABLED}"
if ! nginx -t; then
  fatal "Проверка Nginx не пройдена; исходная конфигурация сохранена в ${BACKUP_DIR}."
fi
systemctl reload nginx

if [[ "${SETUP_SSL,,}" == "y" || "${SETUP_SSL,,}" == "yes" ]]; then
  if [[ -n "${CERTBOT_EMAIL}" ]]; then
    certbot --nginx -d "${SITE_DOMAIN}" --non-interactive --agree-tos --email "${CERTBOT_EMAIL}" --redirect || log "Certbot не завершился. Запустите certbot --nginx -d ${SITE_DOMAIN} после настройки DNS."
  else
    log "Email не указан. HTTPS не включён; запустите certbot --nginx -d ${SITE_DOMAIN} вручную."
  fi
fi

log "Проверяю health check"
for _ in {1..15}; do
  if curl --fail --silent --show-error http://127.0.0.1:8015/healthz >/dev/null; then break; fi
  sleep 1
done
curl --fail --silent http://127.0.0.1:8015/healthz >/dev/null || { systemctl status "${SERVICE_NAME}" --no-pager || true; fatal "Приложение не ответило на health check."; }

cat <<EOF

Установка завершена.
  Сайт:       http://${SITE_DOMAIN}
  Сервис:     systemctl status ${SERVICE_NAME}
  Конфиг:     ${ETC_DIR}/.env (права 0640, секреты не попадают в Git)
  База:       ${DATA_DIR}/site.db
  Обновление: sudo ${APP_DIR}/update.sh
  Удаление:   sudo ${APP_DIR}/uninstall.sh

Если DNS ещё не настроен, создайте A-запись ${SITE_DOMAIN} на IP этого VPS.
Webhook ЮKassa: https://${SITE_DOMAIN}/webhooks/yookassa
EOF
