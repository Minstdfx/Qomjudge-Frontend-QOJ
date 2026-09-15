"""Offline submission receipt/confirmation tests. No requests reach QOJ."""
import copy
import html
import unittest
from datetime import datetime, timezone
from email.utils import formatdate
from urllib.parse import urlsplit

import requests

from qoj import QOJClient, QOJError, parse_verdict
from submission_data import QOJ_TIMEZONE, fingerprint


CID, PID, USER, LANG = 3962, 20063, "fixture-team", "C++26"
NOW = datetime(2026, 9, 15, 6, 18, 47, tzinfo=timezone.utc).timestamp()
SOURCE = "  int main() {\r\n    return 0;\r\n}\r\n"
LIST = f"/contest/{CID}/submissions"
PROBLEM = f"/contest/{CID}/problem/{PID}"
FORM = f'''<form id="form-answer" action="{PROBLEM}" method="post">
<input type="hidden" name="_token" value="fixture-token"><select name="answer_answer_language">
<option value="{LANG}">{LANG}</option></select><button name="submit-answer" value="answer">Submit</button></form>'''


def response(body, path=LIST):
    result = requests.Response()
    result.status_code, result.url = 200, "https://qoj.ac" + path
    result.headers["Date"] = formatdate(NOW, usegmt=True)
    result.encoding, result._content = "utf-8", body.encode("utf-8")
    return result


