"""Offline External Standings regressions; no real account is accessed.

The minimal table structure, result classes and values are stripped from public
https://qoj.ac/results/QOJ842 on 2026-09-15. Names and problem IDs are fixtures;
only three columns are retained. Frozen variants are constructed from TD.frost
in its official /files/results/standings.css?v=2022.02.14v2 stylesheet, not from
the permission-restricted contest 3962. No site scripts or assets are copied.
"""
import unittest

from qoj import QOJError
from results import parse_external_results_table, parse_results_table


PROBLEMS = [{"id": 1001 + i, "label": label, "title": "Fixture problem"}
            for i, label in enumerate(("A", "B", "C"))]
HEADER = '''<tr><th class="stnd">Rank.</th><th class="stnd">Username</th>
<th class="tried">A<br>0/1</th><th class="success">B<br>24/36</th>
<th class="success">C<br>17/31</th><th class="stnd">Solved</th>
<th class="stnd">Penalty</th><th class="stnd">Dirt</th></tr>'''
ROW = '''<tr class="stand02"><td class="stnd"><center>1</center></td>
<td class="stnd">fixture-team</td><td class="stnd"><center>-</center></td>
<td class="opener"><center>+1<br><font size="1">0:14</font></center></td>
<td class="success"><center>+<br><font size="1">0:55</font></center></td>
<td class="stnd">11</td><td class="stnd">1371</td><td class="stnd">38%</td></tr>'''
FOOTER = '''<tr><td class="stnd"></td><td class="stnd"><b>Submissions:</b></td>
<td><b>1</b></td><td><b>36</b></td><td><b>31</b></td></tr>'''
FIXTURE = '<table class="standings">' + HEADER + ROW + FOOTER + '</table>'
OPENER = '<td class="opener"><center>+1<br><font size="1">0:14</font></center></td>'


def parse(source=FIXTURE, problems=PROBLEMS, username="fixture-team"):
    return parse_external_results_table(source, 842, problems, username)


