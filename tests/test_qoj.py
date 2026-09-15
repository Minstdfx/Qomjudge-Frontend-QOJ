"""Offline UOJ/QOJ fixtures: no credentials or live submission requests."""
import json
import unittest
from unittest.mock import Mock

from bs4 import BeautifulSoup
from requests import Response, Timeout

from qoj import (QOJClient, QOJError, parse_contest, parse_problem,
                 parse_verdict, rewrite_proxy, statement_document)


def response(body="", url="https://qoj.ac/contest/1802", status=200, **headers):
    result = Response()
    result.status_code = status
    result.url = url
    result.headers.update(headers)
    result._content = body.encode("utf-8")
    result.encoding = "utf-8"
    return result


def problem_html(extra_form=""):
    options = json.dumps('<option value="C++17">C++ 17</option><option value="Python3">Python 3</option>')
    return f'''<h1>A. Addition</h1><p>Time Limit: 1 s Memory Limit: 256 MB</p>
        <div id="tab-statement"><p>Add $a+b$.</p><iframe src=/download.php?type=statement&id=10></iframe></div>
        <form id="form-answer" action="/contest/1802/problem/10">
          <input type="hidden" name="_token" value="fixture-csrf">
          <script>$('#form-group-answer_answer').source_code_form_group('answer_answer', 'Source code:answer', {options}, false);</script>
          {extra_form}<button name="submit-answer" value="answer">Submit</button>
        </form>'''


def verdict_html(result, identifier=123):
    return f'''<table><thead><tr><th>ID</th><th>Problem</th><th>Submitter</th><th>Result</th></tr></thead>
        <tbody><tr><td><a href="/submission/{identifier}">{identifier}</a></td>
        <td>Accepted is only the problem title</td><td>Compile Error is a username</td>
        <td>{result}</td></tr></tbody></table>'''


def submission_row(identifier, username="fixture-user", problem_id=10, language="C++17"):
    return f'''<tr><td><a href="/submission/{identifier}">{identifier}</a></td>
        <td><a href="/contest/1802/problem/{problem_id}">Problem</a></td>
        <td><a href="/user/profile/{username}">{username}</a></td>
        <td>Judging</td><td>{language}</td></tr>'''


