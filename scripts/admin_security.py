#!/usr/bin/env python3
"""Root-only account recovery and consistent database/configuration backups."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import getpass
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
import time

from dotenv import dotenv_values
from werkzeug.security import generate_password_hash

ENV_FILE = Path("/etc/new-donat-zks95/.env")
BACKUP_DIR = Path("/var/backups/new-donat-zks95/security")


def backup_database(database, env_file=ENV_FILE, destination=BACKUP_DIR):
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    folder = destination / f"{stamp}-{secrets.token_hex(3)}"
    folder.mkdir(mode=0o700)
    target = folder / "site.db"
    target.touch(mode=0o600)
    source = sqlite3.connect(f"{Path(database).resolve().as_uri()}?mode=ro", uri=True, timeout=20)
    output = sqlite3.connect(target)
    try:
        source.backup(output)
    finally:
        output.close()
        source.close()
        if os.name == "posix" and os.geteuid() == 0:
            owner = Path(database).stat()
            for suffix in ("-wal", "-shm"):
                try:
                    descriptor = os.open(str(database) + suffix, os.O_RDONLY | os.O_NOFOLLOW)
                except FileNotFoundError:
                    continue
                try:
                    info = os.fstat(descriptor)
                    if stat.S_ISREG(info.st_mode) and info.st_uid == 0:
                        os.fchown(descriptor, owner.st_uid, owner.st_gid)
                finally:
                    os.close(descriptor)
    shutil.copyfile(env_file, folder / ".env")
    os.chmod(folder / ".env", 0o600)
    return folder


@contextmanager
def database_owner(database):
    # Avoid creating root-owned WAL/SHM files alongside the service user's DB.
    owner = database.stat()
    uid, gid = os.geteuid(), os.getegid()
    try:
        os.setegid(owner.st_gid)
        os.seteuid(owner.st_uid)
        yield
    finally:
        os.seteuid(uid)
        os.setegid(gid)


def recover_account(db, user_id, action, password_hash=None):
    """One transaction invalidates cookies, challenges and all existing sessions."""
    now = int(time.time())
    with db:
        changed = db.execute(
            "UPDATE admin_users SET auth_version = auth_version + 1 WHERE id = ?", (user_id,),
        ).rowcount
        if not changed:
            raise ValueError("Аккаунт не найден.")
        if action == "reset-2fa":
            db.execute("""UPDATE admin_users SET totp_secret = '', totp_last_step = -1,
                       totp_pending_secret = '', totp_pending_id_hash = '', totp_pending_until = 0 WHERE id = ?""", (user_id,))
            db.execute("DELETE FROM admin_recovery_codes WHERE user_id = ?", (user_id,))
        elif action == "reset-password":
            if not password_hash:
                raise ValueError("Не задан новый пароль.")
            db.execute("UPDATE admin_users SET password_hash = ?, must_change_password = 0, updated_at = ? WHERE id = ?",
                       (password_hash, datetime.now(timezone.utc).isoformat(), user_id))
        elif action != "revoke-sessions":
            raise ValueError("Неизвестное действие.")
        db.execute("UPDATE admin_sessions SET revoked_at = ?, revocation_reason = 'ssh-recovery' WHERE user_id = ? AND revoked_at IS NULL",
                   (now, user_id))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "sessions", "backup", "reset-2fa", "reset-password", "revoke-sessions"])
    parser.add_argument("--user", help="Логин администратора (иначе запросим интерактивно)")
    args = parser.parse_args()
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        parser.error("Запустите на VPS через sudo.")
    if not ENV_FILE.is_file():
        parser.error(f"Не найден файл {ENV_FILE}.")
    values = dotenv_values(ENV_FILE, interpolate=False)
    database = Path(values.get("DATABASE_PATH") or "/var/lib/new-donat-zks95/site.db").resolve()
    if not database.is_file():
        parser.error(f"База не найдена: {database}. Пустую базу создавать не будем.")
    if args.action == "backup":
        print(f"Бэкап базы и .env: {backup_database(database)}")
        return
    with database_owner(database), closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=20)) as read_db:
        read_db.row_factory = sqlite3.Row
        users = read_db.execute("SELECT id, username, totp_secret FROM admin_users ORDER BY id").fetchall()
        if args.action == "status":
            for user in users:
                active = read_db.execute("""SELECT COUNT(*) FROM admin_sessions s JOIN admin_users u ON u.id = s.user_id
                    WHERE s.user_id = ? AND s.revoked_at IS NULL AND s.expires_at > ? AND s.auth_version = u.auth_version""",
                    (user["id"], int(time.time()))).fetchone()[0]
                print(f"{user['username']}: 2FA {'включена' if user['totp_secret'] else 'выключена'}, активных сеансов: {active}")
            return
        print("Аккаунты: " + ", ".join(user["username"] for user in users))
        username = args.user or input("Логин администратора: ").strip()
        user = next((user for user in users if user["username"] == username), None)
        if not user:
            parser.error("Аккаунт не найден.")
        if args.action == "sessions":
            rows = read_db.execute("SELECT started_at, ip_address, user_agent, revoked_at, expires_at FROM admin_sessions WHERE user_id = ? ORDER BY id DESC LIMIT 50", (user["id"],)).fetchall()
            for row in rows:
                date = datetime.fromtimestamp(row["started_at"], timezone.utc).isoformat()
                print(f"{date} | {row['ip_address']} | {row['user_agent'][:120]}")
            return
    confirmation = f"{args.action} {username}"
    if input(f"Будет создан бэкап и завершены все сеансы. Введите {confirmation}: ").strip() != confirmation:
        print("Отменено.")
        return
    password_hash = None
    if args.action == "reset-password":
        password = getpass.getpass("Новый пароль (минимум 12 символов): ")
        repeat = getpass.getpass("Повторите пароль: ")
        if len(password) < 12 or password != repeat:
            parser.error("Пароли должны совпадать и содержать минимум 12 символов.")
        password_hash = generate_password_hash(password)
    backup = backup_database(database)
    with database_owner(database):
        connection = sqlite3.connect(database, timeout=20)
        try:
            recover_account(connection, user["id"], args.action, password_hash)
        finally:
            connection.close()
    print(f"Готово: {args.action}, аккаунт {username}. Все его сеансы завершены. Бэкап: {backup}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise SystemExit(f"Ошибка: {exc}") from exc
