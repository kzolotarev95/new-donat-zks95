"""Security boundaries of enrollment and both stages of administrator login."""
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pyotp
import pytest

from app import create_app
from app.db import get_admin_user, get_db, update_admin_password
from app.security import decrypt_value
from app.two_factor import AuthError, consume_factor, take_attempt
from werkzeug.security import generate_password_hash

PASSWORD = "correct horse battery staple"


@pytest.fixture
def clock(monkeypatch):
    value = [int(time.time())]
    fake = SimpleNamespace(time=lambda: value[0])
    monkeypatch.setattr("app.two_factor.time", fake)
    monkeypatch.setattr("app.admin.time", fake)
    return value


def token(client, path="/admin/settings"):
    page = client.get(path)
    return re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))[1]


def login(client, target="/admin/login"):
    return client.post(target, data={"csrf_token": token(client, "/admin/login"),
                                    "username": "admin", "password": PASSWORD})


def enroll(app, client, clock):
    login(client)
    response = client.post("/admin/2fa/setup", data={"csrf_token": token(client), "current_password": PASSWORD})
    assert response.status_code == 302
    with app.app_context():
        secret = decrypt_value(get_admin_user("admin")["totp_pending_secret"])
    response = client.post("/admin/2fa/enable", data={"csrf_token": token(client), "code": pyotp.TOTP(secret).at(clock[0])})
    assert response.status_code == 200
    codes = re.findall(r"\b[0-9a-f]{5}(?:-[0-9a-f]{5}){3}\b", response.get_data(as_text=True))
    assert len(set(codes)) == 10
    return secret, codes


def confirm(client, code):
    return client.post("/admin/login/2fa", data={"csrf_token": token(client, "/admin/login/2fa"), "code": code})


def test_enrollment_is_confirmed_encrypted_and_not_stored_in_cookie(app, client, clock):
    login(client)
    csrf = token(client)
    client.post("/admin/2fa/setup", data={"csrf_token": csrf, "current_password": "wrong"})
    assert client.get("/admin/2fa/qr.svg").status_code == 404
    client.post("/admin/2fa/setup", data={"csrf_token": csrf, "current_password": PASSWORD})
    with app.app_context():
        user = get_admin_user("admin")
        assert not user["totp_secret"]
        secret = decrypt_value(user["totp_pending_secret"])
        assert secret and secret != user["totp_pending_secret"]
    with client.session_transaction() as saved:
        assert secret not in str(dict(saved))
    page = client.get("/admin/settings")
    assert "no-store" in page.headers["Cache-Control"]
    assert secret in page.get_data(as_text=True)
    qr = client.get("/admin/2fa/qr.svg")
    assert qr.status_code == 200 and qr.mimetype == "image/svg+xml"
    assert "no-store" in qr.headers["Cache-Control"]
    client.post("/admin/2fa/enable", data={"csrf_token": csrf, "code": "bad"})
    with app.app_context():
        assert not get_admin_user("admin")["totp_secret"]
    response = client.post("/admin/2fa/enable", data={"csrf_token": csrf, "code": pyotp.TOTP(secret).at(clock[0])})
    codes = re.findall(r"\b[0-9a-f]{5}(?:-[0-9a-f]{5}){3}\b", response.get_data(as_text=True))
    assert len(codes) == 10
    assert "no-store" in response.headers["Cache-Control"]
    with app.app_context():
        user = get_admin_user("admin")
        assert decrypt_value(user["totp_secret"]) == secret
        assert not user["totp_pending_secret"]
        hashes = [row[0] for row in get_db().execute("SELECT code_hash FROM admin_recovery_codes")]
        assert len(hashes) == 10
        assert all(code.replace("-", "") not in hashes for code in codes)
    with client.session_transaction() as saved:
        assert secret not in str(dict(saved))
        assert not any(code in str(dict(saved)) for code in codes)
    assert secret not in client.get("/admin/settings").get_data(as_text=True)
    assert client.get("/admin/2fa/qr.svg").status_code == 404


