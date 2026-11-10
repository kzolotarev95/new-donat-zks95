import re

from app.db import create_payment_record, get_db


def _login(client):
    page = client.get("/admin/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
    response = client.post(
        "/admin/login",
        data={
            "csrf_token": token,
            "username": "admin",
            "password": "correct horse battery staple",
        },
    )
    assert response.status_code == 302


def _add_payments(app, count):
    with app.app_context():
        for index in range(count):
            create_payment_record({
                "payment_id": f"pay-pagination-{index}",
                "idempotency_key": f"idem-pagination-{index}",
                "status": "pending",
                "amount": "500.00",
                "currency": "RUB",
                "description": "Pagination test",
                "customer_nickname": f"tester-{index}",
                "payload": {},
            })
        db = get_db()
        for index in range(count):
            db.execute(
                "UPDATE payments SET created_at = ? WHERE payment_id = ?",
                (f"2026-01-01T00:00:{index:02d}+00:00", f"pay-pagination-{index}"),
            )
        db.commit()


def test_admin_payments_show_fifteen_records_per_page(app, client):
    _add_payments(app, 16)
    _login(client)

    first = client.get("/admin/payments")
    first_body = first.get_data(as_text=True)
    assert first.status_code == 200
    assert first_body.count('class="status status-pending"') == 15
    assert "1 / 2" in first_body
    assert "/admin/payments?page=2" in first_body
    assert "pay-pagination-0" not in first_body

    second = client.get("/admin/payments?page=2")
    second_body = second.get_data(as_text=True)
    assert second.status_code == 200
    assert second_body.count('class="status status-pending"') == 1
    assert "2 / 2" in second_body
    assert "pay-pagination-0" in second_body
    assert "/admin/payments?page=1" in second_body


def test_admin_payments_clamp_page_after_last_page(app, client):
    _add_payments(app, 16)
    _login(client)

    response = client.get("/admin/payments?page=999")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "2 / 2" in body
    assert "pay-pagination-0" in body
