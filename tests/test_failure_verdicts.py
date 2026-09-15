"""Training-mode failure labels, without accessing an account or posting code.

On 2026-09-15 the user-authorized browser displayed submission 2964057's final
Result as `TL`. Its current table headings were ID / Problem / Submitter /
Result / Language / File size / Submit time. IDs/account/code here are fixtures;
the structural layout and visible TL label reproduce that report. Official QOJ
rules https://qoj.ac/blog/qingyu/blog/1341?lang=en also explicitly define TL and
ML. Other added aliases and English variants are generic compatibility cases,
not claims that every spelling was observed in a live QOJ response.
"""
import unittest

from qoj import parse_verdict


SID = 31001


def fixture(result="TL", *, sid=SID, problem="A. Fixture", code="while True:\n    pass\n"):
    return f'''<table><thead><tr><th>ID</th><th>Problem</th><th>Submitter</th>
      <th>Result</th><th>Language</th><th>File size</th><th>Submit time</th></tr></thead>
      <tbody><tr><td><a href="/submission/{sid}">#{sid}</a></td>
      <td><a href="/contest/3962/problem/20053">{problem}</a></td>
      <td><span class="uoj-username" data-nickname="Training team">fixture-team</span></td>
      <td><a href="/submission/{sid}" class="uoj-status" style="color:red">{result}</a></td>
      <td>PyPy3</td><td>32b</td><td><small>2026-09-15 15:00:00</small></td></tr></tbody></table>
      <div class="uoj-submission-code"><pre><code>{code}</code></pre></div>'''


class FailureVerdictTests(unittest.TestCase):
    def test_actual_training_tl_alias_is_final_tle(self):
        result = parse_verdict(fixture(), SID)
        self.assertEqual(result["verdict"], "TLE")
        self.assertEqual(result["message"], "TL")

    def test_short_failure_aliases_and_existing_final_results(self):
        cases = {"TL": "TLE", "ML": "MLE", "OL": "OLE", "RTE": "RE", "JF": "JE", "IE": "JE", "TLE": "TLE",
                 "MLE": "MLE", "OLE": "OLE", "WA": "WA", "RE": "RE", "CE": "CE",
                 "Time Limit Exceeded": "TLE", "Memory Limit Exceeded": "MLE",
                 "Output Limit Exceeded": "OLE", "Wrong Answer": "WA",
                 "Runtime Error": "RE", "Run Time Error": "RE", "Compilation Error": "CE",
                 "Compile Time Error": "CE", "Judgement Failed": "JE", "Judgment Failed": "JE",
                 "Internal Error": "JE", "Accepted": "AC"}
        for visible, expected in cases.items():
            with self.subTest(visible=visible):
                self.assertEqual(parse_verdict(fixture(visible), SID)["verdict"], expected)

    def test_aliases_keep_working_with_supported_trailing_icons(self):
        for alias, expected in (("TL", "TLE"), ("ML", "MLE"), ("OL", "OLE"), ("RTE", "RE"), ("JF", "JE"), ("IE", "JE")):
            for visible in (alias + " ×", alias + " ✘", alias + " ✗", "  " + alias + "  ", alias.lower()):
                with self.subTest(visible=visible):
                    self.assertEqual(parse_verdict(fixture(visible), SID)["verdict"], expected)

    def test_problem_title_and_source_tl_do_not_override_real_result(self):
        for visible, expected in (("WA", "WA"), ("RE", "RE"), ("CE", "CE"), ("Judging", "PENDING"), ("?", "UNKNOWN")):
            with self.subTest(visible=visible):
                source = fixture(visible, problem="TL", code='// TL Time Limit Exceeded\nwhile True:\n    pass\n')
                self.assertEqual(parse_verdict(source, SID)["verdict"], expected)

    def test_other_submission_tl_does_not_determine_requested_verdict(self):
        self.assertEqual(parse_verdict(fixture("TL", sid=SID + 1), SID)["verdict"], "UNKNOWN")
        source = fixture("TL", sid=SID + 1) + fixture("WA")
        self.assertEqual(parse_verdict(source, SID)["verdict"], "WA")

    def test_hidden_values_and_unrecognized_strings_are_not_guessed(self):
        for visible in ('<span data-verdict="TL">?</span>', '<span title="Time Limit Exceeded">Hidden</span>', 'UNTL', 'TL_sample_title'):
            with self.subTest(visible=visible):
                self.assertEqual(parse_verdict(fixture(visible), SID)["verdict"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