def listing(*items, result="Judging"):
    head = '<table><thead><tr><th>ID</th><th>Problem</th><th>Submitter</th><th>Result</th><th>Time</th><th>Memory</th><th>File size</th><th>Submit time</th></tr></thead><tbody>'
    for item in items:
        item = {"sid": item} if isinstance(item, int) else item
        sid, user, pid = item["sid"], item.get("username", USER), item.get("pid", PID)
        stamp = datetime.fromtimestamp(item.get("when", NOW), QOJ_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
        head += f'''<tr><td><a href="/submission/{sid}">#{sid}</a></td><td><a href="/contest/{CID}/problem/{pid}">A</a></td>
          <td><span class="uoj-username" data-nickname="Nickname">{user}</span></td><td>{result}</td>
          <td>2ms</td><td>100kb</td><td>32b</td><td><small>{stamp}</small></td></tr>'''
    return head + '</tbody></table>'


def detail(sid, source=SOURCE, language=LANG, username=USER, source_visible=True):
    text = listing({"sid": sid, "username": username})
    if source_visible:
        text += f'''<div class="card uoj-submission-code"><div class="card-body"><pre><code>{html.escape(source.replace(chr(13) + chr(10), chr(10)))}\n</code></pre></div>
          <div class="card-footer">Source code, Language: {language}</div></div>'''
    else:
        text += '<div class="alert">Source is unavailable.</div>'
    return response(text, f"/submission/{sid}")


def receipt(**updates):
    value = {"version": 1, "contest_id": CID, "problem_id": PID, "username": USER,
             "language": LANG, "language_labels": [LANG], "source_sha256": fingerprint(SOURCE),
             "old_ids": [100], "min_id": 100, "submitted_after": NOW - 2, "submitted_before": NOW + 120}
    value.update(updates)
    return value


class FixtureClient(QOJClient):
    def __init__(self, pages=(), posted=None, details=None):
        super().__init__()
        self.username, self.authenticated = USER, True
        self.pages = [response(page) if isinstance(page, str) else page for page in pages]
        self.posted = posted if posted is not None else response(listing(101))
        self.details = details if details is not None else {101: detail(101)}
        self.events, self.gets, self.posts = [], [], []

    def _page(self, path):
        self.gets.append(path)
        self.events.append(("GET", path))
        if urlsplit(path).path == PROBLEM:
            return response(FORM, PROBLEM)
        if urlsplit(path).path == LIST:
            if not self.pages:
                raise AssertionError("Unexpected extra list GET")
            value = self.pages.pop(0)
        elif urlsplit(path).path.startswith("/submission/"):
            value = self.details[int(urlsplit(path).path.rsplit("/", 1)[-1])]
        else:
            raise AssertionError("Unexpected GET: " + path)
        if callable(value):
            value = value(self)
        if isinstance(value, Exception):
            raise value
        return value

    def fetch(self, method, path, **kwargs):
        self.assert_post(method)
        self.events.append((method, path))
        self.posts.append((method, path, kwargs))
        return self.posted

    @staticmethod
    def assert_post(method):
        if method != "POST":
            raise AssertionError("Unexpected fetch: " + method)


class ConfirmationTests(unittest.TestCase):
    def test_submit_confirms_raw_span_and_absent_language_column_with_explicit_source(self):
        client = FixtureClient(pages=[listing(100)])
        result = client.submit(CID, PID, LANG, SOURCE, "main.cpp")
        self.assertEqual(result["qoj_id"], 101)
        self.assertEqual(len(client.posts), 1)
        self.assertIn("/submission/101", client.gets)

    def test_old_rows_in_post_response_trigger_another_list_get(self):
        client = FixtureClient(pages=[listing(100), listing(101, 100)], posted=response(listing(100)))
        self.assertEqual(client.submit(CID, PID, LANG, SOURCE, "main.cpp")["qoj_id"], 101)
        self.assertEqual(client.gets.count(LIST), 2)
        self.assertEqual(len(client.posts), 1)

    def test_receipt_is_saved_before_post_and_delayed_reconcile_never_resubmits(self):
        client = FixtureClient(pages=[listing(100), listing(100)], posted=response(listing(100)))
        saved = []
        def prepared(value):
            self.assertEqual(client.posts, [])
            saved.append(copy.deepcopy(value))
            client.events.append(("PREPARED", None))
        with self.assertRaises(QOJError):
            client.submit(CID, PID, LANG, SOURCE, "main.cpp", on_prepared=prepared)
        self.assertEqual(saved[0]["source_sha256"], fingerprint(SOURCE))
        self.assertEqual(saved[0]["min_id"], 100)
        self.assertLess(client.events.index(("PREPARED", None)), next(i for i, event in enumerate(client.events) if event[0] == "POST"))
        client.pages.append(response(listing(101, 100)))
        self.assertEqual(client.reconcile_submission(CID, PID, LANG, saved[0])["qoj_id"], 101)
        self.assertEqual(len(client.posts), 1)

    def test_identical_source_for_multiple_same_account_candidates_is_ambiguous(self):
        client = FixtureClient(pages=[listing(101, 102)], details={101: detail(101), 102: detail(102)})
        self.assertIsNone(client.reconcile_submission(CID, PID, LANG, receipt())["qoj_id"])
        self.assertEqual(client.posts, [])

    def test_inaccessible_second_candidate_cannot_be_silently_excluded(self):
        client = FixtureClient(pages=[listing(101, 102)], details={101: detail(101), 102: QOJError("Forbidden", 403)})
        self.assertIsNone(client.reconcile_submission(CID, PID, LANG, receipt())["qoj_id"])

    def test_second_candidate_without_visible_source_cannot_be_silently_excluded(self):
        client = FixtureClient(pages=[listing(101, 102)], details={101: detail(101), 102: detail(102, source_visible=False)})
        self.assertIsNone(client.reconcile_submission(CID, PID, LANG, receipt())["qoj_id"])

    def test_wrong_detail_redirect_cannot_disambiguate_another_candidate(self):
        redirected = detail(102)
        redirected.url = 'https://qoj.ac/submission/999'
        client = FixtureClient(pages=[listing(101, 102)], details={101: detail(101), 102: redirected})
        self.assertIsNone(client.reconcile_submission(CID, PID, LANG, receipt())["qoj_id"])

    def test_old_ids_minimum_timestamp_and_already_bound_ids_exclude_old_runs(self):
        rows = listing(99, 100, 105, {"sid": 101, "when": NOW - 1000}, {"sid": 102, "when": NOW + 1000}, 106, 107)
        client = FixtureClient(pages=[rows], details={106: detail(106)})
        result = client.reconcile_submission(CID, PID, LANG, receipt(old_ids=[100, 105]), exclude_ids=[107])
        self.assertEqual(result["qoj_id"], 106)
        self.assertEqual([path for path in client.gets if path.startswith('/submission/')], ['/submission/106'])

    def test_source_language_and_source_body_conflicts_do_not_match(self):
        for alternative in (detail(101, language="Python3"), detail(101, source="int other() {}")):
            with self.subTest(body=alternative.text[-100:]):
                client = FixtureClient(pages=[listing(101)], details={101: alternative})
                self.assertIsNone(client.reconcile_submission(CID, PID, LANG, receipt())["qoj_id"])

    def test_account_switch_in_post_response_prevents_binding(self):
        posted = response('<nav><a href="/user/profile/different-team">Different</a></nav>' + listing(101))
        client = FixtureClient(pages=[listing(100)], posted=posted)
        with self.assertRaises(QOJError) as caught:
            client.submit(CID, PID, LANG, SOURCE, "main.cpp")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(client.posts), 1)
        self.assertNotIn('/submission/101', client.gets)

    def test_account_switch_during_detail_confirmation_is_rejected(self):
        def switched(client):
            client.username = "different-team"
            return detail(101)
        client = FixtureClient(pages=[listing(101)], details={101: switched})
        with self.assertRaises(QOJError) as caught:
            client.reconcile_submission(CID, PID, LANG, receipt())
        self.assertEqual(caught.exception.status, 409)

    def test_detail_redirect_for_verdict_must_retain_exact_submission_id(self):
        client = FixtureClient(details={101: response('<a class="uoj-score">100 ✓</a>', '/submission/999')})
        with self.assertRaises(QOJError) as caught:
            client.verdict(101)
        self.assertEqual(caught.exception.status, 502)


