"""PC2 results discovery through the real adapter and Flask, without networking.

The table structure follows the public QOJ842 PC2 results page. Team values,
contest 9, and the pending cell are synthetic fixtures, not QOJ3962 samples.
"""
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from app import create_app
from qoj import QOJError
from test_native_adapter_api import BoundFixtureClient
import test_participation as fixtures


RESULTS_PATH = "/results/QOJ9?mode=pc2"
RESULTS_URL = "https://qoj.ac" + RESULTS_PATH
PC2_TABLE = '''<table>
<tr><th>Rank</th><th>Name</th><th>Solved</th><th>Time</th>
    <th>A</th><th>B</th><th>Total att/solv</th></tr>
<tr class="odd"><td><center>7</center></td><td>fixture-team</td>
    <td>1</td><td>34</td><td class="yes">2/14</td>
    <td class="pending">3/--</td><td>5/1</td></tr>
<tr><td></td><td>Submitted/1st Yes/Total Yes</td></tr>
</table>'''


class TrainingFixtureClient(BoundFixtureClient):
    def __init__(self, *, location="contest", candidate=RESULTS_PATH, **kwargs):
        super().__init__(**kwargs)
        self.location = location
        self.candidate = candidate
        self.result_requests = []
        self.results_error = None
        self.results_redirect_to_regular = False
        self.results_response_path = RESULTS_PATH
        self.standings_reference_markup = None
        self.standings_response_path = None

    def _reference(self):
        url = escape(self.candidate, quote=True)
        if self.location == "standings":
            return f'<iframe src="{url}"></iframe>'
        return f'<nav><a href="{url}">Training scoreboard</a></nav>'

    def _page(self, url):
        parts = urlsplit(url)
        if parts.path.startswith("/results/"):
            requested = parts.path + ("?" + parts.query if parts.query else "")
            self.result_requests.append(requested)
            self.gets.append(requested)
            if requested != RESULTS_PATH:
                raise AssertionError("Unexpected results request: " + requested)
            if self.results_error:
                raise self.results_error
            if self.results_redirect_to_regular:
                return fixtures.response(self.board, "/contest/9/standings")
            return fixtures.response(PC2_TABLE, self.results_response_path)
        page = super()._page(url)
        if parts.path == "/contest/9" and self.standings_reference_markup is not None:
            page = fixtures.response(page.text.replace('<a href="/contest/9/standings">Standings</a>',
                                                      self.standings_reference_markup))
        if parts.path == "/contest/9" and self.location == "contest":
            return fixtures.response(page.text + self._reference())
        if parts.path == "/contest/9" and self.location == "article":
            return fixtures.response(page.text + '<article class="uoj-article">' + self._reference() + '</article>')
        if parts.path == "/contest/9/standings":
            if self.location == "standings":
                # The regular data must not be used to bypass a denied PC2 board.
                return fixtures.response(page.text + self._reference(), parts.path)
            if self.location == "redirect":
                return fixtures.response(PC2_TABLE, RESULTS_PATH)
            if self.standings_response_path is not None:
                return fixtures.response(page.text, self.standings_response_path)
        return page


