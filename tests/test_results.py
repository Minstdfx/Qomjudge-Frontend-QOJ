"""Offline QOJ PC2 fixtures; never log in or start a real contest.

Markup stripped from https://qoj.ac/results/QOJ842?mode=pc2 on 2026-09-15.
The account name and metadata problem IDs below are fixture values. The native
headers, numbers, classes and footer structure are retained. Pending variants
are constructed tests based on the advertised CSS class, not captures of the
permission-restricted contest 3962.
"""
import unittest

from results import parse_results_table
from qoj import QOJError


PROBLEMS = [{"id": 1001 + i, "label": chr(65 + i), "title": "Fixture problem"} for i in range(13)]
HEADER = '''<tr><th><strong><u>Rank</u></strong></th><th><strong><u>Name</u></strong></th>
<th><strong><u>Solved</u></strong></th><th><strong><u>Time</u></strong></th>'''
HEADER += ''.join(f'<th>&nbsp;&nbsp;<strong><u>{p["label"]}</u></strong>&nbsp;&nbsp;</th>' for p in PROBLEMS)
HEADER += '<th><strong><u>Total att/solv</u></strong></th></tr>'
ROW = '''<tr class="odd"><td><center>1</center></td><td>fixture-team</td><td>11</td><td>1371</td>
<td class="center">0/--</td><td class="yes">2/14</td><td class="yes">1/55</td><td class="yes">1/29</td>
<td class="center">0/--</td><td class="yes">2/174</td><td class="yes">4/260</td><td class="yes">1/196</td>
<td class="yes">1/125</td><td class="yes">1/64</td><td class="yes">3/239</td><td class="yes">1/70</td>
<td class="yes">1/5</td><td>18/11</td></tr>'''
FOOTER = '''<tr><td></td><td>Submitted/1st Yes/Total Yes</td><td></td><td></td>
<td>1/--/0</td><td>36/14/24</td><td>31/24/17</td><td>48/8/27</td><td>0/--/0</td><td>13/174/1</td>
<td>13/96/3</td><td>19/95/9</td><td>41/26/14</td><td>33/31/20</td><td>7/106/3</td>
<td>18/70/7</td><td>35/5/26</td><td>295/26</td></tr>'''
FIXTURE = '<table>' + HEADER + ROW + FOOTER + '</table>'


def parse(source=FIXTURE, problems=PROBLEMS, username="fixture-team"):
    return parse_results_table(source, 842, problems, username)


