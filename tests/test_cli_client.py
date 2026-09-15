"""Black-box terminal client checks using only a disposable loopback server."""
from __future__ import annotations

import copy
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_TOKEN = "qojcli_" + "x" * 43


class TerminalClientTests(unittest.TestCase):
    """Commands must send one authorized operation and never retry its POST."""

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.work = Path(self.directory.name)
        self.requests = []
        self.overrides = {}
        self.posted = False
        self.after_post_states = []
        self.state_reads_after_post = 0
        self.state = {
            "demo": False,
            "contest": {"id": 9001, "title": "Offline CLI fixture", "problems": [
                {"id": 101, "label": "A", "title": "Fixture addition"},
                {"id": 202, "label": "B", "title": "Fixture sequence"},
            ]},
            "team": {"id": "fixture-team", "role": "captain", "name": "Fixture team"},
            "permissions": {"manage": True, "submit": True, "print": True, "view_content": True},
            "participation": {"mode": "vp", "status": "running", "content_available": True,
                              "can_submit": True, "ends_at": time.time() + 3600},
            "run": {"id": 7, "status": "running", "ends_at": time.time() + 3600},
            "user": {"authenticated": True, "username": "fixture-account"},
            "submissions": [], "poll_interval": 3,
        }
        self.problem = {
            "id": 101, "label": "A", "submission_available": True,
            "languages": [{"value": "cpp20", "label": "GNU G++20"},
                          {"value": "cpp17", "label": "GNU G++17"},
                          {"value": "python3", "label": "Python 3"}],
        }
        self.receipt = {"id": 47, "problem_id": 101, "verdict": "PENDING",
                        "qoj_id": 7654321, "qoj_url": "https://qoj.ac/submission/7654321"}
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                entry = {"method": self.command, "path": self.path,
                         "headers": dict(self.headers.items()), "body": body,
                         "time": time.monotonic()}
                fixture.requests.append(entry)
                status, headers, payload = 200, {}, {}
                override = fixture.overrides.get((self.command, self.path))
                if override:
                    status, headers, payload = override
                elif self.command == "GET" and self.path == "/api/state":
                    payload = copy.deepcopy(fixture.state)
                    if fixture.posted:
                        fixture.state_reads_after_post += 1
                        if fixture.after_post_states:
                            index = min(fixture.state_reads_after_post - 1, len(fixture.after_post_states) - 1)
                            payload["submissions"] = fixture.after_post_states[index]
                elif self.command == "GET" and self.path in ("/api/problems/101", "/api/problems/202"):
                    payload = copy.deepcopy(fixture.problem)
                    payload["id"] = int(self.path.rsplit("/", 1)[-1])
                elif self.command == "POST" and self.path == "/api/submit":
                    fixture.posted = True
                    payload = {"submission": fixture.receipt}
                    status = 201
                elif self.command == "POST" and self.path == "/api/prints":
                    fixture.posted = True
                    payload = {"file": {"id": "fixture-print", "name": "notes.pdf", "size": 100,
                                        "view_url": "/api/prints/fixture-print/view",
                                        "download_url": "/api/prints/fixture-print/download"}}
                    status = 201
                else:
                    status, payload = 404, {"error": "Unexpected fixture request"}
                encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                # A CLI must not create a Cookie-based browser session even if one is offered.
                self.send_header("Set-Cookie", "fixture_browser_session=do-not-use; Path=/")
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.worker.start()
        self.addCleanup(self.close_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.config = {"version": 1, "server_url": self.url, "token": FIXTURE_TOKEN,
                       "contest_id": 9001, "team_id": "fixture-team", "team_name": "Fixture team"}
        self.config_path = self.work / "terminal-config.json"
        self.source = self.work / "A.cpp"
        self.source.write_text('// 中文源码\nint main() { return 0; }\n', encoding="utf-8")

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def command(self, command="submit", *args, file=None, timeout=12):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        script = ROOT / "cli" / f"{command}.py"
        self.assertTrue(script.is_file(), f"Missing terminal command: {script}")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
               "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        result = subprocess.run([sys.executable, str(script), "--config", str(self.config_path),
                                 *map(str, args), str(file or self.source)], cwd=self.work,
                                env=env, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout)
        self.assertNotIn(FIXTURE_TOKEN, result.stdout + result.stderr)
        self.assertNotIn("Traceback (most recent call last)", result.stdout + result.stderr)
        return result

    def posts(self):
        return [entry for entry in self.requests if entry["method"] == "POST"]

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_failure(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def submission_payload(self):
        posts = self.posts()
        self.assertEqual(len(posts), 1, [(r["method"], r["path"]) for r in self.requests])
        self.assertEqual(posts[0]["path"], "/api/submit")
        return json.loads(posts[0]["body"])

    def test_submit_infers_problem_and_cpp17_preserves_utf8_and_exits_without_polling(self):
        result = self.command()
        self.assert_success(result)
        payload = self.submission_payload()
        self.assertEqual(payload, {"problem_id": 101, "language": "cpp17",
                                  "source": self.source.read_bytes().decode("utf-8"), "filename": "A.cpp"})
        self.assertEqual(self.state_reads_after_post, 0)
        self.assertIn("47", result.stdout)
        for request in self.requests:
            headers = {key.lower(): value for key, value in request["headers"].items()}
            self.assertEqual(headers.get("authorization"), "Bearer " + FIXTURE_TOKEN)
            self.assertNotIn("cookie", headers)

    def test_short_options_choose_numeric_problem_and_exact_language(self):
        result = self.command("submit", "-p", "202", "-l", "cpp20")
        self.assert_success(result)
        payload = self.submission_payload()
        self.assertEqual(payload["problem_id"], 202)
        self.assertEqual(payload["language"], "cpp20")

    def test_non_ascii_source_filename_gets_safe_upload_name_without_renaming_local_file(self):
        source = self.work / "我的解法.cpp"
        original = b"\xef\xbb\xbf// UTF-8 BOM\nint main() {}\n"
        source.write_bytes(original)
        result = self.command("submit", "--problem", "A", file=source)
        self.assert_success(result)
        payload = self.submission_payload()
        self.assertRegex(payload["filename"], r"^[A-Za-z0-9][A-Za-z0-9_.+\-]*\.cpp$")
        self.assertEqual(payload["source"], original.decode("utf-8-sig"))
        self.assertEqual(source.read_bytes(), original)

    def test_unknown_or_ambiguous_problem_does_not_submit(self):
        for selection in ("C", "999"):
            with self.subTest(selection=selection):
                self.assert_failure(self.command("submit", "--problem", selection))
                self.assertEqual(self.posts(), [])
        self.state["contest"]["problems"].append({"id": 303, "label": "A", "title": "Ambiguous"})
        self.assert_failure(self.command())
        self.assertEqual(self.posts(), [])

    def test_unknown_or_ambiguous_language_does_not_submit(self):
        self.assert_failure(self.command("submit", "--language", "nonexistent"))
        self.assertEqual(self.posts(), [])
        self.problem["languages"] = [{"value": "cpp17a", "label": "GNU G++17 compiler one"},
                                     {"value": "cpp17b", "label": "GNU G++17 compiler two"}]
        self.assert_failure(self.command())
        self.assertEqual(self.posts(), [])

    def test_invalid_source_is_rejected_before_submit(self):
        for content in (b"", b"\xff\xfe\xfa", b"int\x00 main", b"x" * (256 * 1024 + 1)):
            with self.subTest(size=len(content)):
                self.source.write_bytes(content)
                self.assert_failure(self.command())
                self.assertEqual(self.posts(), [])

    def test_wrong_contest_team_or_role_blocks_both_commands(self):
        original = copy.deepcopy(self.state)
        cases = [("contest", "id", 9002), ("team", "id", "other-team"),
                 ("team", "role", "member"), ("team", "role", "solo"),
                 ("team", "role", "revoked")]
        for section, key, value in cases:
            self.state = copy.deepcopy(original)
            self.state[section][key] = value
            for command in ("submit", "printfile"):
                with self.subTest(section=section, key=key, value=value, command=command):
                    self.assert_failure(self.command(command))
                    self.assertEqual(self.posts(), [])

    def test_permission_and_contest_phase_block_operations(self):
        original = copy.deepcopy(self.state)
        cases = [("permissions", "submit", False), ("participation", "can_submit", False),
                 ("participation", "status", "scheduled"), ("participation", "status", "finished"),
                 ("run", "status", "finished")]
        for section, key, value in cases:
            with self.subTest(section=section, key=key, value=value):
                self.state = copy.deepcopy(original)
                self.state[section][key] = value
                self.assert_failure(self.command())
                self.assertEqual(self.posts(), [])
        self.state = copy.deepcopy(original)
        self.state["permissions"]["print"] = False
        self.assert_failure(self.command("printfile"))
        self.assertEqual(self.posts(), [])

    def test_demo_running_does_not_require_native_participation_snapshot(self):
        self.state["demo"] = True
        self.state.pop("participation")
        self.assert_success(self.command())
        self.submission_payload()

    def test_invalid_config_does_not_send_any_request(self):
        original = self.config.copy()
        cases = [("version", 2), ("contest_id", 0), ("team_id", ""),
                 ("token", "bad\r\nInjected: value"),
                 ("server_url", self.url.replace("http://", "http://user:password@")),
                 ("server_url", self.url + "/other-app")]
        for key, value in cases:
            with self.subTest(key=key):
                self.config = {**original, key: value}
                self.assert_failure(self.command())
                self.assertEqual(self.requests, [])

    def test_wait_matches_local_submission_and_stops_on_error_result(self):
        pending = {**self.receipt, "verdict": "PENDING"}
        unrelated = {"id": 48, "verdict": "AC", "qoj_id": 7654322}
        final = {**self.receipt, "verdict": "TLE", "message": "TL"}
        self.after_post_states = [[unrelated, pending], [unrelated, final]]
        result = self.command("submit", "--wait", "--interval", "1", "--timeout", "10", timeout=15)
        self.assert_success(result)
        self.submission_payload()
        self.assertEqual(self.state_reads_after_post, 2)
        self.assertRegex(result.stdout, r"TLE|Time Limit")
        polls = [r for r in self.requests if r["method"] == "GET" and r["path"] == "/api/state"]
        self.assertGreaterEqual(polls[-1]["time"] - polls[-2]["time"], 2.8)

    def test_all_terminal_error_aliases_stop_after_first_result(self):
        for verdict in ("AC", "WA", "TLE", "TL", "MLE", "ML", "RE", "RTE", "OLE", "OL", "CE", "PE", "JE", "JF", "IE"):
            with self.subTest(verdict=verdict):
                self.requests.clear()
                self.posted = False
                self.state_reads_after_post = 0
                self.after_post_states = [[{**self.receipt, "verdict": verdict}]]
                result = self.command("submit", "--wait", "--timeout", "5", timeout=9)
                self.assert_success(result)
                self.submission_payload()
                self.assertEqual(self.state_reads_after_post, 1)

    def test_wait_on_immediate_terminal_receipt_does_not_poll(self):
        self.receipt["verdict"] = "WA"
        result = self.command("submit", "--wait")
        self.assert_success(result)
        self.submission_payload()
        self.assertEqual(self.state_reads_after_post, 0)

    def test_timeout_is_finite_and_does_not_resubmit(self):
        self.after_post_states = [[self.receipt]]
        started = time.monotonic()
        result = self.command("submit", "--wait", "--timeout", "1", timeout=6)
        self.assert_failure(result)
        self.assertLess(time.monotonic() - started, 5)
        self.submission_payload()
        self.assertLessEqual(self.state_reads_after_post, 1)

    def test_post_errors_are_reported_without_retry(self):
        for command, path in (("submit", "/api/submit"), ("printfile", "/api/prints")):
            for status in (401, 409, 429, 502):
                with self.subTest(command=command, status=status):
                    self.requests.clear()
                    self.overrides = {("POST", path): (status, {}, {"error": "Fixture rejected operation"})}
                    result = self.command(command)
                    self.assert_failure(result)
                    self.assertEqual(len(self.posts()), 1)
                    self.assertIn("Fixture rejected operation", result.stdout + result.stderr)

    def test_get_and_post_redirects_are_not_followed(self):
        for method, path in (("GET", "/api/state"), ("POST", "/api/submit"), ("POST", "/api/prints")):
            for status in (302, 307):
                with self.subTest(method=method, path=path, status=status):
                    self.requests.clear()
                    self.overrides = {(method, path): (status, {"Location": self.url + "/redirect-target"}, {})}
                    command = "printfile" if path == "/api/prints" else "submit"
                    self.assert_failure(self.command(command))
                    self.assertFalse(any(r["path"] == "/redirect-target" for r in self.requests))
                    self.assertEqual(len(self.posts()), int(method == "POST"))

    def test_printfile_uploads_exact_pdf_bytes_as_one_multipart_file(self):
        pdf = self.work / "diagram.pdf"
        content = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
        pdf.write_bytes(content)
        result = self.command("printfile", "--name", "共享图示.pdf", file=pdf)
        self.assert_success(result)
        self.assertEqual(len(self.posts()), 1)
        post = self.posts()[0]
        self.assertEqual(post["path"], "/api/prints")
        headers = {key.lower(): value for key, value in post["headers"].items()}
        content_type = headers["content-type"]
        self.assertTrue(content_type.startswith("multipart/form-data;"))
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + post["body"])
        parts = list(message.iter_parts())
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].get_param("name", header="content-disposition"), "file")
        self.assertEqual(parts[0].get_filename(), "共享图示.pdf")
        self.assertEqual(parts[0].get_payload(decode=True), content)
        self.assertEqual(self.state_reads_after_post, 0)

    def test_printfile_rejects_invalid_extension_encoding_pdf_and_size(self):
        cases = [("program.exe", b"MZ binary"), ("bad.pdf", b"not a PDF"),
                 ("empty.txt", b""), ("binary.cpp", b"a\x00b"),
                 ("invalid.txt", b"\xff"), ("huge.txt", b"x" * (2 * 1024 * 1024 + 1))]
        for name, content in cases:
            with self.subTest(name=name):
                file = self.work / name
                file.write_bytes(content)
                self.assert_failure(self.command("printfile", file=file))
                self.assertEqual(self.posts(), [])

    def test_invalid_poll_options_fail_before_submission(self):
        for option, value in (("--interval", "0"), ("--interval", "nan"),
                              ("--timeout", "0"), ("--timeout", "inf")):
            with self.subTest(option=option, value=value):
                self.assert_failure(self.command("submit", "--wait", option, value))
                self.assertEqual(self.posts(), [])


if __name__ == "__main__":
    unittest.main()