def test_password_alone_cannot_access_any_admin_resource(app, client, clock):
    secret, _ = enroll(app, client, clock)
    other = app.test_client()
    response = login(other, "/admin/login?next=/admin/payments")
    assert response.location.endswith("/admin/login/2fa")
    with other.session_transaction() as saved:
        assert "admin_user_id" not in saved
    for path in ("/admin/", "/admin/settings", "/admin/api/summary", "/admin/backup/download?kind=full", "/admin/2fa/qr.svg"):
        assert other.get(path).status_code == 302
    assert confirm(other, "bad").status_code == 401
    clock[0] += 30
    response = confirm(other, pyotp.TOTP(secret).at(clock[0]))
    assert response.location.endswith("/admin/payments")
    assert other.get("/admin/").status_code == 200


def test_replayed_totp_and_recovery_codes_are_rejected(app, client, clock):
    secret, codes = enroll(app, client, clock)
    second = app.test_client()
    login(second)
    assert confirm(second, pyotp.TOTP(secret).at(clock[0])).status_code == 401
    clock[0] += 30
    assert confirm(second, pyotp.TOTP(secret).at(clock[0])).status_code == 302
    third = app.test_client()
    login(third)
    assert confirm(third, pyotp.TOTP(secret).at(clock[0])).status_code == 401
    assert confirm(third, codes[0]).status_code == 302
    fourth = app.test_client()
    login(fourth)
    assert confirm(fourth, codes[0]).status_code == 401
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM admin_recovery_codes").fetchone()[0] == 9


def test_factor_throttle_survives_new_clients(app, client, clock):
    secret, codes = enroll(app, client, clock)
    other = app.test_client()
    login(other)
    for _ in range(5):
        assert confirm(other, "invalid").status_code == 401
    assert confirm(other, codes[0]).status_code == 429
    fresh = app.test_client()
    login(fresh)
    assert confirm(fresh, codes[0]).status_code == 429
    clock[0] += 301
    assert fresh.get("/admin/login/2fa").status_code == 302
    login(fresh)
    assert confirm(fresh, pyotp.TOTP(secret).at(clock[0])).status_code == 302


def test_setup_secret_is_session_bound_and_expires(app, client, clock):
    login(client)
    other = app.test_client()
    login(other)
    client.post("/admin/2fa/setup", data={"csrf_token": token(client), "current_password": PASSWORD})
    with app.app_context():
        secret = decrypt_value(get_admin_user("admin")["totp_pending_secret"])
    assert other.get("/admin/2fa/qr.svg").status_code == 404
    assert secret not in other.get("/admin/settings").get_data(as_text=True)
    other.post("/admin/2fa/enable", data={"csrf_token": token(other), "code": pyotp.TOTP(secret).at(clock[0])})
    with app.app_context():
        assert not get_admin_user("admin")["totp_secret"]
    clock[0] += 601
    assert client.get("/admin/2fa/qr.svg").status_code == 404
    assert secret not in client.get("/admin/settings").get_data(as_text=True)


def test_enabling_revokes_preexisting_sessions(app, client, clock):
    old = app.test_client()
    login(old)
    enroll(app, client, clock)
    assert old.get("/admin/settings").status_code == 302
    # A legacy password-only session is insufficient even at the current version.
    with app.app_context():
        user = get_admin_user("admin")
    with old.session_transaction() as saved:
        saved["admin_user_id"] = user["id"]
        saved["admin_auth_version"] = user["auth_version"]
    assert old.get("/admin/").status_code == 302


def test_disable_requires_password_and_unused_factor(app, client, clock):
    secret, codes = enroll(app, client, clock)
    other = app.test_client()
    login(other)
    confirm(other, codes[0])
    csrf = token(client)
    for password, code in (("wrong", codes[1]), (PASSWORD, "invalid"), (PASSWORD, pyotp.TOTP(secret).at(clock[0]))):
        client.post("/admin/2fa/disable", data={"csrf_token": csrf, "current_password": password, "code": code})
        with app.app_context():
            assert get_admin_user("admin")["totp_secret"]
    client.post("/admin/2fa/disable", data={"csrf_token": csrf, "current_password": PASSWORD, "code": codes[1]})
    with app.app_context():
        assert not get_admin_user("admin")["totp_secret"]
        assert get_db().execute("SELECT COUNT(*) FROM admin_recovery_codes").fetchone()[0] == 0
    assert other.get("/admin/").status_code == 302
    assert client.get("/admin/").status_code == 200
    assert login(app.test_client()).location.endswith("/admin/")


