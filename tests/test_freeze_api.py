"""API-level separation of public frozen standings and private team results."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app import create_app


class FreezeAPITests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        self.start_time = self.now = 1_800_000_000.0
        clock = patch("app.time.time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.app = self.make_app(freeze_minutes=30)
        self.captain = self.app.test_client()
        self.store = self.app.extensions["store"]

    def make_app(self, freeze_minutes):
        app = create_app(contest_id=1802, demo=True, duration_minutes=120,
                         data_dir=self.data_dir, freeze_minutes=freeze_minutes)
        app.config["TESTING"] = True
        return app

    def state(self, client=None):
        response = (client or self.captain).get("/api/state")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def post(self, path, body=None, client=None):
        client = client or self.captain
        response = client.post(path, json={} if body is None else body,
                               headers={"X-CSRF-Token": self.state(client)["csrf_token"]})
        self.assertIn(response.status_code, (200, 201), response.get_data(as_text=True))
        return response.get_json()

    def start(self, name="Alpha", client=None):
        return self.post("/api/start", {"team_name": name}, client)["run"]

    def result(self, run, elapsed, verdict="AC", problem_id=1):
        identifier = self.store.add_submission(run, {"id": problem_id, "label": chr(64 + problem_id)},
                                               "C++17", now=run["started_at"] + elapsed)
        self.store.update_submission(identifier, verdict=verdict, qoj_id=900000 + identifier,
                                     qoj_url=f"https://qoj.ac/submission/{900000 + identifier}")
        return identifier

    @staticmethod
    def row(state, name):
        return next(row for row in state["scoreboard"] if row["team_name"] == name)

    def test_all_public_rows_hide_frozen_verdicts_while_captain_keeps_real_results(self):
        alpha = self.start()
        other = self.app.test_client()
        beta = self.start("Beta", other)
        self.result(alpha, 60)
        self.now = self.start_time + 5401
        self.result(alpha, 5400, problem_id=2)
        self.result(beta, 5400)
        self.result(beta, 5401, "CE", problem_id=2)
        state = self.state()
        self.assertEqual(state["run"]["solved"], 2)
        self.assertEqual([s["verdict"] for s in state["submissions"]], ["AC", "AC"])
        alpha_public, beta_public = self.row(state, "Alpha"), self.row(state, "Beta")
        self.assertEqual(alpha_public["solved"], 1)
        self.assertEqual(alpha_public["problem_stats"]["2"],
                         {"attempts": 1, "wrong": 0, "pending": 1, "solved": False, "time": None})
        self.assertEqual(beta_public["solved"], 0)
        self.assertEqual(beta_public["problem_stats"]["1"]["pending"], 1)
        self.assertEqual(beta_public["problem_stats"]["2"]["pending"], 1)
        for row in state["scoreboard"]:
            self.assertTrue(row["frozen"])
            self.assertFalse({"submissions", "qoj_id", "qoj_url", "verdict", "source"} & set(row))
        self.assertEqual(state["scoreboard_freeze"], {
            "status": "frozen", "freeze_at": self.start_time + 5400,
            "unfreeze_at": self.start_time + 7200, "frozen_teams": 2,
            "configured_minutes": 30, "auto_unfreeze": True,
        })
        spectator = self.state(self.app.test_client())
        for name in ("Alpha", "Beta"):
            self.assertEqual(self.row(spectator, name)["problem_stats"], self.row(state, name)["problem_stats"])

    def test_member_private_score_matches_captain_but_shared_scoreboard_stays_frozen(self):
        team = self.post("/api/team/create", {"name": "Shared team", "device_name": "Captain"})["team"]
        member = self.app.test_client()
        self.post("/api/team/join", {"code": team["join_code"], "device_name": "Member"}, member)
        run = self.post("/api/start")["run"]
        self.now = self.start_time + 5500
        self.result(run, 5500)
        captain, shared = self.state(), self.state(member)
        self.assertEqual(captain["run"]["solved"], 1)
        self.assertEqual(shared["run"]["problem_stats"], captain["run"]["problem_stats"])
        self.assertEqual(shared["submissions"], [])
        self.assertEqual(captain["scoreboard"], shared["scoreboard"])
        self.assertEqual(shared["scoreboard"][0]["solved"], 0)
        self.assertEqual(shared["scoreboard_freeze"]["status"], "frozen")

    def test_freeze_boundary_is_inclusive_and_manual_finish_cannot_reveal_until_scheduled_end(self):
        run = self.start()
        self.now = self.start_time + 5399
        scheduled = self.state()["scoreboard_freeze"]
        self.assertEqual(scheduled["status"], "scheduled")
        self.assertEqual(scheduled["frozen_teams"], 0)
        self.now = self.start_time + 5400
        self.result(run, 5400)
        frozen = self.state()
        self.assertEqual(frozen["scoreboard_freeze"]["status"], "frozen")
        self.assertEqual(frozen["scoreboard"][0]["solved"], 0)
        finished = self.post("/api/finish")
        self.assertEqual(finished["run"]["status"], "finished")
        self.assertEqual(finished["scoreboard_freeze"]["status"], "frozen")
        self.now = self.start_time + 7199
        self.assertEqual(self.state()["scoreboard"][0]["solved"], 0)
        self.now = self.start_time + 7200
        revealed = self.state()
        self.assertEqual(revealed["scoreboard_freeze"]["status"], "unfrozen")
        self.assertEqual(revealed["scoreboard_freeze"]["frozen_teams"], 0)
        self.assertEqual(revealed["scoreboard"][0]["solved"], 1)
        self.assertEqual(revealed["scoreboard"][0]["penalty"], 90)

    def test_teams_with_different_start_times_freeze_on_their_own_clock(self):
        alpha = self.start()
        self.now = self.start_time + 3600
        other = self.app.test_client()
        beta = self.start("Beta", other)
        self.now = self.start_time + 5500
        self.result(alpha, 5500)
        self.result(beta, 1900)
        state = self.state()
        self.assertTrue(self.row(state, "Alpha")["frozen"])
        self.assertFalse(self.row(state, "Beta")["frozen"])
        self.assertEqual(self.row(state, "Alpha")["solved"], 0)
        self.assertEqual(self.row(state, "Beta")["solved"], 1)
        self.assertEqual(self.state(other)["scoreboard_freeze"]["status"], "scheduled")

    def test_freeze_configuration_is_snapshotted_in_run_and_default_zero_disables_new_runs(self):
        original = self.start()
        self.assertEqual(original["freeze_seconds"], 1800)
        cookie = self.captain.get_cookie("qoj_vp_session").value
        restarted = self.make_app(freeze_minutes=0)
        restored = restarted.test_client()
        restored.set_cookie("qoj_vp_session", cookie)
        self.now = self.start_time + 5500
        self.result(original, 5500)
        current = self.state(restored)
        self.assertEqual(current["run"]["freeze_seconds"], 1800)
        self.assertEqual(current["scoreboard_freeze"]["status"], "frozen")
        self.assertEqual(current["scoreboard_freeze"]["configured_minutes"], 0)
        self.assertEqual(current["scoreboard"][0]["solved"], 0)
        reset = self.post("/api/reset", {"team_name": "New run"}, restored)
        self.assertEqual(reset["run"]["freeze_seconds"], 0)
        self.assertEqual(reset["scoreboard_freeze"]["status"], "disabled")
        self.assertIsNone(reset["scoreboard_freeze"]["freeze_at"])
        self.assertIsNone(reset["scoreboard_freeze"]["unfreeze_at"])


if __name__ == "__main__":
    unittest.main()
