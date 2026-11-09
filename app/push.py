"""Web Push delivery for the authenticated administrator device(s)."""
import base64
import json
import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from .db import (
    count_push_subscriptions,
    delete_push_subscription,
    list_push_subscriptions,
    mark_push_subscription_error,
)
from .settings import get_runtime_setting

log = logging.getLogger(__name__)


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def generate_vapid_keys():
    """Generate the P-256 VAPID key pair expected by the browser Push API."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_number = private_key.private_numbers().private_value.to_bytes(32, "big")
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return _b64(private_number), _b64(public_key)


def public_key():
    return get_runtime_setting("VAPID_PUBLIC_KEY", "").strip()


def claims_email():
    value = get_runtime_setting("VAPID_CLAIMS_EMAIL", "").strip()
    if value and not value.startswith("mailto:"):
        value = f"mailto:{value}"
    return value


def push_enabled():
    return get_runtime_setting("PUSH_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def payment_notifications_enabled():
    return get_runtime_setting("PUSH_NOTIFY_PAYMENTS", "1").strip().lower() in {"1", "true", "yes", "on"}


def system_notifications_enabled():
    return get_runtime_setting("PUSH_NOTIFY_SYSTEM", "1").strip().lower() in {"1", "true", "yes", "on"}


def login_notifications_enabled():
    return get_runtime_setting("PUSH_NOTIFY_LOGINS", "1").strip().lower() in {"1", "true", "yes", "on"}


def push_settings():
    configured_email = get_runtime_setting("VAPID_CLAIMS_EMAIL", "").strip()
    if configured_email.startswith("mailto:"):
        configured_email = configured_email[7:]
    return {
        "enabled": push_enabled(),
        "notify_payments": payment_notifications_enabled(),
        "notify_system": system_notifications_enabled(),
        "notify_logins": login_notifications_enabled(),
        "public_key": public_key(),
        "claims_email": configured_email,
        "configured": bool(public_key() and get_runtime_setting("VAPID_PRIVATE_KEY", "").strip() and claims_email()),
        "subscriptions": count_push_subscriptions(),
    }


def send_push(payload, *, force=False):
    """Send a JSON notification to active subscriptions and prune expired endpoints."""
    if not force and not push_enabled():
        return {"sent": 0, "failed": 0, "skipped": "disabled"}
    private_key = get_runtime_setting("VAPID_PRIVATE_KEY", "").strip()
    vapid_public = public_key()
    subject = claims_email()
    if not private_key or not vapid_public or not subject:
        return {"sent": 0, "failed": 0, "skipped": "not_configured"}
    subscriptions = list_push_subscriptions()
    if not subscriptions:
        return {"sent": 0, "failed": 0, "skipped": "no_subscriptions"}
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        log.exception("pywebpush is not installed")
        return {"sent": 0, "failed": len(subscriptions), "skipped": "dependency_missing"}

    sent = failed = 0
    for subscription in subscriptions:
        info = {
            "endpoint": subscription["endpoint"],
            "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
        }
        try:
            webpush(
                subscription_info=info,
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=private_key,
                vapid_claims={"sub": subject},
                ttl=86400,
                timeout=10,
            )
            sent += 1
        except WebPushException as exc:
            failed += 1
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code in {404, 410}:
                delete_push_subscription(subscription["endpoint"])
            else:
                mark_push_subscription_error(subscription["endpoint"], str(exc))
            log.warning("Push delivery failed for subscription %s: %s", subscription["endpoint"][:80], exc)
        except Exception as exc:  # delivery must never break a payment webhook
            failed += 1
            mark_push_subscription_error(subscription["endpoint"], str(exc))
            log.exception("Unexpected push delivery failure")
    return {"sent": sent, "failed": failed, "skipped": ""}


def notify_payment_success(payment):
    if not payment_notifications_enabled():
        return {"sent": 0, "failed": 0, "skipped": "payment_notifications_disabled"}
    nickname = payment["customer_nickname"] or "Аноним"
    amount = payment["amount"]
    try:
        return send_push(
            {
                "title": "Оплата получена",
                "body": f"{nickname} поддержал проект на {amount} ₽.",
                "icon": "/static/favicon.svg",
                "badge": "/static/favicon.svg",
                "tag": "payment-success",
                "data": {"url": f"/admin/payments/{payment['payment_id']}"},
            }
        )
    except Exception:
        log.exception("Payment push notification failed")
        return {"sent": 0, "failed": 1, "skipped": "delivery_error"}


def notify_service_event(event):
    """Notify the administrator when the site service starts or fails.

    A VPS that is powered off cannot deliver a push itself. The startup event
    therefore confirms recovery after a reboot, while the failure event covers
    systemd failures when the operating system is still reachable.
    """
    if not system_notifications_enabled():
        return {"sent": 0, "failed": 0, "skipped": "system_notifications_disabled"}
    messages = {
        "startup": ("Сервер снова онлайн", "ZKS95 Developer запущен и готов принимать платежи."),
        "failure": ("Сервис сайта остановлен", "systemd обнаружил ошибку сервиса ZKS95 Developer."),
    }
    title, body = messages.get(event, ("Событие сервера", "На сервере произошло системное событие."))
    try:
        return send_push(
            {
                "title": title,
                "body": body,
                "icon": "/static/favicon.svg",
                "badge": "/static/favicon.svg",
                "tag": f"server-{event}",
                "data": {"url": "/admin/notifications"},
            }
        )
    except Exception:
        log.exception("Service event push notification failed")
        return {"sent": 0, "failed": 1, "skipped": "delivery_error"}
