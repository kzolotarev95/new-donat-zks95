"""Durable login push outbox, delivered outside HTTP requests with bounded retries."""
import json
import logging
import secrets
import threading
import time

from flask import current_app
from .admin_sessions import device_name, utc_label
from .db import get_db
from .push import login_notifications_enabled, push_enabled, send_push

log = logging.getLogger(__name__)
_delivery_lock = threading.Lock()


def enqueue_login(user, session_id):
    if not push_enabled() or not login_notifications_enabled():
        return
    db = get_db()
    record = db.execute("SELECT * FROM admin_sessions WHERE id = ? AND user_id = ?", (session_id, user["id"])).fetchone()
    if not record:
        return
    payload = {
        "title": "Вход в панель ZKS95",
        "body": f"{user['username']} · {record['ip_address']}\n{device_name(record['user_agent'])}\n{utc_label(record['started_at'])}",
        "icon": "/static/favicon.svg", "badge": "/static/favicon.svg",
        "tag": f"admin-login-{session_id}", "data": {"url": "/admin/account#sessions"},
    }
    db.execute("INSERT OR IGNORE INTO admin_login_notifications(session_id, payload_json, available_at) VALUES (?, ?, ?)",
               (session_id, json.dumps(payload, ensure_ascii=False), int(time.time())))
    db.commit()
    if not current_app.testing:
        _schedule(current_app._get_current_object())


def _schedule(app):
    # At most one background sender per web worker. The timer handles the rest.
    if not _delivery_lock.acquire(blocking=False):
        return

    def deliver():
        try:
            with app.app_context():
                drain_pending()
        except Exception:
            log.exception("Login push background delivery failed; timer will retry")
        finally:
            _delivery_lock.release()

    try:
        threading.Thread(target=deliver, name="login-push", daemon=True).start()
    except Exception:
        _delivery_lock.release()
        log.exception("Could not start login push sender; timer will retry")


def drain_pending(limit=5):
    db = get_db()
    handled = 0
    for _ in range(limit):
        now = int(time.time())
        record = db.execute(
            """SELECT * FROM admin_login_notifications WHERE
            (status = 'pending' AND available_at <= ?) OR (status = 'processing' AND lease_until <= ?)
            ORDER BY id LIMIT 1""", (now, now),
        ).fetchone()
        if not record:
            break
        claim = secrets.token_urlsafe(16)
        changed = db.execute(
            """UPDATE admin_login_notifications SET status = 'processing', claim_token = ?,
            lease_until = ?, attempts = attempts + 1 WHERE id = ? AND
            ((status = 'pending' AND available_at <= ?) OR (status = 'processing' AND lease_until <= ?))""",
            (claim, now + 600, record["id"], now, now),
        ).rowcount
        db.commit()
        if not changed:
            continue
        try:
            if not login_notifications_enabled():
                result = {"sent": 0, "failed": 0, "skipped": "disabled"}
            else:
                result = send_push(json.loads(record["payload_json"]))
        except Exception:
            log.exception("Login push delivery failed")
            result = {"sent": 0, "failed": 1, "skipped": "delivery_error"}
        attempts = record["attempts"] + 1
        if result.get("sent") and not result.get("failed"):
            status = "sent"
        elif result.get("skipped") == "disabled":
            status = "cancelled"
        else:
            status = "pending" if attempts < 3 else "failed"
        db.execute(
            """UPDATE admin_login_notifications SET status = ?, available_at = ?, lease_until = 0,
            claim_token = '', last_error = ? WHERE id = ? AND claim_token = ?""",
            (status, int(time.time()) + 60 * attempts,
             "" if status == "sent" else result.get("skipped") or "delivery_failed", record["id"], claim),
        )
        db.commit()
        handled += 1
    return handled
