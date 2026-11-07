"""Authenticated administration area for site and payment operations."""
import json
import logging
import math
from functools import wraps
from flask import Blueprint, flash, redirect, render_template, request, session, url_for, current_app, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
from .db import (count_payments, get_admin_user, get_admin_user_by_id, get_payment, get_db, list_payments, mark_admin_login, payment_stats, update_admin_password, update_payment)
from .payments import fetch_payment
from .security import admin_required, csrf_token, require_csrf
from .settings import get_runtime_setting, set_runtime_setting
from .captcha import site_key as recaptcha_site_key, verify as verify_recaptcha

log = logging.getLogger(__name__)
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.app_context_processor
def admin_context():
    return {"csrf_token": csrf_token, "admin_username": session.get("admin_username")}


@admin_bp.before_request
def protect_admin_posts():
    if request.method == "POST":
        require_csrf()


@admin_bp.get("/login")
def login():
    if session.get("admin_user_id"):
        return redirect(url_for("admin.dashboard"))
    return render_template("admin/login.html", recaptcha_site_key=recaptcha_site_key())


@admin_bp.post("/login")
def login_post():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if not verify_recaptcha(request.form.get("g-recaptcha-response")):
        flash("Подтвердите, что вы не робот.", "error")
        return render_template("admin/login.html", username=username, recaptcha_site_key=recaptcha_site_key()), 400
    user = get_admin_user(username)
    if not user or not check_password_hash(user["password_hash"], password):
        flash("Неверный логин или пароль.", "error")
        return render_template("admin/login.html", username=username), 401
    session.clear()
    session.permanent = True
    session["admin_user_id"] = user["id"]
    session["admin_username"] = user["username"]
    session["must_change_password"] = bool(user["must_change_password"])
    session["_csrf_token"] = csrf_token()
    mark_admin_login(user["id"])
    return redirect(request.args.get("next") or url_for("admin.dashboard"))


@admin_bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin.login"))


@admin_bp.get("/")
@admin_required
def dashboard():
    stats = payment_stats()
    recent = list_payments(limit=8)
    return render_template("admin/dashboard.html", stats=stats, recent=recent)


@admin_bp.get("/payments")
@admin_required
def payments():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    status = (request.args.get("status") or "").strip()
    query = (request.args.get("q") or "").strip()[:120]
    per_page = 30
    total = count_payments(status, query)
    rows = list_payments(per_page, (page - 1) * per_page, status, query)
    return render_template("admin/payments.html", payments=rows, page=page, pages=max(1, math.ceil(total / per_page)), total=total, status=status, query=query)


@admin_bp.post("/payments/<payment_id>/refresh")
@admin_required
def refresh_payment(payment_id):
    record = get_payment(payment_id)
    if not record:
        flash("Платёж не найден.", "error")
        return redirect(url_for("admin.payments"))
    try:
        confirmed = fetch_payment(payment_id)
        if str(confirmed.amount.value) != str(record["amount"]) or confirmed.amount.currency != record["currency"]:
            flash("Проверка отклонена: сумма или валюта не совпадает.", "error")
        else:
            update_payment(payment_id, confirmed.status, json.loads(confirmed.json()), confirmed.status == "succeeded")
            flash(f"Статус обновлён: {confirmed.status}.", "success")
    except Exception:
        log.exception("Admin payment refresh failed")
        flash("Не удалось получить статус из YooKassa.", "error")
    return redirect(url_for("admin.payment_detail", payment_id=payment_id))


@admin_bp.get("/payments/<payment_id>")
@admin_required
def payment_detail(payment_id):
    payment = get_payment(payment_id)
    if not payment:
        flash("Платёж не найден.", "error")
        return redirect(url_for("admin.payments"))
    try:
        payload = json.loads(payment["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    return render_template("admin/payment_detail.html", payment=payment, payload=payload)


@admin_bp.route("/password", methods=["GET", "POST"])
@admin_required
def password():
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        repeat = request.form.get("repeat_password") or ""
        user = get_admin_user_by_id(session["admin_user_id"])
        if not user or not check_password_hash(user["password_hash"], current):
            flash("Текущий пароль неверен.", "error")
        elif len(new) < 12:
            flash("Новый пароль должен содержать минимум 12 символов.", "error")
        elif new != repeat:
            flash("Пароли не совпадают.", "error")
        else:
            update_admin_password(user["id"], generate_password_hash(new), 0)
            flash("Пароль изменён.", "success")
            session["must_change_password"] = False
            return redirect(url_for("admin.dashboard"))
    return render_template("admin/password.html", force=request.args.get("force") == "1")


@admin_bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    fields = [
        ("SITE_DOMAIN", "Домен сайта", False),
        ("YOOKASSA_SHOP_ID", "YooKassa Shop ID", False),
        ("YOOKASSA_SECRET_KEY", "YooKassa secret key", True),
        ("YOOKASSA_RETURN_URL", "URL возврата YooKassa", False),
        ("YOOKASSA_WEBHOOK_URL", "Webhook URL YooKassa", False),
        ("YOOKASSA_WEBHOOK_SECRET", "Webhook HMAC secret", True),
        ("RECAPTCHA_SITE_KEY", "Google reCAPTCHA site key", False),
        ("RECAPTCHA_SECRET_KEY", "Google reCAPTCHA secret key", True),
    ]
    if request.method == "POST":
        for key, _label, secret in fields:
            value = request.form.get(key, "").strip()
            if secret and not value:
                continue
            set_runtime_setting(key, value)
        flash("Настройки сохранены. Новые платежи используют их сразу.", "success")
        return redirect(url_for("admin.settings"))
    values = {}
    for key, _label, secret in fields:
        value = get_runtime_setting(key, current_app.config.get(key, ""))
        values[key] = "" if secret else value
    return render_template("admin/settings.html", fields=fields, values=values)


@admin_bp.get("/api/summary")
@admin_required
def api_summary():
    return jsonify(payment_stats())
