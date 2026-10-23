"""QOJ's native Show unofficial cookie, with synthetic offline responses.

The native checkbox removes show_unofficial_mode when checked and sets an
empty value at /contest when unchecked. Filtering and ranking are upstream.
"""
from http.cookies import SimpleCookie
import unittest
from unittest.mock import patch

from participation import read_scoreboard
from qoj import QOJClient
from test_participation import PROBLEMS, response, scoreboard
from test_qoj_cookies import CookieResponseAdapter


PREFERENCE = "show_unofficial_mode"


class StandingsAdapter(CookieResponseAdapter):
    def send(self, request, **kwargs):
        result = super().send(request, **kwargs)
        cookies = SimpleCookie(request.headers.get("Cookie", ""))
        hidden = PREFERENCE in cookies
        official = [100, 7200, ["fixture-team", 1800, 0, "Fixture Team"], 5 if hidden else 7]
        unofficial = [100, 1000, ["fixture-visitor", 1800, 2, "Visitor"], 3]
        scores = {"fixture-team": [[100, 3600, 42, 2, 100, 0]]}
        if not hidden:
            scores["fixture-visitor"] = [[100, 1000, 43, 0, 100, 0]]
        result._content = scoreboard(score=scores, standings=[official] if hidden else [unofficial, official]).encode()
        result.encoding = "utf-8"
        return result


class UnofficialStandingsTests(unittest.TestCase):
    def setUp(self):
        self.client = QOJClient()
        self.addCleanup(self.client.session.close)
        self.adapter = CookieResponseAdapter()
        self.client.session.mount("https://qoj.ac/", self.adapter)
        network = patch("requests.adapters.HTTPAdapter.send", side_effect=AssertionError("Offline test"))
        network.start()
        self.addCleanup(network.stop)

    def request_cookies(self, index=-1):
        return SimpleCookie(self.adapter.requests[index].headers.get("Cookie", ""))

    def test_hide_uses_native_empty_cookie_and_keeps_account_cookies(self):
        self.client.import_cookies(f"UOJSESSID=fixture-session; preference=en; {PREFERENCE}=imported")
        self.client.session.cookies.set(PREFERENCE, "old", domain=".qoj.ac", path="/contest/9")
        self.client.show_unofficial = False
        self.client.fetch("GET", "/contest/9/standings")
        cookies = self.request_cookies()
        self.assertEqual(cookies[PREFERENCE].value, "")
        self.assertEqual(cookies["UOJSESSID"].value, "fixture-session")
        self.assertEqual(cookies["preference"].value, "en")
        self.assertEqual(self.adapter.requests[-1].headers["Cookie"].count(PREFERENCE + "="), 1)
        preference = [c for c in self.client.session.cookies if c.name == PREFERENCE]
        self.assertEqual([(c.path, c.value, c.secure) for c in preference], [("/contest", "", True)])

    def test_show_removes_imported_and_path_specific_preferences(self):
        self.client.import_cookies(f"UOJSESSID=fixture-session; {PREFERENCE}=")
        self.client.session.cookies.set(PREFERENCE, "", domain="qoj.ac", path="/contest")
        self.client.show_unofficial = True
        self.client.fetch("GET", "/contest/9/standings")
        self.assertNotIn(PREFERENCE, self.request_cookies())
        self.assertEqual(self.request_cookies()["UOJSESSID"].value, "fixture-session")

    def test_unspecified_preserves_imported_preference(self):
        self.client.import_cookies(f"UOJSESSID=fixture-session; {PREFERENCE}=")
        self.client.fetch("GET", "/contest/9/standings")
        self.assertIn(PREFERENCE, self.request_cookies())
        self.assertEqual(next(c.path for c in self.client.session.cookies if c.name == PREFERENCE), "/")

    def test_selection_survives_logout_cookie_import_and_redirect_cookie_updates(self):
        for selected in (True, False):
            with self.subTest(selected=selected):
                self.client.show_unofficial = selected
                self.client.clear()
                self.client.import_cookies(f"UOJSESSID=fixture-new; {PREFERENCE}=old")
                self.adapter.requests.clear()
                self.adapter.queue(f"{PREFERENCE}=unexpected; Path=/contest; Secure",
                                   status=302, location="/contest/9/standings")
                self.client.fetch("GET", "/contest/9")
                self.assertEqual(len(self.adapter.requests), 2)
                for index in range(2):
                    cookies = self.request_cookies(index)
                    self.assertEqual(PREFERENCE in cookies, not selected)
                    if not selected:
                        self.assertEqual(cookies[PREFERENCE].value, "")
                    self.assertEqual(cookies["UOJSESSID"].value, "fixture-new")
                self.assertTrue(all(r.method == "GET" for r in self.adapter.requests))

    def test_preference_retains_native_contest_path_scope(self):
        self.client.show_unofficial = False
        for path in ("/results/QOJ9", "/login", "/contest-other/9", "/problem/10"):
            self.client.fetch("GET", path)
            self.assertNotIn(PREFERENCE, self.request_cookies())
            self.assertTrue(self.adapter.requests[-1].url.endswith(path))
        self.client.fetch("GET", "/contest/9/standings")
        self.assertIn(PREFERENCE, self.request_cookies())

    def test_native_rows_ranks_and_results_follow_upstream_selected_view(self):
        self.adapter = StandingsAdapter()
        self.client.session.mount("https://qoj.ac/", self.adapter)
        self.client.username, self.client.authenticated = "fixture-team", True
        page = response('<a href="/contest/9/standings">Standings</a>')
        for selected in (True, False, None):
            with self.subTest(selected=selected):
                self.client.session.cookies.clear()
                self.client.show_unofficial = selected
                parsed = read_scoreboard(self.client, page, 9, PROBLEMS)
                rows = parsed["scoreboard"]
                self.assertTrue(parsed["scoreboard_available"])
                self.assertEqual(len(rows), 1 if selected is False else 2)
                own = next(row for row in rows if row["is_self"])
                self.assertEqual(own["rank"], 5 if selected is False else 7)
                self.assertEqual((own["solved"], own["penalty"]), (1, "2:00"))
                self.assertTrue(own["problem_stats"]["10"]["solved"])
                self.assertEqual(self.adapter.requests[-1].url, "https://qoj.ac/contest/9/standings")


if __name__ == "__main__":
    unittest.main()
