"""HTTP routes for the public site and payment webhooks."""
import hashlib
import hmac
import json
import logging
import secrets
from urllib.parse import urljoin
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for
from .db import create_payment_record, get_payment, update_payment
from .github import fetch_repositories
from .payments import create_payment, fetch_payment, normalize_amount

bp = Blueprint("site", __name__)
log = logging.getLogger(__name__)


def _absolute_url(path):
    configured = current_app.config.get("SITE_DOMAIN", "").strip()
    if configured:
        base = configured if configured.startswith("http") else f"https://{configured}"
        return urljoin(base.rstrip("/") + "/", path.lstrip("/"))
    return urljoin(request.url_root, path.lstrip("/")) if path.startswith("/") else path


@bp.get("/")
def index():
    try:
        repos = fetch_repositories(current_app.config["GITHUB_USERNAME"], current_app.config["GITHUB_REPOS_URL"])
        github_error = None
    except Exception as exc:
        log.warning("GitHub API unavailable: %s", exc)
        repos, github_error = [], "Репозитории временно недоступны"
    return render_template("index.html", repos=repos, github_error=github_error)


@bp.get("/about")
def about():
    return render_template("about.html")


@bp.get("/projects")
def projects():
    try:
        repos = fetch_repositories(current_app.config["GITHUB_USERNAME"], current_app.config["GITHUB_REPOS_URL"])
        github_error = None
    except Exception:
        repos, github_error = [], "Репозитории временно недоступны"
    return render_template("projects.html", repos=repos, github_error=github_error)


@bp.get("/services")
def services():
    return render_template("services.html")


@bp.get("/support")
def support():
    return render_template("support.html")


@bp.route("/donate", methods=["GET"])
def donate():
    return render_template("donate.html", payment_error=None)


@bp.post("/api/payments")
def payments_api():
    data = request.get_json(silent=True) or request.form
    try:
        amount = normalize_amount(data.get("amount"))
        email = (data.get("email") or "").strip()[:254]
        message = (data.get("message") or "").strip()[:500]
        if email and ("@" not in email or " " in email):
            raise ValueError("Проверьте email")
        if not current_app.config["YOOKASSA_SHOP_ID"] or not current_app.config["YOOKASSA_SECRET_KEY"]:
            return jsonify(error="Платёжная система ещё не настроена администратором"), 503
        description = "Поддержка ZKS95 Developer"
        result, idempotency_key = create_payment(
            amount, description, current_app.config["YOOKASSA_RETURN_URL"] or _absolute_url("/payment/success"),
            {"email": email, "message": message},
        )
        payment_id = result.id
        create_payment_record({
            "payment_id": payment_id, "idempotency_key": idempotency_key,
            "status": result.status, "amount": str(result.amount.value),
            "currency": result.amount.currency, "description": description,
            "customer_email": email, "message": message, "payload": result.json(),
        })
        return jsonify(payment_id=payment_id, confirmation_url=result.confirmation.confirmation_url)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        log.exception("Could not create YooKassa payment")
        return jsonify(error="Не удалось создать платёж. Попробуйте ещё раз."), 502


@bp.get("/payment/success")
def payment_success():
    payment_id = request.args.get("payment_id")
    payment = get_payment(payment_id) if payment_id else None
    return render_template("payment_success.html", payment=payment)


def _verify_webhook_payload(payload):
    """Verify the payment by querying YooKassa; the API result is authoritative.

    YooKassa webhooks are intentionally not trusted by body alone. Optional
    HMAC verification can be enabled with YOOKASSA_WEBHOOK_SECRET when a proxy
    signs incoming requests.
    """
    configured_secret = current_app.config.get("YOOKASSA_WEBHOOK_SECRET")
    if configured_secret:
        signature = request.headers.get("X-Yookassa-Signature", "")
        expected = hmac.new(configured_secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
    return bool(payload.get("object", {}).get("id"))


@bp.post("/webhooks/yookassa")
def yookassa_webhook():
    payload = request.get_json(silent=True) or {}
    if not _verify_webhook_payload(payload):
        return jsonify(error="invalid notification"), 400
    payment_id = payload["object"]["id"]
    try:
        confirmed = fetch_payment(payment_id)
    except Exception:
        log.exception("YooKassa verification failed for %s", payment_id)
        return jsonify(error="temporary verification failure"), 503
    record = get_payment(payment_id)
    if record is None:
        log.warning("Ignoring webhook for unknown payment %s", payment_id)
        return jsonify(ok=True)
    status = confirmed.status
    paid = status == "succeeded"
    update_payment(payment_id, status, json.loads(confirmed.json()), paid=paid)
    return jsonify(ok=True)


@bp.get("/healthz")
def healthz():
    from .db import get_db
    get_db().execute("SELECT 1").fetchone()
    return jsonify(status="ok", service="new-donat-zks95")


@bp.get("/contact")
def contact():
    return render_template("contact.html")


@bp.get("/faq")
def faq():
    return render_template("faq.html")


@bp.errorhandler(404)
def not_found(_error):
    return render_template("404.html"), 404
