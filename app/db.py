"""Small SQLite data layer with explicit schema and transaction boundaries."""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    description TEXT NOT NULL,
    customer_email TEXT,
    message TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    paid_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments(created_at);
CREATE TABLE IF NOT EXISTS admin_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    is_secret INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_db():
    if "db" not in g:
        path = Path(current_app.config["DATABASE_PATH"])
        path.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(path, timeout=20)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()
    _ensure_admin_from_env()


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def create_payment_record(payment):
    db = get_db()
    now = _now()
    db.execute(
        """INSERT INTO payments
        (payment_id, idempotency_key, status, amount, currency, description,
         customer_email, message, payload_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            payment["payment_id"], payment["idempotency_key"], payment["status"],
            payment["amount"], payment["currency"], payment["description"],
            payment.get("customer_email"), payment.get("message"),
            json.dumps(payment.get("payload", {}), ensure_ascii=False), now, now,
        ),
    )
    db.commit()


def get_payment(payment_id):
    return get_db().execute("SELECT * FROM payments WHERE payment_id = ?", (payment_id,)).fetchone()


def update_payment(payment_id, status, payload=None, paid=False):
    db = get_db()
    now = _now()
    db.execute(
        """UPDATE payments SET status = ?, payload_json = ?, updated_at = ?, paid_at =
        CASE WHEN ? THEN COALESCE(paid_at, ?) ELSE paid_at END WHERE payment_id = ?""",
        (status, json.dumps(payload or {}, ensure_ascii=False), now, paid, now, payment_id),
    )
    db.commit()


def _ensure_admin_from_env():
    from flask import current_app
    from werkzeug.security import generate_password_hash
    username = (current_app.config.get("ADMIN_USERNAME") or "").strip()
    password = current_app.config.get("ADMIN_PASSWORD") or ""
    if not username or not password:
        return
    if get_db().execute("SELECT 1 FROM admin_users LIMIT 1").fetchone():
        return
    now = _now()
    get_db().execute("INSERT INTO admin_users(username, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?)", (username, generate_password_hash(password), now, now))
    get_db().commit()

def get_admin_user(username):
    return get_db().execute("SELECT * FROM admin_users WHERE username = ?", (username,)).fetchone()

def get_admin_user_by_id(user_id):
    return get_db().execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()

def update_admin_password(user_id, password_hash):
    get_db().execute("UPDATE admin_users SET password_hash = ?, updated_at = ? WHERE id = ?", (password_hash, _now(), user_id))
    get_db().commit()

def mark_admin_login(user_id):
    get_db().execute("UPDATE admin_users SET last_login_at = ? WHERE id = ?", (_now(), user_id))
    get_db().commit()

def payment_stats():
    db = get_db()
    total = db.execute("SELECT COUNT(*) AS n FROM payments").fetchone()["n"]
    succeeded = db.execute("SELECT COUNT(*) AS n FROM payments WHERE status = 'succeeded'").fetchone()["n"]
    pending = db.execute("SELECT COUNT(*) AS n FROM payments WHERE status = 'pending'").fetchone()["n"]
    amount = db.execute("SELECT COALESCE(SUM(CAST(amount AS REAL)), 0) AS n FROM payments WHERE status = 'succeeded'").fetchone()["n"]
    return {"total": total, "succeeded": succeeded, "pending": pending, "amount": amount}

def list_payments(limit=30, offset=0, status="", query=""):
    clauses, params = [], []
    if status:
        clauses.append("status = ?"); params.append(status)
    if query:
        clauses.append("(payment_id LIKE ? OR customer_email LIKE ?)"); params.extend([f"%{query}%", f"%{query}%"])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.extend([limit, offset])
    return get_db().execute(f"SELECT * FROM payments{where} ORDER BY created_at DESC LIMIT ? OFFSET ?", params).fetchall()

def count_payments(status="", query=""):
    clauses, params = [], []
    if status:
        clauses.append("status = ?"); params.append(status)
    if query:
        clauses.append("(payment_id LIKE ? OR customer_email LIKE ?)"); params.extend([f"%{query}%", f"%{query}%"])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return get_db().execute(f"SELECT COUNT(*) AS n FROM payments{where}", params).fetchone()["n"]
