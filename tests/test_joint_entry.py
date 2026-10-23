"""Joint VP authorization through an offline, real requests transport."""
import json
import unittest
from urllib.parse import urlsplit

from joint_entry import authorize_entry, prepare_entry, validate_preparation
from qoj import QOJClient, QOJError
from test_vp_registration_flow import VirtualRegistrationAdapter


class JointEntryAdapter(VirtualRegistrationAdapter):
    def __init__(self):
        super().__init__()
        self.registration_available = True
        self.terms = "Please follow the contest rules."
        self.changed_terms_after = None
        self.optional_checkbox = False
        self.reveal_confirmation_after_post = False
        self.registration_error = None

    def confirmation(self):
        source = super().confirmation()
        terms = self.terms
        if self.changed_terms_after is not None and self.confirmation_reads > self.changed_terms_after:
            terms = "Changed registration agreement requiring renewed consent."
        if self.optional_checkbox:
            source = source.replace('<button name="submit-register_vp"',
                                    '<label><input type="checkbox" name="public" value="yes" checked>Share profile</label>'
                                    '<button name="submit-register_vp"')
        return source.replace('<button name="submit-register_vp"',
                              f'<p>{terms}</p><script>var transient = "{self.latest_confirmation_token}";</script>'
                              '<button name="submit-register_vp"')

    def send(self, request, **kwargs):
        path = urlsplit(request.url).path
        if request.method == "GET" and path == "/contest/9/virtual/register":
            if self.registration_error or not self.registration_available:
                self.requests.append(request)
                return self.response(request, self.page("Unavailable"), self.registration_error or 404)
        if request.method == "POST" and path == "/contest/9" and self.reveal_confirmation_after_post:
            self.registration_available = True
        return super().send(request, **kwargs)


