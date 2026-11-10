"""Read-only view of the privileged HTTPS reserve reconciler."""
import json
import re
import time
from pathlib import Path

from flask import current_app
from .settings import get_runtime_setting

SETTING = "HTTPS_RESERVE_ENABLED"
STATUS_PATH = "/var/lib/new-donat-zks95-network/status.json"


def reserve_status():
    status = {}
    try:
        with Path(current_app.config.get("HTTPS_RESERVE_STATUS_PATH", STATUS_PATH)).open(encoding="utf-8") as source:
            status = json.loads(source.read(16385))
        if not isinstance(status, dict):
            status = {}
    except (OSError, ValueError):
        pass
    actual = status.get("enabled") is True
    # An existing manually configured 8443 is adopted on the first update.
    desired = str(get_runtime_setting(SETTING, "1" if actual else "0")) == "1"
    domain = str(status.get("domain") or "")
    if not re.fullmatch(r"(?=.{1,253}$)[A-Za-z0-9]+(?:[.-][A-Za-z0-9-]+)*", domain):
        domain = ""
    try:
        fresh = 0 <= time.time() - float(status.get("checked_at", 0)) < 90
    except (ValueError, TypeError):
        fresh = False
    if not fresh:
        state = "unavailable"
        message = "Служба настройки портов ещё не ответила. Установите обновление VPS или проверьте её статус по SSH."
    elif status.get("requested_enabled") is not desired and status.get("requested_enabled") is not None:
        state = "pending"
        message = "Применяем настройку. Обычно это занимает до 15 секунд."
    else:
        state = status.get("state", "error")
        if state not in {"enabled", "disabled", "error"}:
            state = "error"
        message = str(status.get("message") or "Не удалось определить состояние порта.")[:500]
    base = f"https://{domain}:8443" if domain else ""
    return {
        "desired_enabled": desired, "enabled": actual, "state": state,
        "message": message, "domain": domain,
        "site_url": base + "/donate" if base else "",
        "admin_url": base + "/admin/" if base else "",
    }
