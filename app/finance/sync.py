"""Durable, bounded YooKassa sync worker. Runs outside the web workers."""

import secrets
import time
from datetime import datetime, timedelta
import requests
from .ledger import now, upsert_transaction, money, text, timestamp, invalidate
from ..db import get_db
from ..settings import get_runtime_setting


class YooKassaProvider:
    def __init__(self):
        self.shop = get_runtime_setting("YOOKASSA_SHOP_ID", "")
        self.secret = get_runtime_setting("YOOKASSA_SECRET_KEY", "")
        if not self.shop or not self.secret:
            raise ValueError("Заполните Shop ID и секретный ключ в настройках ЮKassa.")

    def payment(self, payment_id):
        return self.detail("payments", payment_id)

    def refund(self, refund_id):
        return self.detail("refunds", refund_id)

    def detail(self, resource, object_id):
        response = requests.get(
            "https://api.yookassa.ru/v3/"
            + resource
            + "/"
            + requests.utils.quote(object_id, safe=""),
            auth=(self.shop, self.secret),
            timeout=(3, 8),
            allow_redirects=False,
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ValueError(
                f"ЮKassa: HTTP {response.status_code} при проверке платежа."
            )
        return response.json()

    def page(self, stage, cursor, since):
        params = {"limit": 100, "created_at.gte": since}
        if cursor:
            params["cursor"] = cursor
        response = requests.get(
            "https://api.yookassa.ru/v3/" + stage,
            auth=(self.shop, self.secret),
            params=params,
            timeout=(3, 8),
            allow_redirects=False,
        )
        if response.status_code != 200:
            # Never store response bodies, credentials, or full request URLs in logs.
            raise ValueError(
                f"ЮKassa: HTTP {response.status_code}. Проверьте доступ и повторите синхронизацию."
            )
        result = response.json()
        if not isinstance(result.get("items"), list):
            raise ValueError("ЮKassa вернула некорректный список.")
        return result


def request_sync():
    db = get_db()
    with db:
        db.execute("UPDATE finance_sync SET requested=1,retry_at=0 WHERE id=1")


def store_refund(db, item):
    amount = item.get("amount") or {}
    rid = text(item.get("id"), 200, True)
    pid = text(item.get("payment_id"), 200, True)
    value = money(amount.get("value"))
    currency = text(amount.get("currency"), 3, True)
    status = text(item.get("status"), 40, True)
    if status not in ("pending", "succeeded", "canceled"):
        raise ValueError("Неизвестный статус возврата.")
    old = db.execute(
        "SELECT * FROM finance_refunds WHERE refund_id=?", (rid,)
    ).fetchone()
    if old and (
        old["payment_id"] != pid
        or old["amount"] != value
        or old["currency"] != currency
    ):
        raise ValueError("Реквизиты существующего возврата изменились.")
    db.execute(
        """INSERT INTO finance_refunds(refund_id,payment_id,amount,currency,status,created_at)
       VALUES(?,?,?,?,?,?) ON CONFLICT(refund_id) DO UPDATE SET status=
       CASE WHEN finance_refunds.status IN ('succeeded','canceled') THEN finance_refunds.status ELSE excluded.status END""",
        (rid, pid, value, currency, status, timestamp(item.get("created_at"))),
    )
    tx = db.execute(
        "SELECT amount,currency FROM finance_transactions WHERE payment_id=?", (pid,)
    ).fetchone()
    total = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM finance_refunds WHERE payment_id=? AND status='succeeded' AND currency=?",
        (pid, currency),
    ).fetchone()[0]
    if tx and (tx["currency"] != currency or total > tx["amount"]):
        raise ValueError("Сумма или валюта возврата не соответствует платежу.")
    db.execute(
        "UPDATE finance_transactions SET refunded=MAX(refunded,?) WHERE payment_id=?",
        (total, pid),
    )
    invalidate(db)


