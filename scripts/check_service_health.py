#!/usr/bin/env python3
"""Recover a locally hung web app without depending on public DNS or TLS.

Three failed probes are required. An intentional systemctl stop is respected;
failed services can be started again after systemd's start limit is reached.
"""
import json
import logging
import subprocess
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

SERVICE = "new-donat-zks95.service"
BASE_URL = "http://127.0.0.1:8015"
log = logging.getLogger(__name__)


def service_state():
    result = subprocess.run(
        ["systemctl", "show", SERVICE, "--property=ActiveState", "--value"],
        capture_output=True, text=True, timeout=5, check=True,
    )
    return result.stdout.strip()


def probe():
    # Local probes must not inherit a system HTTP proxy.
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(BASE_URL + "/healthz", timeout=3) as response:
            body = json.loads(response.read(4096))
            if response.status != 200 or body != {"service": "new-donat-zks95", "status": "ok"}:
                return False
        with opener.open(BASE_URL + "/donate", timeout=3) as response:
            return response.status == 200 and bool(response.read(1))
    except (URLError, OSError, ValueError):
        return False


def main():
    state = service_state()
    if state not in {"active", "failed"}:
        return 0
    for attempt in range(3):
        if probe():
            return 0
        if attempt < 2:
            time.sleep(2)
    # Recheck after probing, so a manual stop during the check stays stopped.
    state = service_state()
    if state not in {"active", "failed"}:
        return 0
    log.error("Three local health checks failed; recovering %s (state=%s)", SERVICE, state)
    if state == "failed":
        subprocess.run(["systemctl", "reset-failed", SERVICE], timeout=5, check=True)
    subprocess.run(["systemctl", "restart", SERVICE], timeout=45, check=True)
    for _ in range(5):
        if probe():
            log.info("Application recovered")
            return 0
        time.sleep(1)
    log.error("Application still unhealthy after recovery; inspect its journal")
    return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
