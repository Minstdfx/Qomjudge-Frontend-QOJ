"""Offline transport regression for imported QOJ cookies and Set-Cookie updates.

The adapter supplies real response headers to requests' normal cookie extraction;
the tests do not replace Session.request or contact any upstream server.
"""
from collections import deque
from contextlib import nullcontext
from email.message import Message
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from requests import Response
from requests.adapters import BaseAdapter

from qoj import QOJClient


def python312_deepvalues(mapping):
    """CPython 3.12 CookieJar iteration preserves nested mapping insertion order."""
    for value in list(mapping.values()):
        try:
            value.items
        except AttributeError:
            yield value
        else:
            yield from python312_deepvalues(value)


class CookieResponseAdapter(BaseAdapter):
    def __init__(self):
        self.responses = deque()
        self.requests = []

    def queue(self, *cookies, status=200, location=None):
        self.responses.append((cookies, status, location))

    def send(self, request, **_kwargs):
        self.requests.append(request)
        cookies, status, location = self.responses.popleft() if self.responses else ((), 200, None)
        message = Message()
        for cookie in cookies:
            message.add_header("Set-Cookie", cookie)
        response = Response()
        response.status_code = status
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        if location is not None:
            response.headers["Location"] = location
            message.add_header("Location", location)
        if cookies:
            # RequestsCookieJar reads msg.get_all(), not this flattened view.
            response.headers["Set-Cookie"] = ", ".join(cookies)
        response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=message),
                                       close=lambda: None, release_conn=lambda: None)
        response._content = b"<!doctype html><p>Offline cookie response</p>"
        response._content_consumed = True
        return response

    def close(self):
        pass


