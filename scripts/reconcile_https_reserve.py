#!/usr/bin/env python3
"""Root-owned, fixed-scope HTTPS listener manager; no Flask imports or shell eval.

Only the boolean HTTPS_RESERVE_ENABLED setting is accepted from the application.
The hostname and TLS configuration come from the existing project Nginx vhost.
"""
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import tempfile
import time

CONFIG = Path("/etc/nginx/sites-available/new-donat-zks95.conf")
ENV_FILE = Path("/etc/new-donat-zks95/.env")
STATE_DIR = Path("/var/lib/new-donat-zks95-network")
BACKUP_DIR = Path("/var/backups/new-donat-zks95/https-reserve")
PORT = 8443
BEGIN = "# BEGIN new-donat-zks95 reserve HTTPS"
END = "# END new-donat-zks95 reserve HTTPS"
log = logging.getLogger(__name__)


class ReserveError(Exception):
    """An actionable error safe to show to the administrator."""


def tokens(text):
    pattern = r'''\s+|\#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[{};]|(?:\\.|[^\s{};#'"\\])+'''
    result = []
    end = 0
    for match in re.finditer(pattern, text):
        if match.start() != end:
            raise ReserveError("Не удалось безопасно разобрать конфигурацию Nginx.")
        end = match.end()
        raw = match[0]
        if raw.isspace() or raw.startswith("#"):
            continue
        value = raw[1:-1] if raw[:1] in {"'", '"'} else raw
        result.append((value, match.start(), match.end()))
    if end != len(text):
        raise ReserveError("Не удалось безопасно разобрать конфигурацию Nginx.")
    return result


def server_blocks(text):
    stream = tokens(text)
    depth, opening, directives, pending = 0, None, [], []
    for index, token in enumerate(stream):
        value, start, end = token
        symbol = text[start:end] in {"{", "}", ";"}
        if symbol and value == "{":
            if depth == 0:
                if index == 0 or stream[index - 1][0] != "server":
                    raise ReserveError("В конфигурации сайта найден неизвестный внешний блок.")
                opening, directives = token, []
            depth += 1
            pending = []
        elif symbol and value == "}":
            depth -= 1
            if depth < 0:
                raise ReserveError("Нарушена структура конфигурации Nginx.")
            if depth == 0:
                yield opening[2], start, directives
            pending = []
        elif depth == 1:
            if symbol and value == ";":
                if pending:
                    directives.append((pending[0][0], [item[0] for item in pending[1:]], pending[0][1], end))
                pending = []
            else:
                pending.append(token)
    if depth:
        raise ReserveError("Нарушена структура конфигурации Nginx.")


def listen_port(arguments):
    match = re.fullmatch(r"(?:\[[0-9a-fA-F:]+\]:|[0-9.*]+:)?([0-9]+)", arguments[0] if arguments else "")
    return int(match[1]) if match else None


def tls_server(text):
    candidates = []
    for opening, closing, directives in server_blocks(text):
        if any(key == "listen" and listen_port(args) == 443 and "ssl" in args for key, args, *_ in directives):
            names = [name for key, args, *_ in directives if key == "server_name" for name in args]
            if (len(names) != 1 or not re.fullmatch(r"(?=.{1,253}$)[A-Za-z0-9]+(?:[.-][A-Za-z0-9-]+)*", names[0])
                    or not any(key == "ssl_certificate" for key, *_ in directives)
                    or not any(key == "ssl_certificate_key" for key, *_ in directives)):
                raise ReserveError("Нужен отдельный HTTPS-блок сайта с доменом и сертификатом.")
            candidates.append((opening, closing, directives, names[0]))
    if len(candidates) > 1:
        raise ReserveError("Найдено несколько HTTPS-блоков. Требуется проверка конфигурации по SSH.")
    return candidates[0] if candidates else None


def configured_reserve(text):
    server = tls_server(text)
    if not server:
        return False
    return any(key == "listen" and listen_port(args) == PORT and "ssl" in args for key, args, *_ in server[2])


def render_config(text, enabled):
    if "Managed by new-donat-zks95" not in text:
        raise ReserveError("Конфигурация Nginx не принадлежит этому проекту.")
    server = tls_server(text)
    if not server:
        if enabled:
            raise ReserveError("Сначала настройте HTTPS и сертификат для текущего домена сайта.")
        return text, ""
    opening, closing, directives, domain = server
    body = text[opening:closing]
    removals = []
    for key, args, start, end in directives:
        if key != "listen" or listen_port(args) != PORT:
            continue
        if "ssl" not in args:
            raise ReserveError("Порт 8443 используется без TLS. Конфигурация не изменена.")
        left = text.rfind("\n", opening, start) + 1
        right = text.find("\n", end, closing)
        if right < 0:
            right = closing
        if left >= opening and not text[left:start].strip() and not text[end:right].strip():
            start, end = left, min(right + 1, closing)
        removals.append((start - opening, end - opening))
    for start, end in reversed(removals):
        body = body[:start] + body[end:]
    for comment in (BEGIN, END, "# Reserved HTTPS access while the upstream path to port 443 is investigated."):
        body = re.sub(r"(?m)^[ \t]*" + re.escape(comment) + r"[ \t]*\r?\n", "", body)
    addition = f"    {BEGIN}\n    listen 8443 ssl;\n    listen [::]:8443 ssl;\n    {END}\n" if enabled else ""
    return text[:opening] + "\n" + addition + body.lstrip("\r\n") + text[closing:], domain


