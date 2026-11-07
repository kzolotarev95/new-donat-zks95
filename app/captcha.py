"""Google reCAPTCHA v2 verification shared by public and admin forms."""
import logging
import requests
from flask import current_app, request
from .settings import get_runtime_setting

log = logging.getLogger(__name__)

def site_key():
    return get_runtime_setting("RECAPTCHA_SITE_KEY", current_app.config.get("RECAPTCHA_SITE_KEY", ""))

def verify(token):
    secret = get_runtime_setting("RECAPTCHA_SECRET_KEY", current_app.config.get("RECAPTCHA_SECRET_KEY", ""))
    if not secret:
        return True
    if not token:
        return False
    try:
        response = requests.post("https://www.google.com/recaptcha/api/siteverify", data={"secret": secret, "response": token, "remoteip": request.remote_addr}, timeout=5)
        return bool(response.json().get("success"))
    except requests.RequestException:
        log.exception("reCAPTCHA verification failed")
        return False
