"""Environment-backed application settings."""
from dataclasses import dataclass
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _env(name, default=""):
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    app_env: str
    app_secret_key: str
    site_domain: str
    yookassa_shop_id: str
    yookassa_secret_key: str
    yookassa_return_url: str
    yookassa_webhook_url: str
    yookassa_webhook_secret: str
    admin_username: str
    admin_password: str
    recaptcha_site_key: str
    recaptcha_secret_key: str
    github_username: str
    database_path: str
    github_repos_url: str
    payment_min_amount: float
    payment_max_amount: float
    payment_currency: str

    @classmethod
    def from_env(cls):
        return cls(
            app_env=_env("APP_ENV", "production"),
            app_secret_key=_env("APP_SECRET_KEY", "change-me-in-production"),
            site_domain=_env("SITE_DOMAIN"),
            yookassa_shop_id=_env("YOOKASSA_SHOP_ID"),
            yookassa_secret_key=_env("YOOKASSA_SECRET_KEY"),
            yookassa_return_url=_env("YOOKASSA_RETURN_URL"),
            yookassa_webhook_url=_env("YOOKASSA_WEBHOOK_URL"),
            yookassa_webhook_secret=_env("YOOKASSA_WEBHOOK_SECRET"),
            admin_username=_env("ADMIN_USERNAME"),
            admin_password=_env("ADMIN_PASSWORD"),
            recaptcha_site_key=_env("RECAPTCHA_SITE_KEY"),
            recaptcha_secret_key=_env("RECAPTCHA_SECRET_KEY"),
            github_username=_env("GITHUB_USERNAME", "kzolotarev95"),
            database_path=_env("DATABASE_PATH", str(Path("instance") / "site.db")),
            github_repos_url=_env("GITHUB_REPOS_URL", "https://api.github.com/users/kzolotarev95/repos"),
            payment_min_amount=float(_env("PAYMENT_MIN_AMOUNT", "100")),
            payment_max_amount=float(_env("PAYMENT_MAX_AMOUNT", "1000000")),
            payment_currency=_env("PAYMENT_CURRENCY", "RUB"),
        )