class ExternalResultsTests(unittest.TestCase):
    def test_actual_external_layout_preserves_explicit_first_blood_and_totals(self):
        parsed = parse()
        self.assertEqual(parsed["source"], "qoj-external")
        self.assertEqual(parsed["ruleset"], "ICPC")
        self.assertIsNone(parsed["finalized"])
        self.assertEqual(len(parsed["rows"]), 1)
        row = parsed["rows"][0]
        self.assertEqual((row["rank"], row["team_name"], row["solved"], row["score"], row["penalty"]),
                         ("1", "fixture-team", "11", "11", "1371"))
        self.assertTrue(row["is_self"])
        self.assertEqual(row["penalty_seconds"], 82260)
        self.assertEqual(row["problem_stats"]["1002"],
                         {"text": "+1\n0:14", "display": "+1\n0:14", "solved": True,
                          "pending": False, "failed": False, "first_blood": True,
                          "attempts": 2, "time": "0:14"})
        self.assertFalse(row["problem_stats"]["1003"]["first_blood"])
        self.assertTrue(row["problem_stats"]["1003"]["solved"])
        self.assertEqual(row["problem_stats"]["1003"]["attempts"], 1)
        self.assertFalse(row["problem_stats"]["1001"]["failed"])
        self.assertEqual(row["problem_stats"]["1001"]["attempts"], 0)

    def test_no_first_blood_is_inferred_from_earliest_or_equal_times(self):
        source = FIXTURE.replace('class="opener"', 'class="success"').replace("0:55", "0:14")
        stats = parse(source)["rows"][0]["problem_stats"]
        self.assertTrue(stats["1002"]["solved"])
        self.assertFalse(any(stat["first_blood"] for stat in stats.values()))
        source = FIXTURE.replace('class="success"><center>', 'class="opener"><center>')
        stats = parse(source)["rows"][0]["problem_stats"]
        self.assertTrue(stats["1002"]["first_blood"])
        self.assertTrue(stats["1003"]["first_blood"])

    def test_constructed_frozen_and_pending_cells_override_success_and_opener(self):
        for cls, display in (("frost", "?3"), ("pending", "?3"),
                             ("success opener frost", "+1<br>0:14"),
                             ("success opener bad pending", "?3"),
                             ("stnd", "?3"), ("opener", "?")):
            with self.subTest(cls=cls, display=display):
                source = FIXTURE.replace(OPENER, f'<td class="{cls}">{display}</td>')
                row = parse(source)["rows"][0]
                stat = row["problem_stats"]["1002"]
                self.assertTrue(stat["pending"])
                self.assertFalse(stat["solved"])
                self.assertFalse(stat["failed"])
                self.assertFalse(stat["first_blood"])
                self.assertEqual(stat["time"], "")
                self.assertEqual(row["solved"], "11")
        stat = parse(FIXTURE.replace(OPENER, '<td class="frost">?3</td>'))["rows"][0]["problem_stats"]["1002"]
        self.assertEqual(stat["display"], "?3")
        self.assertEqual(stat["attempts"], 3)

    def test_actual_bad_class_preserves_visible_attempts_and_time(self):
        source = FIXTURE.replace(OPENER, '<td class="bad"><center>-1<br><font size="1">0:08</font></center></td>')
        stat = parse(source)["rows"][0]["problem_stats"]["1002"]
        self.assertEqual((stat["display"], stat["attempts"], stat["time"]), ("-1\n0:08", 1, "0:08"))
        self.assertTrue(stat["failed"])
        self.assertFalse(stat["solved"])
        self.assertFalse(stat["first_blood"])

    def test_unknown_or_conflicting_classes_do_not_infer_a_solve(self):
        for cls in ("unknown", "stnd", "bad opener", "bad success"):
            with self.subTest(cls=cls):
                stat = parse(FIXTURE.replace('class="opener"', f'class="{cls}"'))["rows"][0]["problem_stats"]["1002"]
                self.assertFalse(stat["solved"])
                self.assertFalse(stat["first_blood"])

    def test_line_breaks_are_preserved_without_inventing_breaks_between_inline_tags(self):
        source = FIXTURE.replace(OPENER, '<td class="opener"><span>+</span><b>1</b><br><font>0:14</font><!-- secret --></td>')
        stat = parse(source)["rows"][0]["problem_stats"]["1002"]
        self.assertEqual(stat["display"], "+1\n0:14")

    def test_hidden_values_and_scripts_do_not_change_frozen_display(self):
        source = FIXTURE.replace(OPENER, '''<td class="frost" data-score="100" data-first-blood="true">?3
          <span hidden>+1</span><span aria-hidden="true">0:14</span><span class="d-none">Accepted</span>
          <span style="display:none"><span style="display:none">secret</span></span>
          <template>hidden</template><script>window.score = 'Accepted';</script></td>''')
        stat = parse(source)["rows"][0]["problem_stats"]["1002"]
        self.assertEqual(stat["display"], "?3")
        self.assertTrue(stat["pending"])
        self.assertFalse(stat["first_blood"])

    def test_table_sections_and_empty_board_are_supported(self):
        source = '<table class="standings"><thead>' + HEADER + '</thead><tbody>' + ROW + '</tbody><tfoot>' + FOOTER + '</tfoot></table>'
        self.assertEqual(len(parse(source)["rows"]), 1)
        self.assertEqual(parse('<table class="standings">' + HEADER + FOOTER + '</table>')["rows"], [])

    def test_problem_count_labels_order_and_ids_must_match(self):
        variants = [PROBLEMS[:-1], list(reversed(PROBLEMS)), PROBLEMS + [PROBLEMS[0]],
                    [dict(PROBLEMS[0], label="Unknown"), *PROBLEMS[1:]],
                    [dict(PROBLEMS[0], id=1002), *PROBLEMS[1:]],
                    [dict(PROBLEMS[0], id=True), *PROBLEMS[1:]]]
        for problems in variants:
            with self.subTest(problems=problems), self.assertRaises(QOJError):
                parse(problems=problems)
        with self.assertRaises(QOJError):
            parse(FIXTURE.replace('C<br>17/31', 'B<br>17/31'))

    def test_explicit_problem_links_must_match_trusted_contest_and_origin(self):
        for target in ('/contest/999/problem/1002', '/contest/842/problem/9999',
                       'https://evil.test/problem/1002', '/problem/1002?unfreeze=1'):
            with self.subTest(target=target), self.assertRaises(QOJError):
                parse(FIXTURE.replace('B<br>24/36', f'<a href="{target}">B</a><br>24/36'))
        source = FIXTURE.replace('B<br>24/36', '<a href="/contest/842/problem/1002">B</a><br>24/36')
        self.assertEqual(len(parse(source)["rows"]), 1)

    def test_identity_requires_exact_account_or_explicit_metadata(self):
        variants = [('<span data-username="fixture-team">Friendly nickname</span>', True),
                    ('<a href="/user/profile/fixture-team">Friendly nickname</a>', True),
                    ('fixture-team-also', False),
                    ('<span data-username="other-team">fixture-team</span>', False),
                    ('<a href="https://evil.test/user/profile/fixture-team">Friendly nickname</a>', False),
                    ('<span data-username="other-team"><a href="/user/profile/fixture-team">nickname</a></span>', False)]
        for name, expected in variants:
            with self.subTest(name=name):
                source = FIXTURE.replace('>fixture-team</td>', f'>{name}</td>')
                self.assertEqual(parse(source)["rows"][0]["is_self"], expected)
        self.assertFalse(parse(username="")["rows"][0]["is_self"])

    def test_unknown_totals_and_rank_remain_opaque(self):
        source = FIXTURE.replace('<center>1</center>', '<center>*</center>').replace('>11</td>', '>?</td>').replace('>1371</td>', '>--</td>')
        row = parse(source)["rows"][0]
        self.assertEqual((row["rank"], row["solved"], row["penalty"]), ("*", "?", "--"))
        self.assertIsNone(row["penalty_seconds"])

    def test_wrong_formats_ambiguous_boards_and_missing_cells_fail_closed(self):
        sources = [FIXTURE + FIXTURE, FIXTURE.replace('class="standings"', 'class="different"'),
                   FIXTURE.replace('>Penalty</th>', '>Unknown</th>'), FIXTURE.replace(OPENER, ''),
                   FIXTURE.replace(OPENER, '<td colspan="2">?</td>'),
                   FIXTURE.replace(OPENER, '<td><table><tr><td>+1</td></tr></table></td>'),
                   '<h1>403 Access denied</h1>', '<script>standings=[];</script>']
        for source in sources:
            with self.subTest(source=source[:80]), self.assertRaises(QOJError):
                parse(source)
        with self.assertRaises(QOJError):
            parse_results_table(FIXTURE, 842, PROBLEMS, "fixture-team")


if __name__ == "__main__":
    unittest.main()
