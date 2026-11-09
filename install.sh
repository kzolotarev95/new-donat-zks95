#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
APP_NAME="new-donat-zks95"; APP_USER="zks95"; APP_DIR="/opt/${APP_NAME}"; ETC_DIR="/etc/${APP_NAME}"; DATA_DIR="/var/lib/${APP_NAME}"; BACKUP_DIR="/var/backups/${APP_NAME}"; LOG_DIR="/var/log/${APP_NAME}"; APP_PORT="8015"; SERVICE_NAME="${APP_NAME}.service"; ALERT_SERVICE_NAME="${APP_NAME}-alert.service"; SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
log(){ printf '\033[1;33m[install]\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m[warning]\033[0m %s\n' "$*" >&2; }
fatal(){ printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; }
[[ "${EUID}" -eq 0 ]] || fatal "Запустите установщик через sudo."
exec 9>/run/lock/new-donat-zks95-maintenance.lock
flock -n 9 || fatal "Другая установка, проверка или обновление проекта уже выполняется."
[[ -f /etc/os-release ]] || fatal "Не удалось определить операционную систему."
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || fatal "Поддерживается Ubuntu 24.04/26.04 LTS. Обнаружено: ${PRETTY_NAME:-unknown}."
if [[ "${VERSION_ID%%.*}" != "24" && "${VERSION_ID%%.*}" != "26" ]]; then warn "Версия Ubuntu ${VERSION_ID:-unknown} не проверялась, продолжаю."; fi
getent hosts github.com >/dev/null 2>&1 || fatal "GitHub недоступен из DNS. Проверьте сеть и повторите."
ENV_FILE="${ETC_DIR}/.env"; EXISTING_DOMAIN=""
if [[ -r "${ENV_FILE}" ]]; then EXISTING_DOMAIN="$(awk -F= '/^SITE_DOMAIN=/{print substr($0,index($0,$2)); exit}' "${ENV_FILE}" || true)"; fi
if [[ -n "${EXISTING_DOMAIN}" ]]; then read -r -p "Домен сайта [${EXISTING_DOMAIN}]: " SITE_DOMAIN; SITE_DOMAIN="${SITE_DOMAIN:-${EXISTING_DOMAIN}}"; else read -r -p "Домен сайта (например donate.example.com): " SITE_DOMAIN; fi
[[ "${SITE_DOMAIN}" =~ ^[A-Za-z0-9.-]+$ ]] || fatal "Некорректное доменное имя."
read -r -p "Настроить HTTPS через Let's Encrypt сейчас? [y/N] " SETUP_SSL; CERTBOT_EMAIL=""
if [[ "${SETUP_SSL,,}" == "y" || "${SETUP_SSL,,}" == "yes" ]]; then read -r -p "Email для Certbot (если HTTPS включён, можно оставить пустым): " CERTBOT_EMAIL; fi
log "Устанавливаю системные зависимости"; export DEBIAN_FRONTEND=noninteractive; WAITED=0
while fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1; do
  WAITED=$((WAITED + 1)); (( WAITED <= 60 )) || fatal "Системный пакетный менеджер занят больше 5 минут. Проверьте unattended-upgrades и повторите."; log "Ожидаю завершения фонового обновления Ubuntu (${WAITED}/60)"; sleep 5
