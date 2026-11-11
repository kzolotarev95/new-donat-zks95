"""Parse a single copied merchant transfer card, without storing its raw text."""

import re
from datetime import datetime

from .ledger import format_money, money

MAX_TEXT = 20000
NUMBER = r"([0-9]+(?: [0-9]{3})*(?:[,.][0-9]{1,2})?)"
RUB = r"\s*(?:₽|руб(?:\.|лей|ля)?|RUB)(?=\W|$)"


def _single(pattern, value, label, *, required=True):
    matches = re.findall(pattern, value, re.IGNORECASE)
    if len(matches) > 1:
        raise ValueError(f"Несколько значений «{label}». Вставьте только одну карточку перевода.")
    if not matches:
        if required:
            raise ValueError(f"Не найдено поле «{label}». Скопируйте карточку перевода целиком.")
        return ""
    return matches[0]


def parse_transfer(raw):
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_TEXT:
        raise ValueError("Вставьте текст одной карточки перевода ЮKassa, не более 20 000 символов.")
    value = raw.replace("\xa0", " ").replace("\u202f", " ").replace("ё", "е")
    value = value.replace("**", "").replace("`", "")
    value = re.sub(r"(?m)^\s*[-•]\s*", "", value)
    if re.search(r"\bотклон[её]н\b", value, re.IGNORECASE):
        raise ValueError("Перевод отклонён. Не добавляйте его как ожидаемую выплату.")
    document = _single(
        r"номер\s+плат[её]жного\s+документа\s*:?\s*(?:№\s*)?([0-9]{1,20})\b",
        value, "Номер платёжного документа",
    )
    transfer = _single(r"\bперевод\s*№\s*([0-9]{1,20})\b", value, "Перевод №", required=False)
    day = _single(
        r"дата\s+отправки\s*:?\s*([0-9]{2}\.[0-9]{2}\.[0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2})\b",
        value, "Дата отправки",
    )
    try:
        day = datetime.strptime(day, "%d.%m.%Y" if "." in day else "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValueError("Некорректная дата отправки в карточке перевода.") from None
    # Require the currency immediately after the amount: never interpret account
    # numbers or a registry date as money. Other currencies are not converted.
    amount = _single(r"\bсумма(?:\s+перевода)?\s*:?\s*" + NUMBER + RUB,
                     value, "Сумма")
    fee = _single(r"\bкомиссия\s*:?\s*" + NUMBER + RUB,
                  value, "Комиссия", required=False)
    amount_kop = money(amount)
    if not amount_kop:
        raise ValueError("Сумма перевода должна быть положительной.")
    fee_kop = money(fee, optional=True)
    source = f"ЮKassa: документ №{document}"
    if transfer:
        source += f", перевод №{transfer}"
    return {
        "external_id": document,
        "date": day,
        "amount": format_money(amount_kop).replace(" ", ""),
        "fee": "" if fee_kop is None else format_money(fee_kop).replace(" ", ""),
        "source": source,
        "comment": "Из карточки перевода ЮKassa; зачисление в банке проверяется отдельно.",
    }