class ParserTests(unittest.TestCase):
    def test_contest_preserves_problem_order_and_excludes_submission_history(self):
        source = '''<h1 class="page-header">Example Contest</h1><p>Duration: 5 hours</p>
          <table><tr><td>A</td><td><a href="/contest/1802/problem/99">A. First</a></td></tr>
          <tr><td>B</td><td><a href="/problem/10">Second</a></td></tr>
          <tr><td>B</td><td><a href="/problem/10">Duplicate</a></td></tr></table>
          <table><tr><td><a href="/submission/3">3</a></td>
          <td><a href="/problem/300">Unrelated history</a></td></tr></table>'''
        contest = parse_contest(source, 1802)
        self.assertEqual(contest["title"], "Example Contest")
        self.assertEqual(contest["duration_seconds"], 18000)
        self.assertEqual([(p["id"], p["label"], p["title"]) for p in contest["problems"]],
                         [(99, "A", "First"), (10, "B", "Second")])

    def test_contest_does_not_invent_duration_or_accept_empty_problem_list(self):
        result = parse_contest('<table><tr><td><a href="/problem/10">A</a></td></tr></table>', 1802)
        self.assertIsNone(result["duration_seconds"])
        with self.assertRaises(QOJError):
            parse_contest('<form id="form-login"></form>', 1802)

    def test_compound_duration_preserves_minutes(self):
        result = parse_contest('<p>Duration: 4 hours 30 minutes</p><table><tr><td><a href="/problem/10">A</a></td></tr></table>', 1802)
        self.assertEqual(result["duration_seconds"], 16200)

    def test_problem_reads_dynamic_language_form_and_preserves_pdf_statement(self):
        problem = parse_problem(problem_html(), 10, "https://qoj.ac/contest/1802/problem/10")
        self.assertEqual(problem["title"], "Addition")
        self.assertTrue(problem["submission_available"])
        self.assertEqual(problem["languages"], [{"value": "C++17", "label": "C++ 17"},
                                                 {"value": "Python3", "label": "Python 3"}])
        statement = BeautifulSoup(problem["statement_html"], "html.parser")
        self.assertEqual(statement.iframe["src"], "/qoj/download.php?type=statement&id=10")
        self.assertEqual(problem["time_limit"], "1 s")
        self.assertEqual(problem["memory_limit"], "256 MB")

    def test_multifile_or_output_only_form_is_not_silently_submitted(self):
        for extra in ("<script>$('#x').text_file_form_group('output', 'Output');</script>",
                      "<script>$('#x').source_code_form_group('second', 'Code', "
                      + json.dumps('<option value="C++17">C++ 17</option>') + ", false);</script>"):
            with self.subTest(extra=extra):
                problem = parse_problem(problem_html(extra), 10, "https://qoj.ac/problem/10")
                self.assertFalse(problem["submission_available"])
                self.assertTrue(problem["message"])

    def test_statement_removes_active_content_and_external_frames(self):
        fragment = '''<script>window.pwned=true;</script><svg onload="attack()"></svg>
            <p onclick="attack()" style="background:url(javascript:attack())">Safe text</p>
            <a href="javascript:alert(1)">Unsafe link</a><img src="/images/sample.png" onerror="attack()">
            <form action="/api/reset"><input name="team_name"></form>
            <iframe src="https://evil.example/embed"></iframe>
            <iframe src="/download.php?type=statement&id=10"></iframe>'''
        document = statement_document(fragment, "https://qoj.ac/problem/10")
        soup = BeautifulSoup(document, "html.parser")
        self.assertNotIn("window.pwned", document)
        self.assertIsNone(soup.body.find("script"))
        self.assertIsNone(soup.find("svg"))
        self.assertIsNone(soup.find("form"))
        self.assertNotIn("onclick", soup.p.attrs)
        self.assertNotIn("style", soup.p.attrs)
        self.assertNotIn("href", soup.a.attrs)
        self.assertNotIn("onerror", soup.img.attrs)
        self.assertEqual(soup.img["src"], "/qoj/images/sample.png")
        self.assertEqual(len(soup.find_all("iframe")), 1)
        self.assertIn("type=statement", soup.iframe["src"])

    def test_verdict_uses_result_cell_instead_of_problem_title_or_source(self):
        for text, expected in (("Compile Error", "CE"), ("Compilation Error", "CE"),
                               ("Time Limit Exceeded", "TLE"), ("Wrong Answer", "WA"),
                               ("Judging", "PENDING"), ("Accepted", "AC"),
                               ("AC ✓", "AC"), ("WA ✗", "WA"), ("TLE ×", "TLE")):
            with self.subTest(text=text):
                source = verdict_html(text) + '<pre>// Accepted; Wrong Answer; Compile Error</pre>'
                self.assertEqual(parse_verdict(source, 123)["verdict"], expected)

    def test_score_mapping_keeps_partial_scores_unsolved(self):
        for value, expected in (("100", "AC"), ("100.0", "AC"), ("70", "WA"),
                                ("0", "WA"), ("120", "UNKNOWN")):
            with self.subTest(value=value):
                self.assertEqual(parse_verdict(verdict_html(f'<a class="uoj-score">{value}</a>'), 123)["verdict"], expected)
        source = verdict_html('<a class="uoj-score">0</a>') + '<div class="panel-title">Runtime Error</div>'
        self.assertEqual(parse_verdict(source, 123)["verdict"], "RE")

    def test_verdict_missing_requested_submission_remains_unknown(self):
        self.assertEqual(parse_verdict('<pre>Accepted</pre>', 123)["verdict"], "UNKNOWN")
        self.assertEqual(parse_verdict(verdict_html('<a class="uoj-score">100</a>', identifier=999), 123)["verdict"], "UNKNOWN")

    def test_proxy_rewrites_absolute_relative_ajax_and_unquoted_asset_paths(self):
        source = '''<a href="https://qoj.ac/contest/1802">Contest</a>
          <img src="../../images/logo.png"><iframe src=/download.php?type=statement&id=10></iframe>
          <a href="https://external.example/help">External</a><form id="login"></form>
          <script>$.post('/login', data);</script><style>x{background:url(/images/x.png)}</style>'''
        rewritten = rewrite_proxy(source, "text/html", "https://qoj.ac/contest/1802/problem/10")
        self.assertIn('href="/qoj/contest/1802"', rewritten)
        self.assertIn('src="/qoj/contest/images/logo.png"', rewritten)
        self.assertIn("src=/qoj/download.php", rewritten)
        self.assertIn("$.post('/qoj/login'", rewritten)
        self.assertIn("url(/qoj/images/x.png)", rewritten)
        self.assertIn('action="/qoj/contest/1802/problem/10"', rewritten)
        self.assertIn('href="https://external.example/help"', rewritten)
        self.assertNotIn("/qoj/qoj/", rewritten)

    def test_javascript_rewrite_preserves_non_url_slash_literals(self):
        source = "var delimiter='/'; var pattern='/abc/'; $.post('/login', data); window.location='/';"
        rewritten = rewrite_proxy(source, "application/javascript", "https://qoj.ac/js/uoj.js")
        self.assertIn("var delimiter='/';", rewritten)
        self.assertIn("var pattern='/abc/';", rewritten)
        self.assertIn("$.post('/qoj/login', data)", rewritten)
        self.assertIn("window.location='/qoj/'", rewritten)

    def test_submission_identity_requires_account_problem_and_language_match(self):
        source = '<table><thead><tr><th>ID</th><th>Problem</th><th>Submitter</th><th>Result</th><th>Language</th></tr></thead><tbody>' + submission_row(1) + submission_row(2, username="someone-else")
        source += submission_row(3, problem_id=99) + submission_row(4, language="Python3") + '</tbody></table>'
        self.assertEqual(set(QOJClient._rows(source, 10, "fixture-user", "C++17")), {1})
        self.assertEqual(QOJClient._rows(source, 10, "", "C++17"), {})


