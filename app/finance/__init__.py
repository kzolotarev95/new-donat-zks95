"""Finance screens mounted inside the existing protected admin blueprint."""

import math
import sqlite3
from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    Response,
    abort,
)
from . import analytics, operations
from .ledger import (
    get_db,
    money,
    format_money,
    text,
    timestamp,
    audit,
    income_rows,
    RECEIPT_STATUSES,
)
from .banking import import_statement, add_operation, normalize, MAX_BYTES
from .sync import request_sync
from ..security import admin_required
from ..settings import set_runtime_settings, get_runtime_setting
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

finance_bp = Blueprint("finance", __name__, url_prefix="/finance")


@finance_bp.before_request
@admin_required
def authorize():
    limits = {"q": 200, "donor": 64, "status": 40}
    for key, limit in limits.items():
        if len(request.args.get(key, "")) > limit:
            abort(400, description="Слишком длинный фильтр.")
    return None


@finance_bp.context_processor
def context():
    return {
        "rub": format_money,
        "finance_input_money": lambda value: "" if value is None else format_money(value).replace(" ", ""),
        "finance_input_date": analytics.local_date,
        "receipt_statuses": RECEIPT_STATUSES,
        "periods": analytics.PERIODS,
        "finance_sync": dict(
            get_db().execute("SELECT * FROM finance_sync WHERE id=1").fetchone()
        ),
        "finance_timezone": get_runtime_setting("FINANCE_TIMEZONE", "Europe/Samara"),
        "finance_enabled": get_runtime_setting("FINANCE_SYNC_ENABLED", "0") == "1",
        "finance_from": get_runtime_setting("FINANCE_SYNC_FROM", "2020-01-01"),
        "finance_page_url": lambda page: url_for(
            request.endpoint, **{**request.args.to_dict(), "page": page}
        ),
    }


def selection(default="month"):
    try:
        return analytics.period({"period": default, **request.args.to_dict()})
    except ValueError as exc:
        abort(400, description=str(exc))


def paginate(rows):
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    pages = max(1, math.ceil(len(rows) / 15))
    page = min(page, pages)
    return rows[(page - 1) * 15 : page * 15], page, pages, len(rows)


def page_bounds(total):
    pages = max(1, math.ceil(total / 15))
    try:
        page = min(pages, max(1, int(request.args.get("page", "1"))))
    except ValueError:
        page = 1
    return page, pages


def render(view, **kwargs):
    return render_template(
        "admin/finance/" + view + ".html", finance_tab=view, **kwargs
    )


@finance_bp.get("")
@finance_bp.get("/")
def dashboard():
    selected = selection()
    return render("dashboard", selection=selected, report=analytics.totals(selected))


@finance_bp.get("/api/summary")
def summary():
    return jsonify(analytics.totals(selection()))


@finance_bp.get("/payments")
def payments():
    selected = selection("all")
    args = request.args
    clauses = [
        "COALESCE(t.captured_at,t.created_at)>=?",
        "COALESCE(t.captured_at,t.created_at)<?",
    ]
    params = [selected["since"], selected["until"]]
    query = text(args.get("q"), 200)
    donor = text(args.get("donor"), 64)
    status = text(args.get("status"), 40)
    if query:
        clauses.append("(instr(t.payment_id,?)>0 OR instr(lower(t.donor),lower(?))>0)")
        params += [query, query]
    if donor:
        clauses.append("instr(lower(t.donor),lower(?))>0")
        params.append(donor)
    if status:
        clauses.append("t.status=?")
        params.append(status)
    for key, op in (("min", ">="), ("max", "<=")):
        if args.get(key):
            try:
                amount = money(args[key])
            except ValueError as exc:
                abort(400, description=str(exc))
            clauses.append(f"t.amount{op}?")
            params.append(amount)
    sorts = {
        "date": "COALESCE(t.captured_at,t.created_at) DESC",
        "oldest": "t.created_at ASC",
        "amount": "t.amount DESC",
        "donor": "t.donor COLLATE NOCASE",
    }
    order = sorts.get(args.get("sort"), "COALESCE(t.captured_at,t.created_at) DESC")
    sql = """SELECT t.*,p.credited_at,r.status receipt_status FROM finance_transactions t
      LEFT JOIN finance_reconciliation l ON l.payment_id=t.payment_id LEFT JOIN finance_payouts p ON p.id=l.payout_id
      LEFT JOIN finance_receipts r ON r.income_key='payment:'||t.payment_id WHERE """ + " AND ".join(
        clauses
    )
    total = get_db().execute("SELECT COUNT(*) FROM (" + sql + ")", params).fetchone()[0]
    pages = max(1, math.ceil(total / 15))
    try:
        page = min(pages, max(1, int(args.get("page", "1"))))
    except ValueError:
        page = 1
    rows = (
        get_db()
        .execute(
            sql + " ORDER BY " + order + " LIMIT 15 OFFSET ?",
            params + [(page - 1) * 15],
        )
        .fetchall()
    )
    return render(
        "payments", rows=rows, page=page, pages=pages, total=total, selection=selected
    )


