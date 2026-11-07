import pytest
from app import create_app

@pytest.fixture()
def app(tmp_path):
    return create_app({
        "TESTING": True,
        "APP_ENV": "testing",
        "SECRET_KEY": "test",
        "DATABASE_PATH": str(tmp_path / "site.db"),
        "YOOKASSA_SHOP_ID": "",
        "YOOKASSA_SECRET_KEY": "",
    })

@pytest.fixture()
def client(app):
    return app.test_client()
