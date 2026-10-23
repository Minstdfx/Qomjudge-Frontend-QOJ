"""Public QOJ sidebar markup plus synthetic participant clocks; offline only."""
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from participation import _scoreboard_freeze_after, participation
from test_participation import FixtureClient, NOW


def sidebar(value="4:00:00", extra=""):
    # Minimal system DOM copied from the public QOJ 4.5.47 page at
    # https://contest.ucup.ac/contest/2567 on 2026-10-23; no team data/tokens.
    return ('<div class="uoj-content"><div class="row"><div class="col-sm-3">'
            '<h4>Ruleset: ICPC</h4><ul class="list-group">'
            '<li class="list-group-item">Scoreboard will be frozen after\n\t'
            + value + '</li>' + extra + '</ul></div></div></div>')


class FreezeFixtureClient(FixtureClient):
    def __init__(self, markup=None, **kwargs):
        super().__init__(**kwargs)
        self.markup = sidebar() if markup is None else markup

    def _page(self, path):
        result = super()._page(path)
        if urlsplit(path).path == "/contest/9":
            result._content += self.markup.encode()
        return result


class FreezeRuleTests(unittest.TestCase):
    def setUp(self):
        no_network = patch("requests.adapters.HTTPAdapter.send", side_effect=AssertionError("Offline test"))
        no_network.start()
        self.addCleanup(no_network.stop)

    def test_observed_sidebar_extracts_relative_hours_minutes_seconds(self):
        self.assertEqual(_scoreboard_freeze_after(sidebar()), 14400)
        self.assertEqual(_scoreboard_freeze_after(sidebar("4:30:12")), 16212)
        self.assertEqual(_scoreboard_freeze_after(sidebar("0:00:00")), 0)

    def test_article_notice_and_arbitrary_script_do_not_establish_freeze(self):
        for source in ('<article>' + sidebar() + '</article>',
                       '<div class="uoj-article">' + sidebar() + '</div>',
                       '<table><tr><td>' + sidebar() + '</td></tr></table>',
                       '<li class="list-group-item">Scoreboard will be frozen after 4:00:00</li>',
                       '<script>frozen=true;freeze_time=14400;finalized=0;</script>',
                       '<h4 class="text-center text-danger">封榜于 2024-01-01 12:00:00</h4>'):
            with self.subTest(source=source):
                self.assertIsNone(_scoreboard_freeze_after(source))

    def test_invalid_or_conflicting_sidebar_rules_remain_unknown(self):
        for value in ("4:60:00", "4:00:60", "-1:00:00", "4:00", "4:00:00 extra", "in 4:00:00"):
            with self.subTest(value=value):
                self.assertIsNone(_scoreboard_freeze_after(sidebar(value)))
        conflicting = '<li class="list-group-item">Scoreboard will be frozen after 3:00:00</li>'
        self.assertIsNone(_scoreboard_freeze_after(sidebar(extra=conflicting)))
        self.assertEqual(_scoreboard_freeze_after(sidebar() + sidebar()), 14400)

    def test_vp_anchors_to_account_start_not_historical_schedule(self):
        client = FreezeFixtureClient(entered=True, start=NOW - 14400)
        client.markup += '<h4>Contest ended 2024-01-01 15:00:00; frozen at 2024-01-01 14:00:00</h4>'
        state = participation(client, 9)
        self.assertEqual(state["scoreboard_freeze_at"], NOW)
        self.assertTrue(state["scoreboard_frozen"])
        self.assertEqual(state["started_at"], NOW - 14400)
        self.assertEqual(client.posts, [])

    def test_boundary_uses_upstream_http_time_for_official_and_vp(self):
        for mode in ("official", "vp"):
            for elapsed, frozen in ((14399, False), (14400, True), (14401, True)):
                with self.subTest(mode=mode, elapsed=elapsed):
                    client = FreezeFixtureClient(mode=mode, entered=True, start=NOW - elapsed,
                                                 virtual_table=mode == "vp")
                    with patch("participation.time.time", return_value=NOW + 86400):
                        state = participation(client, 9, mode)
                    self.assertEqual(state["scoreboard_freeze_at"], NOW - elapsed + 14400)
                    self.assertIs(state["scoreboard_frozen"], frozen)

    def test_unregistered_and_future_windows_keep_timing_hidden(self):
        for client in (FreezeFixtureClient(),
                       FreezeFixtureClient(mode="official", entered=True, start=NOW + 3600, virtual_table=False)):
            with self.subTest(mode=client.mode):
                state = participation(client, 9, client.mode)
                self.assertIsNone(state["scoreboard_freeze_at"])
                self.assertIsNone(state["scoreboard_frozen"])
                self.assertNotIn("/contest/9/standings", client.gets)

    def test_missing_rule_does_not_infer_freeze_from_pending_or_finalized(self):
        # FixtureClient's board has ?4 and finalized=0, neither is freeze proof.
        state = participation(FreezeFixtureClient(markup="", entered=True), 9)
        self.assertTrue(state["scoreboard_available"])
        self.assertTrue(state["scoreboard"][0]["problem_stats"]["11"]["pending"])
        self.assertIsNone(state["scoreboard_freeze_at"])
        self.assertIsNone(state["scoreboard_frozen"])

    def test_rule_outside_participation_duration_is_ignored(self):
        for value in ("5:00:00", "6:00:00"):
            state = participation(FreezeFixtureClient(markup=sidebar(value), entered=True), 9)
            self.assertIsNone(state["scoreboard_freeze_at"])
            self.assertIsNone(state["scoreboard_frozen"])

    def test_end_does_not_invent_upstream_unfreeze(self):
        state = participation(FreezeFixtureClient(entered=True, start=NOW - 18000), 9)
        self.assertEqual(state["status"], "finished")
        self.assertEqual(state["scoreboard_freeze_at"], NOW - 3600)
        self.assertIsNone(state["scoreboard_frozen"])


if __name__ == "__main__":
    unittest.main()
