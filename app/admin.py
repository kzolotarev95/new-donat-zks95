"""Authenticated administration area for site and payment operations."""
import json
import logging
import math
import re
import sqlite3
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import urlparse
from flask import Blueprint, flash, redirect, render_template, request, session, url_for, current_app, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
from .db import (DONOR_COLORS, count_payments, create_donor, delete_donor, get_admin_user, get_admin_user_by_id, get_payment, get_db, list_donors, list_payments, mark_admin_login, payment_stats, record_donor_payment, update_admin_account, update_admin_password, update_donor, update_payment)
from .payments import fetch_payment
from .security import admin_required, csrf_token, require_csrf
from .settings import get_runtime_setting, set_runtime_setting
from .visual import VISUAL_EFFECTS, VISUAL_SPEEDS, normalize_visual_effect, normalize_visual_speed
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
        return render_template("admin/login.html", username=username, captcha_error="Подтвердите, что вы не робот.", recaptcha_site_key=recaptcha_site_key()), 400
    user = get_admin_user(username)
    if not user or not check_password_hash(user["password_hash"], password):
        flash("Неверный логин или пароль.", "error")
        return render_template("admin/login.html", username=username, recaptcha_site_key=recaptcha_site_key()), 401
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


def _donor_form_values():
    nickname = (request.form.get("nickname") or "").strip()[:64]
    color = (request.form.get("color") or "red").strip()
    note = (request.form.get("note") or "").strip()[:120]
    social_url = (request.form.get("social_url") or "").strip()[:500]
    pinned = request.form.get("pinned") == "1"
    raw_amount = (request.form.get("total_amount") or "0").strip().replace(",", ".")
    try:
        total_amount = Decimal(raw_amount).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        total_amount = None
    return nickname, color, note, total_amount, social_url, pinned


@admin_bp.get("/donors")
@admin_required
def donors():
    return render_template("admin/donors.html", donors=list_donors(active_only=False), donor_colors=DONOR_COLORS)


@admin_bp.post("/donors")
@admin_required
def create_donor_admin():
    nickname, color, note, total_amount, social_url, pinned = _donor_form_values()
    if len(nickname) < 2 or any(char in "<>/\\" for char in nickname):
        flash("Укажите корректный никнейм от 2 символов.", "error")
    elif total_amount is None or total_amount < 0:
        flash("Сумма должна быть числом от 0 ₽.", "error")
    elif social_url and urlparse(social_url).scheme not in {"http", "https"}:
        flash("Ссылка должна начинаться с http:// или https://.", "error")
    else:
        try:
            create_donor(nickname, color, note, total_amount, social_url, pinned)
            flash("Донатер добавлен.", "success")
        except sqlite3.IntegrityError:
            flash("Такой никнейм уже есть в списке.", "error")
    return redirect(url_for("admin.donors"))


@admin_bp.post("/donors/<int:donor_id>")
@admin_required
def edit_donor(donor_id):
    nickname, color, note, total_amount, social_url, pinned = _donor_form_values()
    if len(nickname) < 2 or any(char in "<>/\\" for char in nickname):
        flash("Укажите корректный никнейм от 2 символов.", "error")
    elif total_amount is None or total_amount < 0:
        flash("Сумма должна быть числом от 0 ₽.", "error")
    elif social_url and urlparse(social_url).scheme not in {"http", "https"}:
        flash("Ссылка должна начинаться с http:// или https://.", "error")
    else:
        try:
            update_donor(donor_id, nickname, color, note, total_amount, social_url, pinned, request.form.get("is_active") == "1")
            flash("Данные донатера обновлены.", "success")
        except sqlite3.IntegrityError:
            flash("Такой никнейм уже есть в списке.", "error")
    return redirect(url_for("admin.donors"))


