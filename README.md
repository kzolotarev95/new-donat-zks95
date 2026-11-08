# ZKS95 Developer

Production-ready персональный портал разработчика с GitHub API, приёмом поддержки через ЮKassa и установкой на Ubuntu VPS. Проект рассчитан на Ubuntu 24.04/26.04 LTS и не вмешивается в чужие сайты, Xray/x-ui или сертификаты на сервере.

**Стек:** Flask 3 · SQLite (WAL) · Gunicorn · Nginx · systemd · YooKassa SDK · GitHub Actions.

## Возможности

- адаптивный чёрно-красный интерфейс с анимациями и компактной админкой;
- корневой адрес сайта автоматически открывает страницу `/donate`;
- страницы About, Projects, Services, Donate, Contact и FAQ;
- динамический список публичных репозиториев из GitHub API;
- создание платежа YooKassa, redirect на платёжную форму и webhook `/webhooks/yookassa`;
- закрытая админ-панель `/admin` с входом, сменой логина/пароля, платежами, донатерами и runtime-настройками;
- публичная стенка донатеров на `/donate`: никнейм появляется после подтверждённой оплаты, цвет и ручные записи управляются в `Админ → Донатеры`;
- форма поддержки принимает никнейм (при пустом API-значении используется `Аноним`), сообщение и сумму; в карточке донатера показывается накопленная сумма;
- в `Админ → Донатеры` записи можно закреплять: закреплённые донатеры всегда выводятся первыми, а также задавать ссылку на профиль;
- в `Админ → Настройки` можно включить глобальный эффект фона (Снег, Космос, Аврора или Техно-сетка) и выбрать его скорость; настройка сохраняется в SQLite и применяется ко всем страницам для всех посетителей;
- в `Админ → Бэкап` доступны отдельный экспорт/импорт донатеров и полный JSON-бэкап настроек, платежей, донатеров и push-подписок;
- Google reCAPTCHA v2 только для входа в админ-панель;
- web push для iOS/Android/Desktop: подписка устройства в `Админ → Уведомления`, тестовая отправка и уведомления об успешных платежах;
- idempotency key для создания платежей, защита от повторной обработки и перепроверка статуса через API ЮKassa;
- SQLite с сохранением платежей, статусами и журналом payload без секретов;
- автоматические `install.sh`, `update.sh`, `uninstall.sh` с backup и health check;
- systemd hardening (`NoNewPrivileges`, `ProtectSystem`, отдельный пользователь `zks95`);
- Nginx-конфигурация с резервной копией существующего файла и без удаления чужих сайтов;
- CI: синтаксис Python, тесты, shellcheck и сканирование секретов.

## Быстрая установка на VPS

Однострочная установка:

```bash
curl -fsSL "https://raw.githubusercontent.com/kzolotarev95/new-donat-zks95/main/vps/install-vps.sh?v=$(date +%s)" | sudo sh
```

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

## Админ-панель

После установки панель доступна по адресу `https://ваш-домен/admin`. Данные первого входа: `admin / admin`. После входа логин и пароль можно изменить в разделе `Аккаунт`. Если база уже была создана до добавления панели, восстановите временный вход из SSH:

```bash
sudo -u zks95 env DATABASE_PATH=/var/lib/new-donat-zks95/site.db \
  /opt/new-donat-zks95/.venv/bin/python \
  /opt/new-donat-zks95/scripts/create_admin.py --default
```

В панели доступны обзор, список транзакций с фильтрами по никнейму, детализация payload YooKassa, ручная перепроверка статуса, управление донатерами, смена логина/пароля и настройки YooKassa/reCAPTCHA. Секретные значения YooKassa и Google сохраняются в SQLite зашифрованными ключом `APP_SECRET_KEY` и не показываются обратно.

Настройки разделены на отдельные секции `Сайт`, `YooKassa`, `Google reCAPTCHA` и `Глобальный эффект`, чтобы параметры не смешивались в одной форме.

В `Админ → Бэкап` кнопка **Скачать список** создаёт переносимый файл только с донатерами. **Скачать полный бэкап** включает runtime-настройки, платежи, донатеров и push-подписки; файл содержит секреты в переносимом виде и должен храниться как пароль. Перед восстановлением полного бэкапа требуется явное подтверждение замены текущих данных. Учётная запись администратора текущего VPS не заменяется.

### Push-уведомления

Откройте `Админ → Уведомления`, нажмите **Создать новые VAPID-ключи**, укажите email для VAPID и сохраните настройки. Затем нажмите **Включить на этом устройстве** и разрешите уведомления браузеру. Кнопка **Отправить тестовый push** проверяет доставку на все подключённые устройства. Успешные платежи отправляются автоматически, когда включены глобальные push-уведомления и оповещения о платежах.

На iPhone Apple разрешает web push для сайтов, добавленных на экран «Домой»: откройте сайт в Safari, выберите «Поделиться → На экран “Домой”», запустите установленное приложение, затем включите уведомления в админке. На Android и Desktop достаточно открыть админку через HTTPS и разрешить уведомления.

Для reCAPTCHA v2 создайте ключи в [Google reCAPTCHA Admin Console](https://www.google.com/recaptcha/admin), выберите тип **I'm not a robot Checkbox**, добавьте домен сайта и внесите Site Key и Secret Key в `Admin → Настройки`. CAPTCHA показывается только на `/admin/login`; форма пожертвований её не использует.

## Обновление и откат

Однострочное обновление:

```bash
curl -fsSL "https://raw.githubusercontent.com/kzolotarev95/new-donat-zks95/main/vps/update-vps.sh?v=$(date +%s)" | sudo sh
```

```bash
sudo /opt/new-donat-zks95/update.sh
```

Скрипт делает `git fetch`, проверяет новые коммиты, сохраняет код/базу/.env, выполняет fast-forward, обновляет зависимости, инициализирует схему, перезапускает systemd и проверяет `/healthz`. При ошибке возвращает предыдущий Git revision и перезапускает сервис. Пользовательские настройки и база не трогаются.

## Удаление

Однострочное удаление:

```bash
curl -fsSL "https://raw.githubusercontent.com/kzolotarev95/new-donat-zks95/main/vps/uninstall-vps.sh?v=$(date +%s)" | sudo sh
```

Если провайдер кэширует старый `raw.githubusercontent.com`, используйте versioned wrapper:

```bash
curl -fsSL https://raw.githubusercontent.com/kzolotarev95/new-donat-zks95/main/vps/update-vps-2.sh | sudo sh
```

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

- `.env` исключён из Git, права на сервере 0600 и владелец `root:zks95`;
- секретный ключ приложения генерируется при установке;
- SQL-запросы параметризованы;
- никнейм и сумма валидируются, сумма ограничена 100–1 000 000 ₽;
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
