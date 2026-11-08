from app.db import create_donor


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
    assert "donor-wall" in response.get_data(as_text=True)

def test_donor_amount_and_local_avatar(app, client):
    with app.app_context():
        donor_id = create_donor("Alice", "c042", "Спасибо", "123.45")
    page = client.get("/donate")
    assert "Alice" in page.get_data(as_text=True)
    assert "123.45 ₽" in page.get_data(as_text=True)
    avatar = client.get(f"/api/donors/{donor_id}/avatar.svg")
    assert avatar.status_code == 200
    assert avatar.mimetype == "image/svg+xml"

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

def test_donate_does_not_require_recaptcha(app, client):
    app.config["RECAPTCHA_SECRET_KEY"] = "captcha-secret"
    response = client.post("/api/payments", json={"amount": "500"})
    assert response.status_code == 503
    assert "не настроена" in response.get_json()["error"]

def test_admin_login_renders_recaptcha_when_configured(app, client):
    app.config["RECAPTCHA_SITE_KEY"] = "site-key"
    response = client.get("/admin/login")
    body = response.get_data(as_text=True)
    assert "g-recaptcha" in body
    assert "site-key" in body
