"""Authenticated administration area for site and payment operations."""
import json
import logging
import math
import re
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import urlparse
from io import BytesIO
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for, current_app, jsonify, send_file
from werkzeug.security import check_password_hash, generate_password_hash
from .db import (DONOR_COLORS, count_payments, count_push_subscriptions, create_donor, delete_donor, delete_push_subscription, get_admin_user, get_admin_user_by_id, get_payment, get_db, list_donors, list_payments, mark_admin_login, payment_stats, record_donor_payment, update_admin_account, update_admin_password, update_donor, update_payment, upsert_push_subscription)
from .payments import fetch_payment
from .push import generate_vapid_keys, notify_payment_success, push_settings, send_push
from .security import admin_required, authenticated_admin, csrf_token, establish_admin_session, require_csrf
from . import two_factor
from . import admin_sessions
from .login_notifications import enqueue_login
from .admin_manual import COMMAND_GROUPS
from .settings import get_runtime_setting, set_runtime_setting
from .visual import VISUAL_EFFECTS, VISUAL_SPEEDS, normalize_visual_effect, normalize_visual_speed
from .captcha import site_key as recaptcha_site_key, verify as verify_recaptcha
from .backup import export_backup, import_backup
from .https_reserve import SETTING as HTTPS_RESERVE_SETTING, reserve_status

log = logging.getLogger(__name__)
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.app_context_processor
def admin_context():
    return {"csrf_token": csrf_token, "admin_username": session.get("admin_username")}


@admin_bp.before_request
def protect_admin_posts():
    # A known or short signing key would let an attacker forge both login stages.
    secret = str(current_app.config.get("SECRET_KEY") or "")
    if current_app.config["APP_ENV"] == "production" and len(secret) < 32:
        abort(503, description="Для входа в админку задайте надёжный APP_SECRET_KEY длиной от 32 символов в конфигурации сервера.")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        require_csrf()


@admin_bp.after_request
def protect_admin_responses(response):
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _safe_admin_next(target):
    target = str(target or "")
    try:
        parsed = urlparse(target)
    except ValueError:
        return url_for("admin.dashboard")
    if (parsed.scheme or parsed.netloc or "\\" in target or any(ord(char) < 32 for char in target)
            or not (parsed.path == "/admin" or parsed.path.startswith("/admin/"))):
        return url_for("admin.dashboard")
    return target


def _finish_login(user, target=None, second_factor=False, login_method="password"):
    record_id = establish_admin_session(user, second_factor, new_login=True, login_method=login_method)
    mark_admin_login(user["id"])
    try:
        enqueue_login(user, record_id)
    except Exception:
        log.exception("Could not queue the admin login notification")
    return redirect(_safe_admin_next(target))


def _pending_login_user():
    user = get_admin_user_by_id(session.get("two_factor_user_id", 0))
    issued = session.get("two_factor_issued", 0)
    now = time.time()
    if (not user or not user["totp_secret"] or not 0 <= now - issued < two_factor.CHALLENGE_SECONDS
            or session.get("two_factor_version") != user["auth_version"]):
        return None
    return user


@admin_bp.get("/login")
def login():
    if authenticated_admin():
        return redirect(url_for("admin.dashboard"))
    if _pending_login_user():
        return redirect(url_for("admin.two_factor_login"))
    return render_template("admin/login.html", recaptcha_site_key=recaptcha_site_key())


@admin_bp.post("/login")
def login_post():
    session.pop("two_factor_user_id", None)
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    bucket = f"password:{username.casefold()[:128]}"
    ip_bucket = f"password-ip:{admin_sessions.client_address()}"
    try:
        two_factor.take_attempt(bucket, limit=10)
        two_factor.take_attempt(ip_bucket, limit=60)
    except two_factor.AuthError as exc:
        return render_template("admin/login.html", username=username, login_error=str(exc), recaptcha_site_key=recaptcha_site_key()), exc.status
    if not verify_recaptcha(request.form.get("g-recaptcha-response")):
        return render_template("admin/login.html", username=username, captcha_error="Подтвердите, что вы не робот.", recaptcha_site_key=recaptcha_site_key()), 400
    user = get_admin_user(username)
    if not user or not check_password_hash(user["password_hash"], password):
        flash("Неверный логин или пароль.", "error")
        return render_template("admin/login.html", username=username, recaptcha_site_key=recaptcha_site_key()), 401
    two_factor.clear_rate(bucket)
    target = _safe_admin_next(request.args.get("next"))
    if user["totp_secret"]:
        session.clear()
        session["two_factor_user_id"] = user["id"]
        session["two_factor_version"] = user["auth_version"]
        session["two_factor_issued"] = int(time.time())
        session["two_factor_next"] = target
        return redirect(url_for("admin.two_factor_login"))
    return _finish_login(user, target)


