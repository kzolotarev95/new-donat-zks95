import json
from types import SimpleNamespace
from app.db import create_donor, create_payment_record, get_db, record_donor_payment

def test_webhook_rechecks_payment(monkeypatch, app, client):
    with app.app_context():
        create_payment_record({
            "payment_id": "pay-1", "idempotency_key": "idem-1",
            "status": "pending", "amount": "500.00", "currency": "RUB",
            "description": "Support", "customer_nickname": "tester", "payload": {},
        })
    class Payment:
        id = "pay-1"
        status = "succeeded"
        amount = SimpleNamespace(value="500.00", currency="RUB")
        def json(self):
            return json.dumps({"id": self.id, "status": self.status})
    monkeypatch.setattr("app.routes.fetch_payment", lambda _payment_id: Payment())
    response = client.post("/webhooks/yookassa", json={"event": "payment.succeeded", "object": {"id": "pay-1"}})
    assert response.status_code == 200
    with app.app_context():
        from app.db import get_payment
        assert get_payment("pay-1")["status"] == "succeeded"
        from app.db import list_donors
        assert list_donors()[0]["nickname"] == "tester"
        assert list_donors()[0]["note"] == "Поддержал разработку"


def test_record_donor_payment_defaults_note_without_overwriting_custom_note(app):
    with app.app_context():
        create_donor("Blank note")
        create_donor("Custom note", note="Спасибо за проект")
        record_donor_payment("Blank note", "100.00")
        record_donor_payment("Custom note", "100.00")
        rows = {
            row["nickname"]: row
            for row in get_db().execute("SELECT * FROM donors WHERE nickname IN (?, ?)", ("Blank note", "Custom note"))
        }
        assert rows["Blank note"]["note"] == "Поддержал разработку"
        assert rows["Custom note"]["note"] == "Спасибо за проект"
