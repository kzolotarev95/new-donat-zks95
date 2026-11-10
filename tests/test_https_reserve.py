"""Bounded root operations, rollback and authenticated reserve-port requests."""
import json
import re
import sqlite3
from types import SimpleNamespace
import time

import pytest

from app.settings import get_runtime_setting
from scripts import reconcile_https_reserve as helper

HTTP = """server {
    listen 80;
    server_name donate.example;
    return 301 https://$host$request_uri;
}
"""
TLS = """# Managed by new-donat-zks95 installer.
server {
    listen 443 ssl; # managed by Certbot
    listen [::]:443 ssl;
    server_name donate.example;
    ssl_certificate /etc/letsencrypt/live/donate.example/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/donate.example/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8015;
        add_header Example "}";
    }
}
"""


def login(client):
    body = client.get("/admin/login").get_data(as_text=True)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', body)[1]
    response = client.post("/admin/login", data={"csrf_token": csrf, "username": "admin", "password": "correct horse battery staple"})
    assert response.status_code == 302
    with client.session_transaction() as session:
        return session["_csrf_token"]


def write_status(app, tmp_path, **overrides):
    status = {"requested_enabled": False, "enabled": False, "state": "disabled",
              "domain": "donate.example", "message": "Выключен", "checked_at": time.time()}
    status.update(overrides)
    path = tmp_path / "status.json"
    path.write_text(json.dumps(status), encoding="utf-8")
    app.config["HTTPS_RESERVE_STATUS_PATH"] = str(path)


def test_enable_preserves_https_http_proxy_and_is_idempotent():
    original = TLS + HTTP
    enabled, domain = helper.render_config(original, True)
    assert domain == "donate.example"
    assert enabled.endswith(HTTP)
    assert enabled.count("listen 8443 ssl;") == 1
    assert enabled.count("listen [::]:8443 ssl;") == 1
    assert "listen 443 ssl; # managed by Certbot" in enabled
    assert "proxy_pass http://127.0.0.1:8015;" in enabled
    assert "ssl_certificate /etc/letsencrypt/live/donate.example/fullchain.pem;" in enabled
    assert helper.render_config(enabled, True)[0] == enabled
    disabled, _ = helper.render_config(enabled, False)
    assert disabled == original
    assert helper.render_config(disabled, False)[0] == disabled


def test_adopts_existing_manual_port_and_follows_new_domain():
    manual = TLS.replace("    listen [::]:443 ssl;", "    listen [::]:443 ssl;\n    listen 8443 ssl;\n    listen [::]:8443 ssl;")
    assert helper.configured_reserve(manual)
    changed, _ = helper.render_config(manual, True)
    assert changed.count("listen 8443 ssl;") == 1
    changed, domain = helper.render_config(changed.replace("donate.example", "new.example"), True)
    assert domain == "new.example"
    assert "/live/new.example/fullchain.pem" in changed
    assert helper.configured_reserve(changed)


@pytest.mark.parametrize("source", [HTTP, TLS.replace("# Managed by new-donat-zks95 installer.", "# Other site"), TLS + TLS, TLS.replace("listen 443 ssl;", "listen 443 ssl;\n    listen 8443;")])
def test_unsafe_or_ambiguous_config_is_rejected(source):
    with pytest.raises(helper.ReserveError):
        helper.render_config(source, True)


def test_other_server_block_and_quoted_comments_are_not_modified():
    other = 'server { listen 9443 ssl; server_name other.example; return 200 "hello # ; }"; }\n'
    enabled, domain = helper.render_config(TLS + other, True)
    assert domain == "donate.example"
    assert enabled.endswith(other)
    assert 'add_header Example "}";' in enabled


def test_db_request_is_read_only_and_accepts_only_a_boolean(tmp_path):
    database = tmp_path / "site.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
    connection.commit()
    assert helper.read_request(database) is None
    for raw, expected in [("1", True), ("0", False), ("1; arbitrary command", None)]:
        connection.execute("INSERT OR REPLACE INTO app_settings VALUES ('HTTPS_RESERVE_ENABLED', ?)", (raw,))
        connection.commit()
        if expected is None:
            with pytest.raises(helper.ReserveError):
                helper.read_request(database)
        else:
            assert helper.read_request(database) is expected
    assert connection.execute("SELECT count(*) FROM app_settings").fetchone()[0] == 1
    connection.close()


@pytest.mark.parametrize("response,valid", [("true", True), ("false", True), ("null", True), ('"1"', False), ('{"port":22}', False)])
def test_privileged_side_accepts_only_typed_boolean_from_reader(tmp_path, monkeypatch, response, valid):
    def run(arguments, **kwargs):
        assert arguments[:4] == ["/usr/sbin/runuser", "-u", "zks95", "--"]
        return SimpleNamespace(returncode=0, stdout=response, stderr="")
    monkeypatch.setattr(helper.subprocess, "run", run)
    if valid:
        assert helper.unprivileged_request(tmp_path / "site.db") is json.loads(response)
    else:
        with pytest.raises(helper.ReserveError):
            helper.unprivileged_request(tmp_path / "site.db")


@pytest.mark.parametrize("occupied,configured,rejected", [(False, False, False), (True, False, True), (True, True, False)])
def test_port_collision_protects_existing_listeners(monkeypatch, occupied, configured, rejected):
    monkeypatch.setattr(helper, "command", lambda _: 'LISTEN users:(("nginx",pid=1,fd=4))' if occupied else '')
    if rejected:
        with pytest.raises(helper.ReserveError, match="занят"):
            helper.check_port(configured)
    else:
        helper.check_port(configured)
    monkeypatch.setattr(helper, "command", lambda _: 'LISTEN users:(("xray",pid=1,fd=4))')
    with pytest.raises(helper.ReserveError, match="занят"):
        helper.check_port(True)