@admin_bp.post("/donors/<int:donor_id>/delete")
@admin_required
def remove_donor(donor_id):
    delete_donor(donor_id)
    flash("Донатер удалён.", "success")
    return redirect(url_for("admin.donors"))


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
            was_paid = record["status"] == "succeeded"
            update_payment(payment_id, confirmed.status, json.loads(confirmed.json()), confirmed.status == "succeeded")
            if confirmed.status == "succeeded" and not was_paid:
                record_donor_payment(record["customer_nickname"], record["amount"])
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
        elif len(new) < 6:
            flash("Новый пароль должен содержать минимум 6 символов.", "error")
        elif new != repeat:
            flash("Пароли не совпадают.", "error")
        else:
            update_admin_password(user["id"], generate_password_hash(new), 0)
            flash("Пароль изменён.", "success")
            session["must_change_password"] = False
            return redirect(url_for("admin.dashboard"))
    return render_template("admin/password.html", force=False)


@admin_bp.route("/account", methods=["GET", "POST"])
@admin_required
def account():
    user = get_admin_user_by_id(session["admin_user_id"])
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        username = (request.form.get("username") or "").strip()
        new_password = request.form.get("new_password") or ""
        repeat = request.form.get("repeat_password") or ""
        if not user or not check_password_hash(user["password_hash"], current):
            flash("Текущий пароль неверен.", "error")
        elif not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
            flash("Логин: 3–64 символа, только латиница, цифры, точка, дефис или _.", "error")
        elif new_password and (len(new_password) < 6 or new_password != repeat):
            flash("Новый пароль должен быть не короче 6 символов и совпадать в обоих полях.", "error")
        elif username != user["username"] and get_admin_user(username):
            flash("Такой логин уже занят.", "error")
        else:
            update_admin_account(user["id"], username, generate_password_hash(new_password) if new_password else None)
            session["admin_username"] = username
            session["must_change_password"] = False
            flash("Данные аккаунта сохранены.", "success")
            return redirect(url_for("admin.account"))
    return render_template("admin/account.html", user=user)


@admin_bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    fields = [
        ("SITE_DOMAIN", "Домен сайта", False, "text", ()),
        ("YOOKASSA_SHOP_ID", "YooKassa Shop ID", False, "text", ()),
        ("YOOKASSA_SECRET_KEY", "YooKassa secret key", True, "text", ()),
        ("YOOKASSA_RETURN_URL", "URL возврата YooKassa", False, "text", ()),
        ("YOOKASSA_WEBHOOK_URL", "Webhook URL YooKassa", False, "text", ()),
        ("YOOKASSA_WEBHOOK_SECRET", "Webhook HMAC secret", True, "text", ()),
        ("RECAPTCHA_SITE_KEY", "Google reCAPTCHA site key", False, "text", ()),
        ("RECAPTCHA_SECRET_KEY", "Google reCAPTCHA secret key", True, "text", ()),
        ("VISUAL_EFFECT", "Глобальный эффект", False, "select", tuple(VISUAL_EFFECTS.items())),
        ("VISUAL_EFFECT_SPEED", "Скорость эффекта", False, "select", tuple(VISUAL_SPEEDS.items())),
    ]
    if request.method == "POST":
        for key, _label, secret, _kind, _options in fields:
            value = request.form.get(key, "").strip()
            if secret and not value:
                continue
            if key == "VISUAL_EFFECT":
                value = normalize_visual_effect(value)
            elif key == "VISUAL_EFFECT_SPEED":
                value = normalize_visual_speed(value)
            set_runtime_setting(key, value)
        flash("Настройки сохранены. Новые платежи используют их сразу.", "success")
        return redirect(url_for("admin.settings"))
    values = {}
    for key, _label, secret, _kind, _options in fields:
        value = get_runtime_setting(key, current_app.config.get(key, ""))
        values[key] = "" if secret else value
    return render_template("admin/settings.html", fields=fields, values=values)


@admin_bp.get("/api/summary")
@admin_required
def api_summary():
    return jsonify(payment_stats())