class ResultsTableTests(unittest.TestCase):
    def test_published_footer_counts_all_attempts_not_teams_in_rows(self):
        totals = parse()["problem_totals"]
        self.assertEqual(totals["1002"], {"solved": 24, "submissions": 36})
        self.assertEqual(totals["1001"], {"solved": 0, "submissions": 1})
        reordered = parse(problems=list(reversed(PROBLEMS)))["problem_totals"]
        self.assertEqual(reordered, totals)

    def test_footer_unknown_counts_stay_unknown_and_hidden_counts_are_ignored(self):
        unknown = FIXTURE.replace('36/14/24', '?/--/?<span hidden>999/1/999</span>')
        self.assertEqual(parse(unknown)["problem_totals"]["1002"], {"solved": None, "submissions": None})
        self.assertEqual(parse(FIXTURE.replace(FOOTER, ''))["problem_totals"], {})

    def test_actual_pc2_layout_preserves_upstream_totals_and_cells(self):
        parsed = parse()
        self.assertEqual(parsed["source"], "qoj-pc2")
        self.assertEqual(parsed["ruleset"], "ICPC")
        self.assertIsNone(parsed["finalized"])
        self.assertEqual(len(parsed["rows"]), 1)
        row = parsed["rows"][0]
        self.assertEqual((row["rank"], row["team_name"], row["solved"], row["penalty"]),
                         ("1", "fixture-team", "11", "1371"))
        self.assertEqual(row["penalty_seconds"], 82260)
        self.assertTrue(row["is_self"])
        self.assertEqual(row["problem_stats"]["1002"],
                         {"text": "2/14", "display": "2/14", "solved": True,
                          "pending": False, "failed": False, "attempts": 2, "time": "14"})
        self.assertEqual(row["problem_stats"]["1001"]["display"], "0/--")
        self.assertFalse(row["problem_stats"]["1001"]["solved"])
        self.assertFalse(row["problem_stats"]["1001"]["failed"])

    def test_thead_tbody_and_changed_problem_metadata_order_are_supported(self):
        source = '<table><thead>' + HEADER + '</thead><tbody>' + ROW + FOOTER + '</tbody></table>'
        row = parse(source, list(reversed(PROBLEMS)))["rows"][0]
        self.assertEqual(row["problem_stats"]["1002"]["display"], "2/14")
        self.assertEqual(row["problem_stats"]["1013"]["display"], "1/5")

    def test_failed_attempts_are_preserved_without_an_invented_time(self):
        source = FIXTURE.replace('<td class="yes">2/14</td>', '<td class="no">7/--</td>')
        stat = parse(source)["rows"][0]["problem_stats"]["1002"]
        self.assertEqual(stat["display"], "7/--")
        self.assertEqual(stat["attempts"], 7)
        self.assertFalse(stat["solved"])
        self.assertTrue(stat["failed"])
        self.assertEqual(stat["time"], "")

    def test_constructed_pending_cells_never_infer_acceptance_or_score(self):
        for value in ("3/--", "3/14", "?3", "?"):
            with self.subTest(value=value):
                source = FIXTURE.replace('<td class="yes">2/14</td>', f'<td class="yes no pending">{value}</td>')
                row = parse(source)["rows"][0]
                stat = row["problem_stats"]["1002"]
                self.assertEqual(stat["display"], value)
                self.assertTrue(stat["pending"])
                self.assertFalse(stat["solved"])
                self.assertFalse(stat["failed"])
                self.assertEqual(stat["time"], "")
                self.assertEqual(row["solved"], "11")  # Never recompute native totals.

    def test_unknown_or_duplicate_problem_labels_fail_closed(self):
        variants = [PROBLEMS[:-1], PROBLEMS + [PROBLEMS[0]],
                    [dict(PROBLEMS[0], label="Other"), *PROBLEMS[1:]],
                    [dict(PROBLEMS[0], id=1002), *PROBLEMS[1:]]]
        for problems in variants:
            with self.subTest(problems=problems), self.assertRaises(QOJError):
                parse(problems=problems)
        with self.assertRaises(QOJError):
            parse(FIXTURE.replace('<u>M</u>', '<u>A</u>'))

    def test_explicit_problem_links_must_match_contest_and_problem(self):
        original = '<strong><u>B</u></strong>'
        for link in ('/contest/999/problem/1002', '/contest/842/problem/9999',
                     'https://evil.test/contest/842/problem/1002'):
            with self.subTest(link=link), self.assertRaises(QOJError):
                parse(FIXTURE.replace(original, f'<a href="{link}">B</a>'))
        self.assertEqual(len(parse(FIXTURE.replace(original, '<a href="/contest/842/problem/1002">B</a>'))["rows"]), 1)

    def test_identity_uses_account_metadata_not_nickname_substrings(self):
        variants = [('<span data-username="fixture-team">Friendly nickname</span>', True),
                    ('<a href="https://qoj.ac/user/profile/fixture-team">Friendly nickname</a>', True),
                    ('<span data-username="other-team">fixture-team</span>', False),
                    ('fixture-team-also', False),
                    ('<a href="https://evil.test/user/profile/fixture-team">Friendly nickname</a>', False),
                    ('<span data-username="other-team"><a href="/user/profile/fixture-team">Friendly nickname</a></span>', False)]
        for name, expected in variants:
            with self.subTest(name=name):
                source = FIXTURE.replace('<td>fixture-team</td>', f'<td>{name}</td>')
                self.assertEqual(parse(source)["rows"][0]["is_self"], expected)
        self.assertFalse(parse(username="")["rows"][0]["is_self"])

    def test_hidden_content_and_scripts_do_not_reveal_frozen_values(self):
        source = FIXTURE.replace('<td class="yes">2/14</td>', '''<td class="pending">3/--
          <span hidden>Accepted</span><span aria-hidden="true">100</span>
          <span style="display:none"><span style="display:none">2/14</span></span>
          <script>throw new Error('do not execute');</script></td>''')
        stat = parse(source)["rows"][0]["problem_stats"]["1002"]
        self.assertEqual(stat["display"], "3/--")
        self.assertTrue(stat["pending"])

    def test_empty_table_is_an_available_empty_board(self):
        self.assertEqual(parse('<table>' + HEADER + FOOTER + '</table>')["rows"], [])

    def test_missing_columns_duplicate_boards_and_unknown_formats_fail_closed(self):
        sources = [FIXTURE + FIXTURE,
                   FIXTURE.replace('<td class="yes">2/14</td>', ''),
                   FIXTURE.replace('<td class="yes">2/14</td>', '<td colspan="2" class="yes">2/14</td>'),
                   FIXTURE.replace('<u>Time</u>', '<u>Unknown value</u>'),
                   '<script>standings = [];</script>', '<h1>403 Access denied</h1>']
        for source in sources:
            with self.subTest(source=source[:60]), self.assertRaises(QOJError):
                parse(source)

    def test_unknown_totals_and_rank_remain_opaque(self):
        source = FIXTURE.replace('<center>1</center>', '<center>*</center>').replace('<td>11</td>', '<td>?</td>').replace('<td>1371</td>', '<td>--</td>')
        row = parse(source)["rows"][0]
        self.assertEqual((row["rank"], row["solved"], row["penalty"]), ("*", "?", "--"))
        self.assertIsNone(row["penalty_seconds"])


if __name__ == "__main__":
    unittest.main()
