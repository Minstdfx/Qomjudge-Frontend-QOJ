"""Native ICPC first-blood rules; fixtures are synthetic, not private contests.

The expected comparison follows QOJ's official uoj.js renderer: inspect the
complete score object, including users absent from standings, using raw seconds.
PC2's published HTML remains authoritative and receives no local first blood.
"""
import unittest

from participation import parse_scoreboard
from test_participation import PROBLEMS, scoreboard
from test_results import parse as parse_pc2


def cell(seconds, value=100, maximum=100, pending=0, wrong=0):
    return [value, seconds, 123, wrong, maximum, pending]


def parse(scores, users=None, rules="ICPC", problems=PROBLEMS):
    users = list(scores) if users is None else users
    standings = [[100, 7200, [user, 1800, 0, user], index + 7] for index, user in enumerate(users)]
    return parse_scoreboard(scoreboard(score=scores, standings=standings, rules=rules), 9, problems, users[0] if users else "")


def stats(result):
    return {row["username"]: row["problem_stats"] for row in result["rows"]}


class NativeFirstBloodTests(unittest.TestCase):
    def test_first_solve_is_independent_of_standings_and_score_object_order(self):
        scores = {"later": [cell(300)], "earliest": [cell(100)], "middle": [cell(200)]}
        for values in (scores, dict(reversed(list(scores.items())))):
            for order in (("later", "earliest", "middle"), ("middle", "later", "earliest")):
                with self.subTest(scores=list(values), order=order):
                    result = parse(values, list(order))
                    self.assertEqual([row["username"] for row in result["rows"]], list(order))
                    self.assertEqual([row["rank"] for row in result["rows"]], [7, 8, 9])
                    flags = {user: values["10"]["first_blood"] for user, values in stats(result).items()}
                    self.assertEqual(flags, {"later": False, "earliest": True, "middle": False})

    def test_raw_seconds_distinguish_solutions_with_the_same_displayed_minute(self):
        result = stats(parse({"later": [cell(3659)], "earliest": [cell(3601)]}))
        self.assertEqual(result["later"]["10"]["time"], result["earliest"]["10"]["time"])
        self.assertEqual(result["earliest"]["10"]["time"], "1:00")
        self.assertFalse(result["later"]["10"]["first_blood"])
        self.assertTrue(result["earliest"]["10"]["first_blood"])

    def test_all_exact_timestamp_ties_receive_first_blood(self):
        result = stats(parse({"tied-a": [cell(123, wrong=4)], "later": [cell(124)],
                              "tied-b": [cell(123, wrong=0)]}))
        self.assertTrue(result["tied-a"]["10"]["first_blood"])
        self.assertTrue(result["tied-b"]["10"]["first_blood"])
        self.assertFalse(result["later"]["10"]["first_blood"])

    def test_earlier_score_user_absent_from_standings_prevents_false_first_blood(self):
        result = parse({"visible": [cell(200)], "outside-standings": [cell(100)]}, ["visible"])
        self.assertEqual(len(result["rows"]), 1)
        self.assertFalse(result["rows"][0]["problem_stats"]["10"]["first_blood"])
        self.assertNotIn("outside-standings", str(result["rows"]))

    def test_list_and_dict_problem_columns_use_upstream_column_order(self):
        result = stats(parse({"alice": {"1": cell(200), "0": cell(100)},
                              "bob": [cell(150), cell(180)]}, problems=list(reversed(PROBLEMS))))
        self.assertTrue(result["alice"]["10"]["first_blood"])
        self.assertFalse(result["alice"]["11"]["first_blood"])
        self.assertFalse(result["bob"]["10"]["first_blood"])
        self.assertTrue(result["bob"]["11"]["first_blood"])

    def test_sparse_columns_do_not_mix_first_blood_between_problems(self):
        result = stats(parse({"alice": {"1": cell(500)}, "bob": {"0": cell(100)}}))
        self.assertFalse(result["alice"]["10"]["first_blood"])
        self.assertTrue(result["alice"]["11"]["first_blood"])
        self.assertTrue(result["bob"]["10"]["first_blood"])
        self.assertFalse(result["bob"]["11"]["first_blood"])

    def test_nonstandard_full_scores_qualify_but_partial_scores_do_not(self):
        result = stats(parse({"partial": [cell(1, value=100, maximum=200)],
                              "first-full": [cell(100, value=50, maximum=50)],
                              "later-full": [cell(200, value=200, maximum=200)]}))
        self.assertFalse(result["partial"]["10"]["first_blood"])
        self.assertTrue(result["first-full"]["10"]["first_blood"])
        self.assertFalse(result["later-full"]["10"]["first_blood"])

    def test_pending_full_score_still_participates_in_the_native_minimum(self):
        result = stats(parse({"pending": [cell(100, pending=1)], "later": [cell(200)]}))
        self.assertTrue(result["pending"]["10"]["first_blood"])
        self.assertTrue(result["pending"]["10"]["pending"])
        self.assertTrue(result["pending"]["10"]["text"].startswith("?1"))
        self.assertFalse(result["later"]["10"]["first_blood"])

    def test_unsolved_pending_cell_does_not_beat_a_confirmed_full_score(self):
        result = stats(parse({"pending": [cell(1, value=0, pending=1)], "accepted": [cell(100)]}))
        self.assertFalse(result["pending"]["10"]["first_blood"])
        self.assertTrue(result["accepted"]["10"]["first_blood"])

    def test_earlier_hacked_97_blocks_later_acceptance_but_is_not_first_blood(self):
        result = stats(parse({"hacked": [cell(100, value=97)], "accepted": [cell(200)]}))
        self.assertTrue(result["hacked"]["10"]["hacked"])
        self.assertTrue(result["hacked"]["10"]["solved"])
        self.assertFalse(result["hacked"]["10"]["first_blood"])
        self.assertFalse(result["accepted"]["10"]["first_blood"])

    def test_hacked_timestamp_tie_does_not_remove_normal_full_score_first_blood(self):
        result = stats(parse({"hacked": [cell(100, value=97, maximum=97)], "accepted": [cell(100)]}))
        self.assertFalse(result["hacked"]["10"]["first_blood"])
        self.assertTrue(result["accepted"]["10"]["first_blood"])

    def test_hacked_user_outside_standings_also_participates_in_minimum(self):
        result = stats(parse({"visible": [cell(200)], "hacked": {"0": cell(100, value=97)}}, ["visible"]))
        self.assertFalse(result["visible"]["10"]["first_blood"])

    def test_empty_missing_and_unsolved_cells_have_boolean_false(self):
        result = stats(parse({"missing": {}, "blank": [None], "failed": [cell(-1, value=0)]}))
        for problem_stats in result.values():
            for item in problem_stats.values():
                self.assertIs(item["first_blood"], False)

    def test_non_icpc_rules_do_not_mark_first_blood(self):
        for rules in ("OI", "IOI", "UOJ"):
            with self.subTest(rules=rules):
                result = stats(parse({"first": [cell(100)], "later": [cell(200)]}, rules=rules))
                for problem_stats in result.values():
                    for item in problem_stats.values():
                        self.assertIs(item["first_blood"], False)

    def test_malformed_or_hidden_outside_scores_disable_first_blood_without_losing_valid_rows(self):
        hidden_scores = ["hidden", None, {"0": "?"}, {"0": [100]},
                         {"0": cell(None)}, {"0": cell("?")},
                         {"0": cell(1, value=None)}, {"0": cell(1, value="?")},
                         {"0": cell(1, maximum=None)}, {"0": cell(1, maximum="?")},
                         {"0": cell(True)}, {"0": cell(1, value=True)}]
        for hidden in hidden_scores:
            with self.subTest(hidden=hidden):
                result = parse({"visible": [cell(100)], "outside-standings": hidden}, ["visible"])
                self.assertEqual(len(result["rows"]), 1)
                self.assertEqual(result["rows"][0]["rank"], 7)
                item = result["rows"][0]["problem_stats"]["10"]
                self.assertTrue(item["solved"])
                self.assertFalse(item["first_blood"])

    def test_pc2_public_html_keeps_original_stats_without_first_blood_field(self):
        parsed = parse_pc2()
        self.assertEqual(parsed["source"], "qoj-pc2")
        self.assertEqual(parsed["rows"][0]["problem_stats"]["1002"]["text"], "2/14")
        for row in parsed["rows"]:
            for item in row["problem_stats"].values():
                self.assertNotIn("first_blood", item)


if __name__ == "__main__":
    unittest.main()