class JointEntryTests(unittest.TestCase):
    def setUp(self):
        self.client = QOJClient()
        self.addCleanup(self.client.session.close)
        self.adapter = JointEntryAdapter()
        self.client.session.adapters.clear()
        self.client.session.mount("https://qoj.ac/", self.adapter)
        self.client.authenticated = True
        self.client.username = self.adapter.username

    def launch(self, plan, values=None):
        return authorize_entry(self.client, 9, plan["fingerprint"], plan["action"]["id"],
                               {} if values is None else values)

    def test_prepare_final_form_with_get_only_and_no_secret_or_contest_content(self):
        plan = prepare_entry(self.client, 9)
        self.assertEqual(plan["action"]["id"], "virtual-register:register_vp")
        self.assertIn(self.adapter.terms, plan["message"])
        self.assertEqual(len(plan["fingerprint"]), 64)
        self.assertEqual(self.adapter.posts, [])
        self.assertFalse(self.adapter.entered)
        text = json.dumps(plan)
        for secret in ("fixture-confirmation-", "fixture-home-", "_token", "Hidden A", "Hidden statements"):
            self.assertNotIn(secret, text)
        self.assertEqual(self.client._vp_entry_pages[(9, self.client.username)],
                         "https://qoj.ac/contest/9/virtual/register")

    def test_new_csrf_token_does_not_change_authorization_or_prevent_one_final_post(self):
        first = prepare_entry(self.client, 9)
        old_token = self.adapter.latest_confirmation_token
        second = prepare_entry(self.client, 9)
        self.assertNotEqual(old_token, self.adapter.latest_confirmation_token)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        result = self.launch(first)
        self.assertEqual(result["status"], "running")
        self.assertEqual(len(self.adapter.posts), 1)
        request = self.adapter.posts[0]
        self.assertEqual(urlsplit(request.url).path, "/contest/9/virtual/register")
        self.assertNotEqual(self.adapter.body(request)["_token"], [old_token])
        self.assertEqual(self.adapter.body(request)["_token"], [self.adapter.latest_confirmation_token])
        self.assertEqual(self.launch(first)["status"], "running")
        self.assertEqual(len(self.adapter.posts), 1)

    def test_one_step_server_falls_back_on_missing_registration_get(self):
        self.adapter.registration_available = False
        self.adapter.legacy_one_step = True
        plan = prepare_entry(self.client, 9)
        self.assertEqual(plan["action"]["id"], "regvir")
        self.assertEqual(self.adapter.posts, [])
        self.assertEqual(self.launch(plan)["status"], "running")
        self.assertEqual(len(self.adapter.posts), 1)
        self.assertEqual(urlsplit(self.adapter.posts[0].url).path, "/contest/9")

    def test_changed_terms_or_schema_invalidate_ready_without_posting(self):
        for change in ("terms", "schema"):
            with self.subTest(change=change):
                self.adapter.terms = "Original agreement"
                self.adapter.with_checkbox = False
                plan = prepare_entry(self.client, 9)
                if change == "terms":
                    self.adapter.terms = "A different agreement"
                else:
                    self.adapter.with_checkbox = True
                with self.assertRaises(QOJError) as caught:
                    self.launch(plan)
                self.assertIn(caught.exception.status, (400, 409))
                self.assertEqual(self.adapter.posts, [])

    def test_change_between_precheck_and_final_get_is_rejected_at_post_boundary(self):
        plan = prepare_entry(self.client, 9)
        # enter() reads the confirmation once for participation, then once
        # more for POST. The final read changes terms with identical fields.
        self.adapter.changed_terms_after = self.adapter.confirmation_reads + 1
        with self.assertRaises(QOJError) as caught:
            self.launch(plan)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.adapter.posts, [])

    def test_checkbox_consent_is_explicit_and_field_injection_is_rejected(self):
        self.adapter.with_checkbox = True
        plan = prepare_entry(self.client, 9)
        for values in ({}, {"agree": False}, {"agree": "yes"}, {"agree": True, "injected": "x"}):
            with self.subTest(values=values), self.assertRaises(QOJError):
                self.launch(plan, values)
            self.assertEqual(self.adapter.posts, [])
        validate_preparation(plan, plan["action"]["id"], {"agree": True})
        self.assertEqual(self.launch(plan, {"agree": True})["status"], "running")
        self.assertEqual(len(self.adapter.posts), 1)

    def test_optional_default_checked_cannot_be_silently_accepted(self):
        self.adapter.optional_checkbox = True
        plan = prepare_entry(self.client, 9)
        with self.assertRaises(QOJError):
            validate_preparation(plan, plan["action"]["id"], {})
        with self.assertRaises(QOJError):
            self.launch(plan)
        self.assertEqual(self.adapter.posts, [])
        self.assertEqual(self.launch(plan, {"public": False})["status"], "running")
        self.assertNotIn("public", self.adapter.body(self.adapter.posts[0]))

    def test_unexpected_second_confirmation_requires_new_consent_without_second_post(self):
        self.adapter.registration_available = False
        plan = prepare_entry(self.client, 9)
        self.adapter.reveal_confirmation_after_post = True
        self.adapter.with_checkbox = True
        state = self.launch(plan)
        self.assertEqual(state["status"], "unregistered")
        self.assertTrue(state["entry_pending"])
        self.assertEqual(state["entry_stage"], "confirmation")
        self.assertFalse(self.adapter.entered)
        self.assertEqual(len(self.adapter.posts), 1)
        self.assertEqual(state["actions"][0]["fields"][0]["name"], "agree")

    def test_prepare_refuses_existing_participation_and_auth_or_permission_errors(self):
        self.adapter.entered = True
        with self.assertRaises(QOJError) as caught:
            prepare_entry(self.client, 9)
        self.assertEqual(caught.exception.status, 409)
        self.adapter.entered = False
        for status in (401, 403):
            with self.subTest(status=status):
                self.client.authenticated = True
                self.client.username = self.adapter.username
                self.adapter.registration_error = status
                with self.assertRaises(QOJError) as caught:
                    prepare_entry(self.client, 9)
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(self.adapter.posts, [])

    def test_account_changed_since_preparation_cannot_use_previous_grant(self):
        plan = prepare_entry(self.client, 9)
        self.adapter.username = "different-team"
        self.client.username = "different-team"
        with self.assertRaises(QOJError) as caught:
            self.launch(plan)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.adapter.posts, [])

    def test_wrong_contest_or_action_cannot_post(self):
        plan = prepare_entry(self.client, 9)
        with self.assertRaises(QOJError):
            authorize_entry(self.client, 10, plan["fingerprint"], plan["action"]["id"], {})
        with self.assertRaises(QOJError):
            authorize_entry(self.client, 9, plan["fingerprint"], "regvir", {})
        self.assertEqual(self.adapter.posts, [])

    def test_timeout_never_automatically_resends(self):
        plan = prepare_entry(self.client, 9)
        self.adapter.post_failure = "timeout-after-commit"
        with self.assertRaises(QOJError):
            self.launch(plan)
        self.assertEqual(len(self.adapter.posts), 1)
        state = self.launch(plan)
        self.assertEqual(state["status"], "running")
        self.assertEqual(len(self.adapter.posts), 1)


if __name__ == "__main__":
    unittest.main()
