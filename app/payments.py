"""YooKassa integration isolated from HTTP routes."""
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def _sdk():
    from yookassa import Configuration, Payment
    Configuration.account_id = __import__("flask").current_app.config["YOOKASSA_SHOP_ID"]
    Configuration.secret_key = __import__("flask").current_app.config["YOOKASSA_SECRET_KEY"]
    return Payment


def normalize_amount(raw):
    try:
        amount = Decimal(str(raw).replace(",", ".")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Введите сумму в рублях")
    from flask import current_app
    if amount < Decimal(str(current_app.config["PAYMENT_MIN_AMOUNT"])):
        raise ValueError(f"Минимальная сумма — {current_app.config['PAYMENT_MIN_AMOUNT']:.0f} ₽")
    if amount > Decimal(str(current_app.config["PAYMENT_MAX_AMOUNT"])):
        raise ValueError("Сумма превышает допустимый лимит")
    return amount


def create_payment(amount, description, return_url, metadata=None):
    from flask import current_app
    payment_idempotency = str(uuid.uuid4())
    Payment = _sdk()
    payload = {
        "amount": {"value": f"{amount:.2f}", "currency": current_app.config["PAYMENT_CURRENCY"]},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": description[:128],
        "metadata": metadata or {},
    }
    result = Payment.create(payload, payment_idempotency)
    return result, payment_idempotency


def fetch_payment(payment_id):
    return _sdk().find_one(payment_id)
