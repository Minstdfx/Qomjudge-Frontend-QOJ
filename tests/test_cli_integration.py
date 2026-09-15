"""CLI subprocesses against a disposable, real offline-demo HTTP service."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest

from flask import request
import requests
from werkzeug.serving import make_server, WSGIRequestHandler

from app import create_app


ROOT = Path(__file__).resolve().parents[1]


class QuietRequestHandler(WSGIRequestHandler):
    def log(self, *_args, **_kwargs):
        pass


class CLIIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)
        self.app = create_app(contest_id=9001, demo=True, data_dir=self.work / "server-data")
        self.app.config["TESTING"] = True
        self.terminal_requests = []

        @self.app.after_request
        def record_terminal_response(response):
            if "Authorization" in request.headers:
                self.terminal_requests.append((request.method, request.path, response.status_code))
                self.assertFalse(response.headers.getlist("Set-Cookie"))
            return response

        self.server = make_server("127.0.0.1", 0, self.app, threaded=True,
                                  request_handler=QuietRequestHandler)
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.worker.start()
        self.addCleanup(self.close_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.captain = self.browser()
        self.other_captain = self.browser()
        self.member = self.browser()
        self.team = self.post(self.captain, "/api/team/create", {"name": "Offline CLI A"})["team"]
        self.post(self.member, "/api/team/join", {"code": self.team["join_code"], "device_name": "Reader A"})
        self.post(self.other_captain, "/api/team/create", {"name": "Offline CLI B"})
        self.run_id = self.post(self.captain, "/api/start")["run"]["id"]
        self.post(self.other_captain, "/api/start")
        issued = self.post(self.captain, "/api/cli/token")
        self.config = issued["config"]
        self.assertEqual(self.config["server_url"], self.url)
        self.config_path = self.work / "qoj-cli.json"
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.source = self.work / "A.cpp"
        self.source_text = "// CLI 集成测试源码，不来自真实比赛\nint main() { return 0; }\n"
        self.source.write_bytes(self.source_text.encode("utf-8"))

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)

    def browser(self):
        browser = requests.Session()
        browser.trust_env = False
        self.addCleanup(browser.close)
        return browser

    def get(self, browser, path):
        with browser.get(self.url + path, timeout=5) as response:
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

    def post(self, browser, path, body=None):
        csrf = self.get(browser, "/api/state")["csrf_token"]
        with browser.post(self.url + path, json={} if body is None else body,
                          headers={"X-CSRF-Token": csrf}, timeout=5) as response:
            self.assertIn(response.status_code, (200, 201), response.text)
            return response.json()

    def command(self, command, file=None, *args, timeout=20):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
               "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        result = subprocess.run([sys.executable, str(ROOT / "cli" / f"{command}.py"),
                                 "--config", str(self.config_path), str(file or self.source), *args],
                                cwd=self.work, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
        self.assertNotIn(self.config["token"], result.stdout + result.stderr)
        self.assertNotIn("Traceback (most recent call last)", result.stdout + result.stderr)
        return result

    def test_submit_waits_for_demo_ac_once_and_browser_can_read_saved_source(self):
        result = self.command("submit", self.source, "--wait", "--timeout", "15")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("AC Accepted", result.stdout)
        self.assertIn("离线模拟", result.stdout)
        submit_posts = [entry for entry in self.terminal_requests if entry[:2] == ("POST", "/api/submit")]
        self.assertEqual(submit_posts, [("POST", "/api/submit", 201)])
        # The demo's first result refresh is terminal. The process must exit at
        # that response instead of scheduling another result poll.
        self.assertEqual(self.terminal_requests, [
            ("GET", "/api/state", 200), ("GET", "/api/problems/1", 200),
            ("POST", "/api/submit", 201), ("GET", "/api/state", 200),
        ])
        state = self.get(self.captain, "/api/state")
        self.assertEqual(len(state["submissions"]), 1)
        submitted = state["submissions"][0]
        self.assertEqual(submitted["verdict"], "AC")
        saved = self.get(self.captain, f"/api/submissions/{submitted['id']}/source")
        self.assertEqual(saved["source"], self.source_text)
        self.assertEqual(saved["filename"], "A.cpp")
        self.assertEqual(self.get(self.other_captain, "/api/state")["submissions"], [])
        with self.other_captain.get(self.url + f"/api/submissions/{submitted['id']}/source", timeout=5) as response:
            self.assertEqual(response.status_code, 404)
        with self.member.get(self.url + f"/api/submissions/{submitted['id']}/source", timeout=5) as response:
            self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.get(self.captain, "/api/state")["team"]["devices"]), 2)

    def test_printfile_stores_utf8_and_pdf_for_own_members_only(self):
        text_file = self.work / '比赛笔记.txt'
        text = '终端虚拟打印测试\n<script>alert("test only")</script>\n'
        text_file.write_bytes(text.encode("utf-8"))
        pdf_file = self.work / "diagram.pdf"
        pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
        pdf_file.write_bytes(pdf)
        for path in (text_file, pdf_file):
            result = self.command("printfile", path)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("虚拟打印已上传", result.stdout)
        uploads = [entry for entry in self.terminal_requests if entry[0] == "POST"]
        self.assertEqual(uploads, [("POST", "/api/prints", 201), ("POST", "/api/prints", 201)])
        files = self.get(self.captain, "/api/prints")["files"]
        self.assertEqual(len(files), 2)
        self.assertEqual(self.get(self.member, "/api/prints")["files"], files)
        self.assertEqual(self.get(self.other_captain, "/api/prints")["files"], [])
        for item in files:
            original = pdf if item["kind"] == "pdf" else text.encode("utf-8")
            self.assertEqual((self.work / "server-data" / "prints" / f"{item['id']}.bin").read_bytes(), original)
            with self.member.get(self.url + item["download_url"], timeout=5) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, original)
            with self.member.get(self.url + item["view_url"], timeout=5) as response:
                self.assertEqual(response.status_code, 200)
                if item["kind"] == "text":
                    self.assertIn("终端虚拟打印测试", response.text)
                    self.assertIn("&lt;script&gt;", response.text)
                    self.assertNotIn('<script>alert("test only")</script>', response.text)
                else:
                    self.assertEqual(response.content, pdf)
                    self.assertIn("application/pdf", response.headers["Content-Type"])
            for route in (item["view_url"], item["download_url"]):
                with self.other_captain.get(self.url + route, timeout=5) as response:
                    self.assertEqual(response.status_code, 404)

    def test_revoked_token_stops_commands_without_new_records(self):
        result = self.command("submit")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.post(self.captain, "/api/cli/revoke")
        before = self.get(self.captain, "/api/state")["submissions"]
        self.terminal_requests.clear()
        for name in ("submit", "printfile"):
            result = self.command(name)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("401", result.stderr)
        self.assertEqual(self.terminal_requests, [("GET", "/api/state", 401), ("GET", "/api/state", 401)])
        after = self.get(self.captain, "/api/state")["submissions"]
        self.assertEqual([item["id"] for item in after], [item["id"] for item in before])
        self.assertEqual(self.get(self.captain, "/api/prints")["files"], [])
        self.assertEqual(len(self.app.extensions["store"].submissions(self.run_id)), 1)

    def test_interrupted_chunked_response_after_acceptance_does_not_resubmit(self):
        backend_url = self.url

        class InterruptedResponse(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                size = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(size) if size else None
                headers = {key: self.headers[key] for key in ("Authorization", "Content-Type") if key in self.headers}
                with requests.Session() as relay:
                    relay.trust_env = False
                    with relay.request(self.command, backend_url + self.path, data=body,
                                       headers=headers, timeout=5) as response:
                        status, content = response.status_code, response.content
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if self.command == "POST":
                    self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    # Flask has stored and accepted the submission. Interrupt a
                    # declared 256-byte chunk to reproduce IncompleteRead at the CLI.
                    self.wfile.write(b"100\r\n" + content[:5])
                    self.wfile.flush()
                    self.close_connection = True
                else:
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)

        relay_server = ThreadingHTTPServer(("127.0.0.1", 0), InterruptedResponse)
        relay_worker = threading.Thread(target=relay_server.serve_forever,
                                        kwargs={"poll_interval": 0.02}, daemon=True)
        relay_worker.start()
        try:
            self.config["server_url"] = f"http://127.0.0.1:{relay_server.server_port}"
            self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
            result = self.command("submit")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("提交请求未重试", result.stderr)
            self.assertEqual(self.terminal_requests, [
                ("GET", "/api/state", 200), ("GET", "/api/problems/1", 200),
                ("POST", "/api/submit", 201),
            ])
            stored = self.app.extensions["store"].submissions(self.run_id)
            self.assertEqual(len(stored), 1)
            self.assertEqual(self.app.extensions["store"].submission_payload(stored[0]["id"])["source"],
                             self.source_text)
        finally:
            relay_server.shutdown()
            relay_server.server_close()
            relay_worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
