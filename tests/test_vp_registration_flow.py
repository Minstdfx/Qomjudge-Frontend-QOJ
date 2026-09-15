"""Two-step VP registration through real requests and Flask, entirely offline.

Accounts, contest metadata and tokens are synthetic. The default confirmation
form preserves the user-provided register_vp field names and structure. An
optional synthetic checkbox variant remains unchecked until the user confirms it.
"""
from email.message import Message
from email.utils import formatdate
import html
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import BaseAdapter

from app import create_app
from qoj import QOJClient, QOJError
import test_participation as fixtures


class VirtualRegistrationAdapter(BaseAdapter):
    """HTTP-shaped QOJ fixture reusable by isolated browser regressions."""

    def __init__(self, with_checkbox=False):
        self.requests = []
        self.entered = False
        self.username = "fixture-team"
        self.confirmation_reads = 0
        self.home_reads = 0
        self.latest_confirmation_token = ""
        self.latest_home_token = ""
        self.post_failure = None
        self.first_redirect = "/contest/9/virtual/register"
        self.confirmation_action = "/contest/9/virtual/register"
        self.legacy_one_step = False
        self.start = fixtures.NOW - 60
        self.with_checkbox = with_checkbox
        self.confirmation_gets_before_expiry = None

    @property
    def posts(self):
        return [request for request in self.requests if request.method == "POST"]

    def body(self, request):
        raw = request.body or ""
        return parse_qs(raw.decode("utf-8") if isinstance(raw, bytes) else raw, keep_blank_values=True)

    def page(self, content):
        username = html.escape(self.username, quote=True)
        return (f'<!doctype html><html><head><title>Fixture VP - QOJ.ac</title></head><body>'
                f'<nav><a href="/user/profile/{username}">{username}</a><a href="/logout">Logout</a></nav>'
                '<h1>Offline VP fixture</h1>' + content + '</body></html>')

    def confirmation(self):
        self.confirmation_reads += 1
        self.latest_confirmation_token = f"fixture-confirmation-{self.confirmation_reads}"
        action = html.escape(self.confirmation_action, quote=True)
        consent = ('<label><input type="checkbox" name="agree" value="yes" required>'
                   'I confirm starting this virtual contest.</label>') if self.with_checkbox else ''
        return self.page(f'''<form id="form-register_vp" method="post" action="{action}">
            <input type="hidden" name="_token" value="{self.latest_confirmation_token}">
            {consent}
            <button name="submit-register_vp" type="submit" value="register_vp">Confirm registration</button>
            </form>''' + fixtures.PROBLEM_TABLE + fixtures.ATTACHMENT)

    def response(self, request, body="", status=200, location=None):
        response = requests.Response()
        response.status_code = status
        response.request = request
        response.url = request.url
        response.encoding = "utf-8"
        response.headers.update({"Content-Type": "text/html; charset=utf-8", "Date": formatdate(fixtures.NOW, usegmt=True)})
        message = Message()
        if location:
            response.headers["Location"] = location
            message.add_header("Location", location)
        response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=message),
                                       close=lambda: None, release_conn=lambda: None)
        response._content = body.encode("utf-8")
        response._content_consumed = True
        return response

    def send(self, request, **_kwargs):
        self.requests.append(request)
        path = urlsplit(request.url).path
        if request.method == "POST" and path == "/contest/9":
            payload = self.body(request)
            if payload != {"_token": [self.latest_home_token], "submit-regvir": ["regvir"]}:
                return self.response(request, self.page('<div class="alert-danger">Invalid initial fixture fields.</div>'))
            if self.legacy_one_step:
                self.entered = True
                return self.response(request, status=303, location="/contest/9")
            return self.response(request, status=303, location=self.first_redirect)
        if request.method == "POST" and path == "/contest/9/virtual/register":
            payload = self.body(request)
            if self.post_failure == "timeout":
                raise requests.Timeout("Fixture uncertain confirmation request")
            if self.post_failure == "timeout-after-commit":
                self.entered = True
                raise requests.Timeout("Fixture response lost after confirmation")
            if self.post_failure == "reject":
                return self.response(request, self.page('<div class="alert-danger">Fixture registration rejected.</div>'))
            expected = {"_token": [self.latest_confirmation_token], "submit-register_vp": ["register_vp"]}
            if self.with_checkbox:
                expected["agree"] = ["yes"]
            if payload != expected:
                return self.response(request, self.page('<div class="alert-danger">Invalid or expired confirmation fields.</div>'))
            self.entered = True
            return self.response(request, status=303, location="/contest/9")
        if request.method != "GET":
            raise AssertionError(f"Unexpected fixture method: {request.method} {path}")
        if path in ("/contest/9", "/contest/10"):
            self.home_reads += 1
            self.latest_home_token = f"fixture-home-{self.home_reads}"
            if self.entered and path == "/contest/9":
                content = '<div class="card"><h4 class="card-header">Virtual Contest</h4><div class="countdown" data-rest="17940"></div></div>'
            else:
                content = (f'<form id="form-regvir" method="post" action="{path}">'
                           f'<input type="hidden" name="_token" value="{self.latest_home_token}">'
                           '<button name="submit-regvir" value="regvir">Start!</button></form>')
            return self.response(request, self.page(content + fixtures.LINKS + fixtures.PROBLEM_TABLE + fixtures.ATTACHMENT))
        if path == "/contest/9/virtual/register":
            if self.confirmation_gets_before_expiry is not None:
                if self.confirmation_gets_before_expiry <= 0:
                    return self.response(request, status=302, location="/contest/9")
                self.confirmation_gets_before_expiry -= 1
            return self.response(request, self.confirmation())
        if path == "/contest/9/registrants":
            content = fixtures.registrants(self.start, username=self.username) if self.entered else "<table></table>"
            return self.response(request, self.page(content))
        if path == "/contest/9/standings":
            if not self.entered:
                raise AssertionError("Read scoreboard before confirming VP")
            return self.response(request, self.page(fixtures.scoreboard()))
        raise AssertionError("Unexpected fixture endpoint: " + path)

    def close(self):
        pass


class VPRegistrationFlowTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.upstream = QOJClient()
        self.addCleanup(self.upstream.session.close)
        self.adapter = VirtualRegistrationAdapter()
        self.upstream.session.mount("https://qoj.ac/", self.adapter)
        self.upstream.authenticated = True
        self.upstream.username = self.adapter.username
        network = patch("requests.adapters.HTTPAdapter.send", side_effect=AssertionError("No real network in VP registration tests"))
        network.start()
        self.addCleanup(network.stop)
        self.app = create_app(contest_id=9, mode="vp", data_dir=Path(directory.name), client_factory=lambda: self.upstream)
        self.app.config["TESTING"] = True
        self.browser = self.app.test_client()
        self.csrf = self.state()["csrf_token"]

    def state(self):
        response = self.browser.get("/api/state")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def post(self, path, body=None):
        return self.browser.post(path, json={} if body is None else body,
                                 headers={"X-CSRF-Token": self.csrf})

    def start(self, state, values=None):
        action = state["participation"]["actions"][0]["id"]
        return self.post("/api/start", {"mode": "vp", "action": action, "selection": {} if values is None else values})

    def assert_hidden(self, state):
        self.assertFalse(state["participation"]["content_available"])
        self.assertFalse(state["permissions"]["view_content"])
        self.assertFalse(state["permissions"]["submit"])
        self.assertEqual(state["contest"]["problems"], [])
        self.assertEqual(state["contest"]["attachments"], [])
        self.assertEqual(state["scoreboard"], [])
        self.assertIsNone(state["run"])

    def assert_confirmation(self, state):
        self.assertEqual(state["participation"]["status"], "unregistered")
        self.assertEqual(state["participation"].get("entry_stage"), "confirmation")
        self.assertFalse(state["participation"].get("entry_uncertain", False))
        self.assert_hidden(state)
        self.assertEqual(len(state["participation"]["actions"]), 1)

    def begin_confirmation(self):
        before = len(self.adapter.posts)
        response = self.start(self.state())
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(len(self.adapter.posts), before + 1)
        state = response.get_json()
        self.assert_confirmation(state)
        self.assertEqual(urlsplit(self.adapter.posts[-1].url).path, "/contest/9")
        return state

    def test_first_post_follows_303_to_confirmation_without_starting_or_exposing_content(self):
        state = self.begin_confirmation()
        fields = state["participation"]["actions"][0]["fields"]
        self.assertEqual(fields, [])
        self.assertFalse(self.adapter.entered)
        self.assertNotIn("agree", self.adapter.body(self.adapter.posts[0]))
        for _ in range(3):
            self.assert_confirmation(self.state())
        refreshed = self.post("/api/refresh")
        self.assertEqual(refreshed.status_code, 200, refreshed.get_data(as_text=True))
        self.assert_confirmation(self.state())
        self.assertEqual(len(self.adapter.posts), 1)
        self.assertFalse(any("/standings" in request.url or "/problem/" in request.url for request in self.adapter.requests))
        for path in ("/api/problems/10", "/api/attachments"):
            self.assertEqual(self.browser.get(path).status_code, 403)

    def test_second_explicit_confirmation_refetches_token_and_sends_one_post_to_continuation(self):
        state = self.begin_confirmation()
        displayed_token = self.adapter.latest_confirmation_token
        response = self.start(state)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        running = response.get_json()
        self.assertEqual(len(self.adapter.posts), 2)
        request = self.adapter.posts[-1]
        self.assertEqual(urlsplit(request.url).path, "/contest/9/virtual/register")
        self.assertNotEqual(self.adapter.body(request)["_token"], [displayed_token])
        self.assertEqual(self.adapter.body(request)["_token"], [self.adapter.latest_confirmation_token])
        self.assertEqual(self.adapter.body(request), {"_token": [self.adapter.latest_confirmation_token],
                                                      "submit-register_vp": ["register_vp"]})
        self.assertEqual(running["participation"]["status"], "running")
        self.assertTrue(running["participation"]["content_available"])
        self.assertTrue(running["permissions"]["submit"])
        self.assertEqual(running["run"]["started_at"], self.adapter.start)
        self.assertEqual(running["run"]["ends_at"], self.adapter.start + 18000)
        self.assertEqual([problem["id"] for problem in running["contest"]["problems"]], [10, 11])
        self.assertTrue(running["scoreboard"])
        self.assertEqual(self.state()["run"]["id"], running["run"]["id"])
        self.assertEqual(len(self.adapter.posts), 2)

    def test_missing_consent_or_injected_field_never_posts_confirmation(self):
        self.adapter.with_checkbox = True
        for values in ({}, {"agree": False}, {"agree": True, "_token": "injected"},
                       {"agree": True, "unexpected": "field"}):
            with self.subTest(values=values):
                state = self.state()
                if state["participation"].get("entry_stage") != "confirmation":
                    state = self.begin_confirmation()
                before = len(self.adapter.posts)
                response = self.start(state, values)
                self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
                self.assertEqual(len(self.adapter.posts), before)
                self.assert_confirmation(self.state())

    def test_synthetic_required_checkbox_is_not_selected_or_posted_automatically(self):
        self.adapter.with_checkbox = True
        state = self.begin_confirmation()
        agree = next(field for field in state["participation"]["actions"][0]["fields"] if field["name"] == "agree")
        self.assertEqual(agree["type"], "checkbox")
        self.assertTrue(agree["required"])
        self.assertFalse(agree.get("checked", False))
        self.assert_confirmation(self.state())
        self.assertEqual(len(self.adapter.posts), 1)
        response = self.start(state, {"agree": True})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["participation"]["status"], "running")
        self.assertEqual(self.adapter.body(self.adapter.posts[-1])["agree"], ["yes"])
        self.assertEqual(len(self.adapter.posts), 2)

    def test_rejected_confirmation_keeps_second_step_without_resending_initial_post(self):
        state = self.begin_confirmation()
        self.adapter.post_failure = "reject"
        response = self.start(state)
        self.assertEqual(response.status_code, 422, response.get_data(as_text=True))
        self.assertIn("Fixture registration rejected", response.get_json()["error"])
        self.assertEqual(len(self.adapter.posts), 2)
        self.assert_confirmation(self.state())
        self.assert_confirmation(self.state())
        self.assertEqual(len(self.adapter.posts), 2)
        self.adapter.post_failure = None
        response = self.start(self.state())
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["participation"]["status"], "running")
        self.assertEqual([urlsplit(request.url).path for request in self.adapter.posts],
                         ["/contest/9", "/contest/9/virtual/register", "/contest/9/virtual/register"])

    def test_uncertain_confirmation_preserves_step_and_never_automatically_retries(self):
        state = self.begin_confirmation()
        self.adapter.post_failure = "timeout"
        response = self.start(state)
        self.assertEqual(response.status_code, 502, response.get_data(as_text=True))
        self.assertEqual(len(self.adapter.posts), 2)
        for _ in range(3):
            self.assert_confirmation(self.state())
        self.assertEqual(len(self.adapter.posts), 2)

    def test_lost_response_after_success_recovers_running_by_get_without_reposting(self):
        state = self.begin_confirmation()
        self.adapter.post_failure = "timeout-after-commit"
        response = self.start(state)
        self.assertEqual(response.status_code, 502, response.get_data(as_text=True))
        recovered = self.state()
        self.assertEqual(recovered["participation"]["status"], "running")
        self.assertTrue(recovered["participation"]["content_available"])
        self.assertEqual(len(self.adapter.posts), 2)

    def test_pending_confirmation_is_not_used_for_other_contest_mode_or_account(self):
        self.begin_confirmation()
        for contest_id, mode in ((10, "vp"), (9, "official")):
            with self.subTest(contest_id=contest_id, mode=mode):
                before = len(self.adapter.requests)
                state = self.upstream.participation(contest_id, mode)
                self.assertNotEqual(state.get("entry_stage"), "confirmation")
                new_requests = self.adapter.requests[before:]
                self.assertFalse(any(urlsplit(request.url).path == "/contest/9/virtual/register" for request in new_requests))
                self.assertEqual(len(self.adapter.posts), 1)
        self.adapter.username = "other-fixture-account"
        before = len(self.adapter.requests)
        state = self.state()
        self.assertNotEqual(state["participation"].get("entry_stage"), "confirmation")
        self.assert_hidden(state)
        self.assertFalse(any(urlsplit(request.url).path == "/contest/9/virtual/register" for request in self.adapter.requests[before:]))
        self.assertEqual(len(self.adapter.posts), 1)

    def test_external_or_other_contest_redirect_cannot_become_a_continuation(self):
        for target in ("https://other.invalid/contest/9/virtual/register", "/contest/10"):
            with self.subTest(target=target):
                self.adapter.first_redirect = target
                before = len(self.adapter.posts)
                response = self.start(self.state())
                self.assertGreaterEqual(response.status_code, 400, response.get_data(as_text=True))
                self.assertEqual(len(self.adapter.posts), before + 1)
                state = self.state()
                self.assertNotEqual(state["participation"].get("entry_stage"), "confirmation")
                self.assert_hidden(state)
                self.assertFalse(any(urlsplit(request.url).hostname != "qoj.ac" for request in self.adapter.requests))

    def test_confirmation_form_cannot_post_outside_current_contest(self):
        for target in ("https://other.invalid/contest/9/virtual/register", "/contest/10/virtual/register", "/logout"):
            with self.subTest(target=target):
                # Each malformed advertised form represents a separate session;
                # a previous rejected continuation must not seed the next case.
                self.upstream.clear()
                self.upstream.authenticated = True
                self.upstream.username = self.adapter.username
                self.adapter.confirmation_action = target
                response = self.start(self.state())
                self.assertGreaterEqual(response.status_code, 400, response.get_data(as_text=True))
                self.assert_hidden(self.state())
                self.assertTrue(all(urlsplit(request.url).path == "/contest/9" for request in self.adapter.posts))

    def test_legacy_one_post_vp_still_enters_directly(self):
        self.adapter.legacy_one_step = True
        response = self.start(self.state())
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        state = response.get_json()
        self.assertEqual(state["participation"]["status"], "running")
        self.assertNotEqual(state["participation"].get("entry_stage"), "confirmation")
        self.assertTrue(state["permissions"]["submit"])
        self.assertEqual(len(self.adapter.posts), 1)
        self.assertEqual(self.adapter.confirmation_reads, 0)

    def test_expired_confirmation_redirect_restores_start_and_drops_stale_hint(self):
        original_action = self.state()["participation"]["actions"][0]["id"]
        self.begin_confirmation()
        self.adapter.confirmation_gets_before_expiry = 0
        restored = self.state()
        self.assertEqual(restored["participation"]["status"], "unregistered")
        self.assertNotEqual(restored["participation"].get("entry_stage"), "confirmation")
        self.assertEqual(restored["participation"]["actions"][0]["id"], original_action)
        self.assertIsNone(restored["contest_error"])
        self.assert_hidden(restored)
        after_redirect = len(self.adapter.requests)
        for _ in range(3):
            refreshed = self.state()
            self.assertEqual(refreshed["participation"]["actions"][0]["id"], original_action)
            self.assertIsNone(refreshed["contest_error"])
        self.assertFalse(any(urlsplit(request.url).path == "/contest/9/virtual/register"
                             for request in self.adapter.requests[after_redirect:]))
        self.assertEqual(len(self.adapter.posts), 1)
        # A new explicit Start remains usable after QOJ offers a fresh step.
        self.adapter.confirmation_gets_before_expiry = None
        response = self.start(restored)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assert_confirmation(response.get_json())
        self.assertEqual(len(self.adapter.posts), 2)

    def test_expiring_between_state_and_fresh_token_get_never_posts_another_form(self):
        original_action = self.state()["participation"]["actions"][0]["id"]
        confirmed_page = self.begin_confirmation()
        action = confirmed_page["participation"]["actions"][0]["id"]
        self.adapter.confirmation_gets_before_expiry = 1
        before_posts = len(self.adapter.posts)
        before_requests = len(self.adapter.requests)
        with self.assertRaises(QOJError) as raised:
            self.upstream.enter(9, "vp", selection={"action": action, "values": {}})
        self.assertEqual(raised.exception.status, 409)
        requests_after = self.adapter.requests[before_requests:]
        self.assertEqual(sum(urlsplit(request.url).path == "/contest/9/virtual/register"
                             for request in requests_after), 2)
        self.assertTrue(all(request.method == "GET" for request in requests_after))
        self.assertEqual(len(self.adapter.posts), before_posts)
        restored = self.state()
        self.assertEqual(restored["participation"]["status"], "unregistered")
        self.assertNotEqual(restored["participation"].get("entry_stage"), "confirmation")
        self.assertEqual(restored["participation"]["actions"][0]["id"], original_action)
        self.assertIsNone(restored["contest_error"])
        self.assert_hidden(restored)
        self.assertEqual(len(self.adapter.posts), before_posts)


if __name__ == "__main__":
    unittest.main()
