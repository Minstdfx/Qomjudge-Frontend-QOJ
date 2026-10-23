"""Joint starts use isolated accounts and real HTTP-shaped native VP fixtures."""
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from app import create_app
from joint_vp import JointVPError
from qoj import QOJClient
from test_joint_entry import JointEntryAdapter


class JointVPAPITests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.upstreams = []

        def factory():
            client = QOJClient()
            adapter = JointEntryAdapter()
            adapter.username = f"offline-joint-{len(self.upstreams) + 1}"
            adapter.start += len(self.upstreams)
            client.session.adapters.clear()
            client.session.mount("https://qoj.ac/", adapter)
            client.username = adapter.username
            client.authenticated = True
            self.upstreams.append((client, adapter))
            self.addCleanup(client.session.close)
            return client

        self.factory = factory
        network = patch("requests.adapters.HTTPAdapter.send", side_effect=AssertionError("Offline fixtures only"))
        network.start()
        self.addCleanup(network.stop)
        self.app = create_app(contest_id=9, data_dir=self.directory, client_factory=factory)
        self.app.config["TESTING"] = True
        self.addCleanup(self.app.extensions["joint_vp"].close)
        self.browsers = []
        self.tokens = {}
        self.teams = []
        for index in range(3):
            browser = self.app.test_client()
            state = browser.get("/api/state").get_json()
            self.tokens[id(browser)] = state["csrf_token"]
            self.browsers.append(browser)
            team = self.ok(self.post(browser, "/api/team/create", {"name": f"Fixture team {index + 1}"}))["team"]
            self.teams.append(team)
        self.host = self.browsers[0]

    def post(self, browser, path, body=None):
        return browser.post(path, json={} if body is None else body,
                            headers={"X-CSRF-Token": self.tokens[id(browser)]})

    def ok(self, response):
        self.assertIn(response.status_code, (200, 201, 202), response.get_data(as_text=True))
        return response.get_json()

    def action(self, browser, name, body=None):
        return self.post(browser, "/api/joint-vp/" + name, body)

    def room(self, browser=None):
        return self.ok((browser or self.host).get("/api/joint-vp"))["joint_vp"]

    def create_group(self):
        room = self.ok(self.action(self.host, "create"))["joint_vp"]
        for browser in self.browsers[1:]:
            self.ok(self.action(browser, "join", {"code": room["code"]}))
        return room

    def prepare(self, browser):
        return self.ok(self.action(browser, "prepare"))["joint_vp"]["self"]["plan"]

    def ready(self, browser):
        plan = self.prepare(browser)
        return self.ok(self.action(browser, "ready", {
            "fingerprint": plan["fingerprint"], "action": plan["action"]["id"],
            "values": {}, "consent": True,
        }))["joint_vp"]

    def ready_all(self):
        self.create_group()
        for browser in self.browsers:
            self.ready(browser)

    def wait_finished(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            room = self.room()
            if room["status"] != "starting":
                return room
            time.sleep(0.01)
        self.fail("Synthetic joint start did not finish")

    def test_three_native_accounts_start_once_with_own_clocks_and_original_board(self):
        self.ready_all()
        for client, adapter in self.upstreams:
            self.assertEqual(adapter.posts, [])
        self.assertTrue(self.room()["can_start"])
        self.assertEqual(self.action(self.host, "start").status_code, 202)
        room = self.wait_finished()
        self.assertEqual([team["status"] for team in room["members"]], ["started"] * 3)
        for browser, (client, adapter) in zip(self.browsers, self.upstreams):
            self.assertEqual(len(adapter.posts), 1)
            self.assertTrue(adapter.posts[0].url.endswith("/virtual/register"))
            state = self.ok(browser.get("/api/state"))
            self.assertEqual(state["participation"]["status"], "running")
            self.assertEqual(state["run"]["started_at"], adapter.start)
            self.assertEqual(state["run"]["ends_at"], adapter.start + 18000)
            self.assertEqual(state["scoreboard"], client.participation(9)["scoreboard"])
        self.assertEqual(self.action(self.host, "start").status_code, 409)
        self.assertEqual([len(adapter.posts) for _, adapter in self.upstreams], [1, 1, 1])

    def test_roles_csrf_and_member_projection_do_not_expose_authorizations(self):
        self.ready_all()
        member = self.app.test_client()
        self.tokens[id(member)] = self.ok(member.get("/api/state"))["csrf_token"]
        self.ok(self.post(member, "/api/team/join", {"code": self.teams[0]["join_code"], "device_name": "Reader"}))
        visible = self.room(member)
        self.assertEqual(len(visible["members"]), 3)
        text = str(visible)
        for private in ("owner_sid", "fingerprint", "_token", "fixture-confirmation-", "values"):
            self.assertNotIn(private, text)
        self.assertNotIn("code", visible)
        self.assertFalse(visible.get("can_start", False))
        for name in ("create", "join", "prepare", "ready", "unready", "start", "cancel", "leave", "refresh"):
            self.assertEqual(self.action(member, name).status_code, 403, name)
        self.assertEqual(self.action(self.browsers[1], "start").status_code, 403)
        self.assertEqual(self.action(self.browsers[1], "cancel").status_code, 403)
        self.assertEqual(self.host.post("/api/joint-vp/start", json={}).status_code, 403)

    def test_explicit_consent_and_all_teams_ready_are_required(self):
        self.create_group()
        plan = self.prepare(self.host)
        body = {"fingerprint": plan["fingerprint"], "action": plan["action"]["id"], "values": {}}
        self.assertEqual(self.action(self.host, "ready", body).status_code, 400)
        self.assertEqual(self.action(self.host, "start").status_code, 409)
        self.ready(self.host)
        self.assertEqual(self.action(self.host, "start").status_code, 409)
        self.assertTrue(all(not adapter.posts for _, adapter in self.upstreams))

    def test_ready_prevents_account_mode_and_manual_start_until_unready(self):
        self.create_group()
        self.ready(self.host)
        for path, body in (("/api/logout", {}), ("/api/connect", {"cookie": "offline=value"}),
                           ("/api/mode", {"mode": "official"}),
                           ("/api/start", {"mode": "vp", "action": "virtual-register:register_vp", "selection": {}}),
                           ("/api/team/dissolve", {})):
            blocked = self.post(self.host, path, body)
            self.assertEqual(blocked.status_code, 409, path)
            self.assertEqual(blocked.get_json()["code"], "joint_vp_locked")
        self.assertTrue(self.upstreams[0][0].authenticated)
        self.assertEqual(self.upstreams[0][0].username, self.upstreams[0][1].username)
        self.ok(self.action(self.host, "unready"))
        self.ok(self.post(self.host, "/api/mode", {"mode": "official"}))
        self.assertEqual(self.action(self.host, "prepare").status_code, 409)
        self.assertTrue(all(not adapter.posts for _, adapter in self.upstreams))

    def test_changed_confirmation_blocks_only_that_team_before_any_post(self):
        self.ready_all()
        self.upstreams[1][1].terms = "A new registration agreement"
        self.ok(self.action(self.host, "start"))
        room = self.wait_finished()
        self.assertEqual(room["members"][0]["status"], "started")
        self.assertIn(room["members"][1]["status"], {"error", "needs_confirmation"})
        self.assertEqual(room["members"][2]["status"], "started")
        self.assertEqual([len(adapter.posts) for _, adapter in self.upstreams], [1, 0, 1])

    def test_timeout_after_commit_is_read_only_reconciled_without_resending(self):
        self.ready_all()
        self.upstreams[1][1].post_failure = "timeout-after-commit"
        self.ok(self.action(self.host, "start"))
        self.wait_finished()
        second = self.browsers[1]
        self.assertEqual(self.room(second)["self"]["status"], "uncertain")
        self.assertEqual(self.post(second, "/api/start", {"mode": "vp"}).status_code, 409)
        self.ok(self.action(second, "refresh"))
        self.assertEqual(self.room(second)["self"]["status"], "started")
        self.assertEqual(len(self.upstreams[1][1].posts), 1)

    def test_group_cancel_and_team_dissolve_do_not_touch_other_accounts(self):
        self.create_group()
        self.ok(self.action(self.host, "cancel"))
        self.assertTrue(all(not adapter.posts for _, adapter in self.upstreams))
        self.ok(self.post(self.browsers[1], "/api/team/dissolve"))
        self.assertIsNone(self.room(self.browsers[1]))
        self.assertIsNotNone(self.ok(self.browsers[2].get("/api/team"))["team"])
        with self.app.extensions["teams"].connect() as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_manual_recovery_updates_joint_roster_after_read_only_confirmation(self):
        self.ready_all()
        adapter = self.upstreams[1][1]
        adapter.post_failure = "timeout"
        self.ok(self.action(self.host, "start"))
        self.wait_finished()
        second = self.browsers[1]
        self.assertEqual(self.room(second)["self"]["status"], "uncertain")
        self.ok(self.action(second, "refresh"))
        self.assertEqual(self.room(second)["self"]["status"], "error")
        self.assertEqual(len(adapter.posts), 1)
        adapter.post_failure = None
        self.ok(self.post(second, "/api/start", {"mode": "vp", "action": "virtual-register:register_vp", "selection": {}}))
        self.assertEqual(self.room(second)["self"]["status"], "started")
        self.assertEqual(self.room(second)["self"]["started_at"], adapter.start)
        self.assertEqual(len(adapter.posts), 2)  # One authorized manual retry, no automatic repeat.

    def test_restart_keeps_membership_but_drops_ready_without_upstream_posts(self):
        self.ready_all()
        old_room = self.room()["id"]
        self.app.extensions["joint_vp"].close()
        restarted = create_app(contest_id=9, data_dir=self.directory, client_factory=self.factory)
        self.addCleanup(restarted.extensions["joint_vp"].close)
        browser = restarted.test_client()
        browser.set_cookie("qoj_vp_session", self.host.get_cookie("qoj_vp_session").value)
        room = self.ok(browser.get("/api/joint-vp"))["joint_vp"]
        self.assertEqual(room["id"], old_room)
        self.assertFalse(room["can_start"])
        self.assertNotIn(room["self"]["status"], {"ready", "starting"})
        self.assertTrue(all(not adapter.posts for _, adapter in self.upstreams))

    def test_roster_change_cannot_mask_successful_manual_native_entry(self):
        self.create_group()
        self.prepare(self.host)
        service = self.app.extensions["joint_vp"]
        with patch.object(service, "refresh", side_effect=JointVPError("Group changed during entry", 404)):
            result = self.ok(self.post(self.host, "/api/start", {
                "mode": "vp", "action": "virtual-register:register_vp", "selection": {},
            }))
        self.assertEqual(result["participation"]["status"], "running")
        self.assertEqual(len(self.upstreams[0][1].posts), 1)


if __name__ == "__main__":
    unittest.main()
