import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

spec = importlib.util.spec_from_file_location(
    "runtime_health", Path(__file__).resolve().parents[1] / "scripts/check_service_health.py"
)
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


@pytest.mark.parametrize(
    "states,results,actions",
    [
        (["active"], [True], []),
        (["active"], [False, True], []),
        (["inactive"], [], []),
        (["active", "inactive"], [False] * 3, []),
        (["active", "active"], [False] * 3 + [True], ["restart"]),
        (["failed", "failed"], [False] * 3 + [True], ["reset-failed", "restart"]),
    ],
)
def test_recovery_respects_transient_errors_and_manual_stop(monkeypatch, states, results, actions):
    state_values, probe_values = iter(states), iter(results)
    commands = []
    monkeypatch.setattr(health, "service_state", lambda: next(state_values))
    monkeypatch.setattr(health, "probe", lambda: next(probe_values))
    monkeypatch.setattr(health.time, "sleep", lambda _: None)
    monkeypatch.setattr(health.subprocess, "run", lambda command, **_: commands.append(command))
    assert health.main() == 0
    assert commands == [["systemctl", action, health.SERVICE] for action in actions]


@pytest.mark.parametrize("donate_status", [200, 500])
def test_probe_checks_database_and_rendered_page(monkeypatch, donate_status):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status = 200 if self.path == "/healthz" else donate_status
            body = json.dumps({"service": "new-donat-zks95", "status": "ok"}).encode() if self.path == "/healthz" else b"donate page"
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(health, "BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        assert health.probe() is (donate_status == 200)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
