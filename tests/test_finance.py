"""Financial invariants, permissions, imports, sync, migration and backup coverage."""

import io
import json
from datetime import datetime, timezone
import pytest
from openpyxl import Workbook
from app.db import get_db, create_payment_record, update_payment, init_db
from app.finance.ledger import upsert_transaction, money, income_rows
from app.finance.operations import (
    create_payout,
    confirm_payout,
    link_payment,
    save_receipt,
    classify_bank,
    reconcile,
)
from app.finance.banking import import_statement
from app.finance.analytics import totals, period
from app.finance.sync import drain, request_sync
from app.backup import export_backup, import_backup
from app.settings import set_runtime_setting
from tests.test_site_themes import _login


def payment(
    pid="payment-1", amount="1000.00", fee="30.00", status="succeeded", day=None
):
    created = day or datetime.now(timezone.utc).isoformat()
    obj = {
        "id": pid,
        "amount": {"value": amount, "currency": "RUB"},
        "status": status,
        "created_at": created,
        "metadata": {"nickname": "Developer"},
        "payment_method": {"type": "sbp"},
        "description": "Support",
    }
    if fee is not None:
        obj["income_amount"] = {
            "value": str((money(amount) - money(fee)) / 100),
            "currency": "RUB",
        }
    if status == "succeeded":
        obj["captured_at"] = created
    return obj


def seed(app, obj=None):
    with app.app_context():
        with get_db():
            upsert_transaction(get_db(), obj or payment())


def bank_row(app, amount="970.00", external="bank-1"):
    with app.app_context():
        import_statement(
            f"date;amount;currency;id;description\n2026-11-11;{amount};RUB;{external};YooKassa settlement\n".encode(),
            "bank.csv",
            "Сбербанк",
            "1234",
        )
        return (
            get_db()
            .execute(
                "SELECT id FROM finance_bank_operations WHERE external_id=?",
                (external,),
            )
            .fetchone()[0]
        )


def payout_data(**kwargs):
    return {
        "external_id": "settlement-1",
        "date": "2026-11-11",
        "amount": "970",
        "fee": "30",
        "source": "ЮKassa отчёт",
        **kwargs,
    }


def receipt_data(**kwargs):
    return {
        "status": "issued",
        "customer_category": "individual",
        "receipt_date": "2026-11-11",
        "receipt_number": "receipt-1",
        "receipt_url": "https://lknpd.nalog.ru/api/v1/receipt/example",
        "comment": "Проверено владельцем",
        "confirm": "1",
        **kwargs,
    }


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-1", "1.001", "100000000001", "x"])
def test_invalid_money(raw):
    with pytest.raises(ValueError):
        money(raw)


def test_exact_kopecks():
    assert money("1 234,56") == 123456
    assert money("0.01") == 1


@pytest.mark.parametrize(
    "url",
    [
        "/admin/finance",
        "/admin/finance/api/summary",
        "/admin/finance/payments",
        "/admin/finance/bank",
        "/admin/finance/receipts",
        "/admin/finance/audit",
        "/admin/finance/bank/template.csv",
    ],
)
def test_finance_is_private(client, url):
    assert client.get(url).status_code == 302


def test_csrf_and_all_views(app, client):
    seed(app)
    token = _login(client)
    for theme in ("classic", "cyber"):
        with app.app_context():
            set_runtime_setting("SITE_THEME", theme)
        for path in (
            "",
            "/payments",
            "/payments/payment-1",
            "/payouts",
            "/bank",
            "/receipts",
            "/receipts/payment:payment-1",
            "/reconciliation",
            "/audit",
            "/api/summary",
        ):
            response = client.get("/admin/finance" + path)
            assert response.status_code == 200, (
                path,
                response.get_data(as_text=True)[:200],
            )
            assert "no-store" in response.headers["Cache-Control"]
    assert client.post("/admin/finance/sync").status_code == 400
    assert (
        client.post("/admin/finance/sync", data={"csrf_token": token}).status_code
        == 302
    )
    with app.app_context():
        assert get_db().execute("SELECT requested FROM finance_sync").fetchone()[0] == 1


