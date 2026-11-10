import json
from types import SimpleNamespace

from app.db import create_payment_record, get_payment


def _payment(payment_id, status="pending"):
    class ConfirmedPayment:
        id = payment_id
        amount = SimpleNamespace(value="500.00", currency="RUB")

        def __init__(self):
            self.status = status

        def json(self):
            return json.dumps({"id": self.id, "status": self.status})

    return ConfirmedPayment()


def _record(app, payment_id, idempotency_key, status="pending"):
    with app.app_context():
        create_payment_record({
            "payment_id": payment_id,
            "idempotency_key": idempotency_key,
            "status": status,
            "amount": "500.00",
            "currency": "RUB",
            "description": "Support",
            "customer_nickname": "tester",
            "payload": {},
        })


def test_payment_return_without_reference_is_not_success(client):
    response = client.get("/payment/success")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Оплата не завершена" in body
    assert "Попробовать снова" in body
    assert "Payment received" not in body
    assert "no-store" in response.headers["Cache-Control"]


def test_cancelled_payment_return_shows_retry_and_saves_cancelled_status(app, client, monkeypatch):
    _record(app, "pay-cancelled", "idem-cancelled")
    monkeypatch.setattr("app.routes.fetch_payment", lambda _payment_id: _payment("pay-cancelled", "canceled"))

    response = client.get("/payment/success?payment_ref=idem-cancelled")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Оплата не завершена" in body
    assert "Payment received" not in body
    with app.app_context():
        assert get_payment("pay-cancelled")["status"] == "canceled"


def test_successful_payment_return_requires_confirmed_gateway_status(app, client, monkeypatch):
    _record(app, "pay-success", "idem-success")
    monkeypatch.setattr("app.routes.fetch_payment", lambda _payment_id: _payment("pay-success", "succeeded"))

    response = client.get("/payment/success?payment_ref=idem-success")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Payment received" in body
    assert "Спасибо за" in body
    assert "Оплата не завершена" not in body
    with app.app_context():
        assert get_payment("pay-success")["status"] == "pending"