@admin_bp.route("/login/2fa", methods=["GET", "POST"])
def two_factor_login():
    user = _pending_login_user()
    if not user:
        session.clear()
        flash("Время подтверждения истекло. Введите логин и пароль заново.", "error")
        return redirect(url_for("admin.login"))
    recovery = request.args.get("recovery") == "1"
    if request.method == "POST":
        try:
            method = two_factor.consume_factor(user, request.form.get("code"))
        except two_factor.AuthError as exc:
            return render_template("admin/two_factor_login.html", recovery=recovery, error=str(exc)), exc.status
        target = session.get("two_factor_next")
        return _finish_login(user, target, second_factor=True, login_method=method)
    return render_template("admin/two_factor_login.html", recovery=recovery)


@admin_bp.post("/logout")
def logout():
    record = admin_sessions.current_record()
    if record:
        admin_sessions.revoke(record["user_id"], record["id"], "logout")
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
                notify_payment_success(record)
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
            establish_admin_session(get_admin_user_by_id(user["id"]), session.get("admin_2fa", False))
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
            establish_admin_session(get_admin_user_by_id(user["id"]), session.get("admin_2fa", False))
            flash("Данные аккаунта сохранены.", "success")
            return redirect(url_for("admin.account"))
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    return render_template("admin/account.html", user=user, sessions=admin_sessions.history(user, page), push=push_settings(), command_groups=COMMAND_GROUPS)


@admin_bp.post("/account/sessions/revoke")
@admin_required
def revoke_sessions():
    if request.form.get("others") == "1":
        admin_sessions.revoke_others(session["admin_user_id"])
        flash("Другие сеансы завершены.", "success")
    else:
        try:
            record_id = int(request.form.get("session_id", ""))
        except ValueError:
            abort(400)
        record = admin_sessions.current_record()
        if record_id == record["id"]:
            return redirect(url_for("admin.account", _anchor="sessions"))
        if not admin_sessions.revoke(session["admin_user_id"], record_id):
            abort(404)
        flash("Сеанс завершён.", "success")
    return redirect(url_for("admin.account", _anchor="sessions"))


@admin_bp.route("/notifications", methods=["GET", "POST"])
@admin_required
def notifications():
    if request.method == "POST":
        set_runtime_setting("PUSH_ENABLED", "1" if request.form.get("PUSH_ENABLED") == "1" else "0")
        set_runtime_setting("PUSH_NOTIFY_PAYMENTS", "1" if request.form.get("PUSH_NOTIFY_PAYMENTS") == "1" else "0")
        set_runtime_setting("PUSH_NOTIFY_SYSTEM", "1" if request.form.get("PUSH_NOTIFY_SYSTEM") == "1" else "0")
        set_runtime_setting("PUSH_NOTIFY_LOGINS", "1" if request.form.get("PUSH_NOTIFY_LOGINS") == "1" else "0")
        email = (request.form.get("VAPID_CLAIMS_EMAIL") or "").strip()
        if email.startswith("mailto:"):
            email = email[7:]
        if email:
            set_runtime_setting("VAPID_CLAIMS_EMAIL", email)
        public = (request.form.get("VAPID_PUBLIC_KEY") or "").strip()
        if public:
            set_runtime_setting("VAPID_PUBLIC_KEY", public)
        private = (request.form.get("VAPID_PRIVATE_KEY") or "").strip()
        if private:
            set_runtime_setting("VAPID_PRIVATE_KEY", private)
        flash("Настройки push сохранены.", "success")
        return redirect(url_for("admin.notifications"))
    return render_template("admin/notifications.html", push=push_settings())


@admin_bp.post("/notifications/generate-vapid")
@admin_required
def generate_vapid():
    private_key, public = generate_vapid_keys()
    set_runtime_setting("VAPID_PRIVATE_KEY", private_key)
    set_runtime_setting("VAPID_PUBLIC_KEY", public)
    flash("VAPID-ключи созданы. Укажите email и сохраните настройки.", "success")
    return redirect(url_for("admin.notifications"))


