import pytest
from scripts.upgrade_nginx import add_read_retry


def test_upgrade_preserves_tls_other_host_and_is_idempotent():
    other_host = '''server {
    server_name other.example;
    location / {
        proxy_pass http://127.0.0.1:9000;
    }
}
'''
    project_host = '''server {
    listen 443 ssl;
    ssl_certificate /etc/letsencrypt/live/donate.example/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/donate.example/privkey.pem;
    server_name donate.example;
    location / {
        proxy_pass http://127.0.0.1:8015;
    }
    location = /healthz {
        proxy_pass http://127.0.0.1:8015/healthz;
    }
}
'''
    upgraded = add_read_retry(other_host + project_host, 8015)
    assert upgraded.startswith(other_host)
    assert 'ssl_certificate /etc/letsencrypt/live/donate.example/fullchain.pem;' in upgraded
    assert 'ssl_certificate_key /etc/letsencrypt/live/donate.example/privkey.pem;' in upgraded
    assert upgraded.count('location @zks95_read_retry') == 1
    assert 'if ($request_method ~ ^(GET|HEAD)$)' in upgraded
    assert add_read_retry(upgraded, 8015) == upgraded


def test_unknown_config_is_rejected_without_guessing():
    with pytest.raises(ValueError):
        add_read_retry('server { listen 443 ssl; }', 8015)
