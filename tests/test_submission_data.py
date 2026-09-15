"""Offline submission metadata/source tests, with a stripped current QOJ row.

The row structure was read from the public contest 1042 submissions page on
2026-09-15 using its advertised Show all submissions display preference. IDs,
username, title and date below are fixture values. The actual page has no
Language column; accepted score text is `100 \u2713` with data-full/data-score.

The current official https://qoj.ac/js/uoj.js?v=2026.09.14 source-copy function
reads `.uoj-submission-code` panels' `pre code` text. Current authenticated detail
HTML was unavailable; footer language fixtures follow official UniversalOJ's
web/app/libs/uoj-html-lib.php source renderer and are explicitly synthetic.
"""
import hashlib
import unittest
from datetime import datetime, timezone

from submission_data import extract_sources, fingerprint, records


HEAD = '<thead><tr><th>ID</th><th>Problem</th><th>Submitter</th><th>Result</th><th>Time</th><th>Memory</th><th>File size</th><th>Submit time</th></tr></thead>'
ROW = '''<tr><td><a href="/submission/12345" style="color: orange">#12345</a></td>
<td><a href="/contest/1042/problem/21701">A. Fixture</a></td>
<td><span class="uoj-username" data-nickname="Visible nickname" data-rated="1" data-rating="1800" data-user-color="rgb(55, 205, 155)">fixture-team</span> <span aria-hidden="true" class="glyphicon glyphicon-lock"></span></td>
<td><a class="uoj-score" data-full="100.000000" data-score="100.000000" href="/submission/12345">100 ✓</a></td>
<td>2ms</td><td>4528kb</td><td>261b</td><td><small>2026-09-15 14:18:47</small></td></tr>'''
HTML = '<table>' + HEAD + '<tbody>' + ROW + '</tbody></table>'


class SubmissionRecordsTests(unittest.TestCase):
    def test_current_raw_span_and_missing_language_column(self):
        self.assertEqual(records(HTML, 1042), {12345: {
            "username": "fixture-team", "problem_id": 21701, "language": None,
            "submitted_at": datetime(2026, 9, 15, 6, 18, 47, tzinfo=timezone.utc).timestamp(),
            "submitted_text": "2026-09-15 14:18:47", "contest_id": 1042}})
        self.assertNotIn("Visible nickname", str(records(HTML)))

    def test_language_header_is_used_when_present(self):
        html = HTML.replace('<th>File size</th>', '<th>Language</th><th>File size</th>').replace('<td>261b</td>', '<td><a href="/submission/12345">C++26</a></td><td>261b</td>')
        self.assertEqual(records(html, 1042)[12345]["language"], "C++26")

    def test_reordered_headers_and_rendered_profile_identity(self):
        html = '''<table><tr><th>Submitter</th><th>Language</th><th>Problem</th><th>ID</th></tr>
          <tr><td><a href="https://qoj.ac/user/profile/fixture-team">A nickname</a></td>
          <td>C++26</td><td><a href="/contest/1042/problem/21701">A</a></td>
          <td><a href="/contest/1042/submission/12345">#12345</a></td></tr></table>'''
        item = records(html, 1042)[12345]
        self.assertEqual((item["username"], item["language"], item["problem_id"]), ("fixture-team", "C++26", 21701))
        self.assertIsNone(item["submitted_at"])

    def test_cross_contest_or_foreign_identity_links_are_excluded(self):
        for html in (HTML.replace('/contest/1042/', '/contest/999/'),
                     HTML.replace('href="/submission/12345"', 'href="https://evil.test/submission/12345"'),
                     HTML.replace('href="/contest/1042/problem/21701"', 'href="https://evil.test/problem/21701"')):
            with self.subTest(html=html[:80]):
                self.assertEqual(records(html, 1042), {})
        html = HTML.replace('/submission/12345', '/contest/999/submission/12345')
        self.assertEqual(records(html, 1042), {})

    def test_conflicting_ids_accounts_and_duplicate_rows_are_rejected(self):
        variants = [HTML.replace('#12345</a>', '#12345</a><a href="/submission/999">#999</a>', 1),
                    HTML.replace('A. Fixture</a>', 'A. Fixture</a><a href="/problem/999">Other</a>'),
                    HTML.replace('fixture-team</span>', 'fixture-team</span><a href="/user/profile/other">Other</a>'),
                    HTML + HTML.replace('fixture-team</span>', 'other-team</span>')]
        for html in variants:
            with self.subTest(html=html[:80]):
                self.assertEqual(records(html, 1042), {})

    def test_headerless_legacy_rows_still_require_three_explicit_identity_cells(self):
        self.assertEqual(records('<table>' + ROW + '</table>', 1042)[12345]["username"], "fixture-team")
        self.assertEqual(records('<table>' + ROW.replace('class="uoj-username"', 'class="other"') + '</table>', 1042), {})
        self.assertEqual(records(HTML.replace('<th>Submitter</th>', '<th>Unrelated person</th>'), 1042), {})

    def test_script_tables_and_test_source_tables_are_not_records(self):
        self.assertEqual(records('<div class="uoj-submission-code">' + HTML + '</div>', 1042), {})
        self.assertEqual(records('<pre>' + HTML + '</pre>', 1042), {})

    def test_invalid_dates_remain_unknown_and_explicit_timezone_is_honored(self):
        self.assertIsNone(records(HTML.replace('2026-09-15 14:18:47', 'hidden'))[12345]["submitted_at"])
        value = records(HTML.replace('2026-09-15 14:18:47', '2026-09-15T06:18:47Z'))[12345]["submitted_at"]
        self.assertEqual(value, records(HTML)[12345]["submitted_at"])


