"""PDF statement API regressions using Flask clients and offline upstreams."""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from requests import Response

from app import create_app
from demo import DemoClient
from qoj import QOJError
from native_fixture import ActiveNativeFixture


PDF = b"%PDF-1.7\nfixture document\n%%EOF\n"


class PDFRaw(BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.bytes_read = 0
        self.close_count = 0

    def read(self, size=-1):
        result = super().read(size)
        self.bytes_read += len(result)
        return result

    def close(self):
        self.close_count += 1
        super().close()


class PDFResponse(Response):
    @property
    def content(self):
        raise AssertionError("PDF response cannot be eagerly consumed")

    @property
    def text(self):
        raise AssertionError("PDF response cannot be decoded as HTML")


def pdf_response(body=PDF, kind="application/pdf", url="https://qoj.ac/download.php?type=statement&id=1", status=200):
    response = PDFResponse()
    response.status_code = status
    response.url = url
    response.headers.update({"Content-Type": kind, "Content-Length": str(len(body)),
                             "Content-Disposition": 'attachment; filename="statement.pdf"'})
    response.raw = PDFRaw(body)
    return response


class StatementClient(ActiveNativeFixture, DemoClient):
    def __init__(self):
        super().__init__()
        self.problem_calls = []
        self.fetch_calls = []
        self.revision = 1
        self.mixed_text = False
        self.problem_error = None
        self.next_response = None

    def problem(self, contest_id, problem_id):
        self.problem_calls.append((contest_id, problem_id))
        if self.problem_error:
            raise self.problem_error
        info = super().problem(contest_id, problem_id)
        paragraph = "<p>Read this introduction before the PDF.</p>" if self.mixed_text else ""
        info["statement_html"] = f'''<!doctype html><html><head><style>body{{margin:0}}</style></head><body>
            {paragraph}<iframe title="Contest statement" src="/qoj/download.php?type=statement&id={problem_id}&amp;revision={self.revision}"></iframe>
            </body></html>'''
        return info

    def fetch(self, method, url, **kwargs):
        self.fetch_calls.append((method, url, kwargs))
        if isinstance(self.next_response, Exception):
            raise self.next_response
        if self.next_response is None:
            raise AssertionError("Unexpected PDF upstream request")
        return self.next_response


class StatementRoutesTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.upstream = StatementClient()
        self.app = create_app(contest_id=1802, data_dir=Path(directory.name), client_factory=lambda: self.upstream)
        self.app.config["TESTING"] = True
        self.captain = self.app.test_client()

    def detail(self, client=None, refresh=False):
        response = (client or self.captain).get("/api/problems/1" + ("?refresh=1" if refresh else ""))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def post(self, client, path, body):
        token = client.get("/api/state").get_json()["csrf_token"]
        response = client.post(path, json=body, headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def member(self):
        created = self.post(self.captain, "/api/team/create", {"name": "PDF team", "device_name": "Captain"})
        member = self.app.test_client()
        self.post(member, "/api/team/join", {"code": created["team"]["join_code"], "device_name": "Member"})
        return member

    def test_pdf_only_metadata_and_existing_html_route_keep_separate_formats(self):
        detail = self.detail()
        self.assertEqual(detail["statement_format"], "pdf")
        self.assertEqual(len(detail["statement_pdfs"]), 1)
        document = detail["statement_pdfs"][0]
        self.assertRegex(document["id"], r"^[a-f0-9]{24}$")
        self.assertEqual(document["name"], "Contest statement")
        self.assertEqual(document["url"], f'/api/problems/1/pdf/{document["id"]}')
        self.assertEqual(self.upstream.fetch_calls, [])
        legacy = self.captain.get(detail["statement_url"])
        self.assertEqual(legacy.status_code, 200)
        self.assertIn("text/html", legacy.content_type)
        self.assertIn("default-src 'none'", legacy.headers["Content-Security-Policy"])
        self.assertIn("form-action 'none'", legacy.headers["Content-Security-Policy"])
        self.upstream.mixed_text = True
        mixed = self.detail(refresh=True)
        self.assertEqual(mixed["statement_format"], "html")
        self.assertEqual(len(mixed["statement_pdfs"]), 1)

    def test_captain_and_readonly_member_can_stream_verified_pdf_without_sandbox(self):
        member = self.member()
        for client in (self.captain, member):
            with self.subTest(member=client is member):
                document = self.detail(client)["statement_pdfs"][0]
                body = b"%PDF-" + b"x" * (1024 * 1024)
                upstream = pdf_response(body)
                self.upstream.next_response = upstream
                response = client.get(document["url"], buffered=False,
                                      headers={"Range": "bytes=1-10", "Accept-Encoding": "gzip"})
                self.assertEqual(response.status_code, 200)
                self.assertLess(upstream.raw.bytes_read, len(body))
                self.assertEqual(response.content_type, "application/pdf")
                self.assertNotIn("sandbox", response.headers["Content-Security-Policy"])
                self.assertTrue(response.headers["Content-Disposition"].startswith("inline;"))
                self.assertEqual(response.get_data(), body)
                response.close()
                self.assertEqual(upstream.raw.close_count, 1)
                method, url, kwargs = self.upstream.fetch_calls[-1]
                self.assertEqual(method, "GET")
                self.assertEqual(url, "https://qoj.ac/download.php?type=statement&id=1&revision=1")
                self.assertTrue(kwargs["stream"])
                self.assertEqual(kwargs["headers"]["Accept-Encoding"], "identity")
                self.assertEqual(kwargs["headers"]["Referer"], "https://qoj.ac/contest/1802/problem/1")
                self.assertNotIn("Range", kwargs["headers"])

    def test_head_checks_pdf_prefix_and_closes_unconsumed_upstream(self):
        document = self.detail()["statement_pdfs"][0]
        upstream = pdf_response(kind="application/octet-stream")
        self.upstream.next_response = upstream
        response = self.captain.head(document["url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"")
        self.assertEqual(response.content_type, "application/pdf")
        self.assertEqual(response.headers["Content-Length"], str(len(PDF)))
        response.close()
        self.assertEqual(upstream.raw.bytes_read, 5)
        self.assertEqual(upstream.raw.close_count, 1)

    def test_unadvertised_token_and_other_problem_ids_do_not_reach_upstream(self):
        member = self.member()
        document = self.detail()["statement_pdfs"][0]
        for client in (self.captain, member):
            for path in ("/api/problems/1/pdf/" + "0" * 24,
                         "/api/problems/2/pdf/" + document["id"],
                         "/api/problems/999/pdf/" + document["id"]):
                with self.subTest(path=path, member=client is member):
                    self.assertEqual(client.get(path).status_code, 404)
        self.assertEqual(self.upstream.fetch_calls, [])

    def test_refresh_replaces_cached_pdf_links_and_failed_refresh_can_retry(self):
        first = self.detail()["statement_pdfs"][0]
        self.upstream.revision = 2
        self.assertEqual(self.detail()["statement_pdfs"][0], first)
        self.assertEqual(len(self.upstream.problem_calls), 1)
        refreshed = self.detail(refresh=True)["statement_pdfs"][0]
        self.assertNotEqual(first["id"], refreshed["id"])
        self.assertEqual(len(self.upstream.problem_calls), 2)
        self.assertEqual(self.captain.get(first["url"]).status_code, 404)
        self.upstream.problem_error = QOJError("Fixture transient page error", 502)
        self.assertEqual(self.captain.get("/api/problems/1?refresh=1").status_code, 502)
        self.upstream.problem_error = None
        self.assertEqual(self.detail()["statement_pdfs"][0], refreshed)
        self.assertEqual(self.upstream.fetch_calls, [])

    def test_login_html_and_invalid_magic_never_escape_as_unsandboxed_document(self):
        document = self.detail()["statement_pdfs"][0]
        for upstream in (
            pdf_response(b"<form>Login</form>", kind="text/html", url="https://qoj.ac/login"),
            pdf_response(b"<html>Forbidden</html>", kind="text/html"),
            pdf_response(b"<script>unsafe()</script>", kind="application/pdf"),
            pdf_response(PDF, status=403),
        ):
            with self.subTest(kind=upstream.headers["Content-Type"], status=upstream.status_code):
                self.upstream.authenticated = True
                self.upstream.username = "demo-team"
                self.upstream.next_response = upstream
                response = self.captain.get(document["url"])
                self.assertIn(response.status_code, (401, 403, 502))
                self.assertTrue(response.is_json)
                self.assertNotIn("Content-Disposition", response.headers)
                self.assertEqual(upstream.raw.close_count, 1)


if __name__ == "__main__":
    unittest.main()
