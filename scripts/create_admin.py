#!/usr/bin/env python3
"""Create or reset the first ZKS95 admin account from a local terminal."""
import getpass
import os
import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.db import get_admin_user, get_db, update_admin_password  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

app = create_app()
parser = argparse.ArgumentParser()
parser.add_argument("--default", action="store_true", help="Create/reset admin with temporary password admin")
args = parser.parse_args()
with app.app_context():
    if args.default:
        username, password = "admin", "admin"
    else:
        username = input("Admin username [admin]: ").strip() or "admin"
        password = getpass.getpass("Admin password (12+ characters): ")
        repeat = getpass.getpass("Repeat password: ")
        if len(password) < 12 or password != repeat:
            raise SystemExit("Password must have 12+ characters and match.")
    existing = get_admin_user(username)
    if existing:
        update_admin_password(existing["id"], generate_password_hash(password), int(args.default))
        print(f"Password updated for {username}. Change it after login.")
    else:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        get_db().execute("INSERT INTO admin_users(username, password_hash, created_at, updated_at, must_change_password) VALUES (?, ?, ?, ?, ?)", (username, generate_password_hash(password), now, now, int(args.default)))
        get_db().commit()
        print(f"Admin created: {username}.")
