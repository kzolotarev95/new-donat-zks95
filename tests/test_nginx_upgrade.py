import pytest
from scripts.upgrade_nginx import add_read_retry, preserve_public_host, upgrade_config


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


def test_existing_reserve_keeps_public_port_and_certificates_after_upgrade():
    original = '''server {
    listen 443 ssl;
    listen 8443 ssl;
    ssl_certificate /etc/letsencrypt/live/donate.example/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/donate.example/privkey.pem;
    server_name donate.example;
    location / {
        proxy_pass http://127.0.0.1:8015;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location = /healthz {
        proxy_pass http://127.0.0.1:8015/healthz;
    }
}
'''
    # Include an already installed read retry: that must not skip the new fix.
    installed = add_read_retry(original, 8015).replace('Host $http_host;', 'Host $host;')
    updated = upgrade_config(installed, 8015)
    assert 'proxy_set_header Host $host;' not in updated
    assert updated.count('proxy_set_header Host $http_host;') == 3
    assert updated.count('location @zks95_read_retry') == 1
    for line in original.splitlines():
        if line.strip().startswith(('listen ', 'ssl_certificate', 'server_name ')):
            assert line in updated
    assert upgrade_config(updated, 8015) == updated


def test_public_host_migration_only_changes_locations_of_this_backend():
    other = '''server {
    server_name other.example;
    location / {
        proxy_pass http://127.0.0.1:9000;
        proxy_set_header Host $host;
    }
}
'''
    project = '''server {
    location / {
        if ($request_method ~ ^(GET|HEAD)$) {
            error_page 502 504 = @zks95_read_retry;
        }
        proxy_pass http://127.0.0.1:8015;
        proxy_set_header Host $host; # Preserve this comment.
    }
}
'''
    updated = preserve_public_host(other + project, 8015)
    assert updated.startswith(other)
    assert 'Host $http_host; # Preserve this comment.' in updated
    assert preserve_public_host(updated, 8015) == updated


def test_missing_host_header_is_added_and_custom_host_is_preserved():
    missing = '''location / {
    proxy_pass http://127.0.0.1:8015;
}
'''
    updated = preserve_public_host(missing, 8015)
    assert '    proxy_set_header Host $http_host;' in updated
    custom = updated.replace('$http_host', 'custom.example:8443')
    assert preserve_public_host(custom, 8015) == custom
    assert preserve_public_host(missing, 80) == missing
