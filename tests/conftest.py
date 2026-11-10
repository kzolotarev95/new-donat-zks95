import pytest
from app import create_app

@pytest.fixture()
def app(tmp_path):
    return create_app({
        "TESTING": True,
        "APP_ENV": "testing",
        "SECRET_KEY": "test",
        "DATABASE_PATH": str(tmp_path / "site.db"),
        "HTTPS_RESERVE_STATUS_PATH": str(tmp_path / "https-reserve-status.json"),
        "YOOKASSA_SHOP_ID": "",
        "YOOKASSA_SECRET_KEY": "",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "correct horse battery staple",
    })

@pytest.fixture()
def client(app):
    return app.test_client()
