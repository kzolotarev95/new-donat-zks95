"""YooKassa integration isolated from HTTP routes."""
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from .settings import get_runtime_setting
from .payment_limits import get_payment_limits


def _sdk():
    from yookassa import Configuration, Payment
    Configuration.account_id = __import__("flask").current_app.config["YOOKASSA_SHOP_ID"]
    Configuration.account_id = get_runtime_setting("YOOKASSA_SHOP_ID", Configuration.account_id)
    Configuration.secret_key = get_runtime_setting("YOOKASSA_SECRET_KEY", "")
    return Payment


def normalize_amount(raw):
    try:
        text = str(raw).replace(",", ".")
        if len(text) > 64:
            raise ValueError
        amount = Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if not amount.is_finite():
            raise ValueError
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Введите сумму в рублях")
    minimum, maximum = get_payment_limits()
    if amount < minimum:
        raise ValueError(f"Минимальная сумма — {minimum:,} ₽".replace(",", " "))
    if amount > maximum:
        raise ValueError(f"Максимальная сумма — {maximum:,} ₽".replace(",", " "))
    return amount


def create_payment(amount, description, return_url, metadata=None):
    from flask import current_app
    payment_idempotency = str(uuid.uuid4())
    Payment = _sdk()
    payload = {
        "amount": {"value": f"{amount:.2f}", "currency": current_app.config["PAYMENT_CURRENCY"]},
        "capture": True,
        # Ask YooKassa to open the SBP flow directly instead of its generic
        # payment-method chooser. The API accepts this as payment_method_data.
        "payment_method_data": {"type": "sbp"},
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": description[:128],
        "metadata": metadata or {},
    }
    result = Payment.create(payload, payment_idempotency)
    return result, payment_idempotency


def fetch_payment(payment_id):
    return _sdk().find_one(payment_id)
