"""Client-wide rate-limit cooldown and recovery error propagation, offline."""
from email.utils import formatdate
import unittest
from unittest.mock import Mock, patch

from qoj import QOJClient, QOJError
from test_qoj import response
from test_submission_confirmation import CID, PID, LANG, FixtureClient, detail, listing, receipt


class ClientCooldownTests(unittest.TestCase):
    def setUp(self):
        self.ticks = 1000.0
        self.wall = 1_800_000_000.0
        for target, clock in (("qoj.time.monotonic", lambda: self.ticks), ("qoj.time.time", lambda: self.wall)):
            patcher = patch(target, side_effect=clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = self.new_client()

    def new_client(self):
        client = QOJClient()
        self.addCleanup(client.session.close)
        client.session.request = Mock()
        return client

    def assert_limited(self, client, method="GET", target="/contest/3962", **kwargs):
        with self.assertRaises(QOJError) as caught:
            client.fetch(method, target, **kwargs)
        self.assertEqual(caught.exception.status, 429)
        self.assertGreater(caught.exception.retry_after, 0)
        return caught.exception

    def test_one_429_blocks_other_get_head_and_post_without_network_then_expires(self):
        self.client.session.request.return_value = response("limited", status=429)
        self.assertGreaterEqual(self.assert_limited(self.client).retry_after, 60)
        self.client.session.request.return_value = response("ready")
        for method, url in (("GET", "/submission/999"), ("HEAD", "/download.php?type=statement&id=1"),
                            ("POST", "/contest/3962/problem/1")):
            self.assert_limited(self.client, method, url)
        self.assertEqual(self.client.session.request.call_count, 1)
        self.ticks += 7
        remaining = self.assert_limited(self.client).retry_after
        self.assertGreaterEqual(remaining, 53)
        self.assertLessEqual(remaining, 54)
        self.ticks += 53
        self.assertEqual(self.client.fetch("GET", "/contest/3962").text, "ready")
        self.assertEqual(self.client.session.request.call_count, 2)

    def test_retry_after_seconds_can_extend_the_minimum_cooldown(self):
        self.client.session.request.return_value = response("limited", status=429, **{"Retry-After": "120"})
        self.assertGreaterEqual(self.assert_limited(self.client).retry_after, 120)
        self.client.session.request.return_value = response("ready")
        self.ticks += 60
        self.assertGreaterEqual(self.assert_limited(self.client).retry_after, 60)
        self.assertEqual(self.client.session.request.call_count, 1)
        self.ticks += 60
        self.client.fetch("GET", "/submission/999")
        self.assertEqual(self.client.session.request.call_count, 2)

    def test_retry_after_http_date_is_honored(self):
        self.client.session.request.return_value = response("limited", status=429, **{
            "Date": formatdate(self.wall, usegmt=True), "Retry-After": formatdate(self.wall + 180, usegmt=True)})
        limited = self.assert_limited(self.client)
        self.assertGreaterEqual(limited.retry_after, 180)
        self.client.session.request.return_value = response("ready")
        self.ticks += 120
        self.assertGreaterEqual(self.assert_limited(self.client).retry_after, 60)
        self.assertEqual(self.client.session.request.call_count, 1)
        self.ticks += 60
        self.client.fetch("GET", "/contest/3962")
        self.assertEqual(self.client.session.request.call_count, 2)

    def test_short_invalid_or_past_retry_after_cannot_remove_the_sixty_second_minimum(self):
        for header in ("0", "1", "not-a-date", formatdate(self.wall - 3600, usegmt=True)):
            with self.subTest(header=header):
                client = self.new_client()
                client.session.request.return_value = response("limited", status=429, **{"Retry-After": header})
                self.assertGreaterEqual(self.assert_limited(client).retry_after, 60)
                client.session.request.return_value = response("ready")
                self.assert_limited(client, "POST", "/login")
                self.assertEqual(client.session.request.call_count, 1)

    def test_cooldown_uses_monotonic_time_when_wall_clock_changes(self):
        self.client.session.request.return_value = response("limited", status=429)
        self.assert_limited(self.client)
        self.client.session.request.return_value = response("ready")
        self.wall += 1_000_000
        self.ticks += 10
        self.assertGreaterEqual(self.assert_limited(self.client).retry_after, 50)
        self.wall -= 2_000_000
        self.ticks += 10
        self.assertGreaterEqual(self.assert_limited(self.client).retry_after, 40)
        self.assertEqual(self.client.session.request.call_count, 1)
        self.ticks += 40
        self.client.fetch("GET", "/submission/999")
        self.assertEqual(self.client.session.request.call_count, 2)

    def test_clear_and_import_cookies_do_not_reset_rate_limit_cooldown(self):
        self.client.session.request.return_value = response("limited", status=429)
        self.assert_limited(self.client)
        self.client.session.request.return_value = response("ready")
        self.client.clear()
        self.assert_limited(self.client, "GET", "/login")
        self.client.import_cookies("Cookie: session=offline-fixture")
        self.assert_limited(self.client, "POST", "/contest/3962/problem/1")
        self.assertEqual(self.client.session.request.call_count, 1)

    def test_429_after_a_redirect_blocks_all_followup_requests(self):
        self.client.session.request.side_effect = [
            response(status=302, Location="/submission/999"),
            response("limited", url="https://qoj.ac/submission/999", status=429),
        ]
        self.assert_limited(self.client)
        self.assertEqual(self.client.session.request.call_count, 2)
        self.assert_limited(self.client, "POST", "/contest/3962/problem/1")
        self.assertEqual(self.client.session.request.call_count, 2)


class ConfirmationBackoffPropagationTests(unittest.TestCase):
    def test_candidate_detail_rate_limit_and_server_failure_reach_persistent_backoff(self):
        for status in (429, 502):
            with self.subTest(status=status):
                failure = QOJError("Candidate detail temporarily unavailable", status)
                failure.retry_after = 90
                client = FixtureClient(pages=[listing(101, 102)], details={101: failure, 102: detail(102)})
                self.addCleanup(client.session.close)
                with self.assertRaises(QOJError) as caught:
                    client.reconcile_submission(CID, PID, LANG, receipt())
                self.assertIs(caught.exception, failure)
                self.assertEqual(caught.exception.retry_after, 90)
                self.assertNotIn("/submission/102", client.gets)
                self.assertEqual(client.posts, [])


if __name__ == "__main__":
    unittest.main()
