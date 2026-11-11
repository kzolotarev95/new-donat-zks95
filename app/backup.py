"""Portable, authenticated JSON backups for the admin panel."""
from datetime import datetime, timezone
import sqlite3

from .db import get_db
from .security import encrypt_value, decrypt_value
from .settings import SECRET_KEYS
from .finance.schema import TABLES as FINANCE_TABLES

FORMAT = "zks95-backup"
VERSION = 1


def _now():
    return datetime.now(timezone.utc).isoformat()


def _rows(table):
    return [dict(row) for row in get_db().execute(f"SELECT * FROM {table}").fetchall()]


def _settings():
    result = []
    for row in get_db().execute("SELECT key, value, is_secret FROM app_settings ORDER BY key").fetchall():
        value = decrypt_value(row["value"]) if row["is_secret"] else row["value"]
        result.append({"key": row["key"], "value": value, "is_secret": bool(row["is_secret"])})
    return result


def export_backup(kind):
    db = get_db()
    snapshot = not db.in_transaction
    if snapshot:
        db.execute('BEGIN')
    try:
        return _export_backup(kind)
    finally:
        if snapshot:
            db.rollback()  # Release the consistent read snapshot; no data was changed.


def _export_backup(kind):
    if kind == "donors":
        return {
            "format": FORMAT,
            "version": VERSION,
            "kind": "donors",
            "created_at": _now(),
            "donors": _rows("donors"),
        }
    if kind != "full":
        raise ValueError("Неизвестный тип бэкапа")
    return {
        "format": FORMAT,
        "version": VERSION,
        "kind": "full",
        "created_at": _now(),
        "warning": "Файл содержит секретные настройки YooKassa, reCAPTCHA и VAPID. Храните его как пароль.",
        "settings": _settings(),
        "tables": {
            "payments": _rows("payments"),
            "donors": _rows("donors"),
            "push_subscriptions": _rows("push_subscriptions"),
            **{table: _rows(table) for table in FINANCE_TABLES},
        },
    }


def _validate(payload):
    if not isinstance(payload, dict) or payload.get("format") != FORMAT or payload.get("version") != VERSION:
        raise ValueError("Неподдерживаемый формат бэкапа")
    if payload.get("kind") not in {"donors", "full"}:
        raise ValueError("В бэкапе отсутствует корректный тип")


def _text(value, maximum=1000):
    return str(value or "")[:maximum]


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _insert_donors(db, donors, preserve_ids=True):
    for donor in donors:
        nickname = _text(donor.get("nickname"), 64).strip()
        if len(nickname) < 2:
            continue
        db.execute(
            """INSERT INTO donors(id, nickname, color, note, social_url, total_amount, donations_count, pinned,
               is_active, created_at, updated_at, last_donated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(nickname) DO UPDATE SET color=excluded.color, note=excluded.note,
                 social_url=excluded.social_url, total_amount=excluded.total_amount,
                 donations_count=excluded.donations_count, pinned=excluded.pinned,
                 is_active=excluded.is_active, updated_at=excluded.updated_at, last_donated_at=excluded.last_donated_at""",
            (
                _int(donor.get("id"), None) if preserve_ids else None, nickname, _text(donor.get("color"), 12) or "red",
                _text(donor.get("note"), 120), _text(donor.get("social_url"), 500),
                _text(donor.get("total_amount"), 40) or "0", _int(donor.get("donations_count")),
                _int(donor.get("pinned")), _int(donor.get("is_active"), 1),
                _text(donor.get("created_at"), 64) or _now(), _text(donor.get("updated_at"), 64) or _now(),
                _text(donor.get("last_donated_at"), 64),
            ),
        )


def _insert_payments(db, payments):
    for payment in payments:
        if not payment.get("payment_id") or not payment.get("idempotency_key"):
            continue
        db.execute(
            """INSERT OR REPLACE INTO payments(id, payment_id, idempotency_key, status, amount, currency, description,
               customer_email, customer_nickname, message, payload_json, created_at, updated_at, paid_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _int(payment.get("id"), None), _text(payment.get("payment_id"), 200), _text(payment.get("idempotency_key"), 200),
                _text(payment.get("status"), 40), _text(payment.get("amount"), 40), _text(payment.get("currency"), 12),
                _text(payment.get("description"), 500), _text(payment.get("customer_email"), 320),
                _text(payment.get("customer_nickname"), 64), _text(payment.get("message"), 500),
                _text(payment.get("payload_json"), 2000000) or "{}", _text(payment.get("created_at"), 64) or _now(),
                _text(payment.get("updated_at"), 64) or _now(), _text(payment.get("paid_at"), 64),
            ),
        )


def _insert_subscriptions(db, subscriptions):
    for subscription in subscriptions:
        if not subscription.get("endpoint"):
            continue
        db.execute(
            """INSERT OR REPLACE INTO push_subscriptions(id, endpoint, p256dh, auth, user_agent, is_active,
               created_at, updated_at, last_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _int(subscription.get("id"), None), _text(subscription.get("endpoint"), 2000),
                _text(subscription.get("p256dh"), 500), _text(subscription.get("auth"), 500),
                _text(subscription.get("user_agent"), 300), _int(subscription.get("is_active"), 1),
                _text(subscription.get("created_at"), 64) or _now(), _text(subscription.get("updated_at"), 64) or _now(),
                _text(subscription.get("last_error"), 500),
            ),
        )


