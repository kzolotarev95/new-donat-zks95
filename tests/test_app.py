from io import BytesIO
import json

from app.db import create_donor, list_donors
from app.settings import set_runtime_setting


def test_healthcheck(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"

def test_root_opens_donate(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/donate")

def test_donate_page_contains_donor_wall(client):
    response = client.get("/donate")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "donor-wall" in body
    assert "Собираю полезные системы" not in body
    assert "Ники появляются после подтверждения платежа" not in body
    assert "Пара слов разработчику" in body
    assert '<meta property="og:title" content="ZKS95 Developer Donate">' in body
    assert "apple-touch-icon.png" in body
    assert "visual-effect-none" in body
    assert "Золотарев Кирилл Сергеевич" in body
    assert "182501591471" in body
    assert '<footer' not in body
    assert 'href="/requisites"' in body
    assert "не является покупкой товара" in body

def test_recipient_and_services_are_public(client):
    details = client.get("/requisites")
    assert details.status_code == 200
    body = details.get_data(as_text=True)
    assert "Золотарев Кирилл Сергеевич" in body
    assert "Самозанятый (плательщик налога на профессиональный доход)" in body
    assert "182501591471" in body
    assert 'href="/services"' in body
    services = client.get("/services")
    assert services.status_code == 200
    body = services.get_data(as_text=True)
    assert "стоимость, сроки и порядок оплаты" in body
    assert 'href="/requisites"' in body

def test_public_navigation_and_contact_copy(client):
    home = client.get("/")
    home_body = client.get("/donate").get_data(as_text=True)
    contact = client.get("/contact")
    assert "Support Developer" not in home_body
    assert "https://t.me/kzolotarev95" in contact.get_data(as_text=True)
    assert "Открыть Telegram" in contact.get_data(as_text=True)
    assert "mailto:" not in contact.get_data(as_text=True)

def test_donor_amount_and_local_avatar(app, client):
    with app.app_context():
        donor_id = create_donor("Alice", "c042", "Спасибо", "123.45")
    page = client.get("/donate")
    assert "Alice" in page.get_data(as_text=True)
    assert "123.45 ₽" in page.get_data(as_text=True)
    avatar = client.get(f"/api/donors/{donor_id}/avatar.svg")
    assert avatar.status_code == 200
    assert avatar.mimetype == "image/svg+xml"

def test_pinned_donor_is_listed_first(app):
    with app.app_context():
        create_donor("Regular", "c001")
        create_donor("Pinned", "c002", pinned=1)
        assert [donor["nickname"] for donor in list_donors()] == ["Pinned", "Regular"]

def test_pinned_donor_has_distinct_public_card(app, client):
    with app.app_context():
        create_donor("Pinned", "c002", pinned=1)
    body = client.get("/donate").get_data(as_text=True)
    assert 'class="donor-card donor-c002 is-pinned"' in body
    assert 'data-pinned="true"' in body

def test_visual_settings_are_applied_globally(app, client):
    with app.app_context():
        set_runtime_setting("VISUAL_EFFECT", "snow")
        set_runtime_setting("VISUAL_EFFECT_SPEED", "fast")
    response = client.get("/donate")
    body = response.get_data(as_text=True)
    assert "visual-effect-snow" in body
    assert "visual-speed-fast" in body
    assert "visual-layer" in body

def test_missing_payment_config_is_explicit(client):
    response = client.post("/api/payments", json={"amount": "500"})
    assert response.status_code == 503
    assert "не настроена" in response.get_json()["error"]

def test_amount_validation(client):
    response = client.post("/api/payments", json={"amount": "10"})
    assert response.status_code == 400
    assert "Минимальная" in response.get_json()["error"]

def test_not_found(client):
    assert client.get("/does-not-exist").status_code == 404

def test_admin_login_and_dashboard(client):
    login = client.get("/admin/login")
    body = login.get_data(as_text=True)
    assert "Панель" not in body
    assert "admin-login-card" in body
    token = login.get_data(as_text=True).split('name="csrf_token" value="')[1].split('"', 1)[0]
    response = client.post("/admin/login", data={"csrf_token": token, "username": "admin", "password": "correct horse battery staple"})
    assert response.status_code == 302
    assert client.get("/admin/").status_code == 200

def test_admin_requires_authentication(client):
    assert client.get("/admin/payments").status_code == 302

def test_admin_donors_page(client):
    login = client.get("/admin/login")
    token = login.get_data(as_text=True).split('name="csrf_token" value="')[1].split('"', 1)[0]
    client.post("/admin/login", data={"csrf_token": token, "username": "admin", "password": "correct horse battery staple"})
    response = client.get("/admin/donors")
    assert response.status_code == 200
    assert "Добавить вручную" in response.get_data(as_text=True)
    assert "Сумма, ₽" in response.get_data(as_text=True)
    assert "Цвет 100" in response.get_data(as_text=True)

def test_admin_visual_settings_page(client):
    login = client.get("/admin/login")
    token = login.get_data(as_text=True).split('name="csrf_token" value="')[1].split('"', 1)[0]
    client.post("/admin/login", data={"csrf_token": token, "username": "admin", "password": "correct horse battery staple"})
    response = client.get("/admin/settings")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Глобальный эффект" in body
    assert "Техно-сетка" in body
    assert "Google reCAPTCHA" in body
    assert "YooKassa" in body
    assert "Бэкап и перенос" in body
    assert "Скачать список" in body

def test_admin_donor_backup_download_and_import(app, client):
    login = client.get("/admin/login")
    token = login.get_data(as_text=True).split('name="csrf_token" value="')[1].split('"', 1)[0]
    client.post("/admin/login", data={"csrf_token": token, "username": "admin", "password": "correct horse battery staple"})
    page = client.get("/admin/backup")
    assert page.status_code == 200
    assert "Скачать полный бэкап" in page.get_data(as_text=True)
    token = page.get_data(as_text=True).split('name="csrf_token" value="')[1].split('"', 1)[0]
    donor_backup = client.get("/admin/backup/download?kind=donors")
    assert donor_backup.status_code == 200
    assert donor_backup.get_json()["kind"] == "donors"
    full_backup = client.get("/admin/backup/download?kind=full")
    assert full_backup.status_code == 200
    assert full_backup.get_json()["kind"] == "full"
    assert "warning" in full_backup.get_json()
    payload = {"format": "zks95-backup", "version": 1, "kind": "donors", "donors": [{"nickname": "Imported", "color": "c001", "total_amount": "42", "is_active": 1}]}
    imported = client.post(
        "/admin/backup/import",
        data={"csrf_token": token, "backup_file": (BytesIO(json.dumps(payload).encode()), "donors.json")},
        content_type="multipart/form-data",
    )
    assert imported.status_code == 302
    with app.app_context():
        assert any(row["nickname"] == "Imported" for row in list_donors(active_only=False))

def test_admin_notifications_and_subscription(client):
    service_worker = client.get("/service-worker.js")
    assert service_worker.status_code == 200
    assert "showNotification" in service_worker.get_data(as_text=True)
    login = client.get("/admin/login")
    token = login.get_data(as_text=True).split('name="csrf_token" value="')[1].split('"', 1)[0]
    client.post("/admin/login", data={"csrf_token": token, "username": "admin", "password": "correct horse battery staple"})
    page = client.get("/admin/notifications")
    assert page.status_code == 200
    assert "Создать новые VAPID-ключи" in page.get_data(as_text=True)
    assert "Оповещать о запуске и восстановлении сервера" in page.get_data(as_text=True)
    token = page.get_data(as_text=True).split('data-csrf-token="')[1].split('"', 1)[0]
    generated = client.post("/admin/notifications/generate-vapid", data={"csrf_token": token})
    assert generated.status_code == 302
    subscription = client.post(
        "/admin/api/notifications/subscription",
        json={"endpoint": "https://push.example.test/subscription", "keys": {"p256dh": "p256dh", "auth": "auth"}},
        headers={"X-CSRF-Token": token},
    )
    assert subscription.status_code == 200
    assert subscription.get_json()["subscriptions"] == 1
    removed = client.delete(
        "/admin/api/notifications/subscription",
        json={"endpoint": "https://push.example.test/subscription"},
        headers={"X-CSRF-Token": token},
    )
    assert removed.status_code == 200
    assert removed.get_json()["subscriptions"] == 0

def test_donate_does_not_require_recaptcha(app, client):
    app.config["RECAPTCHA_SECRET_KEY"] = "captcha-secret"
    response = client.post("/api/payments", json={"amount": "500"})
    assert response.status_code == 503
    assert "не настроена" in response.get_json()["error"]

def test_public_nav_hides_about(client):
    body = client.get("/donate").get_data(as_text=True)
    assert ">About</a>" not in body

def test_payment_action_without_bank_banner(client):
    body = client.get("/donate").get_data(as_text=True)
    assert "payment-banner" not in body
    assert "Перейти к оплате" in body

def test_system_event_push_setting(app, monkeypatch):
    from app.push import notify_service_event
    from app.settings import set_runtime_setting

    with app.app_context():
        set_runtime_setting("PUSH_NOTIFY_SYSTEM", "1")
        monkeypatch.setattr("app.push.send_push", lambda payload: {"sent": 1, "failed": 0, "skipped": ""})
        result = notify_service_event("startup")
    assert result["sent"] == 1

def test_admin_pwa_starts_inside_admin_scope(client):
    manifest = client.get("/static/manifest.webmanifest").get_json()
    assert manifest["start_url"] == "/admin/"
    assert manifest["scope"] == "/admin/"

def test_admin_login_renders_recaptcha_when_configured(app, client):
    app.config["RECAPTCHA_SITE_KEY"] = "site-key"
    response = client.get("/admin/login")
    body = response.get_data(as_text=True)
    assert "g-recaptcha" in body
    assert "site-key" in body

def test_admin_captcha_error_is_next_to_captcha(app, client):
    app.config["RECAPTCHA_SITE_KEY"] = "site-key"
    app.config["RECAPTCHA_SECRET_KEY"] = "secret-key"
    login = client.get("/admin/login")
    token = login.get_data(as_text=True).split('name="csrf_token" value="')[1].split('"', 1)[0]
    response = client.post("/admin/login", data={"csrf_token": token, "username": "admin", "password": "correct horse battery staple"})
    body = response.get_data(as_text=True)
    assert response.status_code == 400
    assert 'class="captcha-error"' in body
    assert "Подтвердите, что вы не робот." in body
