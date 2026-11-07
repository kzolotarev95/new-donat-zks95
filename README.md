# ZKS95 Developer

Production-ready персональный портал разработчика с GitHub API, приёмом поддержки через ЮKassa и установкой на Ubuntu VPS. Проект рассчитан на Ubuntu 24.04/26.04 LTS и не вмешивается в чужие сайты, Xray/x-ui или сертификаты на сервере.

**Стек:** Flask 3 · SQLite (WAL) · Gunicorn · Nginx · systemd · YooKassa SDK · GitHub Actions.

## Возможности

- адаптивный интерфейс в стиле Premium Gold / Dark / Graphite;
- страницы About, Projects, Services, Support Developer, Donate, Contact и FAQ;
- динамический список публичных репозиториев из GitHub API;
- создание платежа YooKassa, redirect на платёжную форму и webhook `/webhooks/yookassa`;
- idempotency key для создания платежей, защита от повторной обработки и перепроверка статуса через API ЮKassa;
- SQLite с сохранением платежей, статусами и журналом payload без секретов;
- автоматические `install.sh`, `update.sh`, `uninstall.sh` с backup и health check;
- systemd hardening (`NoNewPrivileges`, `ProtectSystem`, отдельный пользователь `zks95`);
- Nginx-конфигурация с резервной копией существующего файла и без удаления чужих сайтов;
- CI: синтаксис Python, тесты, shellcheck и сканирование секретов.

## Быстрая установка на VPS

```bash
git clone https://github.com/kzolotarev95/new-donat-zks95.git
cd new-donat-zks95
chmod +x install.sh update.sh uninstall.sh run.sh
sudo ./install.sh
```

Установщик спросит домен, предложит HTTPS и запросит shop ID/secret key ЮKassa. Он создаёт:

- приложение: `/opt/new-donat-zks95`;
- конфигурацию: `/etc/new-donat-zks95/.env`;
- базу: `/var/lib/new-donat-zks95/site.db`;
- резервные копии: `/var/backups/new-donat-zks95`;
- сервис: `new-donat-zks95.service`;
- Nginx site: `/etc/nginx/sites-available/new-donat-zks95.conf`.

Если DNS ещё не указывает на VPS, выберите установку без HTTPS. После создания A-записи и открытия TCP 80/443 выполните:

```bash
sudo certbot --nginx -d donate.example.com --redirect
sudo certbot renew --dry-run
```

Сайт слушает только `127.0.0.1:8015`; Nginx использует существующий 80/443 без захвата порта приложением.

## Настройка ЮKassa

1. Создайте магазин в [личном кабинете ЮKassa](https://yookassa.ru/).
2. Скопируйте Shop ID и секретный ключ в `/etc/new-donat-zks95/.env`:

```dotenv
YOOKASSA_SHOP_ID=123456
YOOKASSA_SECRET_KEY=live_...
YOOKASSA_RETURN_URL=https://donate.example.com/payment/success
YOOKASSA_WEBHOOK_URL=https://donate.example.com/webhooks/yookassa
```

3. В кабинете ЮKassa добавьте webhook на `https://donate.example.com/webhooks/yookassa` для события `payment.succeeded` (при необходимости также `payment.canceled`).
4. Перезапустите сервис: `sudo systemctl restart new-donat-zks95`.

Секреты не хранятся в Git и не передаются в GitHub Actions. Сервер не считает тело webhook доказательством оплаты: по полученному `payment_id` выполняется запрос к API ЮKassa, затем статус и сумма записываются в SQLite. Повторный webhook обновляет ту же запись.

## Обновление и откат

```bash
sudo /opt/new-donat-zks95/update.sh
```

Скрипт делает `git fetch`, проверяет новые коммиты, сохраняет код/базу/.env, выполняет fast-forward, обновляет зависимости, инициализирует схему, перезапускает systemd и проверяет `/healthz`. При ошибке возвращает предыдущий Git revision и перезапускает сервис. Пользовательские настройки и база не трогаются.

## Удаление

```bash
sudo /opt/new-donat-zks95/uninstall.sh
```

Нужно ввести `REMOVE`. Перед удалением создаётся бэкап базы и `.env` в `/var/backups/new-donat-zks95/uninstall-<date>/`. Удаляются только каталог проекта, его systemd unit, его Nginx symlink/config, пользователь `zks95` и каталог данных. Nginx, пакеты, сертификаты Let's Encrypt и конфигурации других сайтов сохраняются.

## Локальный запуск

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python run.py
```

Проверка: `curl http://127.0.0.1:8000/healthz`.

## GitHub Actions

`.github/workflows/ci.yml` проверяет Python 3.11/3.12, `compileall`, pytest, shellcheck, `bash -n` и `gitleaks`. Workflow `.github/workflows/deploy.yml` существует как отключённый шаблон: он не запускается, пока владелец не добавит secrets и не включит job вручную.

## Безопасность

- `.env` исключён из Git, права на сервере 0640 и владелец `root:zks95`;
- секретный ключ приложения генерируется при установке;
- SQL-запросы параметризованы;
- email и сумма валидируются, сумма ограничена 100–1 000 000 ₽;
- webhook подтверждается запросом к API ЮKassa и обрабатывается идемпотентно;
- systemd запускает процесс от отдельного системного пользователя;
- Nginx не удаляет существующие конфигурации и делает backup перед изменением.

## Структура

```text
app/                 Flask application, DB, GitHub and YooKassa clients
templates/           Jinja templates
static/              CSS and JavaScript
config/               systemd and Nginx templates
scripts/              maintenance helpers
tests/                pytest tests
install.sh            first installation
update.sh             safe update with rollback
uninstall.sh          scoped removal with backup
```

## Ссылки

- Репозиторий: https://github.com/kzolotarev95/new-donat-zks95
- Главный проект: https://github.com/kzolotarev95/luci-app-owrt-remote
- GitHub API: https://api.github.com/users/kzolotarev95/repos
