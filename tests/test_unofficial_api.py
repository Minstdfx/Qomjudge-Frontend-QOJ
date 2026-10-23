"""Offline integration checks for explicit Show unofficial source selection."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from app import create_app
from qoj import QOJError
from test_external_scoreboard_api import ExternalFixtureClient, EXTERNAL_PATH, PC2_PATH
import test_participation as fixtures


NATIVE_PATH = "/contest/9/standings"


class UnofficialFixtureClient(ExternalFixtureClient):
    def __init__(self, *, native_link=True, **kwargs):
        super().__init__(**kwargs)
        self.native_link = native_link
        self.native_error = None

    def _page(self, url):
        path = urlsplit(url).path
        if path == NATIVE_PATH and self.native_error:
            self.gets.append(path)
            raise self.native_error
        page = super()._page(url)
        if path == "/contest/9" and not self.native_link:
            page = fixtures.response(page.text.replace(
                '<a href="/contest/9/standings">Standings</a>', ""), path)
        return page


class UnofficialScoreboardAPITests(unittest.TestCase):
    def setUp(self):
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Offline test"))
        network.start()
        self.addCleanup(network.stop)

    def make_client(self, upstream, selection, scoreboard="auto"):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        server = create_app(contest_id=9, mode=upstream.mode, data_dir=Path(directory.name),
                            client_factory=lambda: upstream, scoreboard=scoreboard,
                            show_unofficial=selection)
        server.config["TESTING"] = True
        return server.test_client()

    def state(self, browser):
        response = browser.get("/api/state")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def assert_source(self, state, source, path):
        self.assertTrue(state["participation"]["scoreboard_available"])
        self.assertEqual(state["participation"]["scoreboard_source"], source)
        self.assertEqual(state["participation"]["scoreboard_url"], "https://qoj.ac" + path)
        self.assertTrue(state["scoreboard"])

    def test_explicit_choice_prefers_native_over_external_and_pc2_configuration(self):
        for selection in (True, False):
            for scoreboard in ("auto", "pc2"):
                with self.subTest(selection=selection, scoreboard=scoreboard):
                    upstream = UnofficialFixtureClient(entered=True)
                    state = self.state(self.make_client(upstream, selection, scoreboard))
                    self.assert_source(state, "qoj", NATIVE_PATH)
                    self.assertEqual(upstream.gets.count(NATIVE_PATH), 1)
                    self.assertEqual(upstream.result_requests, [])
                    self.assertEqual(state["scoreboard"][0]["penalty"], "2:00")
                    expected = "显示非正式参赛者" if selection else "隐藏非正式参赛者"
                    self.assertIn(expected, state["participation"]["scoreboard_message"])

    def test_omitted_choice_keeps_existing_external_or_pc2_source(self):
        for scoreboard, source, path in (("auto", "qoj-external", EXTERNAL_PATH),
                                         ("pc2", "qoj-pc2", PC2_PATH)):
            with self.subTest(scoreboard=scoreboard):
                upstream = UnofficialFixtureClient(entered=True)
                state = self.state(self.make_client(upstream, None, scoreboard))
                self.assert_source(state, source, path)
                self.assertEqual(upstream.result_requests, [path])
                self.assertNotIn(NATIVE_PATH, upstream.gets)

    def test_native_external_navigation_does_not_override_explicit_choice(self):
        for selection in (True, False):
            with self.subTest(selection=selection):
                upstream = UnofficialFixtureClient(location="standings", entered=True)
                state = self.state(self.make_client(upstream, selection))
                self.assert_source(state, "qoj", NATIVE_PATH)
                self.assertEqual(upstream.result_requests, [])

    def test_current_embedded_training_view_remains_authoritative(self):
        for selection in (True, False):
            for embedding in ("iframe", "meta"):
                with self.subTest(selection=selection, embedding=embedding):
                    upstream = UnofficialFixtureClient(location="standings", entered=True)
                    if embedding == "iframe":
                        upstream.board += '<iframe src="' + PC2_PATH + '"></iframe>'
                    else:
                        upstream.board += '<meta http-equiv="refresh" content="0; url=' + PC2_PATH + '">'
                    state = self.state(self.make_client(upstream, selection))
                    self.assert_source(state, "qoj-pc2", PC2_PATH)
                    self.assertEqual(upstream.gets.count(NATIVE_PATH), 1)
                    self.assertEqual(upstream.result_requests, [PC2_PATH])
                    self.assertEqual(state["scoreboard"][0]["problem_stats"]["10"]["text"], "2/14")
                    self.assertIn("不支持 Show unofficial", state["participation"]["scoreboard_message"])

    def test_no_native_link_keeps_advertised_external_with_limitation_message(self):
        for selection in (True, False):
            with self.subTest(selection=selection):
                upstream = UnofficialFixtureClient(native_link=False, entered=True)
                state = self.state(self.make_client(upstream, selection))
                self.assert_source(state, "qoj-external", EXTERNAL_PATH)
                self.assertEqual(upstream.result_requests, [EXTERNAL_PATH])
                self.assertNotIn(NATIVE_PATH, upstream.gets)
                self.assertIn("不支持 Show unofficial", state["participation"]["scoreboard_message"])

    def test_forbidden_native_board_clears_old_rows_without_public_fallback(self):
        for selection in (True, False):
            with self.subTest(selection=selection):
                upstream = UnofficialFixtureClient(entered=True)
                browser = self.make_client(upstream, selection)
                self.assert_source(self.state(browser), "qoj", NATIVE_PATH)
                upstream.gets.clear()
                upstream.native_error = QOJError("Native standings forbidden", 403)
                state = self.state(browser)
                self.assertTrue(state["permissions"]["view_content"])
                self.assertFalse(state["participation"]["scoreboard_available"])
                self.assertEqual(state["scoreboard"], [])
                self.assertIn("Native standings forbidden", state["participation"]["scoreboard_message"])
                self.assertEqual(upstream.gets.count(NATIVE_PATH), 1)
                self.assertEqual(upstream.result_requests, [])

    def test_unstarted_contests_never_fetch_any_scoreboard(self):
        for selection in (None, True, False):
            for mode, entered in (("vp", False), ("official", True)):
                for scoreboard in ("auto", "pc2"):
                    with self.subTest(selection=selection, mode=mode, scoreboard=scoreboard):
                        upstream = UnofficialFixtureClient(
                            entered=entered, mode=mode, start=fixtures.NOW + 3600,
                            virtual_table=False, pc2_iframe=True,
                        )
                        state = self.state(self.make_client(upstream, selection, scoreboard))
                        self.assertFalse(state["permissions"]["view_content"])
                        self.assertEqual(state["scoreboard"], [])
                        self.assertEqual(state["contest"]["problems"], [])
                        self.assertIsNone(state["run"])
                        self.assertEqual(upstream.result_requests, [])
                        self.assertNotIn(NATIVE_PATH, upstream.gets)


if __name__ == "__main__":
    unittest.main()