@admin_bp.post("/notifications/test")
@admin_required
def test_notification():
    result = send_push(
        {
            "title": "Тестовое уведомление ZKS95",
            "body": "Push-уведомления работают на этом устройстве.",
            "icon": "/static/favicon.svg",
            "badge": "/static/favicon.svg",
            "tag": "push-test",
            "data": {"url": "/admin/notifications"},
        },
        force=True,
    )
    if result["sent"]:
        flash(f"Тестовый push отправлен: {result['sent']} устройств.", "success")
    elif result["skipped"] == "no_subscriptions":
        flash("Сначала подключите это устройство кнопкой «Включить на этом устройстве».", "error")
    elif result["skipped"] == "not_configured":
        flash("Сначала создайте VAPID-ключи, укажите email и сохраните настройки.", "error")
    elif result["skipped"] == "dependency_missing":
        flash("На сервере не установлен модуль pywebpush. Выполните обновление проекта.", "error")
    else:
        flash(f"Push не доставлен. Устройств: {result['failed']}.", "error")
    return redirect(url_for("admin.notifications"))


@admin_bp.post("/api/notifications/subscription")
@admin_required
def save_push_subscription():
    data = request.get_json(silent=True) or {}
    try:
        endpoint = upsert_push_subscription(data, request.headers.get("User-Agent", ""))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, endpoint=endpoint, subscriptions=count_push_subscriptions())


@admin_bp.delete("/api/notifications/subscription")
@admin_required
def remove_push_subscription():
    data = request.get_json(silent=True) or {}
    endpoint = str(data.get("endpoint") or "").strip()
    if endpoint:
        delete_push_subscription(endpoint)
    return jsonify(ok=True, subscriptions=count_push_subscriptions())


@admin_bp.get("/backup")
@admin_required
def backup_page():
    return render_template("admin/backup.html")


@admin_bp.get("/backup/download")
@admin_required
def backup_download():
    kind = request.args.get("kind", "donors").strip().lower()
    if kind not in {"donors", "full"}:
        return jsonify(error="Неизвестный тип бэкапа"), 400
    content = json.dumps(export_backup(kind), ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"new-donat-zks95-{kind}-backup.json"
    return send_file(BytesIO(content), mimetype="application/json", as_attachment=True, download_name=filename)


@admin_bp.post("/backup/import")
@admin_required
def backup_import():
    uploaded = request.files.get("backup_file")
    if not uploaded or not uploaded.filename:
        flash("Выберите JSON-файл бэкапа.", "error")
        return redirect(url_for("admin.backup_page"))
    raw = uploaded.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        flash("Файл бэкапа слишком большой (максимум 10 МБ).", "error")
        return redirect(url_for("admin.backup_page"))
    try:
        payload = json.loads(raw.decode("utf-8"))
        kind = payload.get("kind")
        if kind == "full" and request.form.get("confirm_full") != "1":
            raise ValueError("Для полного восстановления подтвердите замену текущих данных.")
        result = import_backup(payload, replace_donors=request.form.get("replace_donors") == "1")
        flash(f"Бэкап загружен: {result['message']}", "success")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        flash(f"Не удалось загрузить бэкап: {exc}", "error")
    return redirect(url_for("admin.backup_page"))


@admin_bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    sections = [
        ("Сайт", [
            ("SITE_DOMAIN", "Домен сайта", False, "text", ()),
        ]),
        ("YooKassa", [
            ("YOOKASSA_SHOP_ID", "Shop ID", False, "text", ()),
            ("YOOKASSA_SECRET_KEY", "Secret key", True, "text", ()),
            ("YOOKASSA_RETURN_URL", "URL возврата", False, "text", ()),
            ("YOOKASSA_WEBHOOK_URL", "Webhook URL", False, "text", ()),
            ("YOOKASSA_WEBHOOK_SECRET", "Webhook HMAC secret", True, "text", ()),
        ]),
        ("Google reCAPTCHA", [
            ("RECAPTCHA_SITE_KEY", "Site key", False, "text", ()),
            ("RECAPTCHA_SECRET_KEY", "Secret key", True, "text", ()),
        ]),
        ("Глобальный эффект", [
            ("VISUAL_EFFECT", "Эффект", False, "select", tuple(VISUAL_EFFECTS.items())),
            ("VISUAL_EFFECT_SPEED", "Скорость", False, "select", tuple(VISUAL_SPEEDS.items())),
        ]),
    ]
    if request.method == "POST":
        for _section, fields in sections:
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
    for _section, fields in sections:
        for key, _label, secret, _kind, _options in fields:
            value = get_runtime_setting(key, current_app.config.get(key, ""))
            values[key] = "" if secret else value
    user = get_admin_user_by_id(session["admin_user_id"])
    return render_template("admin/settings.html", sections=sections, values=values,
                           https_reserve=reserve_status(),
                           two_factor_enabled=bool(user["totp_secret"]),
                           setup_secret=two_factor.pending_setup(user),
                           recovery_count=two_factor.recovery_count(user))


@admin_bp.post("/settings/https-reserve")
@admin_required
def https_reserve_save():
    value = request.form.get("enabled", "0")
    if value not in {"0", "1"}:
        abort(400, description="Допускается только включение или отключение порта 8443.")
    set_runtime_setting(HTTPS_RESERVE_SETTING, value)
    log.info("Admin requested HTTPS reserve enabled=%s user_id=%s", value, session["admin_user_id"])
    flash("Настройка порта сохранена. Системная служба применит её в течение 15 секунд.", "success")
    return redirect(url_for("admin.settings", _anchor="https-reserve"))


@admin_bp.get("/api/https-reserve")
@admin_required
def https_reserve_status():
    return jsonify(reserve_status())


def _confirm_security_password(user):
    bucket = f"security:{user['id']}"
    two_factor.take_attempt(bucket)
    if not check_password_hash(user["password_hash"], request.form.get("current_password") or ""):
        raise two_factor.AuthError("Текущий пароль неверен.")
    two_factor.clear_rate(bucket)


@admin_bp.post("/2fa/setup")
@admin_required
def two_factor_setup():
    user = get_admin_user_by_id(session["admin_user_id"])
    try:
        _confirm_security_password(user)
        two_factor.start_setup(user)
    except two_factor.AuthError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.settings", _anchor="two-factor"))


@admin_bp.get("/2fa/qr.svg")
@admin_required
def two_factor_qr():
    import pyotp
    import qrcode
    import qrcode.image.svg
    user = get_admin_user_by_id(session["admin_user_id"])
    secret = two_factor.pending_setup(user)
    if not secret:
        abort(404)
    uri = pyotp.TOTP(secret).provisioning_uri(name=user["username"], issuer_name="ZKS95 Developer")
    image = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathFillImage, border=4)
    output = BytesIO()
    image.save(output)
    return send_file(BytesIO(output.getvalue()), mimetype="image/svg+xml", max_age=0)


