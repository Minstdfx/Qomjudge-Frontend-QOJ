"""Offline HTTP checks for the complete virtual-contest workflow."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app import create_app
from demo import DemoClient
from requests import Response
from native_fixture import ActiveNativeFixture


class VirtualContestAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = create_app(contest_id=1802, demo=True,
                              data_dir=Path(self.directory.name))
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def state(self, client=None):
        response = (client or self.client).get("/api/state")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.is_json)
        return response.get_json()

    def post(self, path, payload=None, client=None, headers=None):
        client = client or self.client
        token = self.state(client)["csrf_token"]
        request_headers = {"X-CSRF-Token": token}
        request_headers.update(headers or {})
        return client.post(path, json={} if payload is None else payload,
                           headers=request_headers)

    def start(self, name="Local team", client=None):
        response = self.post("/api/start", {"team_name": name}, client=client)
        self.assertIn(response.status_code, (200, 201), response.get_data(as_text=True))
        return self.state(client)

    @staticmethod
    def source(**changes):
        payload = {"problem_id": 1, "language": "C++17",
                   "source": "#include <iostream>\nint main() { std::cout << 8; }\n",
                   "filename": "main.cpp"}
        payload.update(changes)
        return payload

    def test_initial_state_and_problem_are_available_without_network(self):
        state = self.state()
        for field in ("csrf_token", "contest", "run", "user", "submissions", "scoreboard"):
            self.assertIn(field, state)
        self.assertTrue(state["csrf_token"])
        self.assertIsNone(state["run"])
        self.assertEqual(state["submissions"], [])
        self.assertEqual(state["contest"]["id"], 1802)
        self.assertEqual(len(state["contest"]["problems"]), 8)
        problem = self.client.get("/api/problems/1")
        self.assertEqual(problem.status_code, 200)
        self.assertTrue(problem.is_json)
        statement = self.client.get("/api/problems/1/statement")
        self.assertEqual(statement.status_code, 200)
        self.assertIn("text/html", statement.content_type)
        self.assertIn("A + B", statement.get_data(as_text=True))

    def test_duplicate_start_does_not_reset_existing_clock(self):
        initial = self.start()
        duplicate = self.post("/api/start", {"team_name": "Accidental click"})
        self.assertEqual(duplicate.status_code, 409)
        current = self.state()
        self.assertEqual(current["run"]["id"], initial["run"]["id"])
        self.assertEqual(current["run"]["started_at"], initial["run"]["started_at"])

    def test_submission_requires_active_contest_and_finish_stops_submitting(self):
        self.assertEqual(self.post("/api/submit", self.source()).status_code, 409)
        self.start()
        finished = self.post("/api/finish")
        self.assertEqual(finished.status_code, 200)
        self.assertEqual(self.post("/api/submit", self.source()).status_code, 409)
        self.assertEqual(self.state()["submissions"], [])

    def test_reset_keeps_old_history_but_starts_empty_current_run(self):
        old = self.start()
        submitted = self.post("/api/submit", self.source())
        self.assertIn(submitted.status_code, (200, 201, 202))
        self.assertEqual(len(self.state()["submissions"]), 1)
        reset = self.post("/api/reset", {"team_name": "Second attempt"})
        self.assertIn(reset.status_code, (200, 201))
        current = self.state()
        self.assertNotEqual(current["run"]["id"], old["run"]["id"])
        self.assertEqual(current["submissions"], [])
        store = self.app.extensions["store"]
        self.assertEqual(len(store.submissions(old["run"]["id"])), 1)
        own_rows = [row for row in current["scoreboard"] if row["is_self"]]
        self.assertEqual(len(own_rows), 1)
        self.assertEqual(own_rows[0]["team_name"], "Second attempt")

    def test_browsers_share_standings_but_keep_runs_and_submissions_separate(self):
        alice = self.start("Alice")
        other_client = self.app.test_client()
        self.assertIsNone(self.state(other_client)["run"])
        bob = self.start("Bob", client=other_client)
        self.assertNotEqual(alice["run"]["id"], bob["run"]["id"])
        self.assertIn(self.post("/api/submit", self.source()).status_code, (200, 201, 202))
        self.assertEqual(len(self.state()["submissions"]), 1)
        self.assertEqual(self.state(other_client)["submissions"], [])
        for client, own_name in ((self.client, "Alice"), (other_client, "Bob")):
            board = self.state(client)["scoreboard"]
            self.assertEqual({r["team_name"] for r in board}, {"Alice", "Bob"})
            self.assertEqual([r["team_name"] for r in board if r["is_self"]], [own_name])

    def test_expired_contest_is_rejected_on_server(self):
        run_id = self.start()["run"]["id"]
        store = self.app.extensions["store"]
        with store.connect() as database:
            database.execute("UPDATE runs SET ends_at=0 WHERE id=?", (run_id,))
        response = self.post("/api/submit", self.source())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.state()["submissions"], [])

    def test_csrf_and_origin_protection_prevent_mutations(self):
        self.state()
        for headers in ({}, {"X-CSRF-Token": "wrong-token"}):
            response = self.client.post("/api/start", json={"team_name": "CSRF"}, headers=headers)
            self.assertEqual(response.status_code, 403)
        response = self.post("/api/start", {"team_name": "Cross origin"},
                             headers={"Origin": "https://attacker.example"})
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.state()["run"])
        valid = self.post("/api/start", {"team_name": "Same origin"},
                          headers={"Origin": "http://localhost"})
        self.assertIn(valid.status_code, (200, 201))

    def test_invalid_problem_language_and_source_do_not_create_submissions(self):
        self.start()
        for payload in (self.source(problem_id=99999), self.source(language="shell"),
                        self.source(source=""), self.source(source=None),
                        self.source(problem_id="not-an-id")):
            with self.subTest(payload=payload):
                response = self.post("/api/submit", payload)
                self.assertIn(response.status_code, (400, 404, 422), response.get_data(as_text=True))
        self.assertEqual(self.state()["submissions"], [])
        self.assertEqual(self.client.get("/api/problems/99999").status_code, 404)

    def test_demo_submission_is_explicitly_simulated_and_becomes_accepted(self):
        run_id = self.start()["run"]["id"]
        response = self.post("/api/submit", self.source())
        self.assertIn(response.status_code, (200, 201, 202), response.get_data(as_text=True))
        rows = self.state()["submissions"]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["qoj_id"])
        self.assertTrue(rows[0]["message"])
        # Advance only the persisted submission age; no sleeping or network needed.
        store = self.app.extensions["store"]
        with store.connect() as database:
            database.execute("UPDATE submissions SET submitted_at=submitted_at-60 WHERE run_id=?", (run_id,))
        for runtime in self.app.extensions["clients"].values():
            runtime["last_poll"] = 0
        complete = self.state()
        self.assertEqual(complete["submissions"][0]["verdict"], "AC")
        self.assertEqual(next(r for r in complete["scoreboard"] if r["is_self"])["solved"], 1)

    def test_client_deadline_and_source_size_cannot_override_server_rules(self):
        self.start()
        for changes in ({"source": "x" * (256 * 1024 + 1)}, {"problem_id": True},
                        {"filename": "../../solution.cpp"}, {"language": ["C++17"]}):
            with self.subTest(changes=list(changes)):
                response = self.post("/api/submit", self.source(**changes))
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.state()["submissions"], [])

    def test_json_shape_and_content_type_are_validated(self):
        for payload in ([], "string", 42):
            response = self.post("/api/start", payload)
            self.assertEqual(response.status_code, 400)
        token = self.state()["csrf_token"]
        response = self.client.post("/api/start", data="team_name=Local",
                                    headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 415)


class CapturingProxyClient(ActiveNativeFixture, DemoClient):
    def __init__(self):
        super().__init__()
        self.calls = []

    def fetch(self, method, target, headers=None, data=None, **kwargs):
        self.calls.append((method, target, headers, data))
        response = Response()
        response.status_code = 200
        response.url = "https://qoj.ac" + target
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response.headers["Set-Cookie"] = "upstream_secret=must-stay-server-side"
        response._content = b'<html><body><a href="/contest/1802">Contest</a></body></html>'
        return response


class ProxyProtectionTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.upstream = CapturingProxyClient()
        self.app = create_app(contest_id=1802, data_dir=Path(directory.name),
                              client_factory=lambda: self.upstream)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_native_proxy_post_requires_origin_or_same_site_referrer(self):
        for headers in ({}, {"Origin": "https://attacker.example"},
                        {"Referer": "https://attacker.example/qoj/login"}):
            response = self.client.post("/qoj/login", data={"username": "test"}, headers=headers)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.upstream.calls, [])
        response = self.client.post("/qoj/login", data={"username": "test"},
                                    headers={"Referer": "http://localhost/qoj/login"})
        self.assertEqual(response.status_code, 200)
        method, target, headers, body = self.upstream.calls[0]
        self.assertEqual((method, target), ("POST", "/login"))
        self.assertEqual(headers["Origin"], "https://qoj.ac")
        self.assertEqual(headers["Referer"], "https://qoj.ac/login")
        self.assertIn(b"username=test", body)

    def test_proxy_rewrites_local_links_and_keeps_upstream_cookies_server_side(self):
        response = self.client.get("/qoj/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn('/qoj/contest/1802', response.get_data(as_text=True))
        self.assertNotIn("upstream_secret", " ".join(response.headers.getlist("Set-Cookie")))
        self.assertNotIn("Cookie", self.upstream.calls[0][2])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")


if __name__ == "__main__":
    unittest.main()
