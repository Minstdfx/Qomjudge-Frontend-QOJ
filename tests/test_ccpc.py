"""Offline CCPC reveal thresholds, identity projection and persistence checks."""
import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ccpc import CCPCConfig, CCPCScoreboard


PROBLEMS = [{"id": 817234, "label": "A", "title": "Synthetic Alpha"},
            {"id": 913789, "label": "B", "title": "Synthetic Beta"},
            {"id": 624508, "label": "C", "title": "Synthetic Gamma"}]


def stat(solved=True, attempts=1, time="0:20", **extra):
    return {"solved": solved, "attempts": attempts, "time": time,
            "display": ("+" if solved else "-") + (str(attempts - 1) if solved and attempts > 1 else "")
            + ("\n" + time if time else ""), **extra}


def row(name="alpha", cells=None, **extra):
    return {"rank": 1, "team_name": name.title(), "username": name, "is_self": False,
            "solved": 1, "penalty": "0:20", "penalty_seconds": 1200,
            "problem_stats": cells if cells is not None else {str(PROBLEMS[0]["id"]): stat()}, **extra}


class CCPCConfigTests(unittest.TestCase):
    def test_defaults_and_valid_overrides(self):
        self.assertEqual(CCPCConfig().style, "standard")
        config = CCPCConfig("ccpc", 50, "A=0, C=20", 240)
        self.assertEqual(config.overrides, {"A": 0, "C": 20})
        self.assertIsNone(config.validate_problems(PROBLEMS))

    def test_ccpc_requires_explicit_threshold(self):
        with self.assertRaisesRegex(ValueError, "ccpc-unlock"):
            CCPCConfig("ccpc")

    def test_standard_rejects_ccpc_options(self):
        for kwargs in ({"unlock": 0}, {"overrides": {}}, {"reveal_after": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "scoreboard-style"):
                CCPCConfig(**kwargs)

    def test_integer_ranges_are_strict(self):
        for value in (-1, 20001, True, 1.0, "10", None):
            with self.subTest(unlock=value), self.assertRaises(ValueError):
                CCPCConfig("ccpc", value)
        for value in (-1, 10081, True, 0.5, "240"):
            with self.subTest(reveal=value), self.assertRaises(ValueError):
                CCPCConfig("ccpc", 50, reveal_after=value)
        for value in (0, 20000):
            self.assertEqual(CCPCConfig("ccpc", value).unlock, value)

    def test_bad_override_syntax_and_duplicates(self):
        for value in ("", " ", "A=2,A=3", "A=2,", "A=+1", "A=-1", "A=2.5", "A=1=2",
                      "=1", "题=2", "A A=2", "A=20001", {"A": True}, {"A": "2"}, ["A=1"]):
            with self.subTest(overrides=value), self.assertRaises(ValueError):
                CCPCConfig("ccpc", 50, value)

    def test_unknown_or_ambiguous_problem_labels_fail(self):
        with self.assertRaisesRegex(ValueError, "不存在.*Z"):
            CCPCConfig("ccpc", 50, "Z=1").validate_problems(PROBLEMS)
        for problems in ([{"id": 1, "label": "A"}, {"id": 2, "label": "A"}],
                         [{"id": 1, "label": "A"}, {"id": 1, "label": "B"}],
                         [{"id": True, "label": "A"}], [{"id": 0, "label": "A"}],
                         [{"id": 1, "label": "A B"}], None):
            with self.subTest(problems=problems), self.assertRaises(ValueError):
                CCPCConfig("ccpc", 50).validate_problems(problems)


class CCPCScoreboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.secret = b"synthetic-ccpc-secret-for-tests"
        self.board = self.make_board()

    def make_board(self, unlock=2, overrides=None, reveal_after=None, secret=None):
        return CCPCScoreboard(self.path, CCPCConfig("ccpc", unlock, overrides, reveal_after), secret or self.secret)

    def project(self, rows=None, board=None, problems=None, **kwargs):
        options = {"scope": "synthetic-run", "now": 200, "started_at": 100}
        options.update(kwargs)
        return (board or self.board).project([row()] if rows is None else rows,
                                             PROBLEMS if problems is None else problems, **options)

    def test_threshold_boundary_and_per_problem_override(self):
        board = self.make_board(unlock=2, overrides="B=0,C=3")
        first = self.project(board=board)
        self.assertEqual([problem["label"] for problem in first["meta"]["revealed_problems"]], ["B"])
        self.assertFalse(first["rows"][0]["ccpc_cells"][0]["revealed"])
        second = self.project([row(), row("beta")], board=board)
        self.assertEqual([problem["label"] for problem in second["meta"]["revealed_problems"]], ["A", "B"])
        self.assertEqual(second["meta"]["hidden_count"], 1)
        self.assertEqual(second["meta"]["revealed_problems"][0]["solved"], 2)
        self.assertTrue(second["rows"][0]["ccpc_cells"][0]["revealed"])

    def test_duplicate_team_and_duplicate_attempts_do_not_inflate_threshold(self):
        duplicate = row(cells={str(PROBLEMS[0]["id"]): stat(attempts=7)})
        result = self.project([row(), duplicate])
        self.assertFalse(result["rows"][0]["ccpc_cells"][0]["revealed"])
        result = self.project([row(), duplicate, row("beta")])
        totals = result["meta"]["revealed_problems"][0]
        self.assertEqual(totals["solved"], 2)
        self.assertEqual(totals["submissions"], 8)

    def test_team_name_fallback_is_deduplicated(self):
        one = row(username="")
        self.assertEqual(self.project([one, copy.deepcopy(one)])["meta"]["hidden_count"], 3)
        second = row("beta", username="")
        self.assertEqual(self.project([one, second])["meta"]["hidden_count"], 2)

    def test_pending_and_hacked_results_never_count_as_published_solves(self):
        for extra in ({"pending": True}, {"display": "?4"}, {"display": "2/??"}, {"hacked": True}):
            with self.subTest(extra=extra):
                result = self.project([row(), row("beta", {str(PROBLEMS[0]["id"]): stat(**extra)})], scope=str(extra))
                self.assertEqual(result["meta"]["hidden_count"], 3)
                self.assertNotEqual(result["rows"][1]["ccpc_cells"][0]["status"], "accepted")

    def test_upstream_totals_cannot_reveal_filtered_teams(self):
        result = self.project(published_totals={str(PROBLEMS[0]["id"]): {"solved": 100, "submissions": 500}})
        self.assertEqual(result["meta"]["hidden_count"], 3)

    def test_static_same_page_totals_include_resubmissions_after_acceptance(self):
        published = {str(PROBLEMS[0]["id"]): {"solved": 2, "submissions": 9}}
        for source in ("qoj-external", "qoj-pc2"):
            with self.subTest(source=source):
                result = self.project([row(), row("beta")], source=source, published_totals=published)
                self.assertEqual(result["meta"]["revealed_problems"][0]["submissions"], 9)
                self.assertEqual(result["meta"]["revealed_problems"][0]["solved"], 2)
                self.assertEqual(result["rows"][0]["ccpc_cells"][0]["display"], "+\n0:20")
        native = self.project([row(), row("beta")], published_totals=published)
        self.assertEqual(native["meta"]["revealed_problems"][0]["submissions"], 2)

    def test_unverifiable_static_totals_do_not_replace_visible_counts(self):
        for published in ({"solved": 3, "submissions": 9}, {"solved": 2, "submissions": 1},
                          {"solved": 2, "submissions": True}, {"solved": 2, "submissions": -1},
                          {"solved": 2, "submissions": 9.0}, {"solved": 2, "submissions": 2**53},
                          {"solved": 2.0, "submissions": 9}, {"solved": None, "submissions": 9}):
            with self.subTest(published=published):
                result = self.project([row(), row("beta")], source="qoj-external",
                                      published_totals={str(PROBLEMS[0]["id"]): published})
                self.assertEqual(result["meta"]["revealed_problems"][0]["submissions"], 2)
        unknown = row(cells={str(PROBLEMS[0]["id"]): {"solved": True, "display": "unknown"}})
        result = self.project([unknown], board=self.make_board(unlock=1), source="qoj-pc2",
                              published_totals={str(PROBLEMS[0]["id"]): {"solved": 1, "submissions": 9}})
        self.assertIsNone(result["meta"]["revealed_problems"][0]["submissions"])

    def test_hidden_projection_has_no_problem_mapping_or_source_fields(self):
        source = row(qoj_id=468012, other={"problem_id": PROBLEMS[0]["id"]},
                     cells={str(PROBLEMS[0]["id"]): stat(qoj_id=468012, link="https://qoj.ac/problem/817234")})
        untouched = copy.deepcopy(source)
        result = self.project([source])
        serialized = json.dumps(result["rows"])
        for forbidden in ("problem_stats", "problem_id", "qoj_id", "817234", "468012", "https:", "Synthetic Alpha"):
            self.assertNotIn(forbidden, serialized)
        cell = result["rows"][0]["ccpc_cells"][0]
        self.assertEqual(set(cell), {"key", "status", "display", "first_blood", "revealed"})
        self.assertEqual(source, untouched)
        self.assertEqual(result["rows"][0]["penalty_seconds"], 1200)

    def test_untrusted_display_and_time_are_not_copied(self):
        source = row(cells={str(PROBLEMS[0]["id"]): stat(attempts=2,
            display="problem=817234 qoj_id=468012", time="problem/817234")})
        cell = self.project([source])["rows"][0]["ccpc_cells"][0]
        self.assertEqual(cell["display"], "+1")

    def test_cells_have_distinct_stable_keys_per_team_and_scope(self):
        first = self.project([row(), row("beta")])
        keys = [item["ccpc_cells"][0]["key"] for item in first["rows"]]
        self.assertNotEqual(keys[0], keys[1])
        self.assertEqual(keys, [item["ccpc_cells"][0]["key"] for item in self.project([row(), row("beta")])["rows"]])
        self.assertNotEqual(keys[0], self.project(scope="another-run")["rows"][0]["ccpc_cells"][0]["key"])

    def test_accepts_are_sorted_by_time_with_other_attempts_last(self):
        cells = {str(PROBLEMS[0]["id"]): stat(time="1:00"),
                 str(PROBLEMS[1]["id"]): stat(time="30"),
                 str(PROBLEMS[2]["id"]): stat(False, 5, "0:01", display="-5")}
        result = self.project([row(cells=cells)], board=self.make_board(unlock=0))
        self.assertEqual([cell["label"] for cell in result["rows"][0]["ccpc_cells"]], ["B", "A", "C"])
        self.assertEqual(result["rows"][0]["ccpc_cells"][0]["display"], "+\n30")

    def test_missing_or_equal_times_use_opaque_stable_order(self):
        cells = {str(problem["id"]): stat(time="") for problem in PROBLEMS}
        result = self.project([row(cells=cells)])["rows"][0]["ccpc_cells"]
        self.assertEqual([cell["key"] for cell in result], sorted(cell["key"] for cell in result))
        self.assertEqual(result, self.project([row(cells=dict(reversed(list(cells.items()))))])["rows"][0]["ccpc_cells"])

    def test_no_submission_means_no_anonymous_placeholder(self):
        cells = {str(PROBLEMS[0]["id"]): {"display": "", "attempts": 0},
                 str(PROBLEMS[1]["id"]): {"display": "-", "attempts": 0},
                 str(PROBLEMS[2]["id"]): {"display": "0/--", "attempts": 0}}
        self.assertEqual(self.project([row(cells=cells)])["rows"][0]["ccpc_cells"], [])

    def test_pc2_parser_handles_failed_empty_and_timed_results(self):
        from results import parse_results_table

        document = '''<table><tr><th>Rank</th><th>Name</th><th>Solved</th><th>Time</th>
          <th>A</th><th>B</th><th>C</th><th>Total att/solv</th></tr>
          <tr><td>1</td><td>synthetic</td><td>1</td><td>54</td>
          <td class="no">7/--</td><td>0/--</td><td class="yes">3/14</td><td>10/1</td></tr></table>'''
        parsed = parse_results_table(document, 1802, PROBLEMS, "synthetic")
        result = self.project(parsed["rows"], source=parsed["source"], board=self.make_board(unlock=0))
        cells = result["rows"][0]["ccpc_cells"]
        self.assertEqual([(cell["label"], cell["status"], cell["display"]) for cell in cells],
                         [("C", "accepted", "+2\n14"), ("A", "wrong", "−7")])
        self.assertEqual(result["meta"]["revealed_problems"][1]["submissions"], 0)

    def test_first_blood_is_only_from_supported_explicit_source(self):
        source = row(cells={str(PROBLEMS[0]["id"]): stat(first_blood=True)})
        for upstream, expected in (("qoj", True), ("qoj-external", True), ("qoj-pc2", False), ("other", False)):
            with self.subTest(source=upstream):
                cell = self.project([source], source=upstream)["rows"][0]["ccpc_cells"][0]
                self.assertEqual(cell["first_blood"], expected)
        self.assertFalse(self.project()["rows"][0]["ccpc_cells"][0]["first_blood"])

    def test_reveal_all_preserves_pending_and_original_rank_penalty(self):
        source = row(rank="7", solved="2", penalty="120",
                     cells={str(PROBLEMS[0]["id"]): stat(False, 3, "4:10", pending=True, display="?3")})
        for option in ({"frozen": True}, {"freeze_at": 200}, {"finished": True}):
            with self.subTest(option=option):
                result = self.project([source], scope=str(option), **option)
                self.assertTrue(result["meta"]["all_revealed"])
                projected = result["rows"][0]
                self.assertEqual((projected["rank"], projected["solved"], projected["penalty"]), ("7", "2", "120"))
                self.assertEqual(projected["ccpc_cells"][0]["display"], "?3")
                self.assertEqual(projected["ccpc_cells"][0]["status"], "pending")

    def test_pending_does_not_imply_contest_freeze(self):
        result = self.project([row(cells={str(PROBLEMS[0]["id"]): stat(False, 2, "", pending=True, display="?2")})])
        self.assertEqual(result["meta"]["hidden_count"], 3)
        self.assertIn("--ccpc-reveal-after", result["meta"]["message"])

    def test_reveal_after_uses_actual_start_and_exact_boundary(self):
        board = self.make_board(reveal_after=240)
        self.assertFalse(self.project(board=board, now=14499)["meta"]["all_revealed"])
        self.assertTrue(self.project(board=board, now=14500)["meta"]["all_revealed"])
        self.assertFalse(self.project(board=board, now=14500, started_at=None, scope="unknown-clock")["meta"]["all_revealed"])
        self.assertTrue(self.project(board=self.make_board(reveal_after=0))["meta"]["all_revealed"])

    def test_reveals_persist_across_regressions_and_service_restart(self):
        self.project([row(), row("beta")])
        restarted = self.make_board()
        result = self.project([], board=restarted)
        self.assertEqual([problem["label"] for problem in result["meta"]["revealed_problems"]], ["A"])
        self.assertEqual(result["meta"]["revealed_problems"][0]["solved"], 0)

    def test_scope_configuration_problem_mapping_and_secret_isolation(self):
        self.project([row(), row("beta")])
        cases = [{"scope": "other-source-or-account"}, {"board": self.make_board(unlock=3)},
                 {"board": self.make_board(overrides="B=3")}, {"board": self.make_board(reveal_after=240)},
                 {"board": self.make_board(secret=b"another-synthetic-server-secret")},
                 {"problems": [dict(PROBLEMS[0], id=123456), *PROBLEMS[1:]]}]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.project([], **kwargs)["meta"]["hidden_count"], 3)

    def test_concurrent_reveals_are_monotonic_and_do_not_clobber(self):
        def run(index):
            problem = PROBLEMS[index % len(PROBLEMS)]
            cells = {str(problem["id"]): stat()}
            return self.project([row(cells=cells), row("beta", cells)])
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(run, range(24)))
        self.assertTrue(self.project([])["meta"]["all_revealed"])

    def test_unknown_submission_totals_stay_unknown(self):
        source = row(cells={str(PROBLEMS[0]["id"]): {"solved": True, "display": "unrecognized"}})
        result = self.project([source], board=self.make_board(unlock=1))
        self.assertEqual(result["meta"]["revealed_problems"][0]["solved"], 1)
        self.assertIsNone(result["meta"]["revealed_problems"][0]["submissions"])

    def test_demo_records_use_total_submissions_and_numeric_minutes(self):
        source = row(cells={str(PROBLEMS[0]["id"]): {"solved": True, "attempts": 2,
                                                    "submissions": 4, "time": 12, "first_blood": True}})
        result = self.project([source], board=self.make_board(unlock=0), demo=True)
        self.assertEqual(result["meta"]["revealed_problems"][0]["submissions"], 4)
        self.assertFalse(result["rows"][0]["ccpc_cells"][0]["first_blood"])
        self.assertEqual(result["rows"][0]["ccpc_cells"][0]["display"], "+1\n12")

    def test_invalid_rows_and_non_icpc_fail_closed(self):
        for source in ([None], [row(problem_stats=[])], [row(cells={str(PROBLEMS[0]["id"]): []})],
                       [row(username=None, team_name=None)], [row(penalty={"private": 1})], None):
            with self.subTest(rows=source), self.assertRaises(ValueError):
                self.board.project(source, PROBLEMS, scope="invalid", now=1, started_at=0)
        for ruleset in (None, "OI", "IOI", "UOJ"):
            with self.subTest(ruleset=ruleset), self.assertRaisesRegex(ValueError, "ICPC"):
                self.project(ruleset=ruleset)

    def test_standard_style_preserves_existing_board_and_returns_copy(self):
        source = [row()]
        board = CCPCScoreboard(self.path, CCPCConfig(), self.secret)
        result = self.project(source, board=board)
        self.assertEqual(result, {"rows": source, "meta": {"style": "standard"}})
        result["rows"][0]["problem_stats"].clear()
        self.assertTrue(source[0]["problem_stats"])


if __name__ == "__main__":
    unittest.main()
