"""Payment bounds are enforced before contacting YooKassa and saved together."""
from decimal import Decimal
from html.parser import HTMLParser
import json
import re
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

from app import create_app
from app.backup import export_backup
from app.db import get_db
from app.payment_limits import (donation_amount_options, get_payment_limits,
                                save_payment_limits, validate_limits)
from app.settings import set_runtime_setting
from app.payments import create_payment


class DonationFields(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.amount = {}
        self.quick = []
        self.feed(response.get_data(as_text=True))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == "amount":
            self.amount = attrs
        if tag == "button" and "data-amount" in attrs:
            self.quick.append(int(attrs["data-amount"]))


def login(client):
    page = client.get("/admin/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
    assert client.post("/admin/login", data={"csrf_token": token, "username": "admin",
                                           "password": "correct horse battery staple"}).status_code == 302
    page = client.get("/admin/payments").get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', page)[1]


@pytest.fixture
def gateway(app, monkeypatch):
    app.config.update(YOOKASSA_SHOP_ID="test-shop", YOOKASSA_SECRET_KEY="test-key")
    calls = []
    def create(amount, description, return_url, metadata):
        calls.append(amount)
        payment_id = str(uuid.uuid4())
        payment = SimpleNamespace(
            id=payment_id, status="pending", amount=SimpleNamespace(value=f"{amount:.2f}", currency="RUB"),
            confirmation=SimpleNamespace(confirmation_url="https://payments.example/checkout"),
            json=lambda: json.dumps({"id": payment_id, "status": "pending"}),
        )
        return payment, str(uuid.uuid4())
    monkeypatch.setattr("app.routes.create_payment", create)
    return calls


def test_admin_can_save_one_ruble_minimum_and_public_form_matches(app, client):
    csrf = login(client)
    response = client.post("/admin/payments/limits", data={"csrf_token": csrf, "minimum": "1", "maximum": "100000"})
    assert response.status_code == 302
    with app.app_context():
        assert get_payment_limits() == (1, 100000)
        assert app.config["PAYMENT_MIN_AMOUNT"] == 100
    fields = DonationFields(client.get("/donate"))
    assert fields.amount["min"] == "1" and fields.amount["max"] == "100000"
    assert 1 <= int(fields.amount["value"]) <= 100000
    assert all(1 <= amount <= 100000 for amount in fields.quick)


def test_both_boundaries_reach_gateway_and_outside_values_do_not(app, client, gateway):
    with app.app_context():
        save_payment_limits("1", "100000")
    for amount in ("1", "100000"):
        result = client.post("/api/payments", json={"amount": amount, "nickname": "Tester"})
        assert result.status_code == 200
        assert result.get_json()["confirmation_url"] == "https://payments.example/checkout"
    for amount in ("0.99", "100000.01", "1000000"):
        result = client.post("/api/payments", json={"amount": amount, "nickname": "Tester"})
        assert result.status_code == 400
    assert gateway == [Decimal("1.00"), Decimal("100000.00")]


def test_changed_limits_apply_to_old_pages_without_changing_existing_payments(app, client, gateway):
    existing = client.post("/api/payments", json={"amount": "500", "nickname": "Tester"})
    assert existing.status_code == 200
    client.get("/donate")
    with app.app_context():
        save_payment_limits("1000", "2000")
    assert client.post("/api/payments", json={"amount": "500"}).status_code == 400
    with app.app_context():
        row = get_db().execute("SELECT amount, status FROM payments").fetchone()
        assert row["amount"] == "500.00" and row["status"] == "pending"
    assert gateway == [Decimal("500.00")]


@pytest.mark.parametrize("minimum,maximum", [("0", "100"), ("-1", "100"), ("1.5", "100"), ("1", "1000001"),
                                            ("NaN", "100"), ("1", "Infinity"), ("200", "100"), (None, "100"),
                                            ("1e9999999", "100"), ("1", "")])
def test_invalid_limit_submissions_leave_both_settings_unchanged(app, client, minimum, maximum):
    csrf = login(client)
    with app.app_context():
        save_payment_limits("100", "1000")
    client.post("/admin/payments/limits", data={"csrf_token": csrf, "minimum": minimum, "maximum": maximum})
    with app.app_context():
        assert get_payment_limits() == (100, 1000)


def test_authentication_csrf_and_completed_second_factor_are_required(app, client):
    page = client.get("/admin/login").get_data(as_text=True)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
    assert client.post("/admin/payments/limits", data={"csrf_token": csrf, "minimum": "1", "maximum": "1000"}).status_code == 302
    with app.app_context():
        assert get_payment_limits() == (100, 1000000)
    csrf = login(client)
    assert client.post("/admin/payments/limits", data={"minimum": "1", "maximum": "1000"}).status_code == 400
    with app.app_context():
        db = get_db()
        db.execute("UPDATE admin_users SET totp_secret='enabled'")
        db.commit()
    response = client.post("/admin/payments/limits", data={"csrf_token": csrf, "minimum": "1", "maximum": "1000"})
    assert response.status_code == 302 and "/admin/login" in response.headers["Location"]
    with app.app_context():
        assert get_payment_limits() == (100, 1000000)


def test_failed_second_write_rolls_back_first_limit(app):
    with app.app_context():
        save_payment_limits("100", "1000")
        db = get_db()
        db.execute("""CREATE TRIGGER reject_maximum BEFORE UPDATE ON app_settings
                      WHEN NEW.key = 'PAYMENT_MAX_AMOUNT'
                      BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END""")
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            save_payment_limits("200", "2000")
        assert get_payment_limits() == (100, 1000)


def test_limits_survive_new_app_instance_and_enter_full_backup(app):
    with app.app_context():
        save_payment_limits("1", "100000")
        settings = {row["key"]: row["value"] for row in export_backup("full")["settings"]}
        assert settings["PAYMENT_MIN_AMOUNT"] == "1" and settings["PAYMENT_MAX_AMOUNT"] == "100000"
    restarted = create_app(dict(app.config))
    with restarted.app_context():
        assert get_payment_limits() == (1, 100000)


@pytest.mark.parametrize("minimum,maximum", [(1, 100000), (1, 10), (900, 900), (100000, 200000), (700, 1500)])
def test_default_and_quick_buttons_always_fit_the_range(minimum, maximum):
    options = donation_amount_options(minimum, maximum)
    assert minimum <= options["payment_default"] <= maximum
    assert len(set(options["quick_amounts"])) == len(options["quick_amounts"])
    assert all(minimum <= amount <= maximum for amount in options["quick_amounts"])


@pytest.mark.parametrize("amount", ["NaN", "sNaN", "Infinity", "-Infinity", "1e99999999", "9" * 1000])
def test_nonfinite_or_unbounded_amounts_are_rejected_before_gateway(client, gateway, amount):
    assert client.post("/api/payments", json={"amount": amount}).status_code == 400
    assert not gateway


def test_corrupt_imported_limits_block_payments_and_can_be_repaired(app, client, gateway):
    with app.app_context():
        set_runtime_setting("PAYMENT_MIN_AMOUNT", "NaN")
    response = client.get("/donate")
    assert response.status_code == 200
    assert 'aria-describedby="donation-purpose" disabled' in response.get_data(as_text=True)
    assert client.post("/api/payments", json={"amount": "500"}).status_code == 503
    assert not gateway
    csrf = login(client)
    assert "Сохраните допустимый диапазон" in client.get("/admin/payments").get_data(as_text=True)
    client.post("/admin/payments/limits", data={"csrf_token": csrf, "minimum": "1", "maximum": "100000"})
    assert client.post("/api/payments", json={"amount": "1"}).status_code == 200


def test_fixed_amount_range_accepts_only_that_amount(app, client, gateway):
    with app.app_context():
        assert validate_limits("10", "10") == (10, 10)
        save_payment_limits("10", "10")
    assert client.post("/api/payments", json={"amount": "10"}).status_code == 200
    assert client.post("/api/payments", json={"amount": "9"}).status_code == 400
    assert client.post("/api/payments", json={"amount": "11"}).status_code == 400


def test_yookassa_payment_is_created_with_sbp_method(app, monkeypatch):
    captured = {}

    class FakePayment:
        @staticmethod
        def create(payload, idempotency_key):
            captured["payload"] = payload
            captured["idempotency_key"] = idempotency_key
            return object()

    with app.app_context():
        monkeypatch.setattr("app.payments._sdk", lambda: FakePayment)
        result, idempotency_key = create_payment(Decimal("500"), "Test donation", "https://example.test/success")

    assert result is not None
    assert idempotency_key == captured["idempotency_key"]
    assert captured["payload"]["payment_method_data"] == {"type": "sbp"}
    assert captured["payload"]["confirmation"]["type"] == "redirect"