@finance_bp.get("/payments/<payment_id>")
def payment(payment_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM finance_transactions WHERE payment_id=?", (payment_id,)
    ).fetchone()
    if not row:
        abort(404)
    refunds = db.execute(
        "SELECT * FROM finance_refunds WHERE payment_id=? ORDER BY created_at DESC",
        (payment_id,),
    ).fetchall()
    return render("payment", row=row, refunds=refunds)


@finance_bp.post("/payments/<payment_id>/fee")
def payment_fee(payment_id):
    def action():
        db = get_db()
        row = db.execute(
            "SELECT * FROM finance_transactions WHERE payment_id=?", (payment_id,)
        ).fetchone()
        if not row:
            raise ValueError("Платёж не найден.")
        fee = money(request.form.get("fee"))
        if fee > row["amount"] or request.form.get("confirm") != "1":
            raise ValueError("Проверьте комиссию и подтвердите изменение.")
        source = text(request.form.get("source"), 500, True)
        with db:
            db.execute(
                "UPDATE finance_transactions SET fee=? WHERE payment_id=?",
                (fee, payment_id),
            )
            audit(
                db,
                "fee_confirmed",
                "payment:" + payment_id,
                {"fee": row["fee"]},
                {"fee": fee, "source": source},
            )

    return mutate(action, "admin.finance.payment", payment_id=payment_id)


@finance_bp.get("/payouts")
def payouts():
    total = get_db().execute("SELECT COUNT(*) FROM finance_payouts").fetchone()[0]
    page, pages = page_bounds(total)
    rows = operations.reconcile(limit=15, offset=(page - 1) * 15, include_canceled=True)
    return render("payouts", rows=rows, page=page, pages=pages, total=total)


@finance_bp.get("/bank")
def bank():
    q = text(request.args.get("q"), 200)
    total = (
        get_db()
        .execute(
            "SELECT COUNT(*) FROM finance_bank_operations WHERE instr(lower(description||' '||external_id),lower(?))>0",
            (q,),
        )
        .fetchone()[0]
    )
    page, pages = page_bounds(total)
    rows = (
        get_db()
        .execute(
            """SELECT b.*,p.external_id payout_reference FROM finance_bank_operations b
      LEFT JOIN finance_payouts p ON p.bank_id=b.id WHERE instr(lower(b.description||' '||b.external_id),lower(?))>0
      ORDER BY b.booked_at DESC,b.id DESC LIMIT 15 OFFSET ?""",
            (q, (page - 1) * 15),
        )
        .fetchall()
    )
    imports = (
        get_db()
        .execute("SELECT * FROM finance_bank_imports ORDER BY id DESC LIMIT 10")
        .fetchall()
    )
    return render(
        "bank", rows=rows, page=page, pages=pages, total=total, imports=imports
    )


@finance_bp.get("/bank/template.csv")
def bank_template():
    return Response(
        "\ufeffdate;amount;currency;id;description\n2026-11-11;500.00;RUB;bank-operation-id;Назначение поступления\n",
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=bank-template.csv"},
    )


@finance_bp.get("/receipts")
def receipts():
    rows = income_rows()
    q = text(request.args.get("q"), 200).casefold()
    status = request.args.get("status", "")
    rows = [
        r
        for r in rows
        if (not status or r["receipt"]["status"] == status)
        and (
            not q
            or q
            in (
                r["reference"] + " " + r["donor"] + " " + r["receipt"]["receipt_number"]
            ).casefold()
        )
    ]
    selected = selection("all")
    rows = [r for r in rows if selected["start"] <= r["income_day"] <= selected["end"]]
    rows, page, pages, total = paginate(rows)
    return render(
        "receipts", rows=rows, page=page, pages=pages, total=total, selection=selected
    )