class ScoreTests(unittest.TestCase):
    def test_runtime_memory_follow_resource_headers_not_submit_time(self):
        source = listing(101, result='<a class="uoj-score" data-full="100">100 ✓</a>')
        source = source.replace('<td>2ms</td><td>100kb</td>', '<td>12 ms</td><td>512 KB</td>')
        result = parse_verdict(source, 101)
        self.assertEqual(result["verdict"], "AC")
        self.assertEqual(result.get("time_text"), "12 ms")
        self.assertEqual(result.get("memory_text"), "512 KB")
        self.assertNotIn('2026-09-15', result.get("time_text", ""))

    def test_resources_follow_requested_submission_in_reordered_table(self):
        source = '''<table><thead><tr><th>ID</th><th>Submit time</th><th>Memory</th><th>Result</th><th>Time</th></tr></thead><tbody>
          <tr><td><a href="/submission/999">999</a></td><td>2026-09-15 14:18:47</td><td>999 KB</td><td>Accepted</td><td>999 ms</td></tr>
          <tr><td><a href="/submission/101">101</a></td><td>2026-09-15 14:18:48</td><td>512 KB</td><td>Runtime Error</td><td>12 ms</td></tr>
          </tbody></table>'''
        result = parse_verdict(source, 101)
        self.assertEqual(result["verdict"], "RE")
        self.assertEqual(result.get("time_text"), "12 ms")
        self.assertEqual(result.get("memory_text"), "512 KB")

    def test_hidden_resources_are_not_reconstructed_from_attributes(self):
        source = listing(101, result='Accepted').replace('<td>2ms</td>', '<td data-time="12">?</td>').replace('<td>100kb</td>', '<td data-memory="512">?</td>')
        result = parse_verdict(source, 101)
        self.assertEqual(result.get("time_text"), "?")
        self.assertEqual(result.get("memory_text"), "?")

    def test_visible_checkmark_score_and_declared_maximum(self):
        for value, maximum, expected in (("100 ✓", "100.000000", "AC"),
                                        ("100 ✔", "200", "WA"), ("200 ✓", "200", "AC"),
                                        ("100 / 200", "100", "UNKNOWN"), ("100 ✓", "bad", "UNKNOWN")):
            with self.subTest(value=value, maximum=maximum):
                source = listing(101, result=f'<a class="uoj-score" data-full="{maximum}">{value}</a>')
                self.assertEqual(parse_verdict(source, 101)["verdict"], expected)

    def test_hidden_data_score_never_overrides_visible_unknown_result(self):
        for value in ("?", "Hidden", ""):
            with self.subTest(value=value):
                source = listing(101, result=f'<a class="uoj-score" data-full="100" data-score="100">{value}</a>')
                self.assertEqual(parse_verdict(source, 101)["verdict"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