class TrainingResultsAPITests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        clock = patch.object(fixtures, "NOW", fixtures.NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def make_client(self, upstream, mode=None, scoreboard="auto"):
        self.app = create_app(contest_id=9, data_dir=self.data_dir,
                              mode=mode or upstream.mode, scoreboard=scoreboard, client_factory=lambda: upstream)
        self.app.config["TESTING"] = True
        return self.app.test_client()

    def state(self, client):
        response = client.get("/api/state")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def post(self, client, path, body):
        token = self.state(client)["csrf_token"]
        response = client.post(path, json=body, headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def member(self, captain):
        created = self.post(captain, "/api/team/create", {"name": "Results team"})
        member = self.app.test_client()
        self.post(member, "/api/team/join", {"code": created["team"]["join_code"]})
        return member

    def assert_hidden(self, state):
        self.assertFalse(state["permissions"]["view_content"])
        self.assertFalse(state["permissions"]["submit"])
        self.assertEqual(state["scoreboard"], [])
        self.assertEqual(state["contest"]["problems"], [])
        self.assertFalse(state["contest"].get("attachments"))
        self.assertIsNone(state["run"])
        self.assertNotIn("Hidden A", str(state))

    def assert_pc2(self, state):
        self.assertTrue(state["participation"]["scoreboard_available"])
        self.assertEqual(state["participation"]["scoreboard_url"], RESULTS_URL)
        self.assertEqual(state["participation"]["scoreboard_source"], "qoj-pc2")
        self.assertEqual(len(state["scoreboard"]), 1)
        row = state["scoreboard"][0]
        self.assertEqual((row["rank"], row["solved"], row["penalty"]), ("7", "1", "34"))
        self.assertEqual(row["penalty_seconds"], 2040)
        self.assertTrue(row["is_self"])
        self.assertEqual(row["problem_stats"]["10"]["text"], "2/14")
        self.assertEqual(row["problem_stats"]["11"]["text"], "3/--")
        self.assertTrue(row["problem_stats"]["11"]["pending"])
        self.assertFalse(row["problem_stats"]["11"]["solved"])

    def test_official_contest_reference_prefers_pc2_without_reading_regular_board(self):
        upstream = TrainingFixtureClient(mode="official", entered=True, virtual_table=False)
        client = self.make_client(upstream)
        self.assert_pc2(self.state(client))
        self.assertEqual(upstream.result_requests, [RESULTS_PATH])
        self.assertNotIn("/contest/9/standings", upstream.gets)

    def test_standings_iframe_is_followed_instead_of_embedded_regular_data(self):
        upstream = TrainingFixtureClient(location="standings", mode="official", entered=True, virtual_table=False)
        self.assert_pc2(self.state(self.make_client(upstream)))
        self.assertEqual(upstream.gets.count("/contest/9/standings"), 1)
        self.assertEqual(upstream.result_requests, [RESULTS_PATH])

    def test_standings_redirect_to_pc2_uses_the_already_returned_response(self):
        upstream = TrainingFixtureClient(location="redirect", mode="official", entered=True, virtual_table=False)
        self.assert_pc2(self.state(self.make_client(upstream)))
        self.assertEqual(upstream.gets.count("/contest/9/standings"), 1)
        self.assertEqual(upstream.result_requests, [])

    def test_explicit_pc2_configuration_overrides_mode_less_results_reference(self):
        upstream = TrainingFixtureClient(candidate="/results/QOJ9", mode="official", entered=True, virtual_table=False)
        self.assert_pc2(self.state(self.make_client(upstream, scoreboard="pc2")))
        self.assertEqual(upstream.result_requests, [RESULTS_PATH])

    def test_results_path_is_not_guessed_when_contest_does_not_reference_it(self):
        upstream = TrainingFixtureClient(location="none", entered=True)
        state = self.state(self.make_client(upstream))
        self.assertTrue(state["participation"]["scoreboard_available"])
        self.assertEqual(state["scoreboard"][0]["penalty"], "2:00")
        self.assertEqual(upstream.result_requests, [])

    def test_author_content_link_cannot_select_the_scoreboard_source(self):
        upstream = TrainingFixtureClient(location="article", entered=True)
        state = self.state(self.make_client(upstream))
        self.assertEqual(upstream.result_requests, [])
        self.assertEqual(state["scoreboard"][0]["penalty"], "2:00")

    def test_regular_board_query_overrides_and_author_links_cannot_select_a_source(self):
        plain = '<a href="/contest/9/standings">Standings</a>'
        for markup in ('<a href="/contest/9/standings?unfreeze=1">Standings</a>',
                       '<article>' + plain + '</article>', '<form>' + plain + '</form>',
                       '<div class="uoj-article">' + plain + '</div>'):
            with self.subTest(markup=markup):
                upstream = TrainingFixtureClient(location="none", entered=True)
                upstream.standings_reference_markup = markup
                state = self.state(self.make_client(upstream))
                self.assertTrue(state["permissions"]["view_content"])
                self.assertFalse(state["participation"]["scoreboard_available"])
                self.assertEqual(state["scoreboard"], [])
                self.assertNotIn("/contest/9/standings", upstream.gets)
                self.assertEqual(upstream.result_requests, [])

    def test_regular_board_final_response_cannot_add_visibility_query_overrides(self):
        upstream = TrainingFixtureClient(location="none", entered=True)
        upstream.standings_response_path = "/contest/9/standings?unfreeze=1"
        state = self.state(self.make_client(upstream))
        self.assertTrue(state["permissions"]["view_content"])
        self.assertFalse(state["participation"]["scoreboard_available"])
        self.assertEqual(state["scoreboard"], [])
        self.assertEqual(upstream.gets.count("/contest/9/standings"), 1)
        self.assertEqual(upstream.result_requests, [])

    def test_explicit_pc2_configuration_uses_results_without_a_page_reference(self):
        upstream = TrainingFixtureClient(location="none", entered=True)
        self.assert_pc2(self.state(self.make_client(upstream, scoreboard="pc2")))
        self.assertEqual(upstream.result_requests, [RESULTS_PATH])
        self.assertNotIn("/contest/9/standings", upstream.gets)

    def test_explicit_pc2_configuration_does_not_bypass_participation_start(self):
        for mode, entered in (("vp", False), ("official", True)):
            with self.subTest(mode=mode):
                upstream = TrainingFixtureClient(location="none", mode=mode, entered=entered,
                                                 start=fixtures.NOW + 3600, virtual_table=False)
                client = self.make_client(upstream, scoreboard="pc2")
                self.assert_hidden(self.state(client))
                self.assertEqual(upstream.result_requests, [])
                self.assertNotIn("/contest/9/standings", upstream.gets)

    def test_foreign_results_and_unapproved_queries_are_never_requested(self):
        for candidate in ("/results/QOJ10?mode=pc2", "https://evil.test/results/QOJ9?mode=pc2",
                          "/results/QOJ9?mode=pc2&unfreeze=1", "/results/QOJ9?mode=pc2&mode=html",
                          "/results/QOJ9?mode=html", "/results/QOJ9/../QOJ10?mode=pc2"):
            with self.subTest(candidate=candidate):
                upstream = TrainingFixtureClient(candidate=candidate, entered=True)
                state = self.state(self.make_client(upstream))
                self.assertEqual(upstream.result_requests, [])
                self.assertEqual(state["scoreboard"][0]["penalty"], "2:00")

    def test_results_denial_clears_previous_board_without_falling_back(self):
        for location in ("contest", "standings"):
            with self.subTest(location=location):
                upstream = TrainingFixtureClient(location=location, entered=True)
                client = self.make_client(upstream)
                self.assert_pc2(self.state(client))
                upstream.gets.clear()
                upstream.results_error = QOJError("Training results access denied", 403)
                denied = self.state(client)
                self.assertTrue(denied["permissions"]["view_content"])
                self.assertFalse(denied["participation"]["scoreboard_available"])
                self.assertEqual(denied["scoreboard"], [])
                self.assertIn("Training results access denied", denied["participation"]["scoreboard_message"])
                self.assertEqual(upstream.gets.count("/contest/9/standings"), int(location == "standings"))

    def test_results_login_expiry_revokes_cached_contest_content(self):
        upstream = TrainingFixtureClient(entered=True)
        client = self.make_client(upstream)
        self.assert_pc2(self.state(client))
        upstream.results_error = QOJError("Login expired", 401)
        denied = self.state(client)
        self.assertEqual(denied["participation"]["status"], "unavailable")
        self.assert_hidden(denied)

    def test_results_redirect_to_regular_board_cannot_change_the_selected_source(self):
        upstream = TrainingFixtureClient(entered=True)
        upstream.results_redirect_to_regular = True
        state = self.state(self.make_client(upstream))
        self.assertTrue(state["permissions"]["view_content"])
        self.assertFalse(state["participation"]["scoreboard_available"])
        self.assertEqual(state["scoreboard"], [])
        self.assertEqual(upstream.result_requests, [RESULTS_PATH])

    def test_upstream_cannot_silently_drop_the_explicit_pc2_mode(self):
        upstream = TrainingFixtureClient(entered=True)
        upstream.results_response_path = "/results/QOJ9"
        state = self.state(self.make_client(upstream))
        self.assertTrue(state["permissions"]["view_content"])
        self.assertFalse(state["participation"]["scoreboard_available"])
        self.assertEqual(state["scoreboard"], [])
        self.assertTrue(upstream.result_requests)
        self.assertTrue(all(url == RESULTS_PATH for url in upstream.result_requests))

    def test_vp_start_is_required_before_either_team_role_can_read_results(self):
        upstream = TrainingFixtureClient()
        captain = self.make_client(upstream)
        member = self.member(captain)
        for client in (captain, member):
            self.assert_hidden(self.state(client))
        self.assertEqual(upstream.result_requests, [])
        self.assertNotIn("/contest/9/standings", upstream.gets)
        started = self.post(captain, "/api/start", {"mode": "vp", "action": "regvir", "selection": {}})
        self.assert_pc2(started)
        self.assert_pc2(self.state(member))
        self.assertEqual(len(upstream.posts), 1)

    def test_scheduled_official_window_hides_results_for_both_roles_until_start(self):
        start = fixtures.NOW + 3600
        upstream = TrainingFixtureClient(mode="official", entered=True, start=start, virtual_table=False)
        captain = self.make_client(upstream)
        member = self.member(captain)
        for client in (captain, member):
            state = self.state(client)
            self.assertEqual(state["participation"]["status"], "scheduled")
            self.assert_hidden(state)
        self.assertEqual(upstream.result_requests, [])
        self.assertNotIn("/contest/9/standings", upstream.gets)
        fixtures.NOW = start
        for client in (captain, member):
            self.assert_pc2(self.state(client))
        self.assertEqual(upstream.posts, [])

    def test_results_reference_does_not_override_wrong_participation_mode(self):
        upstream = TrainingFixtureClient(mode="official", entered=True, virtual_table=False)
        state = self.state(self.make_client(upstream, mode="vp"))
        self.assertEqual(state["participation"]["status"], "unavailable")
        self.assert_hidden(state)
        self.assertEqual(upstream.result_requests, [])


if __name__ == "__main__":
    unittest.main()