def read_request(database):
    # Query-only connection, no migrations, extensions, imports or secret decoding.
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        row = connection.execute("SELECT value FROM app_settings WHERE key = 'HTTPS_RESERVE_ENABLED'").fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    if row[0] not in {"0", "1"}:
        raise ReserveError("Некорректная настройка резервного порта. Сохраните её заново в админке.")
    return row[0] == "1"


def database_path():
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("DATABASE_PATH="):
            path = Path(line.split("=", 1)[1].strip().strip("\"'"))
            if path.is_absolute():
                return path
    return Path("/var/lib/new-donat-zks95/site.db")


def unprivileged_request(database):
    # SQLite WAL/SHM must never be created as root in the web user's data folder.
    # Parse the application-controlled database with the application's privileges.
    result = subprocess.run([
        "/usr/sbin/runuser", "-u", "zks95", "--", "/usr/bin/python3", "-I",
        str(Path(__file__).resolve()), "--read-request", str(database),
    ], capture_output=True, text=True, timeout=5)
    if result.returncode:
        log.error("Cannot read reserve setting as zks95: %s", result.stderr[-1000:])
        raise ReserveError("Не удалось прочитать настройку резервного порта. Проверьте журнал службы по SSH.")
    try:
        value = json.loads(result.stdout)
    except ValueError as exc:
        raise ReserveError("Некорректный ответ при чтении настройки порта.") from exc
    if value is not None and value is not True and value is not False:
        raise ReserveError("Некорректная настройка резервного порта.")
    return value


def command(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=12)
    if result.returncode:
        log.error("Command failed: %s: %s", arguments[0], result.stderr[-2000:])
        raise ReserveError("Проверка или перезагрузка Nginx не прошла. Подробности — в журнале службы портов.")
    return result.stdout


def check_port(already_configured):
    output = command(["/usr/bin/ss", "-H", "-ltnp", "sport = :8443"])
    if output.strip() and (not already_configured or set(re.findall(r'"([^\"]+)"', output)) != {"nginx"}):
        raise ReserveError("Порт 8443 занят другим сервисом. Чужие службы не изменены.")


def verify_https(domain):
    # Connect locally with the real SNI and validate the site's existing certificate.
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=domain) as secured:
                secured.sendall(f"GET /healthz HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n".encode("ascii"))
                response = http.client.HTTPResponse(secured)
                response.begin()
                body = json.loads(response.read(4096))
                if response.status != 200 or body != {"service": "new-donat-zks95", "status": "ok"}:
                    raise ValueError("health check")
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise ReserveError("Локальная HTTPS-проверка порта 8443 не прошла. Проверьте сертификат и приложение.") from exc


def atomic_write(path, content, mode=0o644):
    descriptor, temporary = tempfile.mkstemp(prefix=".https-reserve-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), mode) if hasattr(os, "fchmod") else os.chmod(temporary, mode)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_config(path, old, new, domain, enabled, backups):
    if new == old:
        if enabled:
            verify_https(domain)
        return
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Repeated failures with unchanged inputs must not create unlimited backups.
    backup = backups / f"nginx-{hashlib.sha256(old.encode()).hexdigest()}.conf"
    if not backup.exists():
        atomic_write(backup, old, 0o600)
    mode = stat.S_IMODE(path.stat().st_mode)
    atomic_write(path, new, mode)
    try:
        command(["/usr/sbin/nginx", "-t"])
        command(["/usr/bin/systemctl", "reload", "nginx"])
        if enabled:
            # A successful systemctl reload alone does not prove a new listener works.
            for attempt in range(5):
                try:
                    verify_https(domain)
                    break
                except ReserveError:
                    if attempt == 4:
                        raise
                    time.sleep(0.3)
    except Exception:
        atomic_write(path, old, mode)
        try:
            command(["/usr/sbin/nginx", "-t"])
            command(["/usr/bin/systemctl", "reload", "nginx"])
        except Exception:
            log.exception("Previous config restored on disk; Nginx reload needs inspection")
        raise


def trusted_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 262144:
        raise ReserveError("Конфигурация должна быть обычным файлом root без прав записи для других пользователей.")
    return path.read_text()


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run the installed root-owned helper through systemd.")
    STATE_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    info = STATE_DIR.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit("Unsafe status directory")
    desired, enabled, domain = None, False, ""
    try:
        old = trusted_file(CONFIG)
        enabled = configured_reserve(old)
        server = tls_server(old)
        domain = server[3] if server else ""
        requested = unprivileged_request(database_path())
        desired = enabled if requested is None else requested
        new, domain = render_config(old, desired)
        if desired:
            check_port(enabled)
        apply_config(CONFIG, old, new, domain, desired, BACKUP_DIR)
        enabled = desired
        state = "enabled" if enabled else "disabled"
        message = "HTTPS-порт 8443 включён в Nginx. Откройте ссылку для проверки доступа из вашей сети." if enabled else "Резервный HTTPS-порт выключен."
    except (ReserveError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        state = "error"
        message = str(exc) if isinstance(exc, ReserveError) else "Не удалось применить настройку. Проверьте журнал службы портов по SSH."
        log.error("HTTPS reserve: %s", message)
    status = {"requested_enabled": desired, "enabled": enabled, "state": state,
              "message": message, "domain": domain, "checked_at": time.time()}
    atomic_write(STATE_DIR / "status.json", json.dumps(status, ensure_ascii=False) + "\n")
    return 0 if state != "error" else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) == 3 and sys.argv[1] == "--read-request":
        print(json.dumps(read_request(Path(sys.argv[2]))))
    else:
        raise SystemExit(main())
