"""Session revocation, trusted client addresses and non-blocking login alerts."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import re
import sqlite3
import threading
import time

import pyotp
import pytest
from werkzeug.security import generate_password_hash, check_password_hash

from app import create_app
from app.admin_sessions import client_address
from app.db import get_admin_user, get_db
from app.login_notifications import drain_pending
from app.security import encrypt_value
from app.settings import set_runtime_setting
from scripts.admin_security import backup_database, recover_account

PASSWORD = "correct horse battery staple"


def token(client, path="/admin/account"):
    response = client.get(path)
    return re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))[1]


def login(client, **kwargs):
    return client.post("/admin/login", data={"csrf_token": token(client, "/admin/login"), "username": "admin", "password": PASSWORD}, **kwargs)


def enable_push(app):
    with app.app_context():
        set_runtime_setting("PUSH_ENABLED", "1")
        set_runtime_setting("PUSH_NOTIFY_LOGINS", "1")


@pytest.mark.parametrize("peer,forwarded,expected", [
    ("127.0.0.1", "203.0.113.7", "203.0.113.7"),
    ("::1", "2001:db8::7", "2001:db8::7"),
    ("198.51.100.9", "203.0.113.7", "198.51.100.9"),
    ("127.0.0.1", "garbage", "127.0.0.1"),
    ("127.0.0.1", "203.0.113.7, 1.2.3.4", "127.0.0.1"),
])
def test_client_address_trusts_only_local_nginx(app, peer, forwarded, expected):
    with app.test_request_context(environ_base={"REMOTE_ADDR": peer}, headers={"X-Real-IP": forwarded, "X-Forwarded-For": "1.2.3.4"}):
        assert client_address() == expected


def test_history_records_completed_login_and_hashes_token(app, client):
    login(client, headers={"X-Real-IP": "203.0.113.7", "User-Agent": "Mozilla/5.0 (iPhone) Safari/605.1"})
    with client.session_transaction() as cookie:
        raw_token = cookie["admin_session_token"]
    with app.app_context():
        row = get_db().execute("SELECT * FROM admin_sessions").fetchone()
        assert row["token_hash"] == hashlib.sha256(raw_token.encode()).hexdigest()
        assert row["ip_address"] == "203.0.113.7"
        assert row["login_method"] == "password"
        assert row["started_at"] <= time.time() < row["expires_at"]
    page = client.get("/admin/account")
    body = page.get_data(as_text=True)
    assert "203.0.113.7" in body and "iPhone · Safari" in body and "Этот сеанс" in body
    assert "UTC" in body and "Мануал команд" in body and "reset-2fa" in body
    assert raw_token not in body and row["token_hash"] not in body
    assert "no-store" in page.headers["Cache-Control"]


def test_failed_password_does_not_create_history_or_push(app, client):
    enable_push(app)
    assert client.post("/admin/login", data={"csrf_token": token(client, "/admin/login"), "username": "admin", "password": "wrong"}).status_code == 401
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0] == 0
        assert get_db().execute("SELECT COUNT(*) FROM admin_login_notifications").fetchone()[0] == 0


def test_logout_revokes_captured_cookie(app, client):
    login(client)
    stolen = client.get_cookie("session").value
    assert client.post("/admin/logout", data={"csrf_token": token(client)}).status_code == 302
    replay = app.test_client()
    replay.set_cookie("session", stolen)
    assert replay.get("/admin/account").status_code == 302
    with app.app_context():
        assert get_db().execute("SELECT revocation_reason FROM admin_sessions").fetchone()[0] == "logout"


def test_revoke_other_sessions_keeps_current_cookie(app, client):
    login(client)
    other = app.test_client()
    login(other)
    response = client.post("/admin/account/sessions/revoke", data={"csrf_token": token(client), "others": "1"})
    assert response.status_code == 302
    assert client.get("/admin/account").status_code == 200
    assert other.get("/admin/account").status_code == 302


def test_single_revoke_is_scoped_to_owner_and_requires_csrf(app, client):
    login(client)
    other = app.test_client()
    login(other)
    with app.app_context():
        row_id = get_db().execute("SELECT MAX(id) FROM admin_sessions").fetchone()[0]
    assert client.post("/admin/account/sessions/revoke", data={"session_id": row_id}).status_code == 400
    assert client.post("/admin/account/sessions/revoke", data={"csrf_token": token(client), "session_id": row_id}).status_code == 302
    assert other.get("/admin/").status_code == 302
    assert client.post("/admin/account/sessions/revoke", data={"csrf_token": token(client), "session_id": 99999}).status_code == 404


def test_server_expiry_and_missing_token_reject_access(app, client):
    login(client)
    with app.app_context():
        get_db().execute("UPDATE admin_sessions SET expires_at = ?", (int(time.time()) - 1,))
        get_db().commit()
    assert client.get("/admin/").status_code == 302
    with client.session_transaction() as cookie:
        cookie["admin_user_id"] = 1
        cookie["admin_auth_version"] = 0
    assert client.get("/admin/").status_code == 302


def test_sessions_survive_application_restart(app, client):
    login(client)
    cookie = client.get_cookie("session").value
    restarted = create_app(dict(app.config))
    resumed = restarted.test_client()
    resumed.set_cookie("session", cookie)
    assert resumed.get("/admin/account").status_code == 200
    with restarted.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0] == 1


def test_remembered_device_has_long_lifetime_and_upgrades_old_active_session(app, client):
    login(client)
    lifetime = int(app.permanent_session_lifetime.total_seconds())
    assert lifetime >= 10 * 365 * 24 * 60 * 60
    now = int(time.time())
    with app.app_context():
        db = get_db()
        db.execute("UPDATE admin_sessions SET last_seen_at = ?, expires_at = ?", (now, now + 300))
        db.commit()
    assert client.get("/admin/").status_code == 200
    with app.app_context():
        row = get_db().execute("SELECT last_seen_at, expires_at FROM admin_sessions").fetchone()
        assert row["last_seen_at"] >= now
        assert row["expires_at"] >= now + lifetime - 5


def test_account_change_preserves_current_history_and_revokes_others(app, client):
    login(client)
    other = app.test_client()
    login(other)
    assert client.post("/admin/account", data={"csrf_token": token(client), "current_password": PASSWORD, "username": "renamed", "new_password": "", "repeat_password": ""}).status_code == 302
    assert client.get("/admin/account").status_code == 200
    assert other.get("/admin/account").status_code == 302
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0] == 2


def test_history_pagination_does_not_expose_other_users(app, client):
    login(client)
    with app.app_context():
        db = get_db()
        db.execute("""INSERT INTO admin_users(username, password_hash, created_at, updated_at) VALUES ('another', 'unused', '', '')""")
        db.execute("""INSERT INTO admin_sessions(user_id, token_hash, auth_version, ip_address, user_agent, login_method, started_at, last_seen_at, expires_at)
            SELECT 2, 'other-secret', 0, '192.0.2.99', '', 'password', started_at, last_seen_at, expires_at FROM admin_sessions LIMIT 1""")
        for i in range(14):
            db.execute("""INSERT INTO admin_sessions(user_id, token_hash, auth_version, ip_address, user_agent, login_method, started_at, last_seen_at, expires_at)
                SELECT 1, ?, 0, '203.0.113.8', '', 'password', started_at, last_seen_at, expires_at FROM admin_sessions LIMIT 1""", (f"test-{i}",))
        db.commit()
        other_id = db.execute("SELECT id FROM admin_sessions WHERE user_id = 2").fetchone()[0]
    body = client.get("/admin/account?page=2").get_data(as_text=True)
    assert "2 / 2" in body and "192.0.2.99" not in body
    assert client.post("/admin/account/sessions/revoke", data={"csrf_token": token(client), "session_id": other_id}).status_code == 404
    assert client.get("/admin/account?page=bad").status_code == 200


def test_push_is_queued_only_after_two_factor_and_not_delivered_in_request(app, client, monkeypatch):
    enable_push(app)
    secret = pyotp.random_base32()
    with app.app_context():
        get_db().execute("UPDATE admin_users SET totp_secret = ?", (encrypt_value(secret),))
        get_db().commit()
    delivered = []
    monkeypatch.setattr("app.login_notifications.send_push", lambda payload: delivered.append(payload) or {"sent": 1, "failed": 0, "skipped": ""})
    assert login(client).location.endswith("/admin/login/2fa")
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM admin_login_notifications").fetchone()[0] == 0
        assert get_db().execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0] == 0
    assert client.post("/admin/login/2fa", data={"csrf_token": token(client, "/admin/login/2fa"), "code": pyotp.TOTP(secret).now()}).status_code == 302
    assert not delivered
    with app.app_context():
        assert get_db().execute("SELECT login_method FROM admin_sessions").fetchone()[0] == "totp"
        assert drain_pending() == 1
        assert drain_pending() == 0
    assert len(delivered) == 1 and "127.0.0.1" in delivered[0]["body"]
    assert delivered[0]["data"]["url"] == "/admin/account#sessions"


def test_push_toggle_and_retry_after_transient_failure(app, client, monkeypatch):
    enable_push(app)
    with app.app_context():
        set_runtime_setting("PUSH_NOTIFY_LOGINS", "0")
    login(client)
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM admin_login_notifications").fetchone()[0] == 0
        set_runtime_setting("PUSH_NOTIFY_LOGINS", "1")
    login(app.test_client())
    attempts = []
    def delivery(payload):
        attempts.append(payload)
        if len(attempts) == 1:
            raise TimeoutError("test transport failure")
        return {"sent": 1, "failed": 0, "skipped": ""}
    monkeypatch.setattr("app.login_notifications.send_push", delivery)
    with app.app_context():
        assert drain_pending() == 1
        row = get_db().execute("SELECT * FROM admin_login_notifications").fetchone()
        assert row["status"] == "pending" and row["attempts"] == 1
        assert drain_pending() == 0
        get_db().execute("UPDATE admin_login_notifications SET available_at = 0")
        get_db().commit()
        assert drain_pending() == 1
        assert get_db().execute("SELECT status FROM admin_login_notifications").fetchone()[0] == "sent"


def test_outbox_is_claimed_once_between_workers(app, client, monkeypatch):
    enable_push(app)
    login(client)
    entered = threading.Event()
    release = threading.Event()
    def delivery(_payload):
        entered.set()
        assert release.wait(5)
        return {"sent": 1, "failed": 0, "skipped": ""}
    monkeypatch.setattr("app.login_notifications.send_push", delivery)
    def worker():
        with app.app_context():
            return drain_pending()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker)
        assert entered.wait(5)
        try:
            assert pool.submit(worker).result(5) == 0
        finally:
            release.set()
        assert first.result(5) == 1


def test_interrupted_push_lease_is_recovered_and_retries_are_bounded(app, client, monkeypatch):
    enable_push(app)
    login(client)
    monkeypatch.setattr("app.login_notifications.send_push", lambda _: {"sent": 0, "failed": 1, "skipped": ""})
    with app.app_context():
        db = get_db()
        db.execute("UPDATE admin_login_notifications SET status = 'processing', attempts = 1, lease_until = ?", (int(time.time()) + 600,))
        db.commit()
        assert drain_pending() == 0
        db.execute("UPDATE admin_login_notifications SET lease_until = 0")
        db.commit()
        assert drain_pending() == 1
        db.execute("UPDATE admin_login_notifications SET available_at = 0")
        db.commit()
        assert drain_pending() == 1
        row = db.execute("SELECT * FROM admin_login_notifications").fetchone()
        assert row["status"] == "failed" and row["attempts"] == 3
        assert drain_pending() == 0


def test_disabling_push_cancels_queued_login_alerts(app, client):
    enable_push(app)
    login(client)
    with app.app_context():
        set_runtime_setting("PUSH_NOTIFY_LOGINS", "0")
        assert drain_pending() == 1
        assert get_db().execute("SELECT status FROM admin_login_notifications").fetchone()[0] == "cancelled"


def test_backup_includes_wal_and_recovery_revokes_sessions(app, client, tmp_path):
    login(client)
    env_file = tmp_path / "config.env"
    env_file.write_text("APP_SECRET_KEY=local-test-secret\n", encoding="utf-8")
    with app.app_context():
        db = get_db()
        db.execute("UPDATE admin_users SET totp_secret = 'encrypted-test', totp_pending_secret = 'pending'")
        db.execute("INSERT INTO admin_recovery_codes VALUES (1, 'hash-test')")
        db.commit()
        folder = backup_database(app.config["DATABASE_PATH"], env_file, tmp_path / "backups")
        with sqlite3.connect(folder / "site.db") as snapshot:
            assert snapshot.execute("SELECT totp_secret FROM admin_users").fetchone()[0] == "encrypted-test"
        assert (folder / ".env").read_text() == env_file.read_text()
        if os.name == "posix":
            assert (folder.stat().st_mode & 0o777) == 0o700
            assert ((folder / "site.db").stat().st_mode & 0o777) == 0o600
        recover_account(db, 1, "reset-2fa")
        user = get_admin_user("admin")
        assert not user["totp_secret"] and not user["totp_pending_secret"]
        assert check_password_hash(user["password_hash"], PASSWORD)
        assert db.execute("SELECT COUNT(*) FROM admin_recovery_codes").fetchone()[0] == 0
        assert db.execute("SELECT revocation_reason FROM admin_sessions").fetchone()[0] == "ssh-recovery"
    assert client.get("/admin/").status_code == 302


def test_ssh_password_reset_preserves_two_factor(app, client):
    login(client)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE admin_users SET totp_secret = 'keep-2fa'")
        db.commit()
        recover_account(db, 1, "reset-password", generate_password_hash("new-password-long"))
        user = get_admin_user("admin")
        assert user["totp_secret"] == "keep-2fa"
        assert check_password_hash(user["password_hash"], "new-password-long")
    assert client.get("/admin/").status_code == 302
