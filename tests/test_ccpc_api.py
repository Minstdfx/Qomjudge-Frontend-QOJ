"""CCPC redaction at the HTTP boundary, using synthetic accounts and clocks."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app import create_app
from test_native_api import NativeClient
from test_external_scoreboard_api import ExternalFixtureClient
from test_training_results_api import TrainingFixtureClient


def accepted(clock="0:10", first=False):
    return {"solved": True, "pending": False, "attempts": 1, "time": clock,
            "display": "+\n" + clock, "text": "+\n" + clock,
            "first_blood": first, "qoj_id": 987654321}


class CCPCAPITests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.now = 1_800_000_100.0
        clock = patch("app.time.time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Offline only"))
        network.start()
        self.addCleanup(network.stop)
        self.upstream = NativeClient(self.now - 100)
        self.upstream.contest_data["problems"] = [
            {"id": 10 + index, "label": label, "title": "Synthetic " + label, "color": "#fe00ee"}
            for index, label in enumerate("ABC")
        ]
        self.upstream.set_status("vp", "running")
        self.snapshot = self.upstream.snapshots["vp"]
        self.snapshot.update(ruleset="ICPC", scoreboard_source="qoj", scoreboard_available=True,
                             scoreboard_problem_totals={"11": {"solved": 999, "submissions": 9999}})
        self.snapshot["scoreboard"] = [
            {"rank": 1, "team_name": "Alpha", "username": self.upstream.username,
             "solved": 1, "penalty": "0:10", "is_self": True,
             "problem_stats": {"10": accepted(first=True), "11": {
                 "solved": False, "pending": False, "attempts": 1, "display": "-1", "time": ""}}},
            {"rank": 2, "team_name": "Beta", "username": "offline-beta", "solved": 1,
             "penalty": "0:20", "is_self": False, "problem_stats": {
                 "10": accepted("0:20"), "11": {"solved": True, "pending": True,
                 "attempts": 2, "display": "?2", "first_blood": True, "qoj_id": 987654322}}},
            {"rank": 3, "team_name": "Gamma", "username": "offline-gamma", "solved": 0,
             "penalty": "0:00", "is_self": False, "problem_stats": {}},
        ]

    def make_app(self, **options):
        settings = {"scoreboard_style": "ccpc", "ccpc_unlock": 3,
                    "ccpc_unlock_overrides": "A=2,C=0"}
        settings.update(options)
        server = create_app(contest_id=1802, data_dir=self.directory,
                            client_factory=lambda: self.upstream, **settings)
        server.config["TESTING"] = True
        self.addCleanup(server.extensions["joint_vp"].close)
        return server

    def state(self, browser):
        response = browser.get("/api/state")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def post(self, browser, path, body):
        token = self.state(browser)["csrf_token"]
        response = browser.post(path, json=body, headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def labels(self, state):
        return {p["label"] for p in state["scoreboard_display"]["revealed_problems"]}

    def test_public_json_is_redacted_but_own_details_and_upstream_ranking_remain(self):
        browser = self.make_app().test_client()
        state = self.state(browser)
        self.assertEqual(self.labels(state), {"A", "C"})
        self.assertEqual(set(state["participation"]["scoreboard_problem_totals"]), {"10", "12"})
        self.assertEqual(state["run"]["problem_stats"], self.snapshot["scoreboard"][0]["problem_stats"])
        for original, public in zip(self.snapshot["scoreboard"], state["scoreboard"]):
            for field in ("rank", "team_name", "solved", "penalty"):
                self.assertEqual(public[field], original[field])
            self.assertNotIn("problem_stats", public)
            self.assertNotIn("98765432", str(public))
            for cell in public["ccpc_cells"]:
                if not cell["revealed"]:
                    self.assertNotIn("problem_id", cell)
                    self.assertNotIn("label", cell)
                self.assertNotIn("qoj_id", cell)
        pending = next(cell for cell in state["scoreboard"][1]["ccpc_cells"] if cell["status"] == "pending")
        self.assertFalse(pending["revealed"])
        self.assertEqual(pending["display"], "?2")
        self.assertFalse(pending["first_blood"])

    def test_actual_threshold_reveals_and_survives_decreased_count_and_restart(self):
        first_app = self.make_app()
        browser = first_app.test_client()
        self.assertNotIn("B", self.labels(self.state(browser)))
        for row in self.snapshot["scoreboard"]:
            row["problem_stats"]["11"] = accepted("1:00")
        revealed = self.state(browser)
        self.assertEqual(self.labels(revealed), {"A", "B", "C"})
        for row in self.snapshot["scoreboard"]:
            row["problem_stats"].pop("11")
        self.assertIn("B", self.labels(self.state(browser)))
        restarted = self.make_app().test_client()
        restarted.set_cookie("qoj_vp_session", browser.get_cookie("qoj_vp_session").value)
        self.assertIn("B", self.labels(self.state(restarted)))
        self.snapshot["started_at"] += 1
        self.assertNotIn("B", self.labels(self.state(restarted)))

    def test_member_uses_captain_reveal_scope_and_has_no_write_permission(self):
        server = self.make_app()
        captain = server.test_client()
        created = self.post(captain, "/api/team/create", {"name": "Synthetic CCPC"})
        member = server.test_client()
        joined = self.post(member, "/api/team/join", {"code": created["team"]["join_code"]})
        self.assertEqual(joined["team"]["role"], "member")
        self.assertEqual(joined["scoreboard"], self.state(captain)["scoreboard"])
        self.assertFalse(joined["permissions"]["submit"])
        self.assertEqual(joined["scoreboard_display"], self.state(captain)["scoreboard_display"])

    def test_explicit_freeze_reveals_ids_without_replacing_frozen_verdicts(self):
        browser = self.make_app().test_client()
        self.snapshot["scoreboard_frozen"] = True
        state = self.state(browser)
        self.assertEqual(self.labels(state), {"A", "B", "C"})
        pending = next(c for c in state["scoreboard"][1]["ccpc_cells"] if c.get("label") == "B")
        self.assertEqual((pending["status"], pending["display"]), ("pending", "?2"))
        self.assertEqual(state["scoreboard"][1]["solved"], 1)

    def test_reveal_after_uses_qoj_start_and_finished_does_not_unfreeze_results(self):
        browser = self.make_app(ccpc_reveal_after=10).test_client()
        self.now = self.snapshot["started_at"] + 599
        self.assertNotIn("B", self.labels(self.state(browser)))
        self.now += 1
        state = self.state(browser)
        self.assertIn("B", self.labels(state))
        self.assertEqual(state["participation"]["ends_at"], self.snapshot["ends_at"])
        self.snapshot["started_at"] += 1
        self.snapshot["status"] = "finished"
        state = self.state(browser)
        self.assertIn("B", self.labels(state))
        self.assertTrue(any(c["display"] == "?2" for c in state["scoreboard"][1]["ccpc_cells"]))

    def test_source_or_account_switch_does_not_reuse_reveals(self):
        browser = self.make_app().test_client()
        self.snapshot["scoreboard_frozen"] = True
        self.assertIn("B", self.labels(self.state(browser)))
        self.snapshot["scoreboard_frozen"] = False
        self.snapshot["scoreboard_source"] = "qoj-pc2"
        changed = self.state(browser)
        self.assertNotIn("B", self.labels(changed))
        self.assertFalse(any(c["first_blood"] for r in changed["scoreboard"] for c in r["ccpc_cells"]))
        self.snapshot["scoreboard_source"] = "qoj"
        self.upstream.username = "new-offline-account"
        self.snapshot["username"] = self.upstream.username
        self.assertNotIn("B", self.labels(self.state(browser)))

    def test_prestart_and_upstream_errors_do_not_leak_cached_metadata(self):
        browser = self.make_app().test_client()
        self.state(browser)
        for status in ("unregistered", "scheduled", "unavailable"):
            self.snapshot.update(status=status, content_available=True)
            state = self.state(browser)
            self.assertEqual(state["scoreboard"], [])
            self.assertEqual(state["scoreboard_display"], {"style": "ccpc"})
            self.assertEqual(state["participation"]["scoreboard_problem_totals"], {})
            self.assertEqual(state["contest"]["problems"], [])
        self.snapshot.update(status="running", content_available=True, scoreboard_available=False)
        state = self.state(browser)
        self.assertEqual(state["scoreboard"], [])
        self.assertNotIn("revealed_problems", state["scoreboard_display"])

    def test_unknown_problem_override_and_non_icpc_fail_closed_only_for_board(self):
        browser = self.make_app(ccpc_unlock_overrides="Z=1").test_client()
        state = self.state(browser)
        self.assertFalse(state["participation"]["scoreboard_available"])
        self.assertEqual(state["scoreboard"], [])
        self.assertIn("Z", state["scoreboard_display"]["error"])
        self.assertTrue(state["permissions"]["view_content"])
        self.snapshot["ruleset"] = "OI"
        state = self.state(self.make_app().test_client())
        self.assertEqual(state["scoreboard"], [])
        self.assertFalse(state["participation"]["scoreboard_available"])

    def test_standard_mode_preserves_original_payload(self):
        browser = self.make_app(scoreboard_style="standard", ccpc_unlock=None,
                                ccpc_unlock_overrides=None).test_client()
        state = self.state(browser)
        self.assertNotIn("scoreboard_display", state)
        self.assertEqual(state["scoreboard"], self.snapshot["scoreboard"])
        self.assertEqual(state["participation"]["scoreboard_problem_totals"], self.snapshot["scoreboard_problem_totals"])

    def test_imported_unofficial_preference_is_part_of_reveal_scope(self):
        from requests import Session
        self.upstream.session = Session()
        self.addCleanup(self.upstream.session.close)
        browser = self.make_app().test_client()
        self.snapshot["scoreboard_frozen"] = True
        self.assertIn("B", self.labels(self.state(browser)))
        self.snapshot["scoreboard_frozen"] = False
        self.upstream.session.cookies.set("show_unofficial_mode", "", domain="qoj.ac", path="/contest")
        self.assertNotIn("B", self.labels(self.state(browser)))

    def test_same_page_external_summary_includes_submissions_after_ac(self):
        with TemporaryDirectory() as directory:
            upstream = ExternalFixtureClient(entered=True)
            upstream.external_html = upstream.external_html.replace("A<br>1/2", "A<br>1/9")
            server = create_app(contest_id=9, data_dir=directory, client_factory=lambda: upstream,
                                scoreboard_style="ccpc", ccpc_unlock=0)
            self.addCleanup(server.extensions["joint_vp"].close)
            state = self.state(server.test_client())
            totals = state["participation"]["scoreboard_problem_totals"]["10"]
            self.assertEqual(totals, {"solved": 1, "submissions": 9})

    def test_real_external_and_pc2_parsers_feed_anonymized_rows(self):
        for factory, source in ((ExternalFixtureClient, "qoj-external"), (TrainingFixtureClient, "qoj-pc2")):
            with self.subTest(source=source), TemporaryDirectory() as directory:
                upstream = factory(entered=True)
                server = create_app(contest_id=9, data_dir=directory, client_factory=lambda: upstream,
                                    scoreboard_style="ccpc", ccpc_unlock=50)
                self.addCleanup(server.extensions["joint_vp"].close)
                state = self.state(server.test_client())
                self.assertEqual(state["participation"]["scoreboard_source"], source)
                self.assertTrue(state["participation"]["scoreboard_available"], state["participation"])
                self.assertTrue(state["scoreboard"][0]["ccpc_cells"])
                self.assertTrue(all(not c["revealed"] for c in state["scoreboard"][0]["ccpc_cells"]))
                self.assertNotIn("problem_stats", state["scoreboard"][0])

    def test_demo_has_no_public_board_before_start_and_supports_zero_threshold(self):
        with TemporaryDirectory() as directory:
            server = create_app(demo=True, data_dir=directory, scoreboard_style="ccpc", ccpc_unlock=0)
            self.addCleanup(server.extensions["joint_vp"].close)
            browser = server.test_client()
            self.assertEqual(self.state(browser)["scoreboard_display"], {"style": "ccpc"})
            self.post(browser, "/api/team/create", {"name": "Offline demo"})
            state = self.post(browser, "/api/start", {})
            self.assertEqual(len(self.labels(state)), 8)
            self.assertTrue(state["scoreboard_display"]["all_revealed"])


if __name__ == "__main__":
    unittest.main()