@finance_bp.get("/receipts/<path:key>")
def receipt(key):
    row = next((r for r in income_rows() if r["income_key"] == key), None)
    if not row:
        abort(404)
    history = (
        get_db()
        .execute(
            "SELECT * FROM finance_audit_log WHERE entity=? ORDER BY id DESC LIMIT 100",
            (key,),
        )
        .fetchall()
    )
    return render("receipt", row=row, history=history)


@finance_bp.get("/reconciliation")
def reconciliation():
    db = get_db()
    missing = db.execute("""SELECT * FROM finance_transactions t WHERE status='succeeded' AND is_test=0
      AND NOT EXISTS(SELECT 1 FROM finance_reconciliation l WHERE l.payment_id=t.payment_id)
      ORDER BY COALESCE(captured_at,created_at) DESC LIMIT 100""").fetchall()
    total = db.execute(
        "SELECT COUNT(*) FROM finance_payouts WHERE status!='canceled'"
    ).fetchone()[0]
    page, pages = page_bounds(total)
    return render(
        "reconciliation",
        rows=operations.reconcile(limit=15, offset=(page - 1) * 15),
        missing=missing,
        page=page,
        pages=pages,
        total=total,
    )


@finance_bp.get("/audit")
def audit_log():
    total = get_db().execute("SELECT COUNT(*) FROM finance_audit_log").fetchone()[0]
    page, pages = page_bounds(total)
    rows = (
        get_db()
        .execute(
            "SELECT * FROM finance_audit_log ORDER BY id DESC LIMIT 15 OFFSET ?",
            ((page - 1) * 15,),
        )
        .fetchall()
    )
    return render("audit", rows=rows, page=page, pages=pages, total=total)


def mutate(action, endpoint, **kwargs):
    try:
        action()
        flash("Изменения сохранены.", "success")
    except (ValueError, sqlite3.IntegrityError) as exc:
        get_db().rollback()
        flash(
            str(exc)
            if isinstance(exc, ValueError)
            else "Запись уже существует или связана с другой операцией.",
            "error",
        )
    return redirect(url_for(endpoint, **kwargs))


@finance_bp.post("/sync")
def sync():
    def action():
        request_sync()
        with get_db():
            audit(get_db(), "sync_requested", "sync", {}, {"requested": True})

    return mutate(action, "admin.finance.dashboard")


@finance_bp.post("/settings")
def settings():
    def action():
        zone = text(request.form.get("timezone"), 80, True)
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Неизвестный часовой пояс.") from None
        start = text(request.form.get("sync_from"), 10, True)
        timestamp(start)
        from datetime import date

        if date.fromisoformat(start) > date.today():
            raise ValueError("Начало истории не может быть в будущем.")
        previous_start = get_runtime_setting("FINANCE_SYNC_FROM", "2020-01-01")
        values = {
            "FINANCE_TIMEZONE": zone,
            "FINANCE_SYNC_FROM": start,
            "FINANCE_SYNC_ENABLED": "1" if request.form.get("enabled") == "1" else "0",
        }
        with get_db():
            if start != previous_start:
                state = (
                    get_db()
                    .execute("SELECT lease_until FROM finance_sync WHERE id=1")
                    .fetchone()
                )
                import time

                if state[0] > time.time():
                    raise ValueError(
                        "Дождитесь завершения синхронизации перед изменением истории."
                    )
                get_db().execute(
                    "UPDATE finance_sync SET since_at='',cursor='',stage='payments',last_success='',retry_at=0 WHERE id=1"
                )
            audit(get_db(), "settings", "finance", {}, values)
        set_runtime_settings(values)

    return mutate(action, "admin.finance.dashboard")


@finance_bp.post("/payouts")
def payout_create():
    return mutate(
        lambda: operations.create_payout(request.form), "admin.finance.payouts"
    )


@finance_bp.post("/payouts/<int:payout_id>/edit")
def payout_edit(payout_id):
    return mutate(
        lambda: operations.edit_payout(payout_id, request.form), "admin.finance.payouts"
    )


@finance_bp.post("/payouts/<int:payout_id>/restore")
def payout_restore(payout_id):
    return mutate(
        lambda: operations.restore_payout(payout_id, request.form.get("confirm")),
        "admin.finance.payouts",
    )


