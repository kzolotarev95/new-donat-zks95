"""BankingProvider implementations: offline statements, no online banking credentials."""

import csv
import hashlib
import io
import re
import zipfile
from abc import ABC, abstractmethod
from datetime import datetime

from .ledger import money, timestamp, text, now, audit
from ..db import get_db

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10000
ALIASES = {
    "date": ("date", "booked_at", "дата", "дата операции"),
    "amount": ("amount", "сумма", "сумма операции", "приход", "зачисление"),
    "id": ("id", "transaction_id", "fitid", "номер операции", "идентификатор"),
    "description": ("description", "назначение", "назначение платежа", "описание"),
    "currency": ("currency", "валюта"),
}


class BankingProvider(ABC):
    @abstractmethod
    def operations(self, content):
        """Return normalized incoming operations from an authorized source."""


def normalize(rows):
    output = []
    for index, row in enumerate(rows):
        if index >= MAX_ROWS:
            raise ValueError("В выписке больше 10 000 строк.")
        data = {str(k).strip().lower(): v for k, v in row.items() if k is not None}

        def field(key, default=""):
            return next((data[name] for name in ALIASES[key] if name in data), default)

        raw_amount = str(field("amount")).strip()
        if raw_amount.startswith("-"):
            continue
        amount = money(raw_amount)
        if amount == 0:
            continue
        raw_date = str(field("date")).strip()
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", raw_date):
            raw_date = datetime.strptime(raw_date, "%d.%m.%Y").date().isoformat()
        currency = text(field("currency", "RUB"), 3).upper()
        if currency not in ("RUB", "USD", "EUR"):
            raise ValueError("Укажите валюту RUB, USD или EUR.")
        output.append(
            {
                "booked_at": timestamp(raw_date),
                "amount": amount,
                "currency": currency,
                "external_id": text(field("id"), 200),
                "description": text(field("description"), 500),
            }
        )
        if len(output) > MAX_ROWS:
            raise ValueError("В выписке больше 10 000 строк.")
    return output


class CSVProvider(BankingProvider):
    def operations(self, content):
        try:
            data = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            data = content.decode("cp1251")
        sample = data[:8192]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        except csv.Error:
            raise ValueError("Не удалось распознать CSV. Используйте шаблон.") from None
        reader = csv.DictReader(io.StringIO(data), dialect=dialect)
        return normalize(reader)


class XLSXProvider(BankingProvider):
    def operations(self, content):
        from openpyxl import load_workbook

        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if (
                sum(f.file_size for f in archive.infolist()) > 30 * 1024 * 1024
                or len(archive.infolist()) > 500
            ):
                raise ValueError("Слишком большой распакованный XLSX.")
        wb = load_workbook(
            io.BytesIO(content), read_only=True, data_only=True, keep_links=False
        )
        try:
            sheet = wb.active
            if (sheet.max_row or 0) > MAX_ROWS + 1 or (sheet.max_column or 0) > 100:
                raise ValueError("XLSX превышает лимит строк или столбцов.")
            rows = sheet.iter_rows(values_only=True)
            headers = next(rows, ())
            return normalize(
                dict(zip(headers, row))
                for row in rows
                if any(v is not None for v in row)
            )
        finally:
            wb.close()


class OFXProvider(BankingProvider):
    def operations(self, content):
        data = content.decode("utf-8-sig", errors="strict")
        if "<!ENTITY" in data.upper() or "<!DOCTYPE" in data.upper():
            raise ValueError("XML с DTD запрещён.")
        operations = []
        for block in re.findall(r"<STMTTRN>(.*?)</STMTTRN>", data, re.I | re.S):

            def field(name, default=""):
                match = re.search(r"<" + name + r">([^<\r\n]*)", block, re.I)
                return match[1].strip() if match else default

            day = field("DTPOSTED")[:8]
            try:
                day = datetime.strptime(day, "%Y%m%d").date().isoformat()
            except ValueError:
                raise ValueError("Некорректная дата OFX.") from None
            operations.append(
                {
                    "date": day,
                    "amount": field("TRNAMT"),
                    "id": field("FITID"),
                    "description": field("MEMO") or field("NAME"),
                    "currency": "RUB",
                }
            )
        currency = re.search(r"<CURDEF>([^<\r\n]+)", data, re.I)
        for row in operations:
            row["currency"] = currency[1].strip() if currency else "RUB"
        if not operations:
            raise ValueError("В OFX нет операций STMTTRN.")
        return normalize(operations)


def add_operation(db, row, source, account, import_id=None):
    identity = row["external_id"] or "|".join(
        str(row[k]) for k in ("booked_at", "amount", "currency", "description")
    )
    fingerprint = hashlib.sha256(
        f"{source.casefold()}|{account.casefold()}|{identity}".encode()
    ).hexdigest()
    existing = db.execute(
        "SELECT * FROM finance_bank_operations WHERE fingerprint=?", (fingerprint,)
    ).fetchone()
    if existing and (
        existing["amount"] != row["amount"]
        or existing["currency"] != row["currency"]
        or existing["booked_at"] != row["booked_at"]
    ):
        raise ValueError(
            "Операция с таким ID уже существует с другой суммой или датой. Проверьте выписку."
        )
    result = db.execute(
        """INSERT OR IGNORE INTO finance_bank_operations
      (fingerprint,external_id,booked_at,amount,currency,source,account,description,import_id,created_at)
      VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            fingerprint,
            row["external_id"],
            row["booked_at"],
            row["amount"],
            row["currency"],
            source,
            account,
            row["description"],
            import_id,
            now(),
        ),
    )
    return result.rowcount


def import_statement(content, filename, source, account):
    if not content or len(content) > MAX_BYTES:
        raise ValueError("Файл должен быть не больше 5 МБ.")
    source = text(source, 120, True)
    account = text(account, 120, True)
    filename = text(filename, 200, True)
    ext = filename.lower().rsplit(".", 1)[-1]
    providers = {"csv": CSVProvider, "xlsx": XLSXProvider, "ofx": OFXProvider}
    if ext not in providers:
        raise ValueError("Поддерживаются CSV, XLSX и OFX.")
    try:
        rows = providers[ext]().operations(content)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(
            "Файл выписки повреждён или имеет неподдерживаемую структуру."
        ) from exc
    if not rows:
        raise ValueError("В выписке нет входящих операций.")
    digest = hashlib.sha256(
        content + source.encode() + b"\0" + account.encode()
    ).hexdigest()
    db = get_db()
    with db:
        existing = db.execute(
            "SELECT id FROM finance_bank_imports WHERE digest=?", (digest,)
        ).fetchone()
        if existing:
            return 0
        cursor = db.execute(
            "INSERT INTO finance_bank_imports(digest,filename,source,account,rows_added,created_at) VALUES(?,?,?,?,0,?)",
            (digest, text(filename, 200), source, account, now()),
        )
        count = sum(
            add_operation(db, row, source, account, cursor.lastrowid) for row in rows
        )
        db.execute(
            "UPDATE finance_bank_imports SET rows_added=? WHERE id=?",
            (count, cursor.lastrowid),
        )
        audit(
            db,
            "bank_import",
            f"import:{cursor.lastrowid}",
            {},
            {"rows_added": count, "source": source, "account": account},
        )
    return count
