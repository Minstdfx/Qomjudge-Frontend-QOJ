"""Native QOJ participation gates, tested without servers or real credentials."""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
import unittest
from unittest.mock import Mock, patch

from requests import Response

from app import create_app
from qoj import QOJClient, QOJError


PRIVATE_PROBLEM = "PRIVATE PROBLEM MUST NOT LEAK"
PRIVATE_ATTACHMENT = "PRIVATE ATTACHMENT MUST NOT LEAK"
PRIVATE_STANDING = "PRIVATE STANDING MUST NOT LEAK"
ATTACHMENT_ID = "abcd0123456789abcd012345"
PDF = b"%PDF-1.7\nnative fixture\n%%EOF\n"


class NativeClient:
    """Always retains private upstream data, even when permission is withdrawn."""
    def __init__(self, start_time):
        self.lock = RLock()
        self.authenticated = True
        self.username = "native-fixture-user"
        self.start_time = start_time
        self.calls = []
        self.error = None
        self.enter_error = None
        self.enter_status = {"vp": "running", "official": "scheduled"}
        self.contest_data = {
            "id": 1802, "title": "Native QOJ Fixture", "duration_seconds": 3600,
            "problems": [{"id": 1, "label": "A", "title": PRIVATE_PROBLEM, "color": "#ff0000"}],
            "attachments": [{"id": ATTACHMENT_ID, "name": PRIVATE_ATTACHMENT,
                             "url": "https://qoj.ac/download.php?type=attachment&id=1802", "external": False,
                             "type": "attachment"}],
        }
        self.snapshots = {mode: self.snapshot(mode, status) for mode, status in
                          (("vp", "unregistered"), ("official", "scheduled"))}

    def snapshot(self, mode, status):
        visible = status in {"running", "finished"}
        return {"mode": mode, "status": status, "content_available": visible,
                "can_submit": status == "running", "authenticated": True, "username": self.username,
                "started_at": self.start_time if visible else None,
                "ends_at": self.start_time + 3600 if visible else None,
                "contest": deepcopy(self.contest_data),
                "actions": [{"id": "enter-vp" if mode == "vp" else "register", "label": "Enter native contest",
                             "fields": [{"name": "choice", "label": "Choice", "type": "select", "required": True,
                                         "options": [{"value": "current", "label": "Current contest"}]}]}],
                "scoreboard": [{"rank": "?x", "team_name": PRIVATE_STANDING, "solved": 17, "penalty": 999,
                                "is_self": True, "problem_stats": {"1": {"solved": True, "time": 333,
                                "wrong": 0, "pending": 0, "attempts": 1}}}]}

    def set_status(self, mode, status, **changes):
        self.snapshots[mode] = self.snapshot(mode, status)
        self.snapshots[mode].update(changes)

    def participation(self, contest_id, mode="vp"):
        self.calls.append(("participation", contest_id, mode))
        if self.error:
            raise self.error
        return deepcopy(self.snapshots[mode])

    def enter(self, contest_id, mode, selection=None):
        self.calls.append(("enter", contest_id, mode, deepcopy(selection)))
        if self.enter_error:
            raise self.enter_error
        self.set_status(mode, self.enter_status[mode])
        return deepcopy(self.snapshots[mode])

    def contest(self, contest_id):
        self.calls.append(("contest", contest_id))
        return deepcopy(self.contest_data)

    def problem(self, contest_id, problem_id):
        self.calls.append(("problem", contest_id, problem_id))
        return {**self.contest_data["problems"][0], "languages": [{"value": "C++17", "label": "C++17"}],
                "submission_available": True,
                "statement_html": '<html><body><iframe src="/qoj/download.php?type=statement&id=1"></iframe></body></html>'}

    def fetch(self, method, url, **kwargs):
        self.calls.append(("fetch", method, url, kwargs))
        response = Response()
        response.status_code = 200
        response.url = url if url.startswith("https://") else "https://qoj.ac" + url
        if "type=statement" in url:
            body, kind = PDF, "application/pdf"
        else:
            body, kind = PRIVATE_ATTACHMENT.encode(), "application/octet-stream"
        response.headers.update({"Content-Type": kind, "Content-Length": str(len(body)),
                                 "Content-Disposition": 'attachment; filename="fixture.txt"'})
        if kwargs.get("stream"):
            response.raw = BytesIO(body)
        else:
            response._content = body
        return response

    def submit(self, *args, **kwargs):
        self.calls.append(("submit", args, kwargs))
        return {"qoj_id": 1234, "qoj_url": "https://qoj.ac/submission/1234"}

    def clear(self):
        self.authenticated = False
        self.username = ""


