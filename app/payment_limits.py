"""One authoritative, atomically stored range for donation amounts."""
from decimal import Decimal, InvalidOperation

from flask import current_app
from .db import get_db
from .settings import set_runtime_settings

LOWEST_AMOUNT = 1
HIGHEST_AMOUNT = 1_000_000
DEFAULT_LIMITS = (100, HIGHEST_AMOUNT)
KEYS = ("PAYMENT_MIN_AMOUNT", "PAYMENT_MAX_AMOUNT")


class InvalidPaymentLimits(ValueError):
    pass


def validate_limits(minimum, maximum):
    values = []
    for raw in (minimum, maximum):
        try:
            text = str(raw).strip().replace(",", ".")
            if len(text) > 32:
                raise ValueError
            value = Decimal(text)
            if (not value.is_finite() or value < LOWEST_AMOUNT or value > HIGHEST_AMOUNT
                    or value != value.to_integral_value()):
                raise ValueError
        except (InvalidOperation, ValueError, TypeError):
            raise InvalidPaymentLimits("Укажите целые суммы от 1 до 1 000 000 ₽.") from None
        values.append(int(value))
    if values[0] > values[1]:
        raise InvalidPaymentLimits("Минимальная сумма не может быть больше максимальной.")
    return tuple(values)


def get_payment_limits():
    # Both values belong to the same SQLite read snapshot.
    rows = get_db().execute(
        "SELECT key, value FROM app_settings WHERE key IN (?, ?)", KEYS,
    ).fetchall()
    saved = {row["key"]: row["value"] for row in rows if row["value"] != ""}
    return validate_limits(*(saved.get(key, current_app.config.get(key, default))
                             for key, default in zip(KEYS, DEFAULT_LIMITS)))


def save_payment_limits(minimum, maximum):
    minimum, maximum = validate_limits(minimum, maximum)
    set_runtime_settings(dict(zip(KEYS, (str(minimum), str(maximum)))))


def donation_amount_options(minimum, maximum):
    default = min(max(500, minimum), maximum)
    quick = sorted({min(max(amount, minimum), maximum) for amount in (250, 500, 1000, 2500)})
    if len(quick) == 1 and minimum < maximum:
        quick = sorted(set(quick + [minimum, maximum]))
    return {"payment_minimum": minimum, "payment_maximum": maximum,
            "payment_default": default, "quick_amounts": quick}
