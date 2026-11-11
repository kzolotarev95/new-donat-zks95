"""Audited manual reconciliation and tax receipt tracking."""

from datetime import date
from .ledger import (
    money,
    text,
    timestamp,
    receipt_url,
    now,
    audit,
    income_rows,
    RECEIPT_STATUSES,
)
from ..db import get_db


def save_receipt(key, data):
    db = get_db()
    income = next((r for r in income_rows(db) if r["income_key"] == key), None)
    if not income:
        raise ValueError("Доход не найден.")
    status = data.get("status")
    if status not in RECEIPT_STATUSES or data.get("confirm") != "1":
        raise ValueError("Выберите статус и подтвердите ручное изменение.")
    category = data.get("customer_category", "individual")
    if category not in ("individual", "business", "unknown"):
        raise ValueError("Некорректная категория покупателя.")
    number = text(data.get("receipt_number"), 200)
    day = text(data.get("receipt_date"), 10)
    link = receipt_url(data.get("receipt_url"))
    comment = text(data.get("comment"), 1000)
    if status in ("issued", "verified"):
        if not number or not day:
            raise ValueError("Укажите номер и дату оформленного чека.")
    if day:
        try:
            date.fromisoformat(day)
        except ValueError:
            raise ValueError("Некорректная дата чека.") from None
    if status in ("refund", "excluded") and not comment:
        raise ValueError("Укажите причину возврата или исключения.")
    after = {
        "status": status,
        "customer_category": category,
        "receipt_date": day,
        "receipt_number": number,
        "receipt_url": link,
        "comment": comment,
    }
    with db:
        db.execute(
            """INSERT INTO finance_receipts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(income_key) DO UPDATE SET
         status=excluded.status,customer_category=excluded.customer_category,receipt_date=excluded.receipt_date,
         receipt_number=excluded.receipt_number,receipt_url=excluded.receipt_url,comment=excluded.comment,updated_at=excluded.updated_at""",
            (key, status, category, day, number, link, comment, now()),
        )
        audit(db, "receipt_update", key, income["receipt"], after)