class NativeAPITests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        self.now = 1_800_000_100.0
        clock = patch("app.time.time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.upstream = NativeClient(start_time=self.now - 100)
        self.app = create_app(contest_id=1802, data_dir=self.data_dir, client_factory=lambda: self.upstream)
        self.app.config["TESTING"] = True
        self.captain = self.app.test_client()

    def state(self, client=None):
        response = (client or self.captain).get("/api/state")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def post(self, path, body=None, client=None):
        client = client or self.captain
        token = self.state(client)["csrf_token"]
        return client.post(path, json={} if body is None else body, headers={"X-CSRF-Token": token})

    def ok(self, response):
        self.assertIn(response.status_code, (200, 201), response.get_data(as_text=True))
        return response.get_json()

    def member(self):
        created = self.ok(self.post("/api/team/create", {"name": "Native team", "device_name": "Captain"}))
        member = self.app.test_client()
        self.ok(self.post("/api/team/join", {"code": created["team"]["join_code"], "device_name": "Member"}, member))
        return member

    def start(self):
        return self.ok(self.post("/api/start", {"mode": "vp", "action": "enter-vp", "selection": {"choice": "current"}}))

    def assert_hidden(self, state):
        self.assertFalse(state["permissions"]["view_content"])
        self.assertFalse(state["permissions"]["submit"])
        self.assertFalse((state.get("contest") or {}).get("problems"))
        self.assertFalse((state.get("contest") or {}).get("attachments"))
        self.assertFalse(state.get("scoreboard"))
        self.assertFalse(state.get("submissions"))
        self.assertIsNone(state.get("run"))
        representation = str(state)
        for sentinel in (PRIVATE_PROBLEM, PRIVATE_ATTACHMENT, PRIVATE_STANDING):
            self.assertNotIn(sentinel, representation)

    def warm_private_cache(self):
        self.start()
        detail_response = self.captain.get("/api/problems/1")
        self.assertEqual(detail_response.status_code, 200)
        detail = detail_response.get_json()
        attachments = self.captain.get("/api/attachments")
        self.assertEqual(attachments.status_code, 200)
        self.ok(self.post("/api/prints", {"name": "private.txt", "content": "PRIVATE PRINT CONTENT"}))
        printed = self.captain.get("/api/prints").get_json()["files"][0]
        return ["/api/problems/1", "/api/problems/1?refresh=1", "/api/problems/1/statement",
                detail["statement_pdfs"][0]["url"], "/api/attachments",
                attachments.get_json()["attachments"][0]["download_url"], "/api/prints",
                printed["view_url"], printed["download_url"], "/qoj/contest/1802",
                "/qoj/contest/1802/problem/1", "/qoj/contest/1802/standings",
                "/qoj/download.php?type=statement&id=1", "/qoj/images/fixture.png"]

    def test_unregistered_and_scheduled_snapshots_never_expose_embedded_private_data(self):
        member = self.member()
        for mode in ("vp", "official"):
            self.ok(self.post("/api/mode", {"mode": mode}))
            for client in (self.captain, member):
                with self.subTest(mode=mode, member=client is member):
                    state = self.state(client)
                    self.assertEqual(state["participation"]["mode"], mode)
                    self.assert_hidden(state)
        self.assertFalse(any(call[0] in {"contest", "problem", "fetch"} for call in self.upstream.calls))

    def test_native_start_calls_enter_once_and_uses_only_upstream_start_and_end(self):
        state = self.start()
        enters = [call for call in self.upstream.calls if call[0] == "enter"]
        self.assertEqual(enters, [("enter", 1802, "vp", {"action": "enter-vp", "values": {"choice": "current"}})])
        self.assertEqual(state["participation"]["status"], "running")
        self.assertEqual(state["run"]["started_at"], self.upstream.start_time)
        self.assertEqual(state["run"]["ends_at"], self.upstream.start_time + 3600)
        self.assertTrue(state["permissions"]["view_content"])
        self.assertTrue(state["permissions"]["submit"])

    def test_device_with_native_run_can_join_shared_team_without_local_finish(self):
        self.upstream.set_status("vp", "running")
        captain_run = self.state()["run"]
        member = self.member()
        shared = self.state(member)
        self.assertEqual(shared["team"]["role"], "member")
        self.assertEqual(shared["run"]["id"], captain_run["id"])
        self.assertEqual(shared["participation"]["status"], "running")
        self.assertFalse(any(call[0] == "enter" for call in self.upstream.calls))

    def test_confirmed_native_run_and_prints_resume_after_restart_without_new_entry(self):
        original = self.start()["run"]
        printed = self.ok(self.post("/api/prints", {"name": "resume.txt", "content": "Persisted native print"}))["file"]
        cookie = self.captain.get_cookie("qoj_vp_session").value
        self.now += 120
        reconnected = NativeClient(start_time=self.upstream.start_time)
        reconnected.set_status("vp", "running")
        restarted = create_app(contest_id=1802, data_dir=self.data_dir, client_factory=lambda: reconnected)
        restarted.config["TESTING"] = True
        client = restarted.test_client()
        client.set_cookie("qoj_vp_session", cookie)
        state = self.state(client)
        self.assertEqual(state["run"]["id"], original["id"])
        self.assertEqual(state["run"]["started_at"], original["started_at"])
        self.assertEqual(state["run"]["ends_at"], original["ends_at"])
        self.assertEqual(client.get("/api/prints").get_json()["files"][0]["id"], printed["id"])
        response = client.get(printed["download_url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"Persisted native print")
        response.close()
        self.assertFalse(any(call[0] == "enter" for call in reconnected.calls))

    def test_native_print_ownership_isolated_by_upstream_username_and_mode(self):
        original = self.start()["run"]
        original_user = self.upstream.username
        printed = self.ok(self.post("/api/prints", {"name": "owner.txt", "content": "Owner private print"}))["file"]
        self.upstream.username = "different-qoj-account"
        self.upstream.snapshots["vp"]["username"] = self.upstream.username
        other_account = self.state()
        self.assertNotEqual(other_account["run"]["id"], original["id"])
        self.assertEqual(self.captain.get("/api/prints").get_json()["files"], [])
        self.assertEqual(self.captain.get(printed["download_url"]).status_code, 404)
        self.upstream.username = original_user
        self.upstream.snapshots["vp"]["username"] = original_user
        self.assertEqual(self.state()["run"]["id"], original["id"])
        self.upstream.set_status("official", "running")
        other_mode = self.ok(self.post("/api/mode", {"mode": "official"}))
        self.assertNotEqual(other_mode["run"]["id"], original["id"])
        self.assertEqual(self.captain.get("/api/prints").get_json()["files"], [])
        self.assertEqual(self.captain.get(printed["view_url"]).status_code, 404)
        restored = self.ok(self.post("/api/mode", {"mode": "vp"}))
        self.assertEqual(restored["run"]["id"], original["id"])
        response = self.captain.get(printed["download_url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"Owner private print")
        response.close()

    def test_official_registration_stays_locked_until_upstream_reports_running(self):
        self.ok(self.post("/api/mode", {"mode": "official"}))
        registered = self.ok(self.post("/api/start", {"mode": "official", "action": "register"}))
        self.assertEqual(registered["participation"]["status"], "scheduled")
        self.assert_hidden(registered)
        self.upstream.set_status("official", "running")
        started = self.state()
        self.assertTrue(started["permissions"]["view_content"])
        self.assertEqual(started["run"]["started_at"], self.upstream.start_time)

    def test_mode_switch_clears_old_private_cache_for_captain_and_member(self):
        member = self.member()
        paths = self.warm_private_cache()
        switched = self.ok(self.post("/api/mode", {"mode": "official"}))
        self.assert_hidden(switched)
        self.upstream.calls.clear()
        for client in (self.captain, member):
            self.assert_hidden(self.state(client))
            for path in paths:
                with self.subTest(path=path, member=client is member):
                    response = client.get(path)
                    self.assertIn(response.status_code, (401, 403, 409), response.get_data(as_text=True))
        self.assertFalse(any(call[0] in {"problem", "fetch"} for call in self.upstream.calls))

    def test_revoked_content_permission_blocks_cached_routes_without_mode_switch(self):
        member = self.member()
        paths = self.warm_private_cache()
        self.upstream.set_status("vp", "unregistered")
        self.upstream.calls.clear()
        for client in (self.captain, member):
            self.assert_hidden(self.state(client))
            for path in paths:
                with self.subTest(path=path, member=client is member):
                    response = client.get(path)
                    self.assertIn(response.status_code, (401, 403, 409), response.get_data(as_text=True))
                    self.assertNotIn("PRIVATE PRINT CONTENT", response.get_data(as_text=True))
        self.assertFalse(any(call[0] in {"problem", "fetch"} for call in self.upstream.calls))

    def test_upstream_participation_failure_fails_closed_even_with_prior_cache(self):
        member = self.member()
        paths = self.warm_private_cache()
        self.upstream.error = QOJError("Fixture upstream participation unavailable", 502)
        self.upstream.calls.clear()
        for client in (self.captain, member):
            response = client.get("/api/state")
            if response.status_code == 200:
                self.assert_hidden(response.get_json())
            else:
                self.assertIn(response.status_code, (401, 403, 409, 502))
            for path in paths:
                with self.subTest(path=path, member=client is member):
                    denied = client.get(path)
                    self.assertIn(denied.status_code, (401, 403, 409, 502))
        self.assertFalse(any(call[0] in {"problem", "fetch"} for call in self.upstream.calls))

    def test_upstream_ranks_and_standings_are_not_recomputed_or_locally_frozen(self):
        self.start()
        expected = deepcopy(self.upstream.snapshots["vp"]["scoreboard"])
        state = self.state()
        self.assertEqual(state["scoreboard"], expected)
        self.now = self.upstream.start_time + 3590
        frozen_by_old_local_rules = self.state()
        self.assertEqual(frozen_by_old_local_rules["scoreboard"], expected)
        self.assertEqual(frozen_by_old_local_rules["scoreboard"][0]["rank"], "?x")

    def test_unknown_native_times_cannot_create_local_timer_or_enable_submission(self):
        self.upstream.set_status("vp", "running", started_at=None, ends_at=None)
        state = self.state()
        self.assertIsNone(state.get("run"))
        self.assertFalse(state["permissions"]["submit"])
        response = self.post("/api/submit", {"problem_id": 1, "language": "C++17", "source": "int main() {}", "filename": "main.cpp"})
        self.assertIn(response.status_code, (403, 409))
        self.assertFalse(any(call[0] == "submit" for call in self.upstream.calls))

    def test_upstream_can_submit_flag_overrides_a_still_active_mirrored_run(self):
        self.start()
        self.upstream.snapshots["vp"]["can_submit"] = False
        state = self.state()
        self.assertTrue(state["permissions"]["view_content"])
        self.assertFalse(state["permissions"]["submit"])
        response = self.post("/api/submit", {"problem_id": 1, "language": "C++17", "source": "int main() {}", "filename": "main.cpp"})
        self.assertIn(response.status_code, (403, 409))
        self.assertFalse(any(call[0] == "submit" for call in self.upstream.calls))

    def test_submission_aborts_if_upstream_account_changes_during_problem_fetch(self):
        original = self.start()["run"]
        original_problem = self.upstream.problem

        def changing_account(contest_id, problem_id):
            info = original_problem(contest_id, problem_id)
            self.upstream.username = "changed-during-fetch"
            self.upstream.snapshots["vp"]["username"] = self.upstream.username
            return info

        self.upstream.problem = changing_account
        response = self.post("/api/submit", {"problem_id": 1, "language": "C++17", "source": "int main() {}", "filename": "main.cpp"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(any(call[0] == "submit" for call in self.upstream.calls))
        self.assertEqual(self.app.extensions["store"].submissions(original["id"]), [])

    def test_submission_rechecks_permission_after_problem_fetch(self):
        self.start()
        original_problem = self.upstream.problem

        def ending_contest(contest_id, problem_id):
            info = original_problem(contest_id, problem_id)
            self.upstream.snapshots["vp"]["can_submit"] = False
            return info

        self.upstream.problem = ending_contest
        response = self.post("/api/submit", {"problem_id": 1, "language": "C++17", "source": "int main() {}", "filename": "main.cpp"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(any(call[0] == "submit" for call in self.upstream.calls))

    def test_submission_rechecks_problem_membership_after_problem_fetch(self):
        self.start()
        original_problem = self.upstream.problem

        def removing_problem(contest_id, problem_id):
            info = original_problem(contest_id, problem_id)
            self.upstream.snapshots["vp"]["contest"]["problems"] = []
            return info

        self.upstream.problem = removing_problem
        response = self.post("/api/submit", {"problem_id": 1, "language": "C++17", "source": "int main() {}", "filename": "main.cpp"})
        self.assertIn(response.status_code, (403, 404, 409))
        self.assertFalse(any(call[0] == "submit" for call in self.upstream.calls))

    def test_pregame_login_redirect_cannot_reach_contest_or_statement_resources(self):
        transport = QOJClient()
        self.addCleanup(transport.session.close)
        self.upstream.fetch = transport.fetch
        for destination in ("/contest/1802", "/download.php?type=statement&id=1"):
            with self.subTest(destination=destination):
                redirect = Response()
                redirect.status_code = 302
                redirect.url = "https://qoj.ac/login"
                redirect.headers["Location"] = destination
                redirect._content = b""
                transport.session.request = Mock(return_value=redirect)
                response = self.captain.get("/qoj/login")
                self.assertEqual(response.status_code, 403)
                transport.session.request.assert_called_once()
                self.assertEqual(transport.session.request.call_args.args[:2], ("GET", "https://qoj.ac/login"))
        self.assert_hidden(self.state())

    def test_local_reset_finish_and_member_native_actions_cannot_change_upstream_contest(self):
        member = self.member()
        initial = self.start()
        self.upstream.calls.clear()
        for path in ("/api/reset", "/api/finish"):
            with self.subTest(path=path):
                self.assertEqual(self.post(path).status_code, 409)
        for path, payload in (("/api/mode", {"mode": "official"}), ("/api/start", {"mode": "vp"})):
            with self.subTest(member_path=path):
                self.assertEqual(self.post(path, payload, member).status_code, 403)
        current = self.state()
        self.assertEqual(current["run"]["id"], initial["run"]["id"])
        self.assertEqual(current["participation"]["status"], "running")
        self.assertFalse(any(call[0] == "enter" for call in self.upstream.calls))

    def test_failed_native_enter_does_not_create_a_local_run(self):
        self.upstream.enter_error = QOJError("Fixture native entry rejected", 422)
        response = self.post("/api/start", {"mode": "vp", "action": "enter-vp"})
        self.assertEqual(response.status_code, 422)
        self.assert_hidden(self.state())
        self.assertEqual(len([call for call in self.upstream.calls if call[0] == "enter"]), 1)

    def test_uncertain_native_entry_reminder_survives_refresh_until_confirmed(self):
        self.upstream.enter_status["vp"] = "unregistered"
        original = self.upstream.enter

        def uncertain_enter(contest_id, mode, selection=None):
            return {**original(contest_id, mode, selection), "entry_uncertain": True,
                    "message": "Request sent once; refresh before starting again."}

        self.upstream.enter = uncertain_enter
        response = self.post("/api/start", {"mode": "vp", "action": "enter-vp"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("sent once", response.get_json()["error"])
        for state in (response.get_json()["state"], self.state()):
            self.assert_hidden(state)
            self.assertTrue(state["participation"]["entry_uncertain"])
            self.assertIn("sent once", state["participation"]["message"])
        self.assertEqual(len([call for call in self.upstream.calls if call[0] == "enter"]), 1)
        self.upstream.set_status("vp", "running")
        self.assertFalse(self.state()["participation"].get("entry_uncertain", False))


if __name__ == "__main__":
    unittest.main()