@pytest.mark.parametrize("failure", ["test", "reload", "health"])
def test_failed_change_restores_config_and_deduplicates_backups(tmp_path, monkeypatch, failure):
    path = tmp_path / "nginx.conf"
    path.write_text(TLS, encoding="utf-8")
    new, domain = helper.render_config(TLS, True)
    calls = []
    def command(args):
        calls.append(args)
        # Fail the new config, allow the rollback validation and reload.
        if path.read_text(encoding="utf-8") != TLS and ((failure == "test" and args[-1] == "-t") or (failure == "reload" and "reload" in args)):
            raise helper.ReserveError("simulated failure")
    def probe(_):
        if failure == "health":
            raise helper.ReserveError("simulated health failure")
    monkeypatch.setattr(helper, "command", command)
    monkeypatch.setattr(helper, "verify_https", probe)
    monkeypatch.setattr(helper.time, "sleep", lambda _: None)
    for _ in range(2):
        with pytest.raises(helper.ReserveError):
            helper.apply_config(path, TLS, new, domain, True, tmp_path / "backups")
        assert path.read_text(encoding="utf-8") == TLS
        assert calls[-1] == ["/usr/bin/systemctl", "reload", "nginx"]
    backups = list((tmp_path / "backups").glob("*.conf"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == TLS


def test_successful_apply_validates_and_checks_tls(tmp_path, monkeypatch):
    path = tmp_path / "nginx.conf"
    path.write_text(TLS, encoding="utf-8")
    new, domain = helper.render_config(TLS, True)
    calls = []
    monkeypatch.setattr(helper, "command", lambda args: calls.append(args))
    monkeypatch.setattr(helper, "verify_https", lambda host: calls.append(host))
    helper.apply_config(path, TLS, new, domain, True, tmp_path / "backups")
    assert path.read_text(encoding="utf-8") == new
    assert calls == [["/usr/sbin/nginx", "-t"], ["/usr/bin/systemctl", "reload", "nginx"], "donate.example"]


def test_web_changes_require_login_and_csrf(app, client):
    assert client.get("/admin/api/https-reserve").status_code == 302
    assert client.post("/admin/settings/https-reserve", data={"enabled": "1"}).status_code == 400
    csrf = login(client)
    assert client.post("/admin/settings/https-reserve", data={"enabled": "1"}).status_code == 400
    with app.app_context():
        assert get_runtime_setting("HTTPS_RESERVE_ENABLED", "missing") == "missing"
    assert client.post("/admin/settings/https-reserve", data={"csrf_token": csrf, "enabled": "8443; bad"}).status_code == 400
    assert client.post("/admin/settings/https-reserve", data={"csrf_token": csrf, "enabled": "1"}).status_code == 302
    with app.app_context():
        assert get_runtime_setting("HTTPS_RESERVE_ENABLED") == "1"


def test_web_pending_applied_disable_and_domain_urls(app, client, tmp_path):
    csrf = login(client)
    write_status(app, tmp_path)
    client.post("/admin/settings/https-reserve", data={"csrf_token": csrf, "enabled": "1"})
    snapshot = client.get("/admin/api/https-reserve").get_json()
    assert snapshot["state"] == "pending" and snapshot["desired_enabled"]
    write_status(app, tmp_path, requested_enabled=True, enabled=True, state="enabled", domain="new.example")
    snapshot = client.get("/admin/api/https-reserve").get_json()
    assert snapshot["state"] == "enabled"
    assert snapshot["site_url"] == "https://new.example:8443/donate"
    page = client.get("/admin/settings").get_data(as_text=True)
    assert page.index('id="two-factor"') < page.index('id="https-reserve"') < page.index("Глобальный эффект")
    assert 'class="https-reserve-toggle"' in page
    client.post("/admin/settings/https-reserve", data={"csrf_token": csrf})
    assert client.get("/admin/api/https-reserve").get_json()["state"] == "pending"
    with app.app_context():
        assert get_runtime_setting("HTTPS_RESERVE_ENABLED") == "0"


def test_status_adopts_manual_port_and_rejects_stale_or_unsafe_urls(app, client, tmp_path):
    login(client)
    write_status(app, tmp_path, requested_enabled=True, enabled=True, state="enabled")
    snapshot = client.get("/admin/api/https-reserve").get_json()
    assert snapshot["desired_enabled"] and snapshot["state"] == "enabled"
    write_status(app, tmp_path, checked_at=time.time() - 120)
    assert client.get("/admin/api/https-reserve").get_json()["state"] == "unavailable"
    write_status(app, tmp_path, requested_enabled=None, state="error", message="Конфигурация не подходит", domain="example.org/path")
    snapshot = client.get("/admin/api/https-reserve").get_json()
    assert snapshot["state"] == "error" and not snapshot["site_url"]


def test_second_factor_pending_session_cannot_change_port(app, client):
    csrf = login(client)
    with app.app_context():
        from app.db import get_db
        get_db().execute("UPDATE admin_users SET totp_secret='not-empty'")
        get_db().commit()
    response = client.post("/admin/settings/https-reserve", data={"csrf_token": csrf, "enabled": "1"})
    assert response.status_code == 302 and "/admin/login" in response.headers["Location"]
    with app.app_context():
        assert get_runtime_setting("HTTPS_RESERVE_ENABLED", "missing") == "missing"
