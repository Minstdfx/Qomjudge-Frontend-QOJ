"""Exercise Vite's development proxy against an isolated offline demo.

Usage: python scripts/frontend_dev_smoke.py [--port VITE_PORT]
Requires npm install, Playwright, Microsoft Edge and the Python app dependencies.
Both listeners bind to 127.0.0.1 on temporary ports and close in finally blocks.
No real QOJ account, request, submission or system clipboard is used.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "Lib" / "site-packages"))

from app import create_app
from flask import request
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_vite(process, origin, log_path):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Vite exited before startup:\n{log_path.read_text(encoding='utf-8', errors='replace')}")
        try:
            with urlopen(origin + "/", timeout=1) as response:
                if response.status == 200 and b"/@vite/client" in response.read():
                    return
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.1)
    raise RuntimeError(f"Vite did not become ready:\n{log_path.read_text(encoding='utf-8', errors='replace')}")


def stop_vite(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        # Only terminate the process tree created by this script; npm/Vite can
        # launch a helper, which should not be left behind after a failed check.
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       creationflags=subprocess.CREATE_NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="Vite loopback port; 0 selects a free port")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    node = shutil.which("node")
    vite_entry = ROOT / "node_modules" / "vite" / "bin" / "vite.js"
    if not node or not vite_entry.is_file():
        parser.error("Node and local Vite are required; run npm install first")

    with TemporaryDirectory(prefix="qoj-vite-demo-") as temporary:
        app = create_app(contest_id=1, demo=True, data_dir=Path(temporary) / "data")
        app.config["TESTING"] = True
        backend_requests = []

        @app.after_request
        def record_proxy_response(response):
            if request.path.startswith("/api/"):
                backend_requests.append({"path": request.path, "method": request.method,
                                         "host": request.host, "origin": request.headers.get("Origin"),
                                         "csrf": bool(request.headers.get("X-CSRF-Token")),
                                         "cookie": "qoj_vp_session" in request.cookies,
                                         "status": response.status_code})
            return response

        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        backend_origin = f"http://127.0.0.1:{server.server_port}"
        vite_port = args.port or unused_port()
        while not args.port and vite_port == server.server_port:
            vite_port = unused_port()
        origin = f"http://127.0.0.1:{vite_port}"
        log_path = Path(temporary) / "vite.log"
        vite = None
        errors, external_requests = [], []
        try:
            environment = os.environ.copy()
            environment["QOJ_BACKEND"] = backend_origin
            environment["NO_COLOR"] = "1"
            # Same Vite CLI/config as npm run dev, without a shell wrapper or a
            # visible Windows console; the port is isolated from other services.
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            with log_path.open("w", encoding="utf-8") as output:
                vite = subprocess.Popen([node, str(vite_entry), "--config", "frontend/vite.config.js",
                                         "--host", "127.0.0.1", "--port", str(vite_port), "--strictPort"],
                                        cwd=ROOT, env=environment, stdout=output, stderr=subprocess.STDOUT, **options)
                wait_for_vite(vite, origin, log_path)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel="msedge", headless=True)
                    try:
                        context = browser.new_context(viewport={"width": 1440, "height": 1000})

                        def local_only(route):
                            parsed = urlsplit(route.request.url)
                            if f"{parsed.scheme}://{parsed.netloc}" != origin:
                                external_requests.append(route.request.url)
                                route.abort()
                            else:
                                route.continue_()

                        context.route("**/*", local_only)
                        page = context.new_page()
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        with page.expect_response(lambda response: response.url == origin + "/api/state") as initial:
                            page.goto(origin, wait_until="networkidle")
                        assert initial.value.status == 200
                        initial_state = initial.value.json()
                        assert initial_state["demo"] is True and initial_state["csrf_token"]
                        expect(page.locator("#demo-banner")).to_be_visible()
                        expect(page.locator("#view-participation")).to_be_visible()
                        assert any(cookie["name"] == "qoj_vp_session" and cookie["httpOnly"]
                                   for cookie in context.cookies(origin))
                        created = context.request.post(origin + "/api/team/create",
                            data={"name": "Vite proxy demo", "device_name": "Vite test captain"},
                            headers={"X-CSRF-Token": initial_state["csrf_token"], "Origin": origin})
                        assert created.status == 200, created.text()
                        assert created.json()["team"]["role"] == "captain"
                        page.reload(wait_until="networkidle")
                        expect(page.locator("#start-team-name")).to_have_text("队伍：Vite proxy demo")
                        with page.expect_response(lambda response: response.url == origin + "/api/start") as started:
                            page.locator("#start-button").click()
                        assert started.value.status == 200, started.value.text()
                        expect(page.locator("#run-status")).to_have_text("比赛进行中")
                        page.locator('[data-tab="problems"]').click()
                        expect(page.locator("#statement-frame")).to_be_visible()
                        expect(page.frame_locator("#statement-frame").locator("h1")).to_contain_text("A + B")
                        page.locator("#statement-submit").click()
                        expect(page.locator("#submit-language")).to_have_value("C++17")
                        source = "#include <iostream>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b;}"
                        page.locator("#source-code").fill(source)
                        with page.expect_response(lambda response: response.url == origin + "/api/submit") as submitted:
                            page.locator("#send-button").click()
                        assert submitted.value.status == 201, submitted.value.text()
                        local_id = submitted.value.json()["submission"]["id"]
                        expect(page.locator("#submit-dialog")).not_to_be_visible()
                        result = page.locator(f'#submissions-table [data-submission-id="{local_id}"]')
                        expect(result).to_contain_text("Accepted", timeout=20000)
                        result.locator("[data-submission-source]").click()
                        expect(page.locator("#submission-source-code")).to_have_value(source)
                        assert not errors, errors
                        assert not external_requests, external_requests
                    finally:
                        browser.close()

                mutations = [item for item in backend_requests if item["method"] == "POST"]
                assert {item["path"] for item in mutations} == {"/api/team/create", "/api/start", "/api/submit"}, mutations
                assert all(item["host"] == f"127.0.0.1:{vite_port}" and item["origin"] == origin
                           and item["csrf"] and item["cookie"] and item["status"] in (200, 201)
                           for item in mutations), mutations
                assert any(item["path"] == "/api/problems/1/statement" and item["status"] == 200
                           for item in backend_requests), backend_requests
        finally:
            try:
                if vite is not None:
                    stop_vite(vite)
            finally:
                server.shutdown()
                server_thread.join(timeout=5)
                server.server_close()
        assert not server_thread.is_alive(), "Temporary demo thread is still running"
        assert vite is not None and vite.poll() is not None, "Vite process is still running"
        print(json.dumps({"frontend_dev": "passed", "backend_port": server.server_port,
                          "vite_port": vite_port, "browser_errors": errors,
                          "checks": ["Vite dev modules", "GET state proxy", "session cookie", "CSRF and Origin",
                                     "create team", "start demo", "statement iframe", "mock submission", "final result", "saved source"],
                          "temporary_listeners_stopped": True, "real_qoj_requests": 0}))


if __name__ == "__main__":
    main()