@admin_bp.post("/2fa/enable")
@admin_required
def two_factor_enable():
    user = get_admin_user_by_id(session["admin_user_id"])
    try:
        codes = two_factor.enable(user, request.form.get("code"))
    except two_factor.AuthError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.settings", _anchor="two-factor"))
    establish_admin_session(get_admin_user_by_id(user["id"]), second_factor=True)
    log.info("Admin 2FA enabled user_id=%s", user["id"])
    return render_template("admin/recovery_codes.html", recovery_codes=codes)


@admin_bp.post("/2fa/cancel")
@admin_required
def two_factor_cancel():
    two_factor.cancel_setup(get_admin_user_by_id(session["admin_user_id"]))
    return redirect(url_for("admin.settings", _anchor="two-factor"))


@admin_bp.post("/2fa/recovery-codes")
@admin_required
def two_factor_recovery_codes():
    user = get_admin_user_by_id(session["admin_user_id"])
    try:
        _confirm_security_password(user)
        two_factor.consume_factor(user, request.form.get("code"))
        codes = two_factor.rotate_recovery_codes(user)
    except two_factor.AuthError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.settings", _anchor="two-factor"))
    establish_admin_session(get_admin_user_by_id(user["id"]), second_factor=True)
    log.info("Admin recovery codes rotated user_id=%s", user["id"])
    return render_template("admin/recovery_codes.html", recovery_codes=codes)


@admin_bp.post("/2fa/disable")
@admin_required
def two_factor_disable():
    user = get_admin_user_by_id(session["admin_user_id"])
    try:
        _confirm_security_password(user)
        two_factor.consume_factor(user, request.form.get("code"))
        two_factor.disable(user)
    except two_factor.AuthError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.settings", _anchor="two-factor"))
    establish_admin_session(get_admin_user_by_id(user["id"]))
    flash("2FA отключена. Резервные коды аннулированы.", "success")
    log.info("Admin 2FA disabled user_id=%s", user["id"])
    return redirect(url_for("admin.settings", _anchor="two-factor"))


@admin_bp.get("/api/summary")
@admin_required
def api_summary():
    return jsonify(payment_stats())
