"""Runtime settings stored in SQLite; secret values are encrypted at rest."""
from datetime import datetime, timezone
from flask import current_app
from .db import get_db
from .security import decrypt_value, encrypt_value

SECRET_KEYS = {
    "YOOKASSA_SECRET_KEY", "YOOKASSA_WEBHOOK_SECRET", "RECAPTCHA_SECRET_KEY",
    "VAPID_PRIVATE_KEY",
}

def _now():
    return datetime.now(timezone.utc).isoformat()

def get_runtime_setting(key, default=""):
    row = get_db().execute("SELECT value, is_secret FROM app_settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return current_app.config.get(key, default)
    value = decrypt_value(row["value"]) if row["is_secret"] else row["value"]
    return value if value != "" else default

def set_runtime_setting(key, value):
    value = (value or "").strip()
    stored = encrypt_value(value) if key in SECRET_KEYS else value
    get_db().execute(
        """INSERT INTO app_settings(key, value, is_secret, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, is_secret=excluded.is_secret, updated_at=excluded.updated_at""",
        (key, stored, int(key in SECRET_KEYS), _now()),
    )
    get_db().commit()

def has_runtime_setting(key):
    row = get_db().execute("SELECT 1 FROM app_settings WHERE key = ?", (key,)).fetchone()
    return bool(row)
