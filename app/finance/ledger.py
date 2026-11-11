"""Validation and accounting primitives. No guessed fees or bank settlement."""

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import session, has_request_context
from ..db import get_db
from ..settings import get_runtime_setting

RECEIPT_STATUSES = {
    "pending": "Ожидает оформления",
    "issued": "Чек оформлен",
    "verified": "Проверен",
    "refund": "Возврат",
    "excluded": "Исключён из учета",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def money(raw, optional=False):
    if optional and (raw is None or raw == ""):
        return None
    try:
        value = Decimal(str(raw).replace("\xa0", "").replace(" ", "").replace(",", "."))
        if (
            not value.is_finite()
            or value < 0
            or value > Decimal("100000000000")
            or value != value.quantize(Decimal(".01"))
        ):
            raise ValueError
        return int(value * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(
            "Введите неотрицательную сумму с точностью до копейки."
        ) from None


def format_money(value):
    return "—" if value is None else f"{Decimal(value) / 100:,.2f}".replace(",", " ")


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=tz())
        return result.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        raise ValueError(
            "Некорректная дата; используйте ГГГГ-ММ-ДД или ISO 8601."
        ) from None


def tz():
    try:
        return ZoneInfo(get_runtime_setting("FINANCE_TIMEZONE", "Europe/Samara"))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Europe/Samara")


def text(raw, limit=500, required=False):
    result = str(raw or "").strip()
    if len(result) > limit or (required and not result):
        raise ValueError(f"Заполните поле (не более {limit} символов).")
    return result


def receipt_url(raw):
    raw = text(raw, 1000)
    if raw:
        parts = urlsplit(raw)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or any(ord(c) < 33 for c in raw)
        ):
            raise ValueError("Ссылка на чек должна быть HTTPS без логина и пароля.")
    return raw


def invalidate(db=None):
    (db or get_db()).execute("DELETE FROM finance_cache")


def audit(db, action, entity, before, after, actor=None):
    db.execute(
        "INSERT INTO finance_audit_log(actor,action,entity,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)",
        (
            actor
            or (
                session.get("admin_username", "system")
                if has_request_context()
                else "system"
            ),
            action,
            entity,
            json.dumps(before, ensure_ascii=False),
            json.dumps(after, ensure_ascii=False),
            now(),
        ),
    )
    invalidate(db)


def upsert_transaction(db, obj, source="yookassa"):
    pid = text(obj.get("id"), 200, True)
    amount = obj.get("amount") or {}
    currency = text(amount.get("currency"), 3, True)
    gross = money(amount.get("value"))
    income = obj.get("income_amount")
    fee = None
    if income and income.get("currency") == currency:
        fee = gross - money(income.get("value"))
        if fee < 0:
            raise ValueError("Доход ЮKassa превышает сумму платежа.")
    refunded = obj.get("refunded_amount") or {}
    refund = (
        money(refunded.get("value", "0"))
        if refunded.get("currency", currency) == currency
        else 0
    )
    refund = max(
        refund,
        db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM finance_refunds WHERE payment_id=? AND status='succeeded' AND currency=?",
            (pid, currency),
        ).fetchone()[0],
    )
    if refund > gross:
        raise ValueError("Возврат превышает сумму платежа.")
    status = text(obj.get("status"), 40, True)
    if status not in ("pending", "waiting_for_capture", "succeeded", "canceled"):
        raise ValueError("Неизвестный статус платежа.")
    previous = db.execute(
        "SELECT amount,currency,status FROM finance_transactions WHERE payment_id=?",
        (pid,),
    ).fetchone()
    if previous and (previous["amount"] != gross or previous["currency"] != currency):
        raise ValueError("Сумма или валюта существующего платежа изменилась.")
    if previous and previous["status"] in ("succeeded", "canceled"):
        status = previous["status"]
    captured = timestamp(obj["captured_at"]) if obj.get("captured_at") else None
    created = timestamp(obj.get("created_at") or now())
    metadata = obj.get("metadata") or {}
    donor = text(metadata.get("nickname") or metadata.get("customer_nickname"), 64)
    db.execute(
        """INSERT INTO finance_transactions(payment_id,status,amount,currency,fee,refunded,donor,description,method,
      created_at,captured_at,updated_at,source,is_test) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(payment_id) DO UPDATE SET status=excluded.status,amount=excluded.amount,currency=excluded.currency,
      fee=COALESCE(excluded.fee,finance_transactions.fee),refunded=MAX(excluded.refunded,finance_transactions.refunded),
      donor=CASE WHEN excluded.donor='' THEN finance_transactions.donor ELSE excluded.donor END,
      description=excluded.description,method=excluded.method,created_at=excluded.created_at,
      captured_at=COALESCE(excluded.captured_at,finance_transactions.captured_at),updated_at=excluded.updated_at,source=excluded.source,
      is_test=MAX(finance_transactions.is_test,excluded.is_test)""",
        (
            pid,
            status,
            gross,
            currency,
            fee,
            refund,
            donor,
            text(obj.get("description"), 500),
            text((obj.get("payment_method") or {}).get("type"), 50),
            created,
            captured,
            now(),
            source,
            int(obj.get("test") is True),
        ),
    )
    invalidate(db)


def mirror_payment(db, row):
    try:
        obj = json.loads(row.get("payload_json") or "{}")
        if isinstance(obj, str):
            obj = json.loads(obj)
        if not isinstance(obj, dict):
            obj = {}
        obj.update(
            id=row["payment_id"],
            status=row["status"],
            amount={"value": row["amount"], "currency": row["currency"]},
        )
        obj.setdefault("created_at", row["created_at"])
        obj.setdefault("description", row.get("description", ""))
        if row.get("paid_at"):
            obj.setdefault("captured_at", row["paid_at"])
        obj["metadata"] = (
            obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
        )
        obj["metadata"]["nickname"] = row.get("customer_nickname") or ""
        upsert_transaction(db, obj, "site")
    except (ValueError, TypeError, KeyError, AttributeError):
        # A legacy malformed row must not prevent application startup or checkout.
        return


def income_rows(db=None):
    db = db or get_db()
    rows = [
        dict(r)
        for r in db.execute("""SELECT 'payment:'||payment_id income_key, payment_id reference,
      COALESCE(captured_at,created_at) income_at, amount, currency, fee, refunded, donor,
      'ЮKassa' source FROM finance_transactions WHERE status='succeeded' AND is_test=0 """)
    ]
    rows += [
        dict(r)
        for r in db.execute("""SELECT 'bank:'||id income_key,external_id reference,booked_at income_at,
      amount,currency,NULL fee,0 refunded,'' donor,source FROM finance_bank_operations WHERE kind='income' """)
    ]
    receipts = {
        r["income_key"]: dict(r) for r in db.execute("SELECT * FROM finance_receipts")
    }
    zone = tz()
    today = datetime.now(zone).date().isoformat()
    for r in rows:
        r["income_day"] = (
            datetime.fromisoformat(r["income_at"]).astimezone(zone).date().isoformat()
        )
        r["receipt"] = receipts.get(
            r["income_key"],
            {
                "status": "pending",
                "customer_category": "individual",
                "receipt_date": "",
                "receipt_number": "",
                "receipt_url": "",
                "comment": "",
            },
        )
        r["base"] = max(0, r["amount"] - r["refunded"])
        r["overdue"] = (
            r["base"] > 0
            and r["receipt"]["status"] == "pending"
            and r["income_day"] < today
        )
    return sorted(rows, key=lambda r: r["income_at"], reverse=True)
