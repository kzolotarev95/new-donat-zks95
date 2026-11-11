#!/usr/bin/env python3
"""Run the bounded financial synchronization worker."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app  # noqa: E402
from app.finance.sync import drain  # noqa: E402

if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        print("Finance records processed:", drain())
