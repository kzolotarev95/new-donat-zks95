"""ZKS95 Developer web application."""
from flask import Flask

from .config import Settings
from .db import close_db, init_app as init_db_app
from .routes import bp


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    settings = Settings.from_env()
    app.config.from_mapping(
        SECRET_KEY=settings.app_secret_key,
        APP_ENV=settings.app_env,
        SITE_DOMAIN=settings.site_domain,
        YOOKASSA_SHOP_ID=settings.yookassa_shop_id,
        YOOKASSA_SECRET_KEY=settings.yookassa_secret_key,
        YOOKASSA_RETURN_URL=settings.yookassa_return_url,
        YOOKASSA_WEBHOOK_URL=settings.yookassa_webhook_url,
        YOOKASSA_WEBHOOK_SECRET=settings.yookassa_webhook_secret,
        GITHUB_USERNAME=settings.github_username,
        DATABASE_PATH=settings.database_path,
        GITHUB_REPOS_URL=settings.github_repos_url,
        PAYMENT_MIN_AMOUNT=settings.payment_min_amount,
        PAYMENT_MAX_AMOUNT=settings.payment_max_amount,
        PAYMENT_CURRENCY=settings.payment_currency,
    )
    if test_config:
        app.config.update(test_config)
    app.config.setdefault("JSON_SORT_KEYS", False)
    app.config.setdefault("TEMPLATES_AUTO_RELOAD", app.config["APP_ENV"] != "production")

    init_db_app(app)
    app.register_blueprint(bp)
    app.teardown_appcontext(close_db)

    @app.context_processor
    def inject_brand():
        return {
            "brand": "ZKS95 Developer",
            "site_domain": app.config.get("SITE_DOMAIN", ""),
            "year": 2026,
        }

    return app