class QOJCookieTransportTests(unittest.TestCase):
    def setUp(self):
        self.client = QOJClient()
        self.addCleanup(self.client.session.close)
        self.adapter = CookieResponseAdapter()
        self.client.session.mount("https://qoj.ac/", self.adapter)
        network = patch("requests.adapters.HTTPAdapter.send", side_effect=AssertionError("No network in cookie tests"))
        network.start()
        self.addCleanup(network.stop)

    def import_fixture(self):
        self.client.import_cookies("Cookie: UOJSESSID=fixture-imported; preference=en; remember=fixture-account")

    def values(self, name, request=None):
        request = request or self.adapter.requests[-1]
        pairs = [part.strip().split("=", 1) for part in request.headers.get("Cookie", "").split(";")]
        return [pair[1] for pair in pairs if len(pair) == 2 and pair[0] == name]

    def assert_other_cookies_preserved(self):
        self.assertEqual(self.values("preference"), ["en"])
        self.assertEqual(self.values("remember"), ["fixture-account"])

    def fetch(self, method="GET"):
        arguments = {"data": {"submit-regvir": "regvir", "_token": "fixture-csrf"}} if method == "POST" else {}
        self.client.fetch(method, "/contest/9001", **arguments)

    def iteration(self, python312):
        return patch("http.cookiejar.deepvalues", python312_deepvalues) if python312 else nullcontext()

    def test_domain_and_host_only_updates_replace_import_on_next_get_and_post(self):
        for python312 in (False, True):
            for domain in ("; Domain=qoj.ac", "; Domain=.qoj.ac", ""):
                with self.subTest(python312=python312, domain=domain), self.iteration(python312):
                    self.import_fixture()
                    self.adapter.queue(f"UOJSESSID=fixture-updated{domain}; Path=/; Secure; HttpOnly")
                    self.fetch()
                    for method in ("GET", "POST"):
                        self.fetch(method)
                        self.assertEqual(self.adapter.requests[-1].method, method)
                        self.assertEqual(self.values("UOJSESSID"), ["fixture-updated"])
                        self.assert_other_cookies_preserved()
                    candidates = [cookie for cookie in self.client.session.cookies if cookie.name == "UOJSESSID" and cookie.path == "/"]
                    self.assertEqual(len(candidates), 1)

    def test_switching_cookie_domain_representation_always_uses_latest_value(self):
        with self.iteration(True):
            self.import_fixture()
            for index, domain in enumerate(("; Domain=.qoj.ac", "", "; Domain=qoj.ac", "")):
                with self.subTest(index=index, domain=domain):
                    expected = f"fixture-version-{index}"
                    self.adapter.queue(f"UOJSESSID={expected}{domain}; Path=/; Secure")
                    self.fetch()
                    self.fetch("POST")
                    self.assertEqual(self.values("UOJSESSID"), [expected])
                    self.assert_other_cookies_preserved()

    def test_expiration_removes_imported_cookie_without_leaving_alias_to_revive(self):
        for python312 in (False, True):
            for domain in ("; Domain=.qoj.ac", "; Domain=qoj.ac", ""):
                for expiration in ("Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT"):
                    with self.subTest(python312=python312, domain=domain, expiration=expiration), self.iteration(python312):
                        self.import_fixture()
                        self.adapter.queue(f"UOJSESSID=deleted{domain}; Path=/; Secure; {expiration}")
                        self.fetch()
                        self.fetch("POST")
                        self.assertEqual(self.values("UOJSESSID"), [])
                        self.assert_other_cookies_preserved()
                        self.assertFalse(any(cookie.name == "UOJSESSID" for cookie in self.client.session.cookies))

    def test_updated_cookie_can_be_deleted_using_other_domain_representation(self):
        with self.iteration(True):
            for update_domain, delete_domain in (("; Domain=.qoj.ac", ""), ("", "; Domain=.qoj.ac")):
                with self.subTest(update_domain=update_domain, delete_domain=delete_domain):
                    self.import_fixture()
                    self.adapter.queue(f"UOJSESSID=fixture-rotated{update_domain}; Path=/; Secure")
                    self.fetch()
                    self.adapter.queue(f"UOJSESSID=deleted{delete_domain}; Path=/; Secure; Max-Age=0")
                    self.fetch()
                    self.fetch()
                    self.assertEqual(self.values("UOJSESSID"), [])
                    self.assert_other_cookies_preserved()

    def test_post_redirect_uses_updated_cookie_and_never_repeats_post(self):
        with self.iteration(True):
            self.import_fixture()
            self.adapter.queue("UOJSESSID=fixture-after-post; Domain=.qoj.ac; Path=/; Secure",
                               status=303, location="/contest/9001")
            self.fetch("POST")
            self.assertEqual([request.method for request in self.adapter.requests], ["POST", "GET"])
            self.assertEqual(self.values("UOJSESSID"), ["fixture-after-post"])
            self.assertIsNone(self.adapter.requests[-1].body)
            self.assert_other_cookies_preserved()

    def test_separate_set_cookie_headers_update_independently(self):
        with self.iteration(True):
            self.import_fixture()
            self.adapter.queue("UOJSESSID=fixture-session; Domain=.qoj.ac; Path=/; Secure",
                               "preference=zh; Domain=.qoj.ac; Path=/; Secure")
            self.fetch()
            self.fetch("POST")
            self.assertEqual(self.values("UOJSESSID"), ["fixture-session"])
            self.assertEqual(self.values("preference"), ["zh"])
            self.assertEqual(self.values("remember"), ["fixture-account"])

    def test_offsite_set_cookie_is_ignored_without_deleting_valid_qoj_cookie(self):
        with self.iteration(True):
            self.import_fixture()
            self.adapter.queue("UOJSESSID=fixture-offsite; Domain=other.invalid; Path=/; Secure")
            self.fetch()
            self.fetch("POST")
            self.assertEqual(self.values("UOJSESSID"), ["fixture-imported"])
            self.assert_other_cookies_preserved()

    def test_same_name_cookies_at_different_paths_update_and_delete_independently(self):
        with self.iteration(True):
            self.import_fixture()
            self.adapter.queue("UOJSESSID=fixture-contest; Domain=.qoj.ac; Path=/contest; Secure")
            self.fetch()
            self.fetch("POST")
            self.assertEqual(self.values("UOJSESSID"), ["fixture-contest", "fixture-imported"])
            self.client.fetch("GET", "/user/profile/fixture")
            self.assertEqual(self.values("UOJSESSID"), ["fixture-imported"])

            # Updating the root representation must leave the more specific
            # path intact, including its usual precedence in Cookie headers.
            self.adapter.queue("UOJSESSID=fixture-new-root; Domain=.qoj.ac; Path=/; Secure")
            self.fetch()
            self.adapter.queue("UOJSESSID=fixture-new-contest; Path=/contest; Secure")
            self.fetch()
            self.fetch("POST")
            self.assertEqual(self.values("UOJSESSID"), ["fixture-new-contest", "fixture-new-root"])

            self.adapter.queue("UOJSESSID=deleted; Path=/; Secure; Max-Age=0")
            self.fetch()
            self.fetch("POST")
            self.assertEqual(self.values("UOJSESSID"), ["fixture-new-contest"])
            self.assert_other_cookies_preserved()

            self.adapter.queue("UOJSESSID=fixture-restored-root; Domain=.qoj.ac; Path=/; Secure")
            self.fetch()
            self.adapter.queue("UOJSESSID=deleted; Domain=.qoj.ac; Path=/contest; Secure; Max-Age=0")
            self.fetch()
            self.fetch("POST")
            self.assertEqual(self.values("UOJSESSID"), ["fixture-restored-root"])
            self.assert_other_cookies_preserved()
            self.assertEqual([(cookie.path, cookie.value) for cookie in self.client.session.cookies
                              if cookie.name == "UOJSESSID"], [("/", "fixture-restored-root")])


if __name__ == "__main__":
    unittest.main()