def test_migration_and_payment_mirror(app):
    with app.app_context():
        create_payment_record(
            {
                "payment_id": "site-pay",
                "idempotency_key": "idem",
                "status": "pending",
                "amount": "500.00",
                "currency": "RUB",
                "description": "Support",
                "payload": json.dumps({"id": "site-pay"}),
            }
        )
        update_payment(
            "site-pay", "succeeded", {"payment_method": {"type": "sbp"}}, paid=True
        )
        init_db()
        init_db()
        row = (
            get_db()
            .execute(
                "SELECT * FROM finance_transactions WHERE payment_id=?", ("site-pay",)
            )
            .fetchone()
        )
        assert (
            row["amount"] == 50000
            and row["status"] == "succeeded"
            and row["fee"] is None
        )
        assert (
            get_db().execute("SELECT COUNT(*) FROM finance_transactions").fetchone()[0]
            == 1
        )
        assert get_db().execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1


def test_totals_do_not_assume_settlement(app):
    seed(app)
    with app.test_request_context():
        data = totals(period({}))["metrics"]
        assert data["selected"] == 100000 and data["fees"] == 3000
        assert data["without_receipts"] == 100000 and data["with_receipts"] == 0
        assert data["paid_out"] == 0 and data["awaiting"] == 0
        assert data["count"] == 1 and data["average"] == 100000


def test_reconciliation_and_no_double_income(app):
    seed(app)
    bid = bank_row(app)
    with app.test_request_context():
        pid = create_payout(payout_data())
        link_payment(pid, "payment-1")
        with pytest.raises(ValueError):
            link_payment(pid, "payment-1")
        assert reconcile()[0]["difference"] == 0
        assert totals(period({}))["metrics"]["paid_out"] == 0
        with pytest.raises(ValueError):
            confirm_payout(pid, bid, "checked", "0")
        confirm_payout(pid, bid, "Checked by bank statement", "1")
        metrics = totals(period({}))["metrics"]
        assert metrics["paid_out"] == 97000
        assert len(income_rows()) == 1 and metrics["selected"] == 100000
        with pytest.raises(ValueError):
            classify_bank(bid, "income", "second income", "1")
        assert (
            get_db().execute("SELECT COUNT(*) FROM finance_audit_log").fetchone()[0]
            == 4
        )


def test_reconciliation_rejects_mismatch_unknown_fee_and_duplicate_bank(app):
    seed(app, payment(fee=None))
    bid = bank_row(app, amount="900")
    with app.test_request_context():
        pid = create_payout(payout_data())
        with pytest.raises(ValueError):
            confirm_payout(pid, bid, "test", "1")
        with pytest.raises(ValueError):
            link_payment(pid, "payment-1")
        assert (
            get_db().execute("SELECT status FROM finance_payouts").fetchone()[0]
            == "expected"
        )


@pytest.mark.parametrize("status", ["issued", "verified"])
def test_receipts_require_manual_confirmation_and_audit(app, status):
    seed(app)
    with app.test_request_context():
        with pytest.raises(ValueError):
            save_receipt("payment:payment-1", receipt_data(confirm="0"))
        with pytest.raises(ValueError):
            save_receipt("payment:payment-1", receipt_data(receipt_number=""))
        with pytest.raises(ValueError):
            save_receipt(
                "payment:payment-1", receipt_data(receipt_url="javascript:alert(1)")
            )
        save_receipt("payment:payment-1", receipt_data(status=status))
        report = totals(period({}))["metrics"]
        assert report["with_receipts"] == 100000 and report["without_receipts"] == 0
        audit = get_db().execute("SELECT * FROM finance_audit_log").fetchone()
        assert (
            audit["action"] == "receipt_update" and "receipt-1" in audit["after_json"]
        )


@pytest.mark.parametrize("ext", ["csv", "xlsx", "ofx"])
def test_bank_import_formats_and_deduplication(app, ext):
    if ext == "csv":
        content = b"date;amount;currency;id;description\n2026-11-11;970.00;RUB;b1;settlement\n"
    elif ext == "xlsx":
        wb = Workbook()
        sheet = wb.active
        sheet.append(["date", "amount", "currency", "id", "description"])
        sheet.append(["2026-11-11", 970, "RUB", "b1", "settlement"])
        buffer = io.BytesIO()
        wb.save(buffer)
        content = buffer.getvalue()
    else:
        content = b"<OFX><CURDEF>RUB<STMTTRN><DTPOSTED>20261111<TRNAMT>970.00<FITID>b1<MEMO>settlement</STMTTRN></OFX>"
    with app.test_request_context():
        assert import_statement(content, "statement." + ext, "Сбербанк", "1234") == 1
        assert import_statement(content, "renamed." + ext, "Сбербанк", "1234") == 0
        assert (
            get_db()
            .execute("SELECT COUNT(*) FROM finance_bank_operations")
            .fetchone()[0]
            == 1
        )
        assert income_rows() == []


