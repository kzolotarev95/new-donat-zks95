#!/usr/bin/env python3
"""Send a best-effort push for a systemd service event.

This helper is intentionally safe to run from ExecStartPost and OnFailure:
delivery errors are logged and never make the web service fail again.
"""
from pathlib import Path
import logging
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from app.push import notify_service_event


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def _marker_path(app):
    database = Path(app.config["DATABASE_PATH"])
    return database.parent / ".last-push-boot-id"


def main():
    event = (sys.argv[1] if len(sys.argv) > 1 else "startup").strip().lower()
    app = create_app()
    with app.app_context():
        marker = _marker_path(app)
        if event == "startup":
            boot_id = _boot_id()
            try:
                if marker.read_text(encoding="utf-8").strip() == boot_id:
                    return 0
            except FileNotFoundError:
                pass
            result = notify_service_event("startup")
            # A successful delivery or a deliberate skip means this boot has
            # been handled. Failed deliveries may be retried by systemd.
            if not result.get("failed"):
                marker.write_text(boot_id + "\n", encoding="utf-8")
            log.info("Startup push result: %s", result)
            return 0
        result = notify_service_event(event)
        log.info("Service event push result: %s", result)
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        log.exception("Could not process service event push")
        raise SystemExit(0)
