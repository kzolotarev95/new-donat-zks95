"""Per-account TOTP enrollment, atomic code consumption and durable throttling."""
import hashlib
import re
import secrets
import time

import pyotp
from flask import session

from .db import get_db
from .security import decrypt_value, encrypt_value

SETUP_SECONDS = 600
CHALLENGE_SECONDS = 300
RATE_SECONDS = 300
RECOVERY_COUNT = 10


class AuthError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def take_attempt(bucket, limit=5):
    """Reserve an attempt atomically so parallel workers share the same limit."""
    now = int(time.time())
    db = get_db()
    db.execute("DELETE FROM admin_auth_limits WHERE reset_at <= ?", (now,))
    changed = db.execute(
        """INSERT INTO admin_auth_limits(bucket, failures, reset_at) VALUES (?, 1, ?)
        ON CONFLICT(bucket) DO UPDATE SET failures = failures + 1 WHERE failures < ?""",
        (_hash(bucket), now + RATE_SECONDS, limit),
    ).rowcount
    db.commit()
    if not changed:
        raise AuthError("Слишком много попыток. Подождите 5 минут и повторите вход.", 429)


def clear_rate(bucket):
    get_db().execute("DELETE FROM admin_auth_limits WHERE bucket = ?", (_hash(bucket),))
    get_db().commit()


def pending_setup(user):
    token = session.get("totp_setup_token", "")
    if (not token or user["totp_secret"] or user["totp_pending_until"] <= int(time.time())
            or not secrets.compare_digest(_hash(token), user["totp_pending_id_hash"])):
        return ""
    return decrypt_value(user["totp_pending_secret"])


def start_setup(user):
    if user["totp_secret"]:
        raise AuthError("2FA уже включена.")
    secret = pyotp.random_base32()
    token = secrets.token_urlsafe(32)
    db = get_db()
    changed = db.execute(
        """UPDATE admin_users SET totp_pending_secret = ?, totp_pending_id_hash = ?,
        totp_pending_until = ? WHERE id = ? AND auth_version = ? AND totp_secret = ''""",
        (encrypt_value(secret), _hash(token), int(time.time()) + SETUP_SECONDS, user["id"], user["auth_version"]),
    ).rowcount
    db.commit()
    if not changed:
        raise AuthError("Настройки аккаунта изменились. Войдите заново.")
    session["totp_setup_token"] = token


def cancel_setup(user):
    if pending_setup(user):
        get_db().execute(
            """UPDATE admin_users SET totp_pending_secret = '', totp_pending_id_hash = '',
            totp_pending_until = 0 WHERE id = ? AND totp_pending_id_hash = ?""",
            (user["id"], user["totp_pending_id_hash"]),
        )
        get_db().commit()
    session.pop("totp_setup_token", None)


def _matching_step(secret, code):
    code = str(code or "").strip().replace(" ", "")
    if not secret or not re.fullmatch(r"[0-9]{6}", code):
        return None
    step = int(time.time()) // 30
    totp = pyotp.TOTP(secret)
    for offset in (0, -1, 1):
        candidate = step + offset
        if candidate >= 0 and secrets.compare_digest(totp.at(candidate * 30), code):
            return candidate
    return None


def _replace_recovery_codes(db, user_id):
    codes = ["-".join(token[i:i + 5] for i in range(0, 20, 5))
             for token in (secrets.token_hex(10) for _ in range(RECOVERY_COUNT))]
    db.execute("DELETE FROM admin_recovery_codes WHERE user_id = ?", (user_id,))
    db.executemany("INSERT INTO admin_recovery_codes(user_id, code_hash) VALUES (?, ?)",
                   [(user_id, _hash(code.replace("-", ""))) for code in codes])
    return codes


def enable(user, code):
    bucket = f"factor:{user['id']}"
    take_attempt(bucket)
    secret = pending_setup(user)
    step = _matching_step(secret, code)
    if step is None:
        raise AuthError("Код неверен или настройка истекла. Проверьте время на телефоне.")
    db = get_db()
    with db:
        changed = db.execute(
            """UPDATE admin_users SET totp_secret = totp_pending_secret, totp_last_step = ?,
            auth_version = auth_version + 1, totp_pending_secret = '', totp_pending_id_hash = '',
            totp_pending_until = 0 WHERE id = ? AND totp_secret = '' AND auth_version = ?
            AND totp_pending_id_hash = ? AND totp_pending_until > ?""",
            (step, user["id"], user["auth_version"], user["totp_pending_id_hash"], int(time.time())),
        ).rowcount
        if not changed:
            raise AuthError("Настройка истекла. Начните подключение заново.")
        codes = _replace_recovery_codes(db, user["id"])
    clear_rate(bucket)
    return codes


def consume_factor(user, code):
    """Accept a TOTP time step or recovery code once, across all workers."""
    bucket = f"factor:{user['id']}"
    take_attempt(bucket)
    if not user["totp_secret"]:
        raise AuthError("2FA не включена.")
    db = get_db()
    normalized = str(code or "").strip().replace(" ", "").replace("-", "").lower()
    changed = 0
    if re.fullmatch(r"[0-9]{6}", normalized):
        step = _matching_step(decrypt_value(user["totp_secret"]), normalized)
        if step is not None:
            changed = db.execute(
                """UPDATE admin_users SET totp_last_step = ? WHERE id = ? AND totp_last_step < ?
                AND totp_secret = ? AND auth_version = ?""",
                (step, user["id"], step, user["totp_secret"], user["auth_version"]),
            ).rowcount
    elif re.fullmatch(r"[0-9a-f]{20}", normalized):
        changed = db.execute(
            """DELETE FROM admin_recovery_codes WHERE user_id = ? AND code_hash = ?
            AND EXISTS (SELECT 1 FROM admin_users WHERE id = ? AND totp_secret = ? AND auth_version = ?)""",
            (user["id"], _hash(normalized), user["id"], user["totp_secret"], user["auth_version"]),
        ).rowcount
    db.commit()
    if not changed:
        raise AuthError("Код неверен, уже использован или истёк. Введите новый код.", 401)
    clear_rate(bucket)


def recovery_count(user):
    return get_db().execute("SELECT COUNT(*) FROM admin_recovery_codes WHERE user_id = ?", (user["id"],)).fetchone()[0]


def rotate_recovery_codes(user):
    db = get_db()
    with db:
        changed = db.execute(
            "UPDATE admin_users SET auth_version = auth_version + 1 WHERE id = ? AND auth_version = ? AND totp_secret != ''",
            (user["id"], user["auth_version"]),
        ).rowcount
        if not changed:
            raise AuthError("Настройки аккаунта изменились. Войдите заново.")
        return _replace_recovery_codes(db, user["id"])


def disable(user):
    db = get_db()
    with db:
        changed = db.execute(
            """UPDATE admin_users SET totp_secret = '', totp_last_step = -1,
            totp_pending_secret = '', totp_pending_id_hash = '', totp_pending_until = 0,
            auth_version = auth_version + 1 WHERE id = ? AND auth_version = ? AND totp_secret != ''""",
            (user["id"], user["auth_version"]),
        ).rowcount
        if not changed:
            raise AuthError("Настройки аккаунта изменились. Войдите заново.")
        db.execute("DELETE FROM admin_recovery_codes WHERE user_id = ?", (user["id"],))