def test_bad_import_is_atomic(app):
    content = b"date;amount;id\n2026-11-11;10;1\nnot-a-date;12;2\n"
    with app.test_request_context():
        with pytest.raises(ValueError):
            import_statement(content, "x.csv", "bank", "account")
        assert (
            get_db()
            .execute("SELECT COUNT(*) FROM finance_bank_operations")
            .fetchone()[0]
            == 0
        )
        with pytest.raises(ValueError):
            import_statement(b"x" * (5 * 1024 * 1024 + 1), "x.csv", "bank", "account")


def test_sync_repeat_refunds_and_no_donor_side_effect(app):
    class Provider:
        def page(self, stage, cursor, since):
            if stage == "payments":
                return {"items": [payment()]}
            return {
                "items": [
                    {
                        "id": "r1",
                        "payment_id": "payment-1",
                        "amount": {"value": "100", "currency": "RUB"},
                        "status": "succeeded",
                        "created_at": "2026-11-11T00:00:00Z",
                    }
                ]
            }

    with app.app_context():
        request_sync()
        assert drain(Provider()) == 2
        request_sync()
        assert drain(Provider()) == 2
        assert (
            get_db().execute("SELECT COUNT(*) FROM finance_transactions").fetchone()[0]
            == 1
        )
        assert (
            get_db().execute("SELECT COUNT(*) FROM finance_refunds").fetchone()[0] == 1
        )
        assert (
            get_db().execute("SELECT refunded FROM finance_transactions").fetchone()[0]
            == 10000
        )
        assert get_db().execute("SELECT COUNT(*) FROM donors").fetchone()[0] == 0
        assert get_db().execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0


def test_sync_unavailable_is_retryable_and_does_not_destroy_ledger(app):
    seed(app)
    with app.app_context():
        request_sync()
        drain()
        row = get_db().execute("SELECT * FROM finance_sync").fetchone()
        assert (
            row["requested"] == 1
            and row["last_error"]
            and row["retry_at"] > 0
            and row["lease_until"] == 0
        )
        assert (
            get_db().execute("SELECT COUNT(*) FROM finance_transactions").fetchone()[0]
            == 1
        )


def test_sync_pagination_resume_and_lock(app):
    class Provider:
        def page(self, stage, cursor, since):
            if stage == "payments" and not cursor:
                return {"items": [payment("one")], "next_cursor": "page2"}
            if stage == "payments":
                return {"items": [payment("two")]}
            return {"items": []}

    with app.app_context():
        request_sync()
        drain(Provider(), max_pages=1)
        assert (
            get_db().execute("SELECT cursor FROM finance_sync").fetchone()[0] == "page2"
        )
        drain(Provider())
        assert (
            get_db().execute("SELECT COUNT(*) FROM finance_transactions").fetchone()[0]
            == 2
        )
        with get_db():
            get_db().execute(
                "UPDATE finance_sync SET requested=1,lease_until=9999999999"
            )
        assert drain(Provider()) == 0


def test_finance_backup_round_trip_and_old_backup(app):
    seed(app)
    bid = bank_row(app)
    with app.test_request_context():
        pid = create_payout(payout_data())
        link_payment(pid, "payment-1")
        confirm_payout(pid, bid, "bank evidence", "1")
        save_receipt("payment:payment-1", receipt_data())
        backup = export_backup("full")
        import_backup(backup)
        assert (
            get_db().execute("SELECT COUNT(*) FROM finance_receipts").fetchone()[0] == 1
        )
        assert (
            get_db().execute("SELECT COUNT(*) FROM finance_audit_log").fetchone()[0]
            == 6
        )
        assert (
            get_db().execute("SELECT status FROM finance_payouts").fetchone()[0]
            == "confirmed"
        )
        for key in list(backup["tables"]):
            if key.startswith("finance_"):
                del backup["tables"][key]
        import_backup(backup)
        assert (
            get_db().execute("SELECT COUNT(*) FROM finance_transactions").fetchone()[0]
            == 0
        )