def create_payout(data):
    amount = money(data.get("amount"))
    fee = money(data.get("fee"), optional=True)
    if not amount:
        raise ValueError("Сумма выплаты должна быть положительной.")
    db = get_db()
    with db:
        cursor = db.execute(
            """INSERT INTO finance_payouts(external_id,expected_at,amount,fee,currency,source,comment,created_at)
          VALUES(?,?,?,?,?,?,?,?)""",
            (
                text(data.get("external_id"), 200, True),
                timestamp(data.get("date")),
                amount,
                fee,
                "RUB",
                text(data.get("source"), 120, True),
                text(data.get("comment"), 1000),
                now(),
            ),
        )
        audit(
            db,
            "payout_created",
            f"payout:{cursor.lastrowid}",
            {},
            dict(
                db.execute(
                    "SELECT * FROM finance_payouts WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
            ),
        )
    return cursor.lastrowid


def link_payment(payout_id, payment_id):
    db = get_db()
    with db:
        payout = db.execute(
            "SELECT * FROM finance_payouts WHERE id=?", (payout_id,)
        ).fetchone()
        tx = db.execute(
            "SELECT * FROM finance_transactions WHERE payment_id=?", (payment_id,)
        ).fetchone()
        if (
            not payout
            or payout["status"] == "canceled"
            or not tx
            or tx["status"] != "succeeded"
            or tx["is_test"]
            or tx["currency"] != payout["currency"]
        ):
            raise ValueError("Можно связать только успешный платёж в валюте выплаты.")
        amount = max(0, tx["amount"] - tx["refunded"] - (tx["fee"] or 0))
        if tx["fee"] is None:
            raise ValueError(
                "Комиссия платежа неизвестна. Сначала синхронизируйте данные или укажите подтверждённую комиссию."
            )
        existing = db.execute(
            "SELECT * FROM finance_reconciliation WHERE payment_id=?", (payment_id,)
        ).fetchone()
        if existing:
            raise ValueError("Этот платёж уже связан с выплатой.")
        db.execute(
            "INSERT INTO finance_reconciliation VALUES(?,?,?,?)",
            (payment_id, payout_id, amount, now()),
        )
        audit(
            db,
            "payment_linked",
            f"payout:{payout_id}",
            {},
            {"payment_id": payment_id, "amount": amount},
        )


def confirm_payout(payout_id, bank_id, comment, confirmed):
    if confirmed != "1":
        raise ValueError("Подтвердите фактическое банковское поступление.")
    db = get_db()
    with db:
        p = db.execute(
            "SELECT * FROM finance_payouts WHERE id=?", (payout_id,)
        ).fetchone()
        b = db.execute(
            "SELECT * FROM finance_bank_operations WHERE id=?", (bank_id,)
        ).fetchone()
        if not p or not b or p["status"] != "expected":
            raise ValueError("Выплата или банковская операция недоступна.")
        if p["currency"] != b["currency"] or p["amount"] != b["amount"]:
            raise ValueError("Сумма или валюта не совпадает; проверьте расхождение.")
        if b["kind"] not in ("unclassified", "settlement"):
            raise ValueError("Операция уже учтена как отдельный доход или исключена.")
        db.execute(
            "UPDATE finance_payouts SET status='confirmed',bank_id=?,credited_at=?,comment=? WHERE id=?",
            (bank_id, b["booked_at"], text(comment, 1000, True), payout_id),
        )
        db.execute(
            "UPDATE finance_bank_operations SET kind='settlement' WHERE id=?",
            (bank_id,),
        )
        audit(
            db,
            "bank_receipt_confirmed",
            f"payout:{payout_id}",
            dict(p),
            {"bank_id": bank_id, "comment": comment, "source": b["source"]},
        )


def classify_bank(bank_id, kind, comment, confirmed):
    if kind not in ("unclassified", "income", "excluded") or confirmed != "1":
        raise ValueError("Подтвердите категорию операции.")
    db = get_db()
    with db:
        row = db.execute(
            "SELECT * FROM finance_bank_operations WHERE id=?", (bank_id,)
        ).fetchone()
        if (
            not row
            or db.execute(
                "SELECT 1 FROM finance_payouts WHERE bank_id=?", (bank_id,)
            ).fetchone()
        ):
            raise ValueError(
                "Операция связана с выплатой и не может считаться отдельным доходом."
            )
        evidence = (row["description"] + " " + row["source"]).lower()
        if kind == "income" and any(
            name in evidence for name in ("юкасс", "yookass", "yoomoney", "юmoney")
        ):
            raise ValueError(
                "Зачисление ЮKassa сопоставьте с выплатой, чтобы не задвоить доход."
            )
        db.execute(
            "UPDATE finance_bank_operations SET kind=? WHERE id=?", (kind, bank_id)
        )
        audit(
            db,
            "bank_classified",
            f"bank:{bank_id}",
            dict(row),
            {"kind": kind, "comment": text(comment, 1000, True)},
        )


def reconcile(limit=None, offset=0, include_canceled=False):
    """Produce suggestions only. A human confirms every actual bank receipt."""
    db = get_db()
    result = []
    sql = (
        "SELECT * FROM finance_payouts"
        + ("" if include_canceled else " WHERE status!='canceled'")
        + " ORDER BY expected_at DESC,id DESC"
    )
    parameters = ()
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        parameters = (limit, offset)
    for p in db.execute(sql, parameters):
        linked = [
            dict(r)
            for r in db.execute(
                """SELECT t.payment_id,t.status,t.fee,t.refunded,t.amount,l.amount original_amount
          FROM finance_reconciliation l JOIN finance_transactions t ON t.payment_id=l.payment_id
          WHERE l.payout_id=?""",
                (p["id"],),
            )
        ]
        linked_amount = sum(
            max(0, r["amount"] - r["refunded"] - (r["fee"] or 0))
            for r in linked
            if r["status"] == "succeeded"
        )
        uncertain = any(r["fee"] is None or r["status"] != "succeeded" for r in linked)
        candidates = [
            dict(b)
            for b in db.execute(
                """SELECT * FROM finance_bank_operations b WHERE amount=? AND currency=?
          AND kind IN ('unclassified','settlement') AND NOT EXISTS(SELECT 1 FROM finance_payouts WHERE bank_id=b.id)""",
                (p["amount"], p["currency"]),
            )
        ]
        result.append(
            {
                **dict(p),
                "linked_amount": linked_amount,
                "linked_count": len(linked),
                "difference": None if uncertain else p["amount"] - linked_amount,
                "linked": linked,
                "candidates": candidates,
            }
        )
    return result
