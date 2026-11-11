"""Read models and cached financial totals in the configured local timezone."""

import json
import time
from datetime import datetime, timedelta

from .ledger import income_rows, tz, timestamp
from ..db import get_db

PERIODS = {
    "today": "Сегодня",
    "yesterday": "Вчера",
    "7d": "7 дней",
    "30d": "30 дней",
    "month": "Текущий месяц",
    "previous_month": "Предыдущий месяц",
    "year": "Текущий год",
    "all": "Всё время",
    "custom": "Произвольный период",
}


def period(args):
    today = datetime.now(tz()).date()
    key = args.get("period", "month")
    if key not in PERIODS:
        key = "month"
    start = today
    end = today
    if key == "yesterday":
        start = end = today - timedelta(days=1)
    elif key == "7d":
        start = today - timedelta(days=6)
    elif key == "30d":
        start = today - timedelta(days=29)
    elif key == "month":
        start = today.replace(day=1)
    elif key == "previous_month":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif key == "year":
        start = today.replace(month=1, day=1)
    elif key == "all":
        first = (
            get_db()
            .execute("""SELECT MIN(day) FROM (
          SELECT MIN(COALESCE(captured_at,created_at)) day FROM finance_transactions
          UNION ALL SELECT MIN(booked_at) FROM finance_bank_operations)""")
            .fetchone()[0]
        )
        start = (
            min(today, datetime.fromisoformat(first).astimezone(tz()).date())
            if first
            else today
        )
    elif key == "custom":
        try:
            start = datetime.strptime(args.get("start", ""), "%Y-%m-%d").date()
            end = datetime.strptime(args.get("end", ""), "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("Укажите начало и конец периода.") from None
    if end < start or (key != "all" and (end - start).days > 3660):
        raise ValueError("Период должен быть от 1 дня до 10 лет.")
    return {
        "key": key,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "since": timestamp(start.isoformat()),
        "until": timestamp((end + timedelta(days=1)).isoformat()),
    }


def local_date(value):
    return (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .astimezone(tz())
        .date()
        .isoformat()
    )


def totals(selection):
    db = get_db()
    key = json.dumps(selection, sort_keys=True) + str(tz())
    cached = db.execute(
        "SELECT payload FROM finance_cache WHERE key=? AND expires_at>?",
        (key, int(time.time())),
    ).fetchone()
    if cached:
        return json.loads(cached["payload"])
    incomes = [r for r in income_rows(db) if r["currency"] == "RUB"]
    today = datetime.now(tz()).date()
    anchors = {
        "today": today.isoformat(),
        "week": (today - timedelta(days=today.weekday())).isoformat(),
        "month": today.replace(day=1).isoformat(),
        "year": today.replace(month=1, day=1).isoformat(),
    }
    metrics = {
        k: sum(r["base"] for r in incomes if v <= r["income_day"] <= today.isoformat())
        for k, v in anchors.items()
    }
    selected = [
        r for r in incomes if selection["start"] <= r["income_day"] <= selection["end"]
    ]
    payments = [r for r in incomes if r["income_key"].startswith("payment:")]
    selected_payments = [r for r in selected if r["income_key"].startswith("payment:")]
    metrics.update(
        total=sum(r["amount"] for r in payments),
        net=sum(r["base"] for r in selected),
        selected=sum(r["amount"] for r in selected_payments),
        count=len(selected_payments),
        average=(
            sum(r["amount"] for r in selected_payments) * 2 + len(selected_payments)
        )
        // (2 * len(selected_payments))
        if selected_payments
        else 0,
        refunds=sum(r["refunded"] for r in selected),
        fees=sum(r["fee"] or 0 for r in selected),
        unknown_fees=sum(r["fee"] is None for r in selected),
        with_receipts=sum(
            r["base"]
            for r in selected
            if r["receipt"]["status"] in ("issued", "verified")
        ),
        without_receipts=sum(
            r["base"]
            for r in selected
            if r["receipt"]["status"] == "pending" and r["base"] > 0
        ),
        pending_receipts=sum(
            r["receipt"]["status"] == "pending" and r["base"] > 0 for r in selected
        ),
        overdue=sum(r["overdue"] for r in selected),
    )
    payouts = [
        dict(r)
        for r in db.execute("SELECT * FROM finance_payouts WHERE currency='RUB'")
    ]
    metrics["paid_out"] = sum(
        p["amount"] for p in payouts if p["status"] == "confirmed"
    )
    metrics["awaiting"] = sum(p["amount"] for p in payouts if p["status"] == "expected")
    metrics["unallocated"] = (
        db.execute("""SELECT COUNT(*) FROM finance_transactions t WHERE status='succeeded' AND is_test=0
      AND NOT EXISTS(SELECT 1 FROM finance_reconciliation r WHERE r.payment_id=t.payment_id)""").fetchone()[
            0
        ]
    )
    daily = {}
    monthly = {}
    day = datetime.fromisoformat(selection["start"]).date()
    fill_days = (datetime.fromisoformat(selection["end"]).date() - day).days <= 3660
    while fill_days and day.isoformat() <= selection["end"]:
        label = day.isoformat()
        daily[label] = {
            "label": label,
            "income": 0,
            "payments": 0,
            "fees": 0,
            "payouts": 0,
        }
        monthly.setdefault(
            label[:7],
            {"label": label[:7], "income": 0, "payments": 0, "fees": 0, "payouts": 0},
        )
        day += timedelta(days=1)
    for r in selected:
        day = r["income_day"]
        month = day[:7]
        for buckets, label in ((daily, day), (monthly, month)):
            b = buckets.setdefault(
                label,
                {"label": label, "income": 0, "payments": 0, "fees": 0, "payouts": 0},
            )
            b["income"] += r["base"]
            b["payments"] += int(r["income_key"].startswith("payment:"))
            b["fees"] += r["fee"] or 0
    for p in payouts:
        day = local_date(p["credited_at"] or p["expected_at"])
        if p["status"] == "confirmed" and selection["start"] <= day <= selection["end"]:
            for buckets, label in ((daily, day), (monthly, day[:7])):
                b = buckets.setdefault(
                    label,
                    {
                        "label": label,
                        "income": 0,
                        "payments": 0,
                        "fees": 0,
                        "payouts": 0,
                    },
                )
                b["payouts"] += p["amount"]
    span = (
        datetime.fromisoformat(selection["end"])
        - datetime.fromisoformat(selection["start"])
    ).days + 1
    previous_end = (
        datetime.fromisoformat(selection["start"]) - timedelta(days=1)
    ).date()
    previous_start = previous_end - timedelta(days=span - 1)
    previous = sum(
        r["base"]
        for r in incomes
        if previous_start.isoformat() <= r["income_day"] <= previous_end.isoformat()
    )
    result = {
        "metrics": metrics,
        "daily": [daily[k] for k in sorted(daily)],
        "monthly": [monthly[k] for k in sorted(monthly)],
        "comparison": [
            {"label": f"{previous_start} — {previous_end}", "income": previous},
            {
                "label": f"{selection['start']} — {selection['end']}",
                "income": metrics["net"],
            },
        ],
    }
    with db:
        db.execute(
            "INSERT OR REPLACE INTO finance_cache VALUES(?,?,?)",
            (key, json.dumps(result), int(time.time()) + 30),
        )
        db.execute("DELETE FROM finance_cache WHERE expires_at<?", (int(time.time()),))
    return result
