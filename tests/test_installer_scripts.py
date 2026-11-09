from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_install_uses_context_for_admin_count_and_safe_env_permissions():
    source = read("install.sh")
    assert 'with app.app_context():' in source
    assert 'chmod 0600 "${ENV_FILE}"' in source
    assert 'PYTHONPATH="${APP_DIR}" DATABASE_PATH="${DB_PATH}"' in source
    assert 'APP_PORT="8015"' in source
    assert 'scripts/reconcile_nginx.sh' in source


def test_update_repairs_runtime_even_when_no_git_changes():
    source = read("update.sh")
    assert 'Обновлений нет' in source
    assert 'repair_runtime()' in source
    assert 'scripts/reconcile_nginx.sh' in source
    assert '127.0.0.1:${APP_PORT}/healthz' in source
    assert 'PYTHONPATH="${APP_DIR}" DATABASE_PATH="${DB_PATH}"' in source


def test_nginx_reconciliation_scans_project_and_conf_d_directories():
    source = read("scripts/reconcile_nginx.sh")
    assert '/etc/nginx/sites-enabled/*' in source
    assert '/etc/nginx/conf.d/*' in source
    assert 'nginx -t' in source
    assert 'Обнаружен чужой Nginx-конфиг' in source


def test_uninstall_is_scoped_and_keeps_nginx_packages_and_certificates():
    source = read("uninstall.sh")
    assert 'scripts/remove_nginx.sh' in source
    assert 'Nginx, сертификаты и системные пакеты не удалялись.' in source
    assert 'Введите REMOVE' in source


def test_runtime_port_is_unified():
    assert '8015' in read("run.py")
    assert '8015' in read("run.sh")
    assert 'config/gunicorn.conf.py' in read("config/systemd/new-donat-zks95.service")
    assert '127.0.0.1:8015' in read("config/gunicorn.conf.py")
    assert '__APP_PORT__' in read("config/nginx/new-donat-zks95.conf.template")


def test_default_admin_bootstrap_is_blocked_in_production():
    source = read("scripts/create_admin.py")
    assert 'insecure admin/admin bootstrap is disabled in production' in source
    assert 'Run without --default.' in source


def test_ssl_setup_requires_dns_and_email_before_certbot():
    source = read("install.sh")
    assert 'Email не указан. HTTPS пропущен' in source
    assert 'DNS_OK=0' in source
    assert 'PORT80_OK=0' in source
    assert 'certbot renew --dry-run' in source
