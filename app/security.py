"""Small security helpers for admin sessions and encrypted settings."""
import base64
import hashlib
import secrets
from functools import wraps
from flask import abort, current_app, redirect, request, session, url_for
from cryptography.fernet import Fernet, InvalidToken


def _fernet():
    digest = hashlib.sha256(current_app.config["SECRET_KEY"].encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_value(value):
    return _fernet().encrypt(value.encode()).decode()


def decrypt_value(value):
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def require_csrf():
    supplied = request.form.get("csrf_token", "")
    if not supplied or not secrets.compare_digest(supplied, session.get("_csrf_token", "")):
        abort(400, description="Недействительный CSRF-токен")


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_user_id"):
            return redirect(url_for("admin.login", next=request.path))
        if session.get("must_change_password") and request.endpoint not in {"admin.password", "admin.logout"}:
            return redirect(url_for("admin.password", force=1))
        return view(*args, **kwargs)
    return wrapped
