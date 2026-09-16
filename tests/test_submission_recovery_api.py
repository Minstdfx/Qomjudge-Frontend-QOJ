"""Durable source and read-only recovery of uncertain submissions, offline."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app import create_app
from qoj import QOJClient, QOJError
from test_native_api import NativeClient
from test_submission_confirmation import listing, response as upstream_response


SOURCE = '#include <cstdio>\r\n// 私有源码 <script>alert(1)</script>\r\nint main() { puts("secret"); }\r\n'


class RecoveryClient(NativeClient):
    def __init__(self, start_time):
        super().__init__(start_time)
        self.set_status("vp", "running")
        self.prepared = {"username": self.username, "before_ids": [12],
                         "source_hash": "private-recovery-fingerprint", "prepared_at": start_time + 100}
        self.recovered = {"qoj_id": 909, "qoj_url": "https://qoj.ac/submission/909"}
        self.recovery_error = None
        self.preflight_error = None
        self.submit_error = QOJError("Submission POST outcome uncertain", 502)
        self.verdict_error = None
        self.verdict_result = {"verdict": "AC", "message": "100 / 100"}
        self.change_username_on_recovery = False
        self.payload_observer = None

    def submit(self, contest_id, pid, language, source, filename, on_prepared=None):
        self.calls.append(("submit", contest_id, pid, language, source, filename))
        if self.payload_observer:
            self.payload_observer()
        if self.preflight_error:
            raise self.preflight_error
        on_prepared(deepcopy(self.prepared))
        raise self.submit_error

    def reconcile_submission(self, contest_id, pid, language, receipt, exclude_ids=()):
        self.calls.append(("reconcile", contest_id, pid, language, deepcopy(receipt), tuple(exclude_ids)))
        if self.recovery_error:
            raise self.recovery_error
        if self.change_username_on_recovery:
            self.username = "different-account"
        return deepcopy(self.recovered)

    def verdict(self, qoj_id):
        self.calls.append(("verdict", qoj_id))
        if self.verdict_error:
            raise self.verdict_error
        return deepcopy(self.verdict_result)


class ParsedVerdictClient(RecoveryClient):
    """Use the real verdict adapter with current QOJ table headers and TL text."""
    verdict = QOJClient.verdict
    _same_account = QOJClient._same_account

    def _page(self, path):
        self.calls.append(("verdict_page", path))
        sid = int(path.rsplit("/", 1)[-1])
        document = listing({"sid": sid, "username": self.username, "pid": 1}, result="TL")
        return upstream_response(document, path)


class SubmissionRecoveryAPITests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        self.now = 1_800_000_100.0
        clock = patch("app.time.time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.upstream = RecoveryClient(self.now - 100)
        self.app = self.make_app(self.upstream)
        self.client = self.app.test_client()
        self.initial = self.state()
        self.run_id = self.initial["run"]["id"]
        self.store = self.app.extensions["store"]

    def make_app(self, upstream):
        app = create_app(contest_id=1802, data_dir=self.data_dir, client_factory=lambda: upstream)
        app.config["TESTING"] = True
        return app

    def state(self, client=None):
        response = (client or self.client).get("/api/state")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def post(self, path, body, client=None):
        client = client or self.client
        token = self.state(client)["csrf_token"]
        return client.post(path, json=body, headers={"X-CSRF-Token": token})

    def uncertain_submission(self, expected_status=502):
        response = self.post("/api/submit", {"problem_id": 1, "language": "C++17",
                                           "source": SOURCE, "filename": "main.cpp"})
        self.assertEqual(response.status_code, expected_status, response.get_data(as_text=True))
        sub = self.store.submissions(self.run_id)[0]
        self.assertEqual(sub["verdict"], "UNKNOWN")
        self.assertIsNone(sub["qoj_id"])
        return sub["id"]

    def calls(self, name):
        return [call for call in self.upstream.calls if call[0] == name]

    def source(self, sub_id, client=None):
        return (client or self.client).get(f"/api/submissions/{sub_id}/source")

    def test_source_is_saved_before_post_and_never_exposed_in_state_or_receipt_response(self):
        def observe_source():
            sub = self.store.submissions(self.run_id)[0]
            saved = self.store.submission_payload(sub["id"])
            self.assertEqual((saved["source"], saved["filename"]), (SOURCE, "main.cpp"))
        self.upstream.payload_observer = observe_source
        sub_id = self.uncertain_submission()
        saved = self.store.submission_payload(sub_id)
        self.assertEqual(saved["receipt"], self.upstream.prepared)
        response = self.source(sub_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"source": SOURCE, "filename": "main.cpp", "language": "C++17", "qoj_id": None})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        state = self.state()
        self.assertNotIn(SOURCE, str(state))
        self.assertNotIn("private-recovery-fingerprint", str(state))
        self.assertNotIn("receipt", state["submissions"][0])
        self.assertNotIn("source", state["submissions"][0])

    def test_unknown_recovers_an_id_and_verdict_without_repeating_post(self):
        sub_id = self.uncertain_submission()
        run = {"id": self.run_id, "started_at": self.initial["run"]["started_at"]}
        known = self.store.add_submission(run, {"id": 1, "label": "A"}, "C++17", now=self.now)
        self.store.update_submission(known, verdict="AC", qoj_id=77)
        self.now += 8
        state = self.state()
        sub = next(row for row in state["submissions"] if row["id"] == sub_id)
        self.assertEqual((sub["qoj_id"], sub["verdict"]), (909, "AC"))
        self.assertEqual(self.calls("reconcile")[0][1:4], (1802, 1, "C++17"))
        self.assertEqual(self.calls("reconcile")[0][4], self.upstream.prepared)
        self.assertEqual(self.calls("reconcile")[0][5], (77,))
        self.assertEqual(self.calls("verdict"), [("verdict", 909)])
        self.assertEqual(len(self.calls("submit")), 1)
        self.assertEqual(self.source(sub_id).get_json()["qoj_id"], 909)

    def test_preflight_failure_is_final_and_keeps_source_without_polling_or_resending(self):
        self.upstream.preflight_error = QOJError("QOJ 提交表单地址与所选题目不一致。", 422)
        response = self.post("/api/submit", {"problem_id": 1, "language": "C++17",
                                           "source": SOURCE, "filename": "main.cpp"})
        self.assertEqual(response.status_code, 422)
        sub = self.store.submissions(self.run_id)[0]
        self.assertEqual(sub["verdict"], "FAILED")
        self.assertIsNone(sub["qoj_id"])
        self.assertIn("代码尚未发送到 QOJ", sub["message"])
        self.assertIsNone(self.store.submission_payload(sub["id"])["receipt"])
        self.assertEqual(self.source(sub["id"]).get_json()["source"], SOURCE)
        for _ in range(3):
            self.now += 8
            self.assertEqual(self.state()["submissions"][0]["verdict"], "FAILED")
        self.assertEqual(self.calls("reconcile"), [])
        self.assertEqual(self.calls("verdict"), [])
        self.assertEqual(len(self.calls("submit")), 1)

        cookie = self.client.get_cookie("qoj_vp_session")
        resumed_upstream = RecoveryClient(self.upstream.start_time)
        resumed = self.make_app(resumed_upstream).test_client()
        resumed.set_cookie("qoj_vp_session", cookie.value)
        self.assertEqual(self.state(resumed)["submissions"][0]["verdict"], "FAILED")
        self.assertFalse(any(call[0] in {"submit", "reconcile", "verdict"} for call in resumed_upstream.calls))
        self.assertEqual(self.source(sub["id"], resumed).get_json()["source"], SOURCE)

    def test_preflight_network_and_rate_limit_failures_do_not_become_pending_submissions(self):
        for status in (502, 429):
            with self.subTest(status=status):
                self.upstream.preflight_error = QOJError("Fetching submission form failed", status)
                response = self.post("/api/submit", {"problem_id": 1, "language": "C++17",
                                                   "source": SOURCE, "filename": "main.cpp"})
                self.assertEqual(response.status_code, status)
                sub = self.store.submissions(self.run_id)[0]
                self.assertEqual(sub["verdict"], "FAILED")
                self.assertEqual(sub["next_poll_at"], 0)
                self.assertIsNone(self.store.submission_payload(sub["id"])["receipt"])
                self.now += 60
                self.state()
        self.assertEqual(self.calls("reconcile"), [])
        self.assertEqual(self.calls("verdict"), [])

    def test_adapter_without_preparation_tracking_keeps_errors_uncertain(self):
        # Compatibility adapters may already have sent a POST before raising;
        # only adapters with the preparation callback can prove non-delivery.
        self.upstream.reconcile_submission = None
        with patch.object(self.upstream, "submit", side_effect=QOJError("Outcome unknown", 502)):
            self.uncertain_submission()

    def test_missing_match_and_network_failures_retain_receipt_for_later_read_only_retry(self):
        sub_id = self.uncertain_submission()
        self.upstream.recovered = {"qoj_id": None, "message": "No unique match yet"}
        self.now += 8
        self.assertEqual(self.state()["submissions"][0]["message"], "No unique match yet")
        self.upstream.recovery_error = QOJError("Temporary read failure", 502)
        self.now += 8
        self.assertEqual(self.state()["submissions"][0]["verdict"], "UNKNOWN")
        self.assertEqual(self.store.submission_payload(sub_id)["receipt"], self.upstream.prepared)
        self.upstream.recovery_error = None
        self.upstream.recovered = {"qoj_id": 909}
        self.upstream.verdict_result = {"verdict": "TLE", "message": "Time Limit Exceeded"}
        self.now += 8
        self.state()
        self.assertEqual(len(self.calls("reconcile")), 2)
        self.now += 22
        self.assertEqual(self.state()["submissions"][0]["verdict"], "TLE")
        self.assertEqual(len(self.calls("reconcile")), 3)
        self.assertEqual(len(self.calls("submit")), 1)

    def test_confirmed_id_survives_verdict_failure_and_next_poll_skips_reconciliation(self):
        self.uncertain_submission()
        self.upstream.verdict_error = QOJError("Verdict temporarily unavailable", 502)
        self.now += 8
        sub = self.state()["submissions"][0]
        self.assertEqual((sub["qoj_id"], sub["verdict"]), (909, "PENDING"))
        self.upstream.verdict_error = None
        self.now += 8
        self.assertEqual(self.state()["submissions"][0]["verdict"], "PENDING")
        self.assertEqual(len(self.calls("verdict")), 1)
        self.now += 22
        self.assertEqual(self.state()["submissions"][0]["verdict"], "AC")
        self.assertEqual(len(self.calls("reconcile")), 1)
        self.assertEqual(len(self.calls("verdict")), 2)

    def test_rate_limit_backoff_uses_retry_after_and_survives_server_restart(self):
        sub_id = self.uncertain_submission()
        limited = QOJError("QOJ rate limit", 429)
        limited.retry_after = 120
        self.upstream.verdict_error = limited
        self.now += 8
        failed_at = self.now
        sub = self.state()["submissions"][0]
        self.assertEqual((sub["qoj_id"], sub["verdict"]), (909, "PENDING"))
        self.assertEqual(sub["next_poll_at"], failed_at + 120)
        cookie = self.client.get_cookie("qoj_vp_session")
        resumed_upstream = RecoveryClient(self.upstream.start_time)
        resumed = self.make_app(resumed_upstream).test_client()
        resumed.set_cookie("qoj_vp_session", cookie.value)
        for elapsed in (8, 60, 112):
            self.now = failed_at + elapsed
            self.assertEqual(self.state(resumed)["submissions"][0]["verdict"], "PENDING")
        self.assertFalse(any(call[0] in {"verdict", "reconcile"} for call in resumed_upstream.calls))
        self.now = failed_at + 120
        sub = self.state(resumed)["submissions"][0]
        self.assertEqual((sub["verdict"], sub["next_poll_at"]), ("AC", 0))
        self.assertEqual([call for call in resumed_upstream.calls if call[0] == "verdict"], [("verdict", 909)])
        self.assertEqual(self.source(sub_id, resumed).get_json()["source"], SOURCE)

    def test_rate_limit_without_retry_after_delays_reconciliation_at_least_sixty_seconds(self):
        sub_id = self.uncertain_submission()
        self.upstream.recovery_error = QOJError("QOJ rate limit", 429)
        self.now += 8
        failed_at = self.now
        self.assertEqual(self.state()["submissions"][0]["next_poll_at"], failed_at + 60)
        self.upstream.recovery_error = None
        for elapsed in (8, 24, 40, 52):
            self.now = failed_at + elapsed
            self.state()
        self.assertEqual(len(self.calls("reconcile")), 1)
        self.assertEqual(self.store.submission_payload(sub_id)["receipt"], self.upstream.prepared)
        self.now = failed_at + 60
        self.assertEqual(self.state()["submissions"][0]["verdict"], "AC")
        self.assertEqual(len(self.calls("reconcile")), 2)
        self.assertEqual(len(self.calls("submit")), 1)

    def test_first_post_rate_limit_waits_before_any_read_only_recovery(self):
        self.upstream.submit_error = QOJError("QOJ rejected POST due to rate limiting", 429)
        sub_id = self.uncertain_submission(expected_status=429)
        failed_at = self.now
        self.assertEqual(self.store.submissions(self.run_id)[0]["next_poll_at"], failed_at + 60)
        for elapsed in (8, 24, 40, 52):
            self.now = failed_at + elapsed
            self.state()
        self.assertEqual(self.calls("reconcile"), [])
        self.assertEqual(self.store.submission_payload(sub_id)["receipt"], self.upstream.prepared)
        self.now = failed_at + 60
        self.assertEqual(self.state()["submissions"][0]["verdict"], "AC")
        self.assertEqual(len(self.calls("submit")), 1)

    def test_saved_terminal_verdicts_are_never_polled_again(self):
        for index, verdict in enumerate(("AC", "WA", "CE", "TLE", "MLE", "OLE", "RE", "PE", "JE")):
            with self.subTest(verdict=verdict):
                sub_id = self.uncertain_submission()
                self.upstream.recovered = {"qoj_id": 1000 + index}
                self.upstream.verdict_result = {"verdict": verdict, "message": "Final fixture result"}
                self.now += 8
                state = self.state()
                sub = next(row for row in state["submissions"] if row["id"] == sub_id)
                self.assertEqual(sub["verdict"], verdict)
                completed_calls = (len(self.calls("reconcile")), len(self.calls("verdict")))
                for _ in range(3):
                    self.now += 8
                    self.state()
                self.assertEqual((len(self.calls("reconcile")), len(self.calls("verdict"))), completed_calls)

    def test_known_id_unknown_result_stops_after_first_confirmed_acceptance(self):
        run = {"id": self.run_id, "started_at": self.initial["run"]["started_at"]}
        sub_id = self.store.add_submission(run, {"id": 1, "label": "A"}, "C++17", now=self.now)
        self.store.update_submission(sub_id, verdict="UNKNOWN", qoj_id=1234)
        self.upstream.verdict_result = {"verdict": "AC", "message": "100 ✓"}
        self.now += 8
        self.assertEqual(self.state()["submissions"][0]["verdict"], "AC")
        for _ in range(3):
            self.now += 8
            self.state()
        self.assertEqual(self.calls("verdict"), [("verdict", 1234)])
        self.assertEqual(self.calls("reconcile"), [])

    def test_final_time_and_memory_survive_restart_without_more_verdict_requests(self):
        sub_id = self.uncertain_submission()
        self.upstream.verdict_result = {"verdict": "AC", "message": "100 ✓",
                                        "time_text": "15 ms", "memory_text": "3,456 KB"}
        self.now += 8
        state = self.state()
        sub = state["submissions"][0]
        self.assertEqual((sub["time_text"], sub["memory_text"]), ("15 ms", "3,456 KB"))
        cookie = self.client.get_cookie("qoj_vp_session")
        resumed_upstream = RecoveryClient(self.upstream.start_time)
        resumed = self.make_app(resumed_upstream).test_client()
        resumed.set_cookie("qoj_vp_session", cookie.value)
        for _ in range(3):
            self.now += 8
            sub = self.state(resumed)["submissions"][0]
            self.assertEqual((sub["id"], sub["verdict"], sub["time_text"], sub["memory_text"]),
                             (sub_id, "AC", "15 ms", "3,456 KB"))
        self.assertFalse(any(call[0] in {"verdict", "reconcile"} for call in resumed_upstream.calls))
        self.assertEqual(self.source(sub_id, resumed).get_json()["source"], SOURCE)

    def test_real_tl_verdict_adapter_persists_tle_and_stops_across_refresh_and_restart(self):
        run = {"id": self.run_id, "started_at": self.initial["run"]["started_at"]}
        sub_id = self.store.add_submission(run, {"id": 1, "label": "A"}, "C++17", now=self.now)
        self.store.update_submission(sub_id, verdict="UNKNOWN", qoj_id=4242)
        cookie = self.client.get_cookie("qoj_vp_session")
        parsed_upstream = ParsedVerdictClient(self.upstream.start_time)
        parsed_client = self.make_app(parsed_upstream).test_client()
        parsed_client.set_cookie("qoj_vp_session", cookie.value)
        state = self.state(parsed_client)
        sub = state["submissions"][0]
        self.assertEqual((sub["id"], sub["qoj_id"], sub["verdict"]), (sub_id, 4242, "TLE"))
        self.assertEqual((sub["time_text"], sub["memory_text"]), ("2ms", "100kb"))
        for _ in range(3):
            self.now += 8
            self.assertEqual(self.state(parsed_client)["submissions"][0]["verdict"], "TLE")
        self.assertEqual([call for call in parsed_upstream.calls if call[0] == "verdict_page"],
                         [("verdict_page", "/submission/4242")])
        self.assertFalse(any(call[0] == "reconcile" for call in parsed_upstream.calls))
        restarted_upstream = ParsedVerdictClient(self.upstream.start_time)
        restarted = self.make_app(restarted_upstream).test_client()
        restarted.set_cookie("qoj_vp_session", cookie.value)
        for _ in range(3):
            self.now += 8
            self.assertEqual(self.state(restarted)["submissions"][0]["verdict"], "TLE")
        self.assertFalse(any(call[0] in {"verdict_page", "reconcile"} for call in restarted_upstream.calls))

    def test_restart_restores_source_receipt_and_native_run_then_recovers_without_post(self):
        sub_id = self.uncertain_submission()
        cookie = self.client.get_cookie("qoj_vp_session")
        resumed_upstream = RecoveryClient(self.upstream.start_time)
        resumed_app = self.make_app(resumed_upstream)
        resumed = resumed_app.test_client()
        resumed.set_cookie("qoj_vp_session", cookie.value)
        state = self.state(resumed)
        self.assertEqual(state["run"]["id"], self.run_id)
        self.assertEqual((state["submissions"][0]["qoj_id"], state["submissions"][0]["verdict"]), (909, "AC"))
        self.assertEqual(self.source(sub_id, resumed).get_json()["source"], SOURCE)
        self.assertFalse(any(call[0] == "submit" for call in resumed_upstream.calls))

    def test_legacy_unknown_without_receipt_is_not_guessed_and_missing_source_is_explicit(self):
        run = {"id": self.run_id, "started_at": self.initial["run"]["started_at"]}
        sub_id = self.store.add_submission(run, {"id": 1, "label": "A"}, "C++17", now=self.now)
        self.store.update_submission(sub_id, verdict="UNKNOWN")
        self.now += 8
        self.assertEqual(self.state()["submissions"][0]["verdict"], "UNKNOWN")
        self.assertEqual(self.calls("reconcile"), [])
        response = self.source(sub_id)
        self.assertEqual(response.status_code, 404)
        self.assertIn("没有保存源代码", response.get_json()["error"])

    def test_source_is_private_to_captain_and_current_run_account_and_mode(self):
        sub_id = self.uncertain_submission()
        created = self.post("/api/team/create", {"name": "Source team"}).get_json()
        member = self.app.test_client()
        joined = self.post("/api/team/join", {"code": created["team"]["join_code"]}, member)
        self.assertEqual(joined.status_code, 200)
        self.assertEqual(self.source(sub_id, member).status_code, 403)
        outsider = self.app.test_client()
        self.assertEqual(self.source(sub_id, outsider).status_code, 404)
        self.assertEqual(self.source(sub_id).status_code, 200)
        self.upstream.set_status("official", "running")
        self.assertEqual(self.post("/api/mode", {"mode": "official"}).status_code, 200)
        self.assertEqual(self.source(sub_id).status_code, 404)
        self.assertEqual(self.post("/api/mode", {"mode": "vp"}).status_code, 200)
        self.upstream.username = "another-account"
        self.assertEqual(self.source(sub_id).status_code, 404)
        self.upstream.username = "native-fixture-user"
        self.upstream.start_time += 1000
        self.upstream.set_status("vp", "running")
        self.assertEqual(self.source(sub_id).status_code, 404)

    def test_revoked_contest_access_prevents_source_reads_and_recovery(self):
        sub_id = self.uncertain_submission()
        self.upstream.set_status("vp", "unregistered")
        self.now += 8
        state = self.state()
        self.assertFalse(state["permissions"]["view_content"])
        self.assertEqual(state["submissions"], [])
        self.assertEqual(self.source(sub_id).status_code, 403)
        self.assertEqual(self.calls("reconcile"), [])

    def test_poll_budget_is_one_submission_per_eight_seconds(self):
        self.uncertain_submission()
        self.uncertain_submission()
        self.upstream.recovered = {"qoj_id": None, "message": "Still pending"}
        self.now += 8
        self.state()
        self.state()
        self.assertEqual(len(self.calls("reconcile")), 1)
        self.now += 8
        self.state()
        self.assertEqual(len(self.calls("reconcile")), 2)
        self.assertEqual(len(self.calls("submit")), 2)

    def test_recovery_cannot_bind_an_id_already_used_by_this_run(self):
        sub_id = self.uncertain_submission()
        run = {"id": self.run_id, "started_at": self.initial["run"]["started_at"]}
        known = self.store.add_submission(run, {"id": 1, "label": "A"}, "C++17", now=self.now)
        self.store.update_submission(known, verdict="AC", qoj_id=909)
        self.now += 8
        self.state()
        sub = next(row for row in self.store.submissions(self.run_id) if row["id"] == sub_id)
        self.assertIsNone(sub["qoj_id"])
        self.assertEqual(sub["verdict"], "UNKNOWN")
        self.assertEqual(self.calls("verdict"), [])

    def test_account_change_during_recovery_cannot_bind_or_expose_old_run(self):
        sub_id = self.uncertain_submission()
        self.upstream.change_username_on_recovery = True
        self.now += 8
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 409)
        self.assertNotIn(SOURCE, response.get_data(as_text=True))
        self.assertIsNone(self.store.submissions(self.run_id)[0]["qoj_id"])
        self.assertEqual(self.store.submission_payload(sub_id)["receipt"], self.upstream.prepared)
        self.assertEqual(self.calls("verdict"), [])


if __name__ == "__main__":
    unittest.main()