def test_payment_search_filters_pagination_and_injection(app, client):
    for i in range(18):
        seed(app, payment("pay-" + str(i), str(100 + i)))
    _login(client)
    response = client.get("/admin/finance/payments?period=year&sort=amount&page=2")
    assert response.status_code == 200 and "2 / 2" in response.get_data(as_text=True)
    response = client.get(
        "/admin/finance/payments?q=%27%20OR%201%3D1%20--&sort=DROP%20TABLE"
    )
    assert response.status_code == 200
    assert client.get("/admin/finance/payments?min=NaN").status_code == 400
    assert (
        client.get("/admin/finance?period=custom&start=bad&end=2026-11-11").status_code
        == 400
    )


def test_period_timezone_boundaries(app):
    with app.app_context():
        set_runtime_setting("FINANCE_TIMEZONE", "Europe/Samara")
        p = period({"period": "custom", "start": "2026-11-01", "end": "2026-11-30"})
        assert p["since"].startswith("2026-10-31T20:00")
        assert p["until"].startswith("2026-11-30T20:00")


def test_refund_updates_reconciliation_and_receipt_base(app):
    from app.finance.sync import store_refund

    seed(app)
    bank_row(app)
    with app.test_request_context():
        pid = create_payout(payout_data())
        link_payment(pid, "payment-1")
        with get_db():
            store_refund(
                get_db(),
                {
                    "id": "ref-new",
                    "payment_id": "payment-1",
                    "status": "succeeded",
                    "amount": {"value": "250", "currency": "RUB"},
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        assert reconcile()[0]["difference"] == 25000
        report = totals(period({}))["metrics"]
        assert (
            report["selected"] == 100000
            and report["net"] == 75000
            and report["refunds"] == 25000
        )
        assert income_rows()[0]["base"] == 75000
        with get_db():
            upsert_transaction(get_db(), payment(status="pending"))
        assert get_db().execute(
            "SELECT status,refunded FROM finance_transactions"
        ).fetchone()[:] == ("succeeded", 25000)


def test_direct_bank_income_is_not_another_payment(app):
    seed(app)
    with app.test_request_context():
        import_statement(
            b"date;amount;id;description\n2026-11-11;500;direct;Development service\n",
            "direct.csv",
            "Bank",
            "1234",
        )
        bid = get_db().execute("SELECT id FROM finance_bank_operations").fetchone()[0]
        classify_bank(
            bid, "income", "Separate client payment, checked no YooKassa duplicate", "1"
        )
        p = period({"period": "all"})
        report = totals(p)
        assert report["metrics"]["total"] == 100000 and report["metrics"]["count"] == 1
        assert (
            report["metrics"]["net"] == 150000
            and sum(d["payments"] for d in report["daily"]) == 1
        )
        assert report["metrics"]["paid_out"] == 0


def test_bank_duplicate_id_conflict_rolls_back_whole_import(app):
    bank_row(app)
    with app.test_request_context():
        with pytest.raises(ValueError):
            import_statement(
                b"date;amount;id\n2026-11-11;1;new-id\n2026-11-11;100;bank-1\n",
                "other.csv",
                "Сбербанк",
                "1234",
            )
        assert (
            get_db()
            .execute("SELECT COUNT(*) FROM finance_bank_operations")
            .fetchone()[0]
            == 1
        )
        assert (
            get_db().execute("SELECT COUNT(*) FROM finance_bank_imports").fetchone()[0]
            == 1
        )


def test_restore_rejects_unsafe_receipt_and_keeps_data(app):
    seed(app)
    with app.test_request_context():
        save_receipt("payment:payment-1", receipt_data())
        payload = export_backup("full")
        payload["tables"]["finance_receipts"][0]["receipt_url"] = "javascript:alert(1)"
        with pytest.raises(ValueError):
            import_backup(payload)
        assert (
            get_db().execute("SELECT COUNT(*) FROM finance_receipts").fetchone()[0] == 1
        )
        assert (
            get_db().execute("SELECT amount FROM finance_transactions").fetchone()[0]
            == 100000
        )
        with get_db():
            get_db().execute("UPDATE finance_sync SET lease_until=9999999999")
        with pytest.raises(ValueError):
            import_backup(export_backup("full"))


def test_pending_objects_older_than_window_are_refreshed(app):
    from app.finance.sync import store_refund

    seed(app, payment(status="pending", day="2025-01-01T00:00:00Z"))

    class Provider:
        def payment(self, pid):
            return payment(pid, day="2025-01-01T00:00:00Z")

        def refund(self, rid):
            return {
                "id": rid,
                "payment_id": "payment-1",
                "status": "succeeded",
                "amount": {"value": "100", "currency": "RUB"},
                "created_at": "2025-01-02T00:00:00Z",
            }

        def page(self, *args):
            return {"items": []}

    with app.app_context():
        with get_db():
            store_refund(
                get_db(),
                {
                    "id": "old-ref",
                    "payment_id": "payment-1",
                    "status": "pending",
                    "amount": {"value": "100", "currency": "RUB"},
                    "created_at": "2025-01-02T00:00:00Z",
                },
            )
        request_sync()
        drain(Provider())
        assert get_db().execute(
            "SELECT status,refunded FROM finance_transactions"
        ).fetchone()[:] == ("succeeded", 10000)
        assert (
            get_db().execute("SELECT status FROM finance_refunds").fetchone()[0]
            == "succeeded"
        )


def test_fee_is_unknown_and_excluded_receipt_does_not_hide_cash(app):
    seed(app, payment(fee=None))
    with app.test_request_context():
        save_receipt(
            "payment:payment-1",
            receipt_data(
                status="excluded", comment="Not taxable, owner classification"
            ),
        )
        metrics = totals(period({}))["metrics"]
        assert (
            metrics["unknown_fees"] == 1
            and metrics["total"] == 100000
            and metrics["net"] == 100000
        )
        assert metrics["without_receipts"] == 0 and metrics["with_receipts"] == 0


def test_finance_http_mutations_and_audit(app, client):
    seed(app)
    bid = bank_row(app)
    _login(client)
    with client.session_transaction() as session:
        token = session["_csrf_token"]
    response = client.post(
        "/admin/finance/payouts", data={**payout_data(), "csrf_token": token}
    )
    assert response.status_code == 302
    with app.app_context():
        pid = get_db().execute("SELECT id FROM finance_payouts").fetchone()[0]
    client.post(
        f"/admin/finance/payouts/{pid}/link",
        data={"csrf_token": token, "payment_id": "payment-1"},
    )
    client.post(
        f"/admin/finance/payouts/{pid}/confirm",
        data={
            "csrf_token": token,
            "bank_id": str(bid),
            "comment": "Checked statement",
            "confirm": "1",
        },
    )
    response = client.post(
        "/admin/finance/receipts/payment:payment-1",
        data={**receipt_data(), "csrf_token": token},
        follow_redirects=True,
    )
    assert response.status_code == 200 and "receipt-1" in response.get_data(
        as_text=True
    )
    with app.app_context():
        assert (
            get_db().execute("SELECT status FROM finance_payouts").fetchone()[0]
            == "confirmed"
        )
    assert client.get("/admin/finance/bank?q=" + "x" * 201).status_code == 400


def test_aggregation_uses_bounded_queries(app):
    for i in range(30):
        seed(app, payment("query-check-" + str(i)))
    with app.app_context():
        statements = []
        db = get_db()
        db.set_trace_callback(statements.append)
        try:
            report = totals(period({}))
        finally:
            db.set_trace_callback(None)
        assert report["metrics"]["count"] == 30
        assert sum(s.lstrip().upper().startswith("SELECT") for s in statements) < 30


def test_parallel_worker_uses_one_lease(app):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    started = Event()
    release = Event()

    class Provider:
        def page(self, stage, cursor, since):
            if stage == "payments":
                started.set()
                assert release.wait(5)
                return {"items": [payment()]}
            return {"items": []}

    def work():
        with app.app_context():
            return drain(Provider())

    with app.app_context():
        request_sync()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(work)
        try:
            assert started.wait(5)
            with app.app_context():
                assert drain(Provider()) == 0
        finally:
            release.set()
        assert future.result(timeout=5) == 1


def test_yookassa_test_payments_are_not_real_income(app):
    obj = payment()
    obj["test"] = True
    seed(app, obj)
    with app.test_request_context():
        assert totals(period({}))["metrics"]["total"] == 0
        assert income_rows() == []
        pid = create_payout(payout_data())
        with pytest.raises(ValueError):
            link_payment(pid, "payment-1")
        with pytest.raises(ValueError):
            save_receipt("payment:payment-1", receipt_data())
        with get_db():
            upsert_transaction(get_db(), payment())
        assert (
            get_db().execute("SELECT is_test FROM finance_transactions").fetchone()[0]
            == 1
        )
