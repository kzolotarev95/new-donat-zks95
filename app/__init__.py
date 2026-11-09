"""ZKS95 Developer web application."""
from flask import Flask
from pathlib import Path

from .config import Settings
from .db import close_db, donor_color, init_app as init_db_app
from .routes import bp
from .admin import admin_bp
from .settings import get_runtime_setting
from .recipient import PAYMENT_RECIPIENT
from .visual import VISUAL_EFFECTS, VISUAL_SPEEDS, normalize_visual_effect, normalize_visual_speed


def create_app(test_config=None):
    project_root = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder=str(project_root / "templates"),
        static_folder=str(project_root / "static"),
    )
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
        ADMIN_USERNAME=settings.admin_username,
        ADMIN_PASSWORD=settings.admin_password,
        RECAPTCHA_SITE_KEY=settings.recaptcha_site_key,
        RECAPTCHA_SECRET_KEY=settings.recaptcha_secret_key,
        GITHUB_USERNAME=settings.github_username,
        DATABASE_PATH=settings.database_path,
        GITHUB_REPOS_URL=settings.github_repos_url,
        PAYMENT_MIN_AMOUNT=settings.payment_min_amount,
        PAYMENT_MAX_AMOUNT=settings.payment_max_amount,
        PAYMENT_CURRENCY=settings.payment_currency,
    )
    if test_config:
        app.config.update(test_config)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=app.config["APP_ENV"] == "production",
        PERMANENT_SESSION_LIFETIME=3600,
    )
    app.config.setdefault("JSON_SORT_KEYS", False)
    app.config.setdefault("TEMPLATES_AUTO_RELOAD", app.config["APP_ENV"] != "production")

    init_db_app(app)
    app.register_blueprint(bp)
    app.register_blueprint(admin_bp)
    app.teardown_appcontext(close_db)

    @app.context_processor
    def inject_brand():
        return {
            "brand": "ZKS95 Developer",
            "payment_recipient": PAYMENT_RECIPIENT,
            "site_domain": app.config.get("SITE_DOMAIN", ""),
            "year": 2026,
            "donor_color": donor_color,
            "visual_effect": normalize_visual_effect(get_runtime_setting("VISUAL_EFFECT", "none")),
            "visual_speed": normalize_visual_speed(get_runtime_setting("VISUAL_EFFECT_SPEED", "normal")),
            "visual_effects": VISUAL_EFFECTS,
            "visual_speeds": VISUAL_SPEEDS,
        }

    return app
