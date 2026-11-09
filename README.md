# ZKS95 Developer

Production-ready персональный портал разработчика с GitHub API, приёмом поддержки.
**Стек:** Flask 3 · SQLite (WAL) · Gunicorn · Nginx · systemd · SDK · GitHub Actions.

Двухфакторная защита входа настраивается в **Админка → Настройки → 2FA**, под Google reCAPTCHA. Поддерживаются приложения TOTP, QR-код и одноразовые резервные коды. [Подключение и восстановление доступа](docs/2FA.md).