def import_backup(payload, replace_donors=False):
    _validate(payload)
    db = get_db()
    if payload["kind"] == "donors":
        donors = payload.get("donors")
        if not isinstance(donors, list):
            raise ValueError("В бэкапе нет списка донатеров")
        try:
            db.execute("BEGIN")
            if replace_donors:
                db.execute("DELETE FROM donors")
            _insert_donors(db, donors, preserve_ids=False)
            db.commit()
        except Exception:
            db.rollback()
            raise
        return {"message": f"донатеров: {len(donors)}"}

    tables = payload.get("tables") or {}
    settings = payload.get("settings") or []
    if not isinstance(settings, list) or not isinstance(tables, dict):
        raise ValueError("Неполные данные полного бэкапа")
    try:
        db.execute("BEGIN")
        import time
        if db.execute('SELECT lease_until FROM finance_sync WHERE id=1').fetchone()[0] > time.time():
            raise ValueError('Дождитесь завершения фоновой синхронизации перед восстановлением.')
        for table in reversed(FINANCE_TABLES):
            db.execute(f"DELETE FROM {table}")
        db.execute("DELETE FROM finance_cache")
        db.execute("UPDATE finance_sync SET requested=0,lease_until=0,token='',cursor='',since_at='',stage='payments',last_success='',last_attempt='',retry_at=0,last_error=''")
        db.execute("DELETE FROM payments")
        db.execute("DELETE FROM donors")
        db.execute("DELETE FROM push_subscriptions")
        db.execute("DELETE FROM app_settings")
        _insert_payments(db, tables.get("payments") or [])
        _insert_donors(db, tables.get("donors") or [])
        _insert_subscriptions(db, tables.get("push_subscriptions") or [])
        if 'finance_transactions' in tables:
            for table in FINANCE_TABLES:
                rows = tables.get(table) or []
                if not isinstance(rows, list) or len(rows)>100000:
                    raise ValueError('Некорректные финансовые таблицы в бэкапе')
                columns = [r['name'] for r in db.execute(f'PRAGMA table_info({table})')]
                for row in rows:
                    if not isinstance(row, dict) or set(row) != set(columns):
                        raise ValueError('Некорректные поля финансовой записи')
                    from .finance.ledger import receipt_url, timestamp
                    for name, value in row.items():
                        if name in ('amount','fee','refunded') and value is not None:
                            if type(value) is not int or not 0 <= value <= 10000000000000:
                                raise ValueError('Некорректная финансовая сумма в бэкапе')
                        if name.endswith('_at') and value:
                            timestamp(value)
                    if table == 'finance_receipts':
                        receipt_url(row['receipt_url'])
                        if row['receipt_date']:
                            datetime.strptime(row['receipt_date'],'%Y-%m-%d')
                        if row['status'] in ('issued','verified') and (not row['receipt_number'] or not row['receipt_date']):
                            raise ValueError('В оформленном чеке отсутствует номер или дата')
                    if table == 'finance_transactions':
                        if row['refunded']>row['amount'] or (row['fee'] is not None and row['fee']>row['amount']):
                            raise ValueError('Комиссия или возврат превышает сумму платежа')
                        if row['status'] not in ('pending','waiting_for_capture','succeeded','canceled'):
                            raise ValueError('Некорректный статус платежа')
                    placeholders = ','.join('?' for _ in columns)
                    db.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})", [row[c] for c in columns])
            mismatch = db.execute('''SELECT 1 FROM finance_payouts p LEFT JOIN finance_bank_operations b ON b.id=p.bank_id
              WHERE p.status='confirmed' AND (b.id IS NULL OR b.amount!=p.amount OR b.currency!=p.currency OR b.kind!='settlement') LIMIT 1''').fetchone()
            if mismatch:
                raise ValueError('Подтверждённые выплаты в бэкапе не соответствуют банковским операциям')
        else:
            from .finance.ledger import mirror_payment
            for row in db.execute('SELECT * FROM payments'):
                mirror_payment(db, dict(row))
        for setting in settings:
            key = _text(setting.get("key"), 120)
            if not key:
                continue
            value = _text(setting.get("value"), 1000000)
            stored = encrypt_value(value) if key in SECRET_KEYS else value
            db.execute(
                "INSERT INTO app_settings(key, value, is_secret, updated_at) VALUES (?, ?, ?, ?)",
                (key, stored, int(key in SECRET_KEYS), _now()),
            )
        from .finance.ledger import audit
        audit(db,'full_backup_restored','backup',{}, {'finance_transactions':db.execute('SELECT COUNT(*) FROM finance_transactions').fetchone()[0]})
        db.commit()
    except sqlite3.DatabaseError as exc:
        db.rollback()
        raise ValueError('Не удалось восстановить финансовые данные. Проверьте целостность бэкапа и повторите попытку.') from exc
    except Exception:
        db.rollback()
        raise
    return {"message": f"полный бэкап: платежей {len(tables.get('payments') or [])}, донатеров {len(tables.get('donors') or [])}"}