class SourceTests(unittest.TestCase):
    def test_current_source_container_and_visible_footer_language(self):
        html = '''<div class="card uoj-submission-code"><div class="card-header">answer</div>
          <div class="card-body"><pre><code class="language-cpp">  int main() { return 0; }\n</code></pre></div>
          <div class="card-footer">Source code, Language: C++26</div></div>
          <pre>Wrong Answer is only test output.</pre>'''
        self.assertEqual(extract_sources(html), [{"source": "  int main() { return 0; }\n", "language": "C++26"}])

    def test_explicit_source_ids_textarea_and_labels(self):
        html = '''<label for="source-code">Source code, Language: Python3</label>
          <textarea id="source-code">  print(&quot;hello&quot;)\n</textarea>
          <div class="card"><pre id="submission-source">int main() {}\n</pre>
          <div class="card-footer">Language: C++26</div></div>'''
        self.assertEqual(extract_sources(html), [
            {"source": '  print("hello")\n', "language": "Python3"},
            {"source": "int main() {}\n", "language": "C++26"}])

    def test_normal_pre_textarea_and_scripts_never_become_source(self):
        html = '<pre>sample input</pre><textarea name="input">custom input</textarea><script>source="pretend";</script>'
        self.assertEqual(extract_sources(html), [])

    def test_nested_source_containers_are_not_duplicated_and_conflicts_stay_unknown(self):
        html = '''<div class="uoj-submission-code"><div class="card uoj-submission-code">
          <pre id="source-code"><code>x &lt; y\n</code></pre><div class="card-footer">Language: C++26</div>
          <label>Language: Python3</label></div></div>'''
        self.assertEqual(extract_sources(html), [{"source": "x < y\n", "language": None}])

    def test_multiple_source_panels_keep_respective_languages(self):
        html = '''<div class="uoj-submission-code"><div class="card"><pre><code>cpp\n</code></pre><div class="card-footer">Language: C++26</div></div>
          <div class="card"><pre><code>python\n</code></pre><div class="card-footer">Language: Python3</div></div></div>'''
        self.assertEqual(extract_sources(html), [{"source": "cpp\n", "language": "C++26"}, {"source": "python\n", "language": "Python3"}])

    def test_fingerprint_normalizes_line_endings_without_stripping_spaces(self):
        self.assertEqual(fingerprint('  x\r\n y\r\n\r\n'), fingerprint('  x\n y'))
        self.assertEqual(fingerprint('x'), hashlib.sha256(b'x').hexdigest())
        self.assertNotEqual(fingerprint(' x'), fingerprint('x'))
        self.assertNotEqual(fingerprint('x '), fingerprint('x'))
        self.assertNotEqual(fingerprint('x\r'), fingerprint('x'))


if __name__ == "__main__":
    unittest.main()
