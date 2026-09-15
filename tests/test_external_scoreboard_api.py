"""External standings through real parsers and Flask, without upstream requests.

The two-problem HTML follows the public QOJ842 external table structure. Names,
numbers, contest 9, and its problem mapping are synthetic integration fixtures.
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
from test_training_results_api import PC2_TABLE
import test_participation as fixtures


EXTERNAL_PATH = "/results/QOJ9"
EXTERNAL_URL = "https://qoj.ac" + EXTERNAL_PATH
PC2_PATH = EXTERNAL_PATH + "?mode=pc2"
PC2_URL = "https://qoj.ac" + PC2_PATH
EXTERNAL_TABLE = '''<table class="standings">
<tr><th class="stnd">Rank.</th><th class="stnd">Username</th>
    <th class="success">A<br>1/2</th><th class="success">B<br>1/1</th>
    <th class="stnd">Solved</th><th class="stnd">Penalty</th><th class="stnd">Dirt</th></tr>
<tr class="stand02"><td class="stnd"><center>7</center></td><td class="stnd">fixture-team</td>
    <td class="opener"><center>+1<br><font size="1">0:14</font></center></td>
    <td class="success"><center>+<br><font size="1">0:55</font></center></td>
    <td class="stnd">2</td><td class="stnd">89</td><td class="stnd">33%</td></tr>
<tr><td class="stnd"></td><td class="stnd"><b>Submissions:</b></td><td>2</td><td>1</td></tr>
</table>'''


class ExternalFixtureClient(BoundFixtureClient):
    def __init__(self, *, location="contest", reference=EXTERNAL_PATH, pc2_iframe=False, **kwargs):
        super().__init__(**kwargs)
        self.location = location
        self.reference = reference
        self.pc2_iframe = pc2_iframe
        self.result_requests = []
        self.external_error = None
        self.external_html = EXTERNAL_TABLE
        self.external_response_path = EXTERNAL_PATH
        self.pc2_response_path = PC2_PATH
        self.native_redirect = None
        self.switch_account_on_external = False

    def _references(self):
        reference = '<nav><a href="' + escape(self.reference, quote=True) + '">External Standings</a></nav>'
        if self.pc2_iframe:
            reference += '<iframe src="' + PC2_PATH + '"></iframe>'
        return reference

    def _page(self, url):
        parts = urlsplit(url)
        if parts.netloc and parts.netloc != "qoj.ac":
            raise AssertionError("Unexpected external host: " + url)
        if parts.path.startswith("/results/"):
            requested = parts.path + ("?" + parts.query if parts.query else "")
            self.result_requests.append(requested)
            self.gets.append(requested)
            if requested == EXTERNAL_PATH:
                if self.external_error:
                    raise self.external_error
                if self.switch_account_on_external:
                    self.username = "different-account"
                return fixtures.response(self.external_html, self.external_response_path)
            if requested == PC2_PATH:
                body = self.external_html if self.pc2_response_path == EXTERNAL_PATH else PC2_TABLE
                return fixtures.response(body, self.pc2_response_path)
            raise AssertionError("Unexpected results URL: " + requested)
        page = super()._page(url)
        if parts.path == "/contest/9/standings" and self.native_redirect:
            body = PC2_TABLE if self.native_redirect == PC2_PATH else self.external_html
            return fixtures.response(body, self.native_redirect)
        if ((parts.path == "/contest/9" and self.location == "contest")
                or (parts.path == "/contest/9/standings" and self.location == "standings")):
            return fixtures.response(page.text + self._references(), parts.path)
        return page


class ExternalScoreboardAPITests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        clock = patch.object(fixtures, "NOW", fixtures.NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def make_client(self, upstream, scoreboard="auto"):
        self.app = create_app(contest_id=9, mode=upstream.mode, scoreboard=scoreboard,
                              data_dir=self.data_dir, client_factory=lambda: upstream)
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

    def assert_external(self, state):
        participation = state["participation"]
        self.assertTrue(participation["scoreboard_available"])
        self.assertEqual(participation["scoreboard_source"], "qoj-external")
        self.assertEqual(participation["scoreboard_url"], EXTERNAL_URL)
        self.assertEqual(len(state["scoreboard"]), 1)
        row = state["scoreboard"][0]
        self.assertTrue(row["is_self"])
        self.assertEqual((row["rank"], row["solved"], row["penalty"]), ("7", "2", "89"))
        first = row["problem_stats"]["10"]
        self.assertTrue(first["first_blood"])
        self.assertTrue(first["solved"])
        self.assertEqual(first["text"], "+1\n0:14")
        self.assertFalse(row["problem_stats"]["11"]["first_blood"])
        self.assertEqual(state["run"]["problem_stats"], row["problem_stats"])

    def assert_pc2(self, state):
        self.assertTrue(state["participation"]["scoreboard_available"])
        self.assertEqual(state["participation"]["scoreboard_source"], "qoj-pc2")
        self.assertEqual(state["participation"]["scoreboard_url"], PC2_URL)
        self.assertEqual(state["scoreboard"][0]["problem_stats"]["10"]["text"], "2/14")
        for item in state["scoreboard"][0]["problem_stats"].values():
            self.assertNotIn("first_blood", item)
        self.assertEqual(state["run"]["problem_stats"], state["scoreboard"][0]["problem_stats"])

    def test_default_external_navigation_preserves_url_and_first_blood_in_run_and_scoreboard(self):
        upstream = ExternalFixtureClient(mode="official", entered=True, virtual_table=False)
        self.assert_external(self.state(self.make_client(upstream)))
        self.assertEqual(upstream.result_requests, [EXTERNAL_PATH])
        self.assertNotIn("/contest/9/standings", upstream.gets)

    def test_readonly_member_receives_same_external_first_blood_fields(self):
        upstream = ExternalFixtureClient(entered=True)
        captain = self.make_client(upstream)
        team = self.post(captain, "/api/team/create", {"name": "First blood team"})
        member = self.app.test_client()
        self.post(member, "/api/team/join", {"code": team["team"]["join_code"]})
        member_state = self.state(member)
        self.assert_external(member_state)
        self.assertEqual(member_state["team"]["role"], "member")
        self.assertFalse(member_state["permissions"]["submit"])
        self.assertEqual(member_state["scoreboard"], self.state(captain)["scoreboard"])

    def test_external_reference_discovered_in_native_standings_replaces_its_json_board(self):
        upstream = ExternalFixtureClient(location="standings", entered=True)
        self.assert_external(self.state(self.make_client(upstream)))
        self.assertEqual(upstream.gets.count("/contest/9/standings"), 1)
        self.assertEqual(upstream.result_requests, [EXTERNAL_PATH])

    def test_explicit_training_iframe_wins_over_default_external_navigation(self):
        for location in ("contest", "standings"):
            with self.subTest(location=location):
                upstream = ExternalFixtureClient(location=location, pc2_iframe=True, entered=True)
                self.assert_pc2(self.state(self.make_client(upstream)))
                self.assertEqual(upstream.result_requests, [PC2_PATH])

    def test_explicit_pc2_configuration_keeps_pc2_despite_advertised_external_default(self):
        upstream = ExternalFixtureClient(entered=True)
        self.assert_pc2(self.state(self.make_client(upstream, scoreboard="pc2")))
        self.assertEqual(upstream.result_requests, [PC2_PATH])
        self.assertNotIn("/contest/9/standings", upstream.gets)

    def test_pc2_embedding_on_default_external_page_overrides_its_other_table(self):
        upstream = ExternalFixtureClient(entered=True)
        upstream.external_html += '<iframe src="' + PC2_PATH + '"></iframe>'
        self.assert_pc2(self.state(self.make_client(upstream)))
        self.assertEqual(upstream.result_requests, [EXTERNAL_PATH, PC2_PATH])

    def test_alternative_pc2_navigation_does_not_replace_the_current_external_page(self):
        upstream = ExternalFixtureClient(entered=True)
        upstream.external_html += '<nav><a href="' + PC2_PATH + '">PC2 format</a></nav>'
        self.assert_external(self.state(self.make_client(upstream)))
        self.assertEqual(upstream.result_requests, [EXTERNAL_PATH])

    def test_native_standings_can_redirect_to_the_advertised_results_format(self):
        for target in (EXTERNAL_PATH, PC2_PATH):
            with self.subTest(target=target):
                upstream = ExternalFixtureClient(location="none", entered=True)
                upstream.native_redirect = target
                state = self.state(self.make_client(upstream))
                if target == EXTERNAL_PATH:
                    self.assert_external(state)
                else:
                    self.assert_pc2(state)
                self.assertEqual(upstream.gets.count("/contest/9/standings"), 1)
                self.assertEqual(upstream.result_requests, [])

    def test_native_board_remains_when_no_external_results_are_advertised(self):
        upstream = ExternalFixtureClient(location="none", entered=True)
        state = self.state(self.make_client(upstream))
        self.assertTrue(state["participation"]["scoreboard_available"])
        self.assertEqual(state["participation"]["scoreboard_source"], "qoj")
        self.assertEqual(state["scoreboard"][0]["penalty"], "2:00")
        self.assertEqual(upstream.result_requests, [])

    def test_unstarted_vp_and_scheduled_official_never_request_any_board_format(self):
        for mode, entered in (("vp", False), ("official", True)):
            for selection in ("auto", "pc2"):
                with self.subTest(mode=mode, selection=selection):
                    upstream = ExternalFixtureClient(mode=mode, entered=entered, pc2_iframe=True,
                                                     start=fixtures.NOW + 3600, virtual_table=False)
                    state = self.state(self.make_client(upstream, scoreboard=selection))
                    self.assertFalse(state["permissions"]["view_content"])
                    self.assertEqual(state["scoreboard"], [])
                    self.assertEqual(state["contest"]["problems"], [])
                    self.assertIsNone(state["run"])
                    self.assertEqual(upstream.result_requests, [])
                    self.assertNotIn("/contest/9/standings", upstream.gets)

    def test_external_failure_clears_old_board_without_trying_pc2_or_native_fallback(self):
        for location in ("contest", "standings"):
            with self.subTest(location=location):
                upstream = ExternalFixtureClient(location=location, entered=True)
                client = self.make_client(upstream)
                self.assert_external(self.state(client))
                upstream.gets.clear()
                upstream.result_requests.clear()
                upstream.external_error = QOJError("External standings forbidden", 403)
                state = self.state(client)
                self.assertTrue(state["permissions"]["view_content"])
                self.assertFalse(state["participation"]["scoreboard_available"])
                self.assertEqual(state["scoreboard"], [])
                self.assertIn("External standings forbidden", state["participation"]["scoreboard_message"])
                self.assertEqual(upstream.result_requests, [EXTERNAL_PATH])
                self.assertEqual(upstream.gets.count("/contest/9/standings"), int(location == "standings"))

    def test_malformed_external_html_does_not_trigger_fallback_to_other_formats(self):
        upstream = ExternalFixtureClient(entered=True)
        upstream.external_html = "<h1>Unknown external scoreboard format</h1>"
        state = self.state(self.make_client(upstream))
        self.assertFalse(state["participation"]["scoreboard_available"])
        self.assertEqual(state["scoreboard"], [])
        self.assertEqual(upstream.result_requests, [EXTERNAL_PATH])
        self.assertNotIn("/contest/9/standings", upstream.gets)

    def test_account_change_during_external_read_hides_all_contest_content(self):
        upstream = ExternalFixtureClient(entered=True)
        upstream.switch_account_on_external = True
        state = self.state(self.make_client(upstream))
        self.assertEqual(state["participation"]["status"], "unavailable")
        self.assertFalse(state["permissions"]["view_content"])
        self.assertEqual(state["scoreboard"], [])
        self.assertEqual(state["contest"]["problems"], [])
        self.assertIsNone(state["run"])

    def test_unsafe_queries_other_contests_and_foreign_external_sources_are_never_requested(self):
        for reference in ("/results/QOJ9?unfreeze=1", "/results/QOJ9?mode=pc2&unfreeze=1",
                          "/results/QOJ9?mode=pc2&mode=pc2", "/results/QOJ10",
                          "https://evil.example/results/QOJ9"):
            with self.subTest(reference=reference):
                upstream = ExternalFixtureClient(reference=reference, entered=True)
                state = self.state(self.make_client(upstream))
                self.assertEqual(upstream.result_requests, [])
                self.assertEqual(state["participation"]["scoreboard_source"], "qoj")
                self.assertEqual(state["scoreboard"][0]["penalty"], "2:00")

    def test_pc2_request_cannot_silently_switch_to_default_external_results(self):
        upstream = ExternalFixtureClient(pc2_iframe=True, entered=True)
        upstream.pc2_response_path = EXTERNAL_PATH
        state = self.state(self.make_client(upstream))
        self.assertFalse(state["participation"]["scoreboard_available"])
        self.assertEqual(state["scoreboard"], [])
        self.assertTrue(upstream.result_requests)
        self.assertTrue(all(url == PC2_PATH for url in upstream.result_requests))

    def test_explicit_external_request_cannot_redirect_to_a_different_results_kind(self):
        upstream = ExternalFixtureClient(entered=True)
        upstream.external_response_path = PC2_PATH
        upstream.external_html = PC2_TABLE
        state = self.state(self.make_client(upstream))
        self.assertFalse(state["participation"]["scoreboard_available"])
        self.assertEqual(state["scoreboard"], [])
        self.assertEqual(upstream.result_requests, [EXTERNAL_PATH])


if __name__ == "__main__":
    unittest.main()
