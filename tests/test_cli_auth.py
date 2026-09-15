"""Offline coverage of terminal credentials and the existing contest API boundary."""
import hashlib
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from app import create_app
from cli_auth import CLIAuthError
from test_native_api import NativeClient


SOURCE = {"problem_id": 1, "language": "C++17", "filename": "a.cpp", "source": "int main() {}\n"}


class CLIAuthTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        self.app = create_app(contest_id=1, demo=True, data_dir=self.data_dir)
        self.app.config["TESTING"] = True
        self.captain = self.app.test_client()
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("No network in CLI tests"))
        network.start()
        self.addCleanup(network.stop)

    def ok(self, response):
        self.assertIn(response.status_code, (200, 201), response.get_data(as_text=True))
        return response.get_json()

    def state(self, browser=None):
        return self.ok((browser or self.captain).get("/api/state"))

    def post(self, path, body=None, browser=None):
        browser = browser or self.captain
        return browser.post(path, json={} if body is None else body,
                            headers={"X-CSRF-Token": self.state(browser)["csrf_token"]})

    def team(self, browser=None, name="Terminal test team"):
        return self.ok(self.post("/api/team/create", {"name": name}, browser))["team"]

    def issue(self, browser=None):
        return self.ok(self.post("/api/cli/token", browser=browser))

    def terminal(self, method, path, token, body=None, browser=None, **options):
        headers = {"Authorization": "Bearer " + token, **options.pop("headers", {})}
        if body is not None:
            options["json"] = body
        return (browser or self.app.test_client()).open(path, method=method, headers=headers, **options)

    def start(self, browser=None):
        return self.ok(self.post("/api/start", browser=browser))

    def test_only_a_created_captain_can_manage_terminal_credentials(self):
        for method, path in (("GET", "/api/cli"), ("POST", "/api/cli/token"), ("POST", "/api/cli/revoke")):
            response = self.captain.get(path) if method == "GET" else self.post(path)
            self.assertEqual(response.status_code, 403)
        team = self.team()
        member = self.app.test_client()
        self.ok(self.post("/api/team/join", {"code": team["join_code"]}, member))
        for browser in (member,):
            self.assertEqual(browser.get("/api/cli").status_code, 403)
            self.assertEqual(self.post("/api/cli/token", browser=browser).status_code, 403)
            self.assertEqual(self.post("/api/cli/revoke", browser=browser).status_code, 403)
        member_device = self.state(member)["team"]["device_id"]
        self.ok(self.post("/api/team/revoke", {"device_id": member_device}))
        self.assertEqual(member.get("/api/cli").status_code, 403)
        self.assertEqual(self.post("/api/cli/token", browser=member).status_code, 403)
        self.assertEqual(self.ok(self.captain.get("/api/cli")), {"enabled": False, "created_at": None})

    def test_configuration_contains_no_browser_secret_and_only_digest_persists(self):
        team = self.team()
        issued = self.issue()
        config = issued["config"]
        self.assertEqual(set(config), {"version", "server_url", "token", "contest_id", "team_id", "team_name"})
        self.assertEqual(config["version"], 1)
        self.assertEqual(config["server_url"], "http://localhost")
        self.assertEqual(config["contest_id"], 1)
        self.assertEqual(config["team_id"], team["id"])
        self.assertEqual(config["team_name"], team["name"])
        with self.captain.session_transaction() as browser_session:
            self.assertNotIn(browser_session["sid"], str(issued))
            self.assertNotIn(browser_session["csrf"], str(issued))
        token = config["token"]
        with self.app.extensions["cli_tokens"].connect() as db:
            row = dict(db.execute("SELECT * FROM cli_tokens").fetchone())
        self.assertEqual(row["token_digest"], hashlib.sha256(token.encode("ascii")).hexdigest())
        self.assertNotIn(token, str(row))
        self.assertEqual(row["created_at"], issued["created_at"])
        status = self.ok(self.captain.get("/api/cli"))
        self.assertEqual(status, {"enabled": True, "created_at": issued["created_at"]})
        self.assertNotIn(token, str(status))
        self.assertEqual([device["id"] for device in self.state()["team"]["devices"]], [team["device_id"]])

    def test_rotation_invalidates_old_token_and_revocation_survives_restart(self):
        self.team()
        first = self.issue()["config"]["token"]
        second = self.issue()["config"]["token"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.terminal("GET", "/api/state", first).status_code, 401)
        self.assertEqual(self.terminal("GET", "/api/state", second).status_code, 200)
        restarted = create_app(contest_id=1, demo=True, data_dir=self.data_dir)
        self.assertEqual(self.terminal("GET", "/api/state", second, browser=restarted.test_client()).status_code, 200)
        self.ok(self.post("/api/cli/revoke"))
        self.assertEqual(self.terminal("GET", "/api/state", second).status_code, 401)
        self.assertEqual(self.terminal("GET", "/api/state", second, browser=restarted.test_client()).status_code, 401)
        self.assertEqual(self.ok(self.captain.get("/api/cli"))["enabled"], False)
        self.ok(self.post("/api/cli/revoke"))

    def test_authorization_is_bound_to_contest_and_captain_role_on_each_request(self):
        team = self.team()
        token = self.issue()["config"]["token"]
        different = create_app(contest_id=2, demo=True, data_dir=self.data_dir)
        self.assertEqual(self.terminal("GET", "/api/state", token, browser=different.test_client()).status_code, 401)
        with self.app.extensions["cli_tokens"].connect() as db:
            for contest_key in ("qoj:1", "demo:2"):
                with self.assertRaises(CLIAuthError):
                    self.app.extensions["cli_tokens"].authorize(token, contest_key)
            db.execute("UPDATE team_devices SET role='revoked' WHERE id=?", (team["device_id"],))
        self.assertEqual(self.terminal("GET", "/api/state", token).status_code, 401)
        self.assertEqual(self.terminal("POST", "/api/submit", token, SOURCE).status_code, 401)

    def test_mismatched_owner_binding_is_rejected_even_with_valid_digest(self):
        self.team()
        token = self.issue()["config"]["token"]
        with self.app.extensions["cli_tokens"].connect() as db:
            db.execute("UPDATE cli_tokens SET owner_sid='not-the-captain'")
        self.assertEqual(self.terminal("GET", "/api/state", token).status_code, 401)

    def test_malformed_or_forged_authorization_never_falls_back_to_browser(self):
        self.team()
        self.start()
        self.issue()
        csrf = self.state()["csrf_token"]
        for value in ("", "Basic abc", "Bearer", "Bearer not-valid", "Bearer qojcli_" + "a" * 43,
                      "Bearer qojcli_" + "a" * 43 + ",Bearer other"):
            with self.subTest(value=value):
                response = self.captain.post("/api/submit", json=SOURCE,
                                            headers={"Authorization": value, "X-CSRF-Token": csrf})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(self.captain.get("/api/state", headers={"Authorization": value}).status_code, 401)
        self.assertEqual(self.state()["submissions"], [])

    def test_bearer_cannot_use_management_proxy_files_or_unlisted_methods(self):
        self.team()
        token = self.issue()["config"]["token"]
        denied = [("GET", "/"), ("GET", "/healthz"), ("GET", "/api/team"), ("GET", "/api/cli"),
                  ("POST", "/api/cli/token"), ("POST", "/api/cli/revoke"), ("POST", "/api/start"),
                  ("POST", "/api/reset"), ("POST", "/api/mode"), ("POST", "/api/connect"),
                  ("POST", "/api/team/create"), ("POST", "/api/team/rotate"), ("POST", "/api/logout"),
                  ("GET", "/qoj/login"), ("GET", "/api/problems/1/statement"),
                  ("GET", "/api/problems/1/pdf/" + "a" * 24), ("GET", "/api/submissions/1/source"),
                  ("GET", "/api/prints/" + "a" * 32 + "/download"), ("GET", "/api/attachments"),
                  ("GET", "/static/index.html"), ("HEAD", "/api/state"), ("POST", "/api/state"),
                  ("DELETE", "/api/prints"), ("OPTIONS", "/api/submit")]
        for method, path in denied:
            with self.subTest(method=method, path=path):
                response = self.terminal(method, path, token, {} if method == "POST" else None)
                self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.state()["run"])

    def test_cli_does_not_mint_a_captain_browser_cookie_or_extra_device(self):
        self.team()
        token = self.issue()["config"]["token"]
        cli_browser = self.app.test_client()
        response = self.terminal("GET", "/api/state", token, browser=cli_browser)
        state = self.ok(response)
        self.assertFalse(response.headers.getlist("Set-Cookie"))
        self.assertEqual(state["csrf_token"], "")
        self.assertNotIn("join_code", state["team"])
        self.assertIsNone(self.state(cli_browser)["team"])
        self.assertEqual(len(self.state()["team"]["devices"]), 1)
        self.assertEqual(self.post("/api/cli/token", browser=cli_browser).status_code, 403)

    def test_cookie_identity_is_unchanged_when_cli_used_with_a_member_cookie(self):
        team = self.team()
        member = self.app.test_client()
        self.ok(self.post("/api/team/join", {"code": team["join_code"]}, member))
        token = self.issue()["config"]["token"]
        with member.session_transaction() as browser_session:
            before = dict(browser_session)
        response = self.terminal("GET", "/api/state", token, browser=member)
        self.assertEqual(self.ok(response)["team"]["role"], "captain")
        with member.session_transaction() as browser_session:
            self.assertEqual(dict(browser_session), before)
        self.assertEqual(self.state(member)["team"]["role"], "member")
        self.assertEqual(self.post("/api/cli/token", browser=member).status_code, 403)

    def test_csrf_and_origin_protection_is_preserved(self):
        self.team()
        token = self.issue()["config"]["token"]
        for path in ("/api/cli/token", "/api/cli/revoke", "/api/submit", "/api/prints"):
            self.assertEqual(self.captain.post(path, json={}).status_code, 403)
        for method, path in (("GET", "/api/state"), ("POST", "/api/prints")):
            response = self.terminal(method, path, token, {} if method == "POST" else None,
                                     headers={"Origin": "https://unrelated.example"})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.terminal("GET", "/api/state", token,
                                      headers={"Origin": "http://localhost"}).status_code, 200)
        self.assertEqual(self.terminal("POST", "/api/prints", token, data="content=hello").status_code, 415)

    def test_submit_print_and_reads_share_captain_run_without_cross_team_leaks(self):
        team_a = self.team()
        token_a = self.issue()["config"]["token"]
        run_a = self.start()["run"]["id"]
        second = self.app.test_client()
        self.team(second, "Other team")
        token_b = self.issue(second)["config"]["token"]
        self.start(second)
        problem = self.ok(self.terminal("GET", "/api/problems/1", token_a))
        self.assertEqual(problem["languages"][0]["value"], "C++17")
        submitted = self.ok(self.terminal("POST", "/api/submit", token_a, SOURCE))["submission"]
        self.assertEqual(self.state()["submissions"][0]["id"], submitted["id"])
        self.assertEqual(self.state(second)["submissions"], [])
        saved = self.app.extensions["store"].submission_payload(submitted["id"])
        self.assertEqual(saved["source"], SOURCE["source"])
        self.assertEqual(len(self.app.extensions["store"].submissions(run_a)), 1)
        printed = self.ok(self.terminal("POST", "/api/prints", token_a,
                                       {"name": "notes.txt", "content": "Only for my team"}))["file"]
        self.assertEqual(self.ok(self.terminal("GET", "/api/prints", token_b))["files"], [])
        self.assertEqual(self.ok(self.terminal("GET", "/api/prints", token_a))["files"][0]["id"], printed["id"])
        member = self.app.test_client()
        self.ok(self.post("/api/team/join", {"code": team_a["join_code"]}, member))
        self.assertIn(b"Only for my team", member.get(printed["view_url"]).data)
        self.assertEqual(second.get(printed["view_url"]).status_code, 404)
        self.assertEqual(self.ok(self.terminal("GET", "/api/state", token_b))["submissions"], [])

    def test_multipart_virtual_print_never_uses_an_os_printer(self):
        self.team()
        self.start()
        token = self.issue()["config"]["token"]
        content = b"%PDF-1.4\nCLI offline example\n%%EOF\n"
        response = self.terminal("POST", "/api/prints", token,
                                 data={"file": (BytesIO(content), "diagram.pdf")},
                                 content_type="multipart/form-data")
        response.request.input_stream.close()
        item = self.ok(response)["file"]
        with self.captain.get(item["download_url"]) as downloaded:
            self.assertEqual(downloaded.data, content)

    def test_submit_and_print_are_rejected_before_start_and_after_finish(self):
        self.team()
        token = self.issue()["config"]["token"]
        for ended in (False, True):
            if ended:
                self.start()
                self.ok(self.post("/api/finish"))
            for path, body in (("/api/submit", SOURCE), ("/api/prints", {"name": "a.txt", "content": "a"})):
                self.assertEqual(self.terminal("POST", path, token, body).status_code, 409)
        self.assertEqual(self.state()["submissions"], [])
        self.assertEqual(self.ok(self.captain.get("/api/prints"))["files"], [])

    def test_native_account_connection_and_upstream_permissions_remain_required(self):
        upstream = NativeClient(start_time=time.time() - 60)
        native_dir = self.data_dir / "native"
        native = create_app(contest_id=1802, demo=False, data_dir=native_dir, client_factory=lambda: upstream)
        self.app = native
        self.captain = native.test_client()
        self.team()
        token = self.issue()["config"]["token"]
        self.assertEqual(self.terminal("POST", "/api/submit", token, SOURCE).status_code, 409)
        self.assertEqual(self.terminal("GET", "/api/problems/1", token).status_code, 403)
        self.assertEqual(self.ok(self.terminal("GET", "/api/state", token))["contest"]["problems"], [])
        upstream.set_status("vp", "running")
        upstream.authenticated = False
        for method, path, body in (("GET", "/api/state", None), ("GET", "/api/problems/1", None),
                                   ("POST", "/api/submit", SOURCE),
                                   ("POST", "/api/prints", {"name": "n.txt", "content": "n"})):
            response = self.terminal(method, path, token, body)
            self.assertEqual(response.status_code, 401)
            self.assertIn("队长网页连接", response.get_json()["error"])
        upstream.authenticated = True
        upstream.set_status("vp", "finished")
        self.assertEqual(self.terminal("POST", "/api/submit", token, SOURCE).status_code, 409)
        self.assertEqual(self.terminal("POST", "/api/prints", token, {"name": "n.txt", "content": "n"}).status_code, 409)
        upstream.set_status("vp", "running")
        self.ok(self.terminal("POST", "/api/submit", token, SOURCE))
        self.assertEqual(len([call for call in upstream.calls if call[0] == "submit"]), 1)
        self.assertFalse([call for call in upstream.calls if call[0] == "enter"])


if __name__ == "__main__":
    unittest.main()
