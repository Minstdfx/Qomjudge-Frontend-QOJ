"""Exercise real native HTML parsing and form handling through the Flask API."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from app import create_app
from qoj import QOJClient, QOJError
import test_participation as fixtures


class BoundFixtureClient(fixtures.FixtureClient):
    participation = QOJClient.participation
    enter = QOJClient.enter

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock = RLock()


class PaginatedVPClient(BoundFixtureClient):
    """QOJ 1802 layout: personal VP marker and duration row on different pages.

    Only the table/pagination structure is reproduced; all identities, times,
    contest metadata and problems are synthetic offline fixtures.
    """

    def __init__(self, **kwargs):
        super().__init__(mode="vp", **kwargs)
        self.fail_later_page = False

    def _page(self, path):
        parsed = urlsplit(path)
        if parsed.path != "/contest/9/registrants":
            return super()._page(path)
        self.gets.append(parsed.path + ("?" + parsed.query if parsed.query else ""))
        page = parse_qs(parsed.query).get("page", ["1"])[0]
        own = fixtures.registrants(self.start).split("</table>")
        other = fixtures.registrants(self.start, username="other-fixture-team").split("</table>")
        if page == "1":
            body = other[0] + "</table>" + own[1] + "</table>"
            body += '<ul class="pagination"><li class="active"><a href="?page=1">1</a></li><li><a href="/contest/9/registrants?page=2">2</a></li></ul>'
        elif page == "2":
            if self.fail_later_page:
                raise QOJError("Fixture registrants page unavailable", 403)
            body = own[0] + "</table>" + other[1] + "</table>"
            body += '<ul class="pagination"><li><a href="/contest/9/registrants?page=1">1</a></li><li class="active"><a href="?page=2">2</a></li></ul>'
        else:
            raise AssertionError("Unadvertised fixture registrants page: " + path)
        return fixtures.response(body, parsed.path + ("?" + parsed.query if parsed.query else ""))


class NativeAdapterAPITests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        clock = patch.object(fixtures, "NOW", fixtures.NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def make_client(self, upstream, mode):
        app = create_app(contest_id=9, data_dir=self.data_dir, mode=mode,
                         client_factory=lambda: upstream)
        app.config["TESTING"] = True
        return app.test_client()

    def state(self, client):
        response = client.get("/api/state")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def start(self, client, payload):
        token = self.state(client)["csrf_token"]
        response = client.post("/api/start", json=payload, headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def assert_hidden(self, state):
        self.assertFalse(state["permissions"]["view_content"])
        self.assertFalse(state["permissions"]["submit"])
        self.assertEqual(state["contest"]["problems"], [])
        self.assertEqual(state["contest"]["attachments"], [])
        self.assertEqual(state["scoreboard"], [])
        self.assertIsNone(state["run"])

    def test_native_vp_form_and_scoreboard_are_parsed_through_real_adapter(self):
        upstream = BoundFixtureClient(mode="vp")
        client = self.make_client(upstream, "vp")
        initial = self.state(client)
        self.assertEqual(initial["participation"]["status"], "unregistered")
        self.assertEqual(initial["participation"]["actions"][0]["id"], "regvir")
        self.assert_hidden(initial)
        entered = self.start(client, {"mode": "vp", "action": "regvir", "selection": {}})
        self.assertEqual(len(upstream.posts), 1)
        method, url, kwargs = upstream.posts[0]
        self.assertEqual((method, url), ("POST", "https://qoj.ac/contest/9"))
        self.assertEqual(kwargs["data"], {"_token": "fresh-token", "submit-regvir": "regvir"})
        self.assertEqual(entered["participation"]["status"], "running")
        self.assertEqual(entered["run"]["started_at"], upstream.start)
        self.assertEqual(entered["run"]["ends_at"], upstream.start + 18000)
        self.assertTrue(entered["permissions"]["view_content"])
        self.assertEqual([problem["id"] for problem in entered["contest"]["problems"]], [10, 11])
        row = entered["scoreboard"][0]
        self.assertEqual((row["rank"], row["penalty"], row["solved"]), (7, "2:00", 1))
        self.assertEqual(row["problem_stats"]["11"]["text"], "?4")
        self.assertEqual(self.state(client)["scoreboard"], entered["scoreboard"])
        self.assertEqual(len(upstream.posts), 1)

    def test_official_window_registration_remains_hidden_until_upstream_clock_starts(self):
        official_start = fixtures.NOW + 3600
        upstream = BoundFixtureClient(mode="official", start=official_start, virtual_table=False)
        client = self.make_client(upstream, "official")
        initial = self.state(client)
        self.assertEqual(initial["participation"]["status"], "unregistered")
        action = initial["participation"]["actions"][0]
        self.assertEqual(action["id"], "register")
        self.assertEqual(action["fields"][0]["options"][0]["value"], "17")
        self.assert_hidden(initial)
        registered = self.start(client, {"mode": "official", "action": "register",
                                         "selection": {"time-window": "17", "agree": True}})
        self.assertEqual(registered["participation"]["status"], "scheduled")
        self.assertEqual(registered["participation"]["started_at"], official_start)
        self.assert_hidden(registered)
        self.assertNotIn("/contest/9/standings", upstream.gets)
        self.assertEqual(len(upstream.posts), 1)
        self.assertEqual(upstream.posts[0][1], "https://qoj.ac/contest/9/register")
        self.assertEqual(upstream.posts[0][2]["data"], {"_token": "window-token", "submit-register": "register",
                                                      "time-window": "17", "agree": "yes"})
        fixtures.NOW = official_start
        started = self.state(client)
        self.assertEqual(started["participation"]["status"], "running")
        self.assertTrue(started["permissions"]["view_content"])
        self.assertTrue(started["permissions"]["submit"])
        self.assertEqual(started["run"]["started_at"], official_start)
        self.assertEqual(started["run"]["ends_at"], official_start + 18000)
        self.assertEqual(len(started["contest"]["problems"]), 2)
        self.assertEqual(started["scoreboard"][0]["rank"], 7)
        self.assertEqual(len(upstream.posts), 1)

    def test_vp_start_joins_registration_time_and_virtual_membership_across_pages(self):
        upstream = PaginatedVPClient()
        client = self.make_client(upstream, "vp")
        initial = self.state(client)
        token = initial["csrf_token"]
        created = client.post("/api/team/create", json={"name": "Pagination fixture team"},
                              headers={"X-CSRF-Token": token})
        self.assertEqual(created.status_code, 200)
        self.assert_hidden(created.get_json())
        self.assertFalse(any("registrants" in path or "standings" in path for path in upstream.gets))

        entered = self.start(client, {"mode": "vp", "action": "regvir", "selection": {}})
        self.assertEqual(entered["participation"]["status"], "running")
        self.assertFalse(entered["participation"].get("entry_uncertain", False))
        self.assertEqual(entered["run"]["started_at"], upstream.start)
        self.assertEqual(entered["run"]["ends_at"], upstream.start + 18000)
        self.assertTrue(entered["permissions"]["submit"])
        self.assertIn("/contest/9/registrants?page=2", upstream.gets)
        self.assertEqual(len(upstream.posts), 1)

        refreshed = self.state(client)
        self.assertEqual(refreshed["run"]["id"], entered["run"]["id"])
        restored = self.start(client, {"mode": "vp", "action": "regvir", "selection": {}})
        self.assertEqual(restored["run"]["id"], entered["run"]["id"])
        self.assertEqual(len(upstream.posts), 1)

    def test_already_started_paginated_vp_recovers_without_another_start_request(self):
        upstream = PaginatedVPClient(entered=True)
        client = self.make_client(upstream, "vp")
        state = self.state(client)
        self.assertEqual(state["participation"]["status"], "running")
        self.assertEqual(state["run"]["started_at"], upstream.start)
        self.assertEqual(upstream.posts, [])

    def test_missing_later_registration_page_does_not_invent_a_local_vp(self):
        upstream = PaginatedVPClient(entered=True)
        upstream.fail_later_page = True
        client = self.make_client(upstream, "vp")
        self.assert_hidden(self.state(client))
        self.assertNotIn("/contest/9/standings", upstream.gets)
        self.assertEqual(upstream.posts, [])

    def test_uncertain_start_recovers_by_refreshing_after_registration_page_returns(self):
        upstream = PaginatedVPClient()
        upstream.fail_later_page = True
        client = self.make_client(upstream, "vp")
        token = self.state(client)["csrf_token"]
        response = client.post("/api/start", json={"mode": "vp", "action": "regvir", "selection": {}},
                               headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["error"])
        uncertain = response.get_json()["state"]
        self.assert_hidden(uncertain)
        self.assertTrue(uncertain["participation"].get("entry_uncertain"))
        self.assertEqual(len(upstream.posts), 1)
        upstream.fail_later_page = False
        recovered = self.state(client)
        self.assertEqual(recovered["participation"]["status"], "running")
        self.assertFalse(recovered["participation"].get("entry_uncertain", False))
        self.assertEqual(recovered["run"]["started_at"], upstream.start)
        self.assertEqual(len(upstream.posts), 1)


if __name__ == "__main__":
    unittest.main()