def test_rotating_recovery_codes_revokes_old_codes_and_sessions(app, client, clock):
    _, codes = enroll(app, client, clock)
    other = app.test_client()
    login(other)
    confirm(other, codes[0])
    response = client.post("/admin/2fa/recovery-codes", data={"csrf_token": token(client), "current_password": PASSWORD, "code": codes[1]})
    assert response.status_code == 200
    replacement = re.findall(r"\b[0-9a-f]{5}(?:-[0-9a-f]{5}){3}\b", response.get_data(as_text=True))
    assert len(replacement) == 10 and not set(codes) & set(replacement)
    assert other.get("/admin/").status_code == 302
    login(other)
    assert confirm(other, codes[2]).status_code == 401
    assert confirm(other, replacement[0]).status_code == 302


@pytest.mark.parametrize("path", ["setup", "enable", "cancel", "disable", "recovery-codes"])
def test_security_actions_reject_missing_csrf(client, path):
    login(client)
    assert client.post(f"/admin/2fa/{path}", data={"current_password": PASSWORD}).status_code == 400


def test_password_reset_does_not_disable_second_factor(app, client, clock):
    _, codes = enroll(app, client, clock)
    other = app.test_client()
    login(other)
    with app.app_context():
        user = get_admin_user("admin")
        update_admin_password(user["id"], generate_password_hash(PASSWORD))
        assert get_admin_user("admin")["totp_secret"]
    assert client.get("/admin/").status_code == 302
    assert other.get("/admin/login/2fa").status_code == 302
    assert login(other).location.endswith("/admin/login/2fa")
    assert confirm(other, codes[0]).status_code == 302


def test_full_backup_cannot_export_or_replace_authentication(app, client, clock):
    secret, codes = enroll(app, client, clock)
    payload = client.get("/admin/backup/download?kind=full").get_json()
    assert secret not in str(payload) and not any(code in str(payload) for code in codes)
    from app.backup import import_backup
    with app.app_context():
        import_backup(payload)
        assert get_admin_user("admin")["totp_secret"]


def test_recovery_code_consumption_is_atomic_between_workers(app, client, clock):
    _, codes = enroll(app, client, clock)
    def use_code():
        with app.app_context():
            try:
                consume_factor(get_admin_user("admin"), codes[0])
                return True
            except AuthError:
                return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: use_code(), range(2)))
    assert sorted(results) == [False, True]


def test_password_rate_limit_and_safe_login_redirect(client):
    csrf = token(client, "/admin/login")
    for _ in range(10):
        assert client.post("/admin/login", data={"csrf_token": csrf, "username": "unknown", "password": "bad"}).status_code == 401
    assert client.post("/admin/login", data={"csrf_token": csrf, "username": "unknown", "password": "bad"}).status_code == 429
    assert login(client, "/admin/login?next=https://example.test/steal").location.endswith("/admin/")


def test_parallel_factor_attempts_cannot_exceed_limit(app, clock):
    def attempt(_):
        with app.app_context():
            try:
                take_attempt("parallel-login")
                return True
            except AuthError:
                return False
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(12)))
    assert results.count(True) == 5


def test_unreadable_totp_secret_does_not_bypass_protection(app, client, clock):
    enroll(app, client, clock)
    with app.app_context():
        get_db().execute("UPDATE admin_users SET totp_secret = 'unreadable', auth_version = auth_version + 1")
        get_db().commit()
    assert client.get("/admin/").status_code == 302
    assert login(client).location.endswith("/admin/login/2fa")
    assert confirm(client, "123456").status_code == 401
    assert client.get("/admin/settings").status_code == 302


def test_production_admin_rejects_weak_session_signing_key(app, client):
    app.config["APP_ENV"] = "production"
    app.config["SECRET_KEY"] = "change-me-in-production"
    assert client.get("/admin/login").status_code == 503
    assert client.get("/donate").status_code == 200


def test_legacy_database_migrates_without_resetting_credentials(tmp_path):
    path = tmp_path / "legacy.db"
    password_hash = generate_password_hash(PASSWORD)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE admin_users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT, created_at TEXT, updated_at TEXT, last_login_at TEXT)")
        db.execute("INSERT INTO admin_users VALUES(1, 'existing', ?, '', '', NULL)", (password_hash,))
    migrated = create_app({"TESTING": True, "SECRET_KEY": "test", "APP_ENV": "testing", "DATABASE_PATH": str(path)})
    with migrated.app_context():
        user = get_admin_user("existing")
        assert user["password_hash"] == password_hash
        assert user["auth_version"] == 0 and user["totp_secret"] == ""