class ClientTransportTests(unittest.TestCase):
    def setUp(self):
        self.client = QOJClient()
        self.addCleanup(self.client.session.close)
        self.client.session.request = Mock()

    def test_offsite_targets_are_rejected_before_network_access(self):
        for target in ("https://evil.example/", "//evil.example/", "http://qoj.ac/",
                       "https://qoj.ac@evil.example/", "https://qoj.ac:444/", "https://qoj.ac\\@evil.example/"):
            with self.subTest(target=target), self.assertRaises(QOJError):
                self.client.fetch("GET", target)
        self.client.session.request.assert_not_called()

    def test_offsite_redirect_is_rejected_before_cookies_could_be_sent(self):
        self.client.session.request.return_value = response(status=302, Location="https://evil.example/")
        with self.assertRaises(QOJError):
            self.client.fetch("GET", "/contest/1802")
        self.assertEqual(self.client.session.request.call_count, 1)

    def test_uncertain_post_and_preserving_redirect_are_never_retried(self):
        self.client.session.request.side_effect = Timeout("fixture timeout")
        with self.assertRaises(QOJError):
            self.client.fetch("POST", "/problem/10", data={"source": "fixture"})
        self.assertEqual(self.client.session.request.call_count, 1)
        self.client.session.request.reset_mock(side_effect=True)
        self.client.session.request.return_value = response(status=307, Location="/problem/10")
        with self.assertRaises(QOJError):
            self.client.fetch("POST", "/problem/10", data={"source": "fixture"})
        self.assertEqual(self.client.session.request.call_count, 1)

    def test_normal_post_redirect_follows_as_get_without_body(self):
        self.client.session.request.side_effect = [response(status=303, Location="/submission/123"),
                                                  response(url="https://qoj.ac/submission/123")]
        result = self.client.fetch("POST", "/problem/10", data={"source": "fixture"},
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(result.url, "https://qoj.ac/submission/123")
        self.assertEqual(self.client.session.request.call_count, 2)
        args, kwargs = self.client.session.request.call_args
        self.assertEqual(args, ("GET", "https://qoj.ac/submission/123"))
        self.assertNotIn("data", kwargs)
        self.assertNotIn("Content-Type", kwargs["headers"])

    def test_mocked_submission_uses_live_token_language_field_and_one_post(self):
        self.client.username = "fixture-user"
        self.client._page = Mock(side_effect=[response(problem_html(), "https://qoj.ac/contest/1802/problem/10"),
                                             response("<table></table>", "https://qoj.ac/contest/1802/submissions")])
        self.client.fetch = Mock(return_value=response(url="https://qoj.ac/submission/123"))
        result = self.client.submit(1802, 10, "C++17", "int main() {}", "main.cpp")
        self.assertEqual(result["qoj_id"], 123)
        self.client.fetch.assert_called_once()
        args, kwargs = self.client.fetch.call_args
        self.assertEqual(args, ("POST", "https://qoj.ac/contest/1802/problem/10"))
        self.assertEqual(kwargs["data"]["_token"], "fixture-csrf")
        self.assertEqual(kwargs["data"]["answer_answer_language"], "C++17")
        self.assertEqual(kwargs["data"]["answer_answer_upload_type"], "file")
        self.assertEqual(kwargs["data"]["submit-answer"], "answer")
        self.assertEqual(kwargs["files"]["answer_answer_file"], ("main.cpp", b"int main() {}", "text/plain"))

    def test_ambiguous_identical_submissions_are_not_arbitrarily_assigned(self):
        self.client.username = "fixture-user"
        source = "int main() {}"
        self.client._page = Mock(side_effect=[
            response(problem_html(), "https://qoj.ac/contest/1802/problem/10"),
            response("<table></table>", "https://qoj.ac/contest/1802/submissions"),
            response(f'<div class="uoj-submission-code"><pre><code>{source}</code></pre></div>', "https://qoj.ac/submission/1"),
            response(f'<div class="uoj-submission-code"><pre><code>{source}</code></pre></div>', "https://qoj.ac/submission/2"),
        ])
        self.client.fetch = Mock(return_value=response('<table>' + submission_row(1) + submission_row(2) + '</table>',
                                                       "https://qoj.ac/contest/1802/submissions"))
        with self.assertRaises(QOJError):
            self.client.submit(1802, 10, "C++17", source, "main.cpp")
        self.client.fetch.assert_called_once()

    def test_missing_upstream_csrf_token_prevents_any_submission_post(self):
        document = problem_html().replace('<input type="hidden" name="_token" value="fixture-csrf">', '')
        self.client._page = Mock(return_value=response(document, "https://qoj.ac/contest/1802/problem/10"))
        self.client.fetch = Mock()
        with self.assertRaises(QOJError):
            self.client.submit(1802, 10, "C++17", "int main() {}", "main.cpp")
        self.client.fetch.assert_not_called()

    def test_cookie_import_is_scoped_to_qoj_and_clear_removes_authentication(self):
        self.client.import_cookies("Cookie: session=fixture; preference=en")
        cookies = list(self.client.session.cookies)
        self.assertEqual({c.name for c in cookies}, {"session", "preference"})
        self.assertTrue(all(c.domain == "qoj.ac" and c.secure and c.path == "/" for c in cookies))
        self.client.authenticated, self.client.username = True, "fixture-user"
        self.client.clear()
        self.assertFalse(list(self.client.session.cookies))
        self.assertFalse(self.client.authenticated)
        self.assertEqual(self.client.username, "")
        with self.assertRaises(QOJError):
            self.client.import_cookies("session=fixture\r\nInjected: header")


if __name__ == "__main__":
    unittest.main()