done
dpkg --configure -a; apt-get update; apt-get install -y openssl python3 python3-venv python3-pip git nginx curl ca-certificates rsync sqlite3
if [[ "${SETUP_SSL,,}" == "y" || "${SETUP_SSL,,}" == "yes" ]]; then apt-get install -y certbot python3-certbot-nginx; fi
CREATED_APP_USER=0
if ! id -u "${APP_USER}" >/dev/null 2>&1; then useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"; CREATED_APP_USER=1; fi
install -d -o "${APP_USER}" -g "${APP_USER}" -m 0755 "${APP_DIR}"
install -d -o "${APP_USER}" -g "${APP_USER}" -m 0750 "${DATA_DIR}" "${LOG_DIR}"
install -d -o root -g "${APP_USER}" -m 0750 "${ETC_DIR}"; install -d -o root -g root -m 0700 "${BACKUP_DIR}"
if [[ "${CREATED_APP_USER}" -eq 1 ]]; then install -m 0600 /dev/null "${ETC_DIR}/.user-created"; fi
log "Копирую приложение в ${APP_DIR}"
rsync -a --delete --exclude='.venv' --exclude='instance' --exclude='*.db' --exclude='__pycache__' "${SCRIPT_DIR}/" "${APP_DIR}/"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}" "${DATA_DIR}" "${LOG_DIR}"
if [[ ! -f "${ENV_FILE}" ]]; then
  log "Создаю конфигурацию с безопасными правами"; read -r -p "YooKassa shop ID (можно задать позже): " SHOP_ID; read -r -s -p "YooKassa secret key (можно задать позже): " SECRET_KEY; printf '\n'; APP_SECRET="$(openssl rand -hex 32)"; umask 077
  cat > "${ENV_FILE}" <<EOF
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
PORT=${APP_PORT}
EOF
else log "Сохраняю существующий ${ENV_FILE}"; fi
chown root:root "${ENV_FILE}"; chmod 0600 "${ENV_FILE}"
log "Создаю виртуальное окружение и ставлю Python-зависимости"
if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then runuser -u "${APP_USER}" -- python3 -m venv "${APP_DIR}/.venv"; fi
runuser -u "${APP_USER}" -- "${APP_DIR}/.venv/bin/pip" install --upgrade pip
runuser -u "${APP_USER}" -- "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
DB_PATH="$(awk -F= '/^DATABASE_PATH=/{print substr($0,index($0,$2)); exit}' "${ENV_FILE}")"; [[ -n "${DB_PATH}" ]] || DB_PATH="${DATA_DIR}/site.db"
runuser -u "${APP_USER}" -- env PYTHONPATH="${APP_DIR}" DATABASE_PATH="${DB_PATH}" "${APP_DIR}/.venv/bin/python" - <<'PY'
from app import create_app
from app.db import get_db
app = create_app()
with app.app_context():
    get_db().execute("SELECT 1").fetchone()
print("database ready")
PY
ADMIN_COUNT="$(runuser -u "${APP_USER}" -- env PYTHONPATH="${APP_DIR}" DATABASE_PATH="${DB_PATH}" "${APP_DIR}/.venv/bin/python" - <<'PY'
from app import create_app
from app.db import get_db
app = create_app()
with app.app_context():
    print(get_db().execute("SELECT COUNT(*) FROM admin_users").fetchone()[0])
PY
)"
if [[ "${ADMIN_COUNT}" == "0" ]]; then
  log "Создайте уникальную учётную запись администратора (не используйте admin/admin)."
  runuser -u "${APP_USER}" -- env PYTHONPATH="${APP_DIR}" DATABASE_PATH="${DB_PATH}" "${APP_DIR}/.venv/bin/python" "${APP_DIR}/scripts/create_admin.py"
