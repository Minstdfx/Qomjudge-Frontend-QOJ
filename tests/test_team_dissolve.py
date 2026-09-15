"""Dissolving a team detaches devices without destroying contest history."""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app import create_app
from cli_auth import CLIAuthError, CLITokenStore
from teams import TeamError, TeamStore


class TeamDissolveStorageTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "teams.sqlite3"
        self.teams = TeamStore(self.path)
        self.key = "demo:9001"

    def test_standalone_store_removes_all_devices_and_keeps_other_teams(self):
        team = self.teams.create("captain", self.key, "Dissolve fixture")
        first = self.teams.join("member-one", self.key, team["join_code"], "First")
        self.teams.join("member-two", self.key, team["join_code"], "Second")
        current = self.teams.revoke("captain", self.key, first["device_id"])
        self.teams.join("replacement", self.key, current["join_code"], "Replacement")
        other_team = self.teams.create("other-captain", self.key, "Other team")
        other_contest = self.teams.create("captain", "demo:9002", "Other contest")
        with self.teams.connect() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='cli_tokens'").fetchone())

        self.teams.dissolve("captain", self.key)

        for sid in ("captain", "member-one", "member-two", "replacement"):
            with self.subTest(sid=sid):
                self.assertIsNone(self.teams.access(sid, self.key))
        with self.teams.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM team_devices WHERE team_id=?", (team["id"],)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM teams WHERE id=?", (team["id"],)).fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.teams.access("other-captain", self.key)["id"], other_team["id"])
        self.assertEqual(self.teams.access("captain", "demo:9002")["id"], other_contest["id"])
        with self.assertRaises(TeamError) as raised:
            self.teams.join("new-member", self.key, current["join_code"], "New")
        self.assertEqual(raised.exception.status, 404)
        self.assertNotEqual(self.teams.create("captain", self.key, "New team")["id"], team["id"])
        self.assertEqual(self.teams.create("member-one", self.key, "Former member team")["role"], "captain")

    def test_only_current_captain_can_dissolve(self):
        team = self.teams.create("captain", self.key, "Protected team")
        member = self.teams.join("member", self.key, team["join_code"], "Member")
        for sid in ("solo", "member"):
            with self.subTest(sid=sid):
                with self.assertRaises(TeamError) as raised:
                    self.teams.dissolve(sid, self.key)
                self.assertEqual(raised.exception.status, 403)
                self.assertEqual(self.teams.access("captain", self.key)["id"], team["id"])
        self.teams.revoke("captain", self.key, member["device_id"])
        with self.assertRaises(TeamError) as raised:
            self.teams.dissolve("member", self.key)
        self.assertEqual(raised.exception.status, 403)
        # Ownership alone cannot authorize a device whose captain role is gone.
        with self.teams.connect() as db:
            db.execute("UPDATE team_devices SET role='revoked' WHERE id=?", (team["device_id"],))
        with self.assertRaises(TeamError) as raised:
            self.teams.dissolve("captain", self.key)
        self.assertEqual(raised.exception.status, 403)
        self.assertIsNotNone(self.teams.access("captain", self.key))

    def test_cli_and_devices_are_removed_atomically_and_rollback_together(self):
        team = self.teams.create("captain", self.key, "Atomic team")
        self.teams.join("member", self.key, team["join_code"], "Member")
        tokens = CLITokenStore(self.path)
        token = tokens.issue("captain", team["id"], self.key)["token"]
        with self.teams.connect() as db:
            db.execute("""CREATE TRIGGER refuse_team_deletion BEFORE DELETE ON teams
                       BEGIN SELECT RAISE(ABORT, 'fixture deletion failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.teams.dissolve("captain", self.key)
        self.assertEqual(tokens.authorize(token, self.key)["team_id"], team["id"])
        self.assertEqual(self.teams.access("member", self.key)["role"], "member")
        self.assertEqual(self.teams.access("captain", self.key)["role"], "captain")
        with self.teams.connect() as db:
            db.execute("DROP TRIGGER refuse_team_deletion")
        self.teams.dissolve("captain", self.key)
        with self.assertRaises(CLIAuthError) as raised:
            tokens.authorize(token, self.key)
        self.assertEqual(raised.exception.status, 401)
        with self.teams.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cli_tokens").fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


class TeamDissolveAPITests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        self.app = create_app(contest_id=9001, demo=True, data_dir=self.data_dir)
        self.app.config["TESTING"] = True
        self.captain = self.app.test_client()
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("No network in dissolution tests"))
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

    def create_team(self, name="Dissolve API fixture", browser=None):
        return self.ok(self.post("/api/team/create", {"name": name}, browser))["team"]

    def join(self, code):
        browser = self.app.test_client()
        self.ok(self.post("/api/team/join", {"code": code, "device_name": "Fixture member"}, browser))
        return browser

    def issue(self, browser=None):
        return self.ok(self.post("/api/cli/token", browser=browser))["config"]["token"]

    def terminal_state(self, token):
        return self.app.test_client().get("/api/state", headers={"Authorization": "Bearer " + token})

    def test_captain_can_dissolve_after_start_preserving_history_files_and_runtime(self):
        team = self.create_team()
        members = [self.join(team["join_code"]), self.join(team["join_code"])]
        run = self.ok(self.post("/api/start"))["run"]
        source = "// Dissolution regression\nint main() {}\n"
        submission = self.ok(self.post("/api/submit", {"problem_id": 1, "language": "C++17",
                                                     "source": source, "filename": "A.cpp"}))["submission"]
        self.app.extensions["store"].update_submission(submission["id"], verdict="AC")
        printed = self.ok(self.post("/api/prints", {"name": "shared.txt", "content": "Shared fixture notes"}))["file"]
        token = self.issue()
        with self.captain.session_transaction() as session:
            sid = session["sid"]
        runtime = self.app.extensions["clients"][sid]
        print_store = self.app.extensions["prints"]
        _item, file_path = print_store.get(sid, "demo:9001", run["id"], printed["id"])
        file_bytes = file_path.read_bytes()
        with self.app.extensions["store"].connect() as db:
            stored_run = dict(db.execute("SELECT * FROM runs WHERE id=?", (run["id"],)).fetchone())
            stored_payload = dict(db.execute("SELECT * FROM submission_payloads WHERE submission_id=?", (submission["id"],)).fetchone())

        dissolved = self.ok(self.post("/api/team/dissolve"))

        self.assertIsNone(dissolved["team"])
        self.assertEqual(dissolved["run"]["id"], run["id"])
        self.assertEqual(dissolved["run"]["status"], "running")
        self.assertEqual(dissolved["run"]["started_at"], run["started_at"])
        self.assertEqual([s["id"] for s in dissolved["submissions"]], [submission["id"]])
        self.assertIs(self.app.extensions["clients"][sid], runtime)
        self.assertEqual(self.terminal_state(token).status_code, 401)
        self.assertEqual(file_path.read_bytes(), file_bytes)
        with self.app.extensions["store"].connect() as db:
            self.assertEqual(dict(db.execute("SELECT * FROM runs WHERE id=?", (run["id"],)).fetchone()), stored_run)
            self.assertEqual(dict(db.execute("SELECT * FROM submission_payloads WHERE submission_id=?", (submission["id"],)).fetchone()), stored_payload)
            self.assertIsNotNone(db.execute("SELECT id FROM print_files WHERE id=?", (printed["id"],)).fetchone())
        captain_files = self.ok(self.captain.get("/api/prints"))["files"]
        self.assertEqual([item["id"] for item in captain_files], [printed["id"]])
        code = self.ok(self.captain.get(f'/api/submissions/{submission["id"]}/source'))
        self.assertEqual(code["source"], source)
        for member in members:
            with self.subTest(member=id(member)):
                current = self.state(member)
                self.assertIsNone(current["team"])
                self.assertIsNone(current["run"])
                self.assertEqual(current["submissions"], [])
                self.assertEqual(self.ok(member.get("/api/prints"))["files"], [])
                self.assertEqual(member.get(printed["view_url"]).status_code, 404)
                self.assertEqual(self.post("/api/team/join", {"code": team["join_code"]}, member).status_code, 404)
        new_team = self.create_team(name="Recreated team")
        self.assertNotEqual(new_team["id"], team["id"])
        self.assertEqual(self.state()["run"]["id"], run["id"])
        self.assertEqual(self.create_team(name="Former member team", browser=members[0])["role"], "captain")
        self.ok(self.post("/api/team/join", {"code": new_team["join_code"]}, members[1]))
        self.assertEqual(self.state(members[1])["run"]["id"], run["id"])

    def test_member_solo_revoked_and_bearer_cannot_dissolve(self):
        team = self.create_team()
        member = self.join(team["join_code"])
        solo = self.app.test_client()
        token = self.issue()
        for browser in (member, solo):
            with self.subTest(role="member" if browser is member else "solo"):
                self.assertEqual(self.post("/api/team/dissolve", browser=browser).status_code, 403)
                self.assertEqual(self.state()["team"]["id"], team["id"])
        bearer = self.app.test_client().post("/api/team/dissolve", json={},
                                            headers={"Authorization": "Bearer " + token})
        self.assertEqual(bearer.status_code, 403)
        self.assertEqual(self.terminal_state(token).status_code, 200)
        member_id = self.state(member)["team"]["device_id"]
        self.ok(self.post("/api/team/revoke", {"device_id": member_id}))
        self.assertEqual(self.post("/api/team/dissolve", browser=member).status_code, 403)
        self.assertEqual(self.state()["team"]["id"], team["id"])

    def test_browser_csrf_required_and_repeat_dissolve_is_rejected(self):
        team = self.create_team()
        self.assertEqual(self.captain.post("/api/team/dissolve", json={}).status_code, 403)
        csrf = self.state()["csrf_token"]
        self.assertEqual(self.captain.post("/api/team/dissolve", json={},
                                          headers={"X-CSRF-Token": csrf, "Origin": "http://other-host.invalid"}).status_code, 403)
        self.assertEqual(self.state()["team"]["id"], team["id"])
        self.assertIsNone(self.ok(self.post("/api/team/dissolve"))["team"])
        self.assertEqual(self.post("/api/team/dissolve").status_code, 403)

    def test_other_team_members_token_and_run_remain_unchanged(self):
        self.create_team()
        self.ok(self.post("/api/start"))
        first_token = self.issue()
        other = self.app.test_client()
        other_team = self.create_team(name="Unaffected fixture team", browser=other)
        other_member = self.join(other_team["join_code"])
        other_run = self.ok(self.post("/api/start", browser=other))["run"]
        other_token = self.issue(other)

        self.ok(self.post("/api/team/dissolve"))

        self.assertEqual(self.terminal_state(first_token).status_code, 401)
        self.assertEqual(self.terminal_state(other_token).status_code, 200)
        for browser in (other, other_member):
            current = self.state(browser)
            self.assertEqual(current["team"]["id"], other_team["id"])
            self.assertEqual(current["run"]["id"], other_run["id"])
            self.assertEqual(current["run"]["status"], "running")
        self.assertEqual(self.state(other)["team"]["join_code"], other_team["join_code"])


if __name__ == "__main__":
    unittest.main()
