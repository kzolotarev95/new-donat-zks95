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
