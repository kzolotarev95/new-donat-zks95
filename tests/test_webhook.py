import json
from types import SimpleNamespace
from app.db import create_payment_record

def test_webhook_rechecks_payment(monkeypatch, app, client):
    with app.app_context():
        create_payment_record({
            "payment_id": "pay-1", "idempotency_key": "idem-1",
            "status": "pending", "amount": "500.00", "currency": "RUB",
            "description": "Support", "payload": {},
        })
    class Payment:
        id = "pay-1"
        status = "succeeded"
        def json(self):
            return json.dumps({"id": self.id, "status": self.status})
    monkeypatch.setattr("app.routes.fetch_payment", lambda _payment_id: Payment())
    response = client.post("/webhooks/yookassa", json={"event": "payment.succeeded", "object": {"id": "pay-1"}})
    assert response.status_code == 200
    with app.app_context():
        from app.db import get_payment
        assert get_payment("pay-1")["status"] == "succeeded"
