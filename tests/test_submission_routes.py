"""Offline regression for QOJ's language-specific problem submission forms.

The route shape is from the public #2564/J form; contents and tokens are
synthetic. No test contacts QOJ or submits a real solution.
"""
import html
import unittest
from unittest.mock import Mock

from requests import Response

from qoj import QOJClient, QOJError


ORIGIN = "https://qoj.ac"
PROBLEM = "/contest/2564/problem/14459"
LIST = "/contest/2564/submissions"


def response(path, body="", status=200, **headers):
    result = Response()
    result.status_code, result.url = status, ORIGIN + path
    result.encoding, result._content = "utf-8", body.encode("utf-8")
    result.headers.update(headers)
    return result


def form(action):
    attr = "" if action is None else ' action="' + html.escape(action, quote=True) + '"'
    return f'''<form id="form-answer" method="post"{attr}>
      <input type="hidden" name="_token" value="fixture-token">
      <select name="answer_answer_language"><option value="C++20">C++20</option></select>
      <button name="submit-answer" value="answer">Submit</button></form>'''


class SubmissionRouteTests(unittest.TestCase):
    def client(self):
        client = QOJClient()
        client.username, client.authenticated = "fixture-team", True
        # Never allow an accidental real request, even if a mock is incomplete.
        client.session.request = Mock(side_effect=AssertionError("Unexpected network request"))
        self.addCleanup(client.session.close)
        return client

    def test_language_page_redirect_uses_live_form_and_posts_exactly_once(self):
        client = self.client()
        localized = PROBLEM + "/statement/en"
        client.session.request.side_effect = [
            response(PROBLEM, status=302, Location=localized),
            response(localized, form(localized)),
            response(LIST, "<table></table>"),
            response(localized, status=303, Location="/submission/901"),
            response("/submission/901"),
        ]
        prepared = Mock()
        result = client.submit(2564, 14459, "C++20", "int main() {}", "J.cpp", on_prepared=prepared)
        self.assertEqual(result["qoj_id"], 901)
        calls = client.session.request.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["GET", "GET", "GET", "POST", "GET"])
        post = calls[3]
        self.assertEqual(post.args[1], ORIGIN + localized)
        self.assertEqual(post.kwargs["headers"]["Referer"], ORIGIN + localized)
        self.assertEqual(post.kwargs["data"]["_token"], "fixture-token")
        self.assertEqual(post.kwargs["data"]["answer_answer_language"], "C++20")
        self.assertEqual(post.kwargs["files"]["answer_answer_file"], ("J.cpp", b"int main() {}", "text/plain"))
        prepared.assert_called_once()
        self.assertEqual(prepared.call_args.args[0]["problem_id"], 14459)
        self.assertNotIn("data", calls[-1].kwargs)
        self.assertNotIn("files", calls[-1].kwargs)

    def test_same_problem_normal_and_language_actions(self):
        cases = [
            (PROBLEM, PROBLEM, PROBLEM),
            (PROBLEM, PROBLEM + "/", PROBLEM + "/"),
            (PROBLEM, PROBLEM + "/statement/default", PROBLEM + "/statement/default"),
            (PROBLEM + "/statement/en", "en", PROBLEM + "/statement/en"),
            (PROBLEM + "/statement/en", None, PROBLEM + "/statement/en"),
            (PROBLEM, ORIGIN + PROBLEM + "/statement/zh-cn", PROBLEM + "/statement/zh-cn"),
            (PROBLEM, "/problem/14459/statement/en", "/problem/14459/statement/en"),
            ("/problem/14459/statement/en", "", "/problem/14459/statement/en"),
        ]
        for page_path, action, target in cases:
            with self.subTest(page=page_path, action=action):
                client = self.client()
                client._page = Mock(side_effect=[response(page_path, form(action)), response(LIST)])
                client.fetch = Mock(return_value=response("/submission/901"))
                self.assertEqual(client.submit(2564, 14459, "C++20", "source", "J.cpp")["qoj_id"], 901)
                client.fetch.assert_called_once()
                self.assertEqual(client.fetch.call_args.args, ("POST", ORIGIN + target))

    def test_other_problem_contest_or_non_problem_actions_never_post(self):
        for action in (
            "/contest/2564/problem/14460/statement/en",
            "/contest/2565/problem/14459/statement/en",
            "/problem/144590/statement/en",
            PROBLEM + "/statement/en/edit", PROBLEM + "/statement/..%2fmanage",
            PROBLEM + "/statement/%65n", PROBLEM + "/statement/", PROBLEM + "/manage",
            "https://other.example" + PROBLEM, "//other.example" + PROBLEM,
            "/submission/901", "/logout",
        ):
            with self.subTest(action=action):
                client = self.client()
                client._page = Mock(return_value=response(PROBLEM, form(action)))
                client.fetch = Mock()
                prepared = Mock()
                with self.assertRaises(QOJError):
                    client.submit(2564, 14459, "C++20", "source", "J.cpp", on_prepared=prepared)
                client.fetch.assert_not_called()
                prepared.assert_not_called()
                self.assertEqual(client._page.call_count, 1)

    def test_redirected_other_problem_page_cannot_supply_a_valid_looking_action(self):
        for page_path in ("/contest/2565/problem/14459", "/contest/2564/problem/14460", "/problem/14460/statement/en"):
            with self.subTest(page=page_path):
                client = self.client()
                client._page = Mock(return_value=response(page_path, form(PROBLEM)))
                client.fetch = Mock()
                prepared = Mock()
                with self.assertRaises(QOJError):
                    client.submit(2564, 14459, "C++20", "source", "J.cpp", on_prepared=prepared)
                client.fetch.assert_not_called()
                prepared.assert_not_called()
                self.assertEqual(client._page.call_count, 1)


if __name__ == "__main__":
    unittest.main()
