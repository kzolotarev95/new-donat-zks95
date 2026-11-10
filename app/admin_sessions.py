"""Revocable admin sessions; browser tokens are stored only as SHA-256 hashes."""
import hashlib
import ipaddress
import secrets
import time
from datetime import datetime, timezone

from flask import current_app, request, session
from .db import get_db


def client_address():
    # Our Nginx overwrites X-Real-IP. Never trust forwarded headers from remote peers.
    peer = request.remote_addr or ""
    try:
        address = ipaddress.ip_address(peer)
        if address.is_loopback and request.headers.get("X-Real-IP"):
            return str(ipaddress.ip_address(request.headers["X-Real-IP"].strip()))
        return str(address)
    except ValueError:
        return peer if len(peer) <= 45 else "unknown"


def device_name(agent):
    platform = next((label for token, label in (
        ("iPad", "iPad"), ("iPhone", "iPhone"), ("Android", "Android"),
        ("Windows", "Windows"), ("Macintosh", "macOS"), ("Linux", "Linux"),
    ) if token in agent), "Устройство")
    browser = next((label for token, label in (
        ("Edg", "Edge"), ("Firefox", "Firefox"), ("FxiOS", "Firefox"),
        ("Chrome", "Chrome"), ("CriOS", "Chrome"), ("Safari", "Safari"),
    ) if token in agent), "Браузер")
    return f"{platform} · {browser}"


def utc_label(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def current_record():
    token = session.get("admin_session_token")
    if not isinstance(token, str) or not token:
        return None
    return get_db().execute(
        "SELECT * FROM admin_sessions WHERE token_hash = ? AND user_id = ?",
        (_digest(token), session.get("admin_user_id")),
    ).fetchone()


def _lifetime():
    return int(current_app.permanent_session_lifetime.total_seconds())


def establish(user, *, new_login=False, login_method="password"):
    now = int(time.time())
    db = get_db()
    previous = current_record()
    if (not new_login and previous and not previous["revoked_at"]
            and previous["expires_at"] > now and previous["user_id"] == user["id"]):
        db.execute("UPDATE admin_sessions SET auth_version = ?, last_seen_at = ?, expires_at = ? WHERE id = ?",
                   (user["auth_version"], now, now + _lifetime(), previous["id"]))
        db.commit()
        return session["admin_session_token"], previous["id"]
    if previous and not previous["revoked_at"]:
        revoke(user["id"], previous["id"], "replaced")
    token = secrets.token_urlsafe(32)
    cursor = db.execute(
        """INSERT INTO admin_sessions
        (user_id, token_hash, auth_version, ip_address, user_agent, login_method, started_at, last_seen_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user["id"], _digest(token), user["auth_version"], client_address(),
         request.headers.get("User-Agent", "")[:500], login_method, now, now, now + _lifetime()),
    )
    db.commit()
    return token, cursor.lastrowid


def validate(user):
    record = current_record()
    now = int(time.time())
    if (not record or record["revoked_at"] or record["expires_at"] <= now
            or record["auth_version"] != user["auth_version"]):
        return False
    expires_at = now + _lifetime()
    # Upgrade still-valid sessions created with the old one-hour lifetime on
    # their first visit. Expired/revoked sessions above must never be revived.
    if now - record["last_seen_at"] >= 60 or record["expires_at"] < expires_at - 60:
        get_db().execute("UPDATE admin_sessions SET last_seen_at = ?, expires_at = ? WHERE id = ? AND revoked_at IS NULL",
                         (now, expires_at, record["id"]))
        get_db().commit()
    return True


def revoke(user_id, record_id, reason="manual"):
    db = get_db()
    changed = db.execute(
        "UPDATE admin_sessions SET revoked_at = ?, revocation_reason = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
        (int(time.time()), reason, record_id, user_id),
    ).rowcount
    db.commit()
    return changed


def revoke_others(user_id):
    record = current_record()
    db = get_db()
    db.execute(
        "UPDATE admin_sessions SET revoked_at = ?, revocation_reason = 'manual' WHERE user_id = ? AND id != ? AND revoked_at IS NULL",
        (int(time.time()), user_id, record["id"]),
    )
    db.commit()


def history(user, page=1, per_page=12):
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM admin_sessions WHERE user_id = ?", (user["id"],)).fetchone()[0]
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), pages)
    rows = db.execute("SELECT * FROM admin_sessions WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                      (user["id"], per_page, (page - 1) * per_page)).fetchall()
    current = current_record()
    now = int(time.time())
    records = []
    for row in rows:
        active = not row["revoked_at"] and row["expires_at"] > now and row["auth_version"] == user["auth_version"]
        records.append({
            "id": row["id"], "ip_address": row["ip_address"], "device": device_name(row["user_agent"]),
            "user_agent": row["user_agent"], "started": utc_label(row["started_at"]),
            "last_seen": utc_label(row["last_seen_at"]), "active": active,
            "current": bool(current and current["id"] == row["id"]),
            "method": {"password": "Пароль", "totp": "Пароль + 2FA", "recovery": "Пароль + резервный код"}.get(row["login_method"], "Пароль"),
            "status": "Активен" if active else "Завершён",
        })
    return {"records": records, "page": page, "pages": pages, "total": total}
