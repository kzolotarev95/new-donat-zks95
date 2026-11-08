"""Small SQLite data layer with explicit schema and transaction boundaries."""
import json
import hashlib
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
    customer_nickname TEXT,
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
    last_login_at TEXT,
    must_change_password INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    is_secret INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS donors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT NOT NULL DEFAULT 'red',
    note TEXT NOT NULL DEFAULT '',
    total_amount TEXT NOT NULL DEFAULT '0',
    donations_count INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_donated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_donors_active ON donors(is_active, updated_at);
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
    columns = {row["name"] for row in db.execute("PRAGMA table_info(admin_users)").fetchall()}
    if "must_change_password" not in columns:
        db.execute("ALTER TABLE admin_users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
    payment_columns = {row["name"] for row in db.execute("PRAGMA table_info(payments)").fetchall()}
    if "customer_nickname" not in payment_columns:
        db.execute("ALTER TABLE payments ADD COLUMN customer_nickname TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS idx_payments_nickname ON payments(customer_nickname)")
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
         customer_email, customer_nickname, message, payload_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            payment["payment_id"], payment["idempotency_key"], payment["status"],
            payment["amount"], payment["currency"], payment["description"],
            payment.get("customer_email"), payment.get("customer_nickname"), payment.get("message"),
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


DONOR_COLORS = {
    "red": "Красный",
    "orange": "Оранжевый",
    "gold": "Золотой",
    "blue": "Синий",
    "green": "Зелёный",
    "violet": "Фиолетовый",
}
DONOR_COLOR_VALUES = {
    "red": "#e10620",
    "orange": "#ed6a24",
    "gold": "#c99418",
    "blue": "#326fe4",
    "green": "#269b65",
    "violet": "#8056d9",
}
for _color_number in range(1, 101):
    _token = f"c{_color_number:03d}"
    DONOR_COLORS[_token] = f"Цвет {_color_number:02d}"
    DONOR_COLOR_VALUES[_token] = f"hsl({(_color_number * 137) % 360} 78% 58%)"
DONOR_PALETTE_KEYS = [f"c{number:03d}" for number in range(1, 101)]


def donor_color(token):
    return DONOR_COLOR_VALUES.get(token, DONOR_COLOR_VALUES["red"])


def list_donors(active_only=True, limit=60):
    where = "WHERE is_active = 1" if active_only else ""
    return get_db().execute(
        f"SELECT * FROM donors {where} ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()


def create_donor(nickname, color="red", note="", total_amount="0"):
    if color not in DONOR_COLORS:
        color = "red"
    now = _now()
    cursor = get_db().execute(
        "INSERT INTO donors(nickname, color, note, total_amount, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (nickname, color, note, str(total_amount), now, now),
    )
    get_db().commit()
    return cursor.lastrowid


def update_donor(donor_id, nickname, color, note, total_amount="0", is_active=1):
    if color not in DONOR_COLORS:
        color = "red"
    get_db().execute(
        "UPDATE donors SET nickname = ?, color = ?, note = ?, total_amount = ?, is_active = ?, updated_at = ? WHERE id = ?",
        (nickname, color, note, str(total_amount), int(bool(is_active)), _now(), donor_id),
    )
    get_db().commit()


def delete_donor(donor_id):
    get_db().execute("DELETE FROM donors WHERE id = ?", (donor_id,))
    get_db().commit()


def get_donor(donor_id):
    return get_db().execute("SELECT * FROM donors WHERE id = ?", (donor_id,)).fetchone()


def record_donor_payment(nickname, amount):
    """Create or update the public donor card after a confirmed payment."""
    nickname = (nickname or "").strip()[:64]
    if not nickname:
        return
    color_index = int(hashlib.sha256(nickname.lower().encode("utf-8")).hexdigest()[:8], 16)
    color = DONOR_PALETTE_KEYS[color_index % len(DONOR_PALETTE_KEYS)]
    now = _now()
    db = get_db()
    db.execute(
        """INSERT INTO donors(nickname, color, note, total_amount, donations_count,
           is_active, created_at, updated_at, last_donated_at)
           VALUES (?, ?, '', ?, 1, 1, ?, ?, ?)
           ON CONFLICT(nickname) DO UPDATE SET
             total_amount = CAST(total_amount AS REAL) + CAST(excluded.total_amount AS REAL),
             donations_count = donations_count + 1,
             updated_at = excluded.updated_at,
             last_donated_at = excluded.last_donated_at""",
        (nickname, color, str(amount), now, now, now),
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
    get_db().execute("INSERT INTO admin_users(username, password_hash, created_at, updated_at, must_change_password) VALUES (?, ?, ?, ?, ?)", (username, generate_password_hash(password), now, now, int(password == "admin")))
    get_db().commit()

def get_admin_user(username):
    return get_db().execute("SELECT * FROM admin_users WHERE username = ?", (username,)).fetchone()

def get_admin_user_by_id(user_id):
    return get_db().execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()

def update_admin_password(user_id, password_hash, must_change_password=0):
    get_db().execute("UPDATE admin_users SET password_hash = ?, updated_at = ?, must_change_password = ? WHERE id = ?", (password_hash, _now(), must_change_password, user_id))
    get_db().commit()

def update_admin_account(user_id, username, password_hash=None):
    if password_hash:
        get_db().execute("UPDATE admin_users SET username = ?, password_hash = ?, updated_at = ?, must_change_password = 0 WHERE id = ?", (username, password_hash, _now(), user_id))
    else:
        get_db().execute("UPDATE admin_users SET username = ?, updated_at = ? WHERE id = ?", (username, _now(), user_id))
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
        clauses.append("(payment_id LIKE ? OR customer_email LIKE ? OR customer_nickname LIKE ?)"); params.extend([f"%{query}%", f"%{query}%", f"%{query}%"])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.extend([limit, offset])
    return get_db().execute(f"SELECT * FROM payments{where} ORDER BY created_at DESC LIMIT ? OFFSET ?", params).fetchall()

def count_payments(status="", query=""):
    clauses, params = [], []
    if status:
        clauses.append("status = ?"); params.append(status)
    if query:
        clauses.append("(payment_id LIKE ? OR customer_email LIKE ? OR customer_nickname LIKE ?)"); params.extend([f"%{query}%", f"%{query}%", f"%{query}%"])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return get_db().execute(f"SELECT COUNT(*) AS n FROM payments{where}", params).fetchone()["n"]