fi
log "Устанавливаю systemd service"
for UNIT_FILE in "${APP_DIR}"/config/systemd/*.service "${APP_DIR}"/config/systemd/*.timer; do
  [[ -f "${UNIT_FILE}" ]] || continue
  install -m 0644 "${UNIT_FILE}" "/etc/systemd/system/$(basename "${UNIT_FILE}")"
done
systemctl daemon-reload; systemctl enable "${SERVICE_NAME}"; systemctl enable "${ALERT_SERVICE_NAME}" >/dev/null 2>&1 || true; systemctl restart "${SERVICE_NAME}"
systemctl enable --now "${APP_NAME}-health.timer"
log "Проверяю и согласую Nginx, не затрагивая чужие сайты"
BACKUP_DIR="${BACKUP_DIR}/install-$(date +%Y%m%d-%H%M%S)"
env SITE_DOMAIN="${SITE_DOMAIN}" APP_DIR="${APP_DIR}" APP_PORT="${APP_PORT}" BACKUP_DIR="${BACKUP_DIR}" "${APP_DIR}/scripts/reconcile_nginx.sh"
PUBLIC_URL="http://${SITE_DOMAIN}"
SSL_STATUS="не запрошен"
if [[ "${SETUP_SSL,,}" == "y" || "${SETUP_SSL,,}" == "yes" ]]; then
  SSL_STATUS="не настроен"
  if [[ -z "${CERTBOT_EMAIL}" ]]; then
    warn "Email не указан. HTTPS пропущен; полная установка продолжается только с HTTP."
  else
    PUBLIC_IP="$(curl -4fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
    DNS_IPS="$(getent ahostsv4 "${SITE_DOMAIN}" 2>/dev/null | awk '{print $1}' | sort -u || true)"
    DNS_OK=0
    if [[ -n "${PUBLIC_IP}" && -n "${DNS_IPS}" ]] && grep -Fxq "${PUBLIC_IP}" <<<"${DNS_IPS}"; then DNS_OK=1; fi
    PORT80_OK=0
    if ss -lnt | grep -Eq '(:80|:http)[[:space:]]'; then
      if curl --resolve "${SITE_DOMAIN}:80:127.0.0.1" -fsS --max-time 5 "http://${SITE_DOMAIN}/healthz" >/dev/null; then PORT80_OK=1; fi
    fi
    if [[ "${DNS_OK}" -ne 1 ]]; then
      warn "HTTPS пропущен: DNS ${SITE_DOMAIN} не указывает на этот VPS (${PUBLIC_IP:-IP VPS не определён}; найдено: ${DNS_IPS:-нет записи})."
    elif [[ "${PORT80_OK}" -ne 1 ]]; then
      warn "HTTPS пропущен: порт 80 не отвечает на локальный health check."
    elif certbot --nginx -d "${SITE_DOMAIN}" --non-interactive --agree-tos --email "${CERTBOT_EMAIL}" --redirect --keep-until-expiring; then
      systemctl enable --now certbot.timer >/dev/null 2>&1 || true
      if certbot renew --dry-run >/dev/null 2>&1; then
        SSL_STATUS="настроен, продление проверено"
      else
        SSL_STATUS="настроен, dry-run продления требует проверки"
        warn "Сертификат выпущен, но certbot renew --dry-run завершился с ошибкой."
      fi
      nginx -t || fatal "После настройки HTTPS проверка Nginx не пройдена."
      PUBLIC_URL="https://${SITE_DOMAIN}"
    else
      warn "Certbot не завершился. Проверьте DNS и доступность TCP 80, затем повторите настройку."
    fi
  fi
fi
log "Проверяю запуск приложения и health check"; HEALTH_OK=0
for _ in {1..20}; do if curl --fail --silent --show-error "http://127.0.0.1:${APP_PORT}/healthz" >/dev/null; then HEALTH_OK=1; break; fi; sleep 1; done
if [[ "${HEALTH_OK}" -ne 1 ]]; then systemctl status "${SERVICE_NAME}" --no-pager || true; journalctl -u "${SERVICE_NAME}" -n 50 --no-pager || true; fatal "Приложение не ответило на health check."; fi
ss -lnt | grep -q ":${APP_PORT}[[:space:]]" || fatal "Порт ${APP_PORT} не слушается."; systemctl is-enabled "${SERVICE_NAME}" >/dev/null || fatal "Сервис не включён в автозапуск."
cat <<EOF

Установка завершена.
  Сайт:       ${PUBLIC_URL}
  Порт:       127.0.0.1:${APP_PORT}
  Сервис:     systemctl status ${SERVICE_NAME}
  Уведомления: ${ALERT_SERVICE_NAME} (OnFailure/startup push)
  HTTPS:      ${SSL_STATUS}
  Конфиг:     ${ENV_FILE} (права 0600, секреты не попадают в Git)
  База:       ${DATA_DIR}/site.db
  Обновление: sudo ${APP_DIR}/update.sh
  Удаление:   sudo ${APP_DIR}/uninstall.sh
  Бэкап Nginx: ${BACKUP_DIR}

Если DNS ещё не настроен, создайте A-запись ${SITE_DOMAIN} на IP этого VPS.
Webhook ЮKassa: https://${SITE_DOMAIN}/webhooks/yookassa
EOF