@finance_bp.post("/payouts/<int:payout_id>/confirm")
def payout_confirm(payout_id):
    def action():
        try:
            bank_id = int(request.form.get("bank_id", "0"))
        except ValueError:
            raise ValueError("Укажите ID банковской операции.") from None
        operations.confirm_payout(
            payout_id, bank_id, request.form.get("comment"), request.form.get("confirm")
        )

    return mutate(action, "admin.finance.payouts")


@finance_bp.post("/payouts/<int:payout_id>/link")
def payout_link(payout_id):
    return mutate(
        lambda: operations.link_payment(
            payout_id, text(request.form.get("payment_id"), 200, True)
        ),
        "admin.finance.reconciliation",
    )


@finance_bp.post("/payouts/<int:payout_id>/unlink")
def payout_unlink(payout_id):
    def action():
        pid = text(request.form.get("payment_id"), 200, True)
        db = get_db()
        with db:
            row = db.execute(
                "SELECT * FROM finance_reconciliation WHERE payout_id=? AND payment_id=?",
                (payout_id, pid),
            ).fetchone()
            if not row:
                raise ValueError("Связь не найдена.")
            db.execute("DELETE FROM finance_reconciliation WHERE payment_id=?", (pid,))
            audit(db, "payment_unlinked", f"payout:{payout_id}", dict(row), {})

    return mutate(action, "admin.finance.reconciliation")


@finance_bp.post("/payouts/<int:payout_id>/cancel")
def payout_cancel(payout_id):
    def action():
        db = get_db()
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM finance_payouts WHERE id=?", (payout_id,)
            ).fetchone()
            if not row or request.form.get("confirm") != "1":
                raise ValueError("Подтвердите отмену записи выплаты.")
            db.execute(
                "DELETE FROM finance_reconciliation WHERE payout_id=?", (payout_id,)
            )
            db.execute(
                "UPDATE finance_payouts SET status='canceled',bank_id=NULL,credited_at=NULL WHERE id=?",
                (payout_id,),
            )
            if row["bank_id"]:
                db.execute(
                    "UPDATE finance_bank_operations SET kind='unclassified' WHERE id=?",
                    (row["bank_id"],),
                )
            audit(
                db,
                "payout_canceled",
                f"payout:{payout_id}",
                dict(row),
                {"reason": text(request.form.get("comment"), 1000, True)},
            )

    return mutate(action, "admin.finance.payouts")


@finance_bp.post("/bank/import")
def bank_import():
    def action():
        file = request.files.get("file")
        if not file:
            raise ValueError("Выберите файл выписки.")
        count = import_statement(
            file.stream.read(MAX_BYTES + 1),
            file.filename,
            request.form.get("source"),
            request.form.get("account"),
        )
        flash(f"Добавлено операций: {count}. Повторные записи пропущены.", "success")

    return mutate(action, "admin.finance.bank")


@finance_bp.post("/bank/manual")
def bank_manual():
    def action():
        rows = normalize([dict(request.form)])
        if not rows:
            raise ValueError("Сумма должна быть положительной.")
        row = rows[0]
        source = text(request.form.get("source"), 120, True)
        account = text(request.form.get("account"), 120, True)
        db = get_db()
        if request.form.get("confirm") != "1":
            raise ValueError("Подтвердите банковскую операцию.")
        with db:
            count = add_operation(db, row, source, account)
            audit(
                db,
                "bank_manual",
                "bank",
                {},
                {
                    "added": count,
                    "source": source,
                    "account": account,
                    "amount": row["amount"],
                },
            )

    return mutate(action, "admin.finance.bank")


@finance_bp.post("/bank/<int:bank_id>/classify")
def bank_classify(bank_id):
    return mutate(
        lambda: operations.classify_bank(
            bank_id,
            request.form.get("kind"),
            request.form.get("comment"),
            request.form.get("confirm"),
        ),
        "admin.finance.bank",
    )


@finance_bp.post("/receipts/<path:key>")
def receipt_save(key):
    return mutate(
        lambda: operations.save_receipt(key, request.form),
        "admin.finance.receipt",
        key=key,
    )


@finance_bp.app_template_filter("finance_date")
def display_date(value):
    if not value:
        return "—"
    from datetime import datetime
    from .ledger import tz

    return (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .astimezone(tz())
        .strftime("%d.%m.%Y %H:%M")
    )