def drain(provider=None, max_pages=10):
    db = get_db()
    epoch = int(time.time())
    token = secrets.token_hex(16)
    with db:
        db.execute("BEGIN IMMEDIATE")
        state = db.execute("SELECT * FROM finance_sync WHERE id=1").fetchone()
        periodic = get_runtime_setting("FINANCE_SYNC_ENABLED", "0") == "1"
        if state["lease_until"] > epoch or state["retry_at"] > epoch:
            return 0
        if not state["requested"] and not periodic:
            return 0
        if not state["since_at"]:
            start = state["last_success"] or get_runtime_setting(
                "FINANCE_SYNC_FROM", "2020-01-01"
            )
            since = (
                datetime.fromisoformat(timestamp(start)) - timedelta(days=3)
            ).isoformat()
            db.execute("UPDATE finance_sync SET since_at=? WHERE id=1", (since,))
            db.execute("UPDATE finance_sync SET last_attempt=? WHERE id=1", (now(),))
        db.execute(
            "UPDATE finance_sync SET lease_until=?,token=? WHERE id=1",
            (epoch + 300, token),
        )
    processed = 0
    try:
        provider = provider or YooKassaProvider()
        if hasattr(provider, "payment"):
            pending = db.execute(
                "SELECT payment_id FROM finance_transactions WHERE status IN ('pending','waiting_for_capture') ORDER BY updated_at LIMIT 5"
            ).fetchall()
            for row in pending:
                obj = provider.payment(row["payment_id"])
                if obj is None:
                    with db:
                        db.execute(
                            "UPDATE finance_transactions SET updated_at=? WHERE payment_id=?",
                            (now(), row["payment_id"]),
                        )
                    continue
                if obj.get("id") != row["payment_id"]:
                    raise ValueError("ЮKassa вернула другой ID платежа.")
                with db:
                    upsert_transaction(db, obj)
                time.sleep(0.25)
        if hasattr(provider, "refund"):
            poll = db.execute(
                "SELECT value FROM finance_meta WHERE key='refund_poll_after'"
            ).fetchone()
            for row in db.execute(
                "SELECT refund_id FROM finance_refunds WHERE status='pending' ORDER BY (refund_id>?) DESC,refund_id LIMIT 5",
                (poll[0] if poll else "",),
            ).fetchall():
                obj = provider.refund(row["refund_id"])
                with db:
                    db.execute(
                        "INSERT INTO finance_meta VALUES('refund_poll_after',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (row["refund_id"],),
                    )
                if obj is None:
                    continue
                if obj.get("id") != row["refund_id"]:
                    raise ValueError("ЮKassa вернула другой ID возврата.")
                with db:
                    store_refund(db, obj)
                time.sleep(0.25)
        for _ in range(max_pages):
            state = db.execute("SELECT * FROM finance_sync WHERE id=1").fetchone()
            result = provider.page(state["stage"], state["cursor"], state["since_at"])
            next_cursor = text(result.get("next_cursor"), 2000)
            with db:
                for item in result["items"]:
                    if state["stage"] == "payments":
                        upsert_transaction(db, item)
                    else:
                        store_refund(db, item)
                    processed += 1
                invalidate(db)
                if next_cursor:
                    if next_cursor == state["cursor"]:
                        raise ValueError("ЮKassa повторила курсор списка.")
                    db.execute(
                        "UPDATE finance_sync SET cursor=? WHERE id=1", (next_cursor,)
                    )
                elif state["stage"] == "payments":
                    db.execute(
                        "UPDATE finance_sync SET stage='refunds',cursor='' WHERE id=1"
                    )
                else:
                    db.execute(
                        "UPDATE finance_sync SET requested=0,stage='payments',cursor='',since_at='',last_success=?,last_error='' WHERE id=1",
                        (state["last_attempt"],),
                    )
                    break
            time.sleep(0.25)
        else:
            with db:
                db.execute("UPDATE finance_sync SET requested=1 WHERE id=1")
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            else "Синхронизация недоступна. Будет повторена через 5 минут."
        )
        with db:
            db.execute(
                "UPDATE finance_sync SET requested=1,last_error=?,retry_at=? WHERE id=1",
                (message[:250], int(time.time()) + 300),
            )
    finally:
        with db:
            db.execute(
                "UPDATE finance_sync SET lease_until=0,token='' WHERE id=1 AND token=?",
                (token,),
            )
    return processed
