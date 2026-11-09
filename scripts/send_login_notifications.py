#!/usr/bin/env python3
"""Retry durable login notifications from the project systemd timer."""
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app  # noqa: E402
from app.login_notifications import drain_pending  # noqa: E402


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    with app.app_context():
        logging.info("Login notification jobs processed: %s", drain_pending())
