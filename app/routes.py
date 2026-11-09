"""HTTP routes for the public site and payment webhooks."""
import hashlib
import hmac
from html import escape
import json
import logging
import secrets
from urllib.parse import urljoin
from flask import Blueprint, Response, current_app, jsonify, redirect, render_template, request, url_for
from .db import create_payment_record, donor_color, get_donor, get_payment, list_donors, record_donor_payment, update_payment
from .github import fetch_repositories
from .payments import create_payment, fetch_payment, normalize_amount
from .push import notify_payment_success
from .settings import get_runtime_setting

bp = Blueprint("site", __name__)
log = logging.getLogger(__name__)


def _absolute_url(path):
    configured = get_runtime_setting("SITE_DOMAIN", current_app.config.get("SITE_DOMAIN", "")).strip()
    if configured:
        base = configured if configured.startswith("http") else f"https://{configured}"
        return urljoin(base.rstrip("/") + "/", path.lstrip("/"))
    return urljoin(request.url_root, path.lstrip("/")) if path.startswith("/") else path


@bp.get("/")
def index():
    return redirect(url_for("site.donate"))


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


@bp.get("/api/repos")
def repos_api():
    try:
        repos = fetch_repositories(current_app.config["GITHUB_USERNAME"], current_app.config["GITHUB_REPOS_URL"])
        return jsonify(repositories=repos)
    except Exception:
        log.exception("GitHub API unavailable")
        return jsonify(error="Репозитории временно недоступны", repositories=[]), 503


@bp.get("/services")
def services():
    return render_template("services.html")


@bp.get("/requisites")
def requisites():
    return render_template("requisites.html")


@bp.get("/support")
def support():
    return render_template("support.html")


@bp.route("/donate", methods=["GET"])
def donate():
    return render_template("donate.html", payment_error=None, donors=list_donors())


@bp.get("/api/donors/<int:donor_id>/avatar.svg")
def donor_avatar(donor_id):
    donor = get_donor(donor_id)
    if donor is None:
        return Response(status=404)
    digest = hashlib.sha256(donor["nickname"].encode("utf-8")).hexdigest()
    accent = donor_color(donor["color"])
    x = 20 + int(digest[0:2], 16) % 60
    y = 20 + int(digest[2:4], 16) % 60
    initial = escape(donor["nickname"][:1].upper())
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
<rect width="100" height="100" rx="24" fill="#0d1015"/>
<circle cx="{x}" cy="{y}" r="34" fill="{accent}" opacity=".9"/>
<path d="M0 78 Q 34 {30 + int(digest[4:6], 16) % 45} 100 18 V100 H0Z" fill="#ffffff" opacity=".12"/>
<circle cx="{78 - int(digest[6:8], 16) % 28}" cy="{18 + int(digest[8:10], 16) % 45}" r="7" fill="#fff" opacity=".48"/>
<text x="50" y="63" text-anchor="middle" fill="#fff" font-family="Arial,sans-serif" font-size="38" font-weight="700">{initial}</text>
</svg>'''
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@bp.post("/api/payments")
def payments_api():
    data = request.get_json(silent=True) or request.form
    try:
        amount = normalize_amount(data.get("amount"))
        nickname = (data.get("nickname") or "Аноним").strip()[:64]
        message = (data.get("message") or "").strip()[:500]
        if len(nickname) < 2:
            raise ValueError("Укажите никнейм")
        if any(ord(char) < 32 for char in nickname) or any(char in "<>/\\" for char in nickname):
            raise ValueError("Никнейм содержит недопустимые символы")
        shop_id = get_runtime_setting("YOOKASSA_SHOP_ID", current_app.config["YOOKASSA_SHOP_ID"])
        secret_key = get_runtime_setting("YOOKASSA_SECRET_KEY", current_app.config["YOOKASSA_SECRET_KEY"])
        if not shop_id or not secret_key:
            return jsonify(error="Платёжная система ещё не настроена администратором"), 503
        description = "Добровольная поддержка авторских проектов ZKS95 Developer"
        return_url = get_runtime_setting("YOOKASSA_RETURN_URL", current_app.config["YOOKASSA_RETURN_URL"])
        result, idempotency_key = create_payment(
            amount, description, return_url or _absolute_url("/payment/success"),
            {"nickname": nickname, "message": message},
        )
        payment_id = result.id
        create_payment_record({
            "payment_id": payment_id, "idempotency_key": idempotency_key,
            "status": result.status, "amount": str(result.amount.value),
            "currency": result.amount.currency, "description": description,
            "customer_nickname": nickname, "message": message, "payload": result.json(),
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
    configured_secret = get_runtime_setting("YOOKASSA_WEBHOOK_SECRET", current_app.config.get("YOOKASSA_WEBHOOK_SECRET", ""))
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
    confirmed_amount = getattr(confirmed, "amount", None)
    if confirmed_amount is None or str(confirmed_amount.value) != str(record["amount"]) or confirmed_amount.currency != record["currency"]:
        log.error("Payment amount mismatch for %s", payment_id)
        return jsonify(error="payment verification failed"), 400
    status = confirmed.status
    paid = status == "succeeded"
    was_paid = record["status"] == "succeeded"
    update_payment(payment_id, status, json.loads(confirmed.json()), paid=paid)
    if paid and not was_paid:
        record_donor_payment(record["customer_nickname"], record["amount"])
        notify_payment_success(record)
    return jsonify(ok=True)


@bp.get("/healthz")
def healthz():
    from .db import get_db
    get_db().execute("SELECT 1").fetchone()
    return jsonify(status="ok", service="new-donat-zks95")


@bp.get("/service-worker.js")
def service_worker():
    response = current_app.send_static_file("sw.js")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@bp.get("/contact")
def contact():
    return render_template("contact.html")


@bp.get("/faq")
def faq():
    return render_template("faq.html")


@bp.errorhandler(404)
def not_found(_error):
    return render_template("404.html"), 404
