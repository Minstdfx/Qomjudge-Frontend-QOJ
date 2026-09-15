"""PDF discovery, byte validation and response lifecycle regressions (offline)."""
import hashlib
import html
import io
import unittest
from urllib.parse import quote

from flask import Flask
from requests.structures import CaseInsensitiveDict

from qoj import QOJError
from statements import pdf_response, statement_pdfs


PDF = b"%PDF-1.7\n" + bytes(range(256)) * 600 + b"\n%%EOF\n"


class RecordingRaw:
    """A small urllib3-shaped stream that records decoding and bounded reads."""

    def __init__(self, body=PDF, short_reads=None, fail_at=None):
        self.buffer = io.BytesIO(body)
        self.short_reads = short_reads
        self.fail_at = fail_at
        self.reads = []
        self.streams = []
        self.bytes_read = 0
        self.closed = False

    def read(self, size=-1, decode_content=False):
        self.reads.append((size, decode_content))
        if self.fail_at is not None and self.bytes_read >= self.fail_at:
            raise OSError("fixture network interruption")
        if self.short_reads is not None and size > self.short_reads:
            size = self.short_reads
        chunk = self.buffer.read(size)
        self.bytes_read += len(chunk)
        return chunk

    def stream(self, size, decode_content=False):
        self.streams.append((size, decode_content))
        while chunk := self.read(size, decode_content=decode_content):
            yield chunk

    def close(self):
        self.closed = True
        self.buffer.close()


class Upstream:
    def __init__(self, body=PDF, status=200, url="https://qoj.ac/download.php?type=statement&id=10", headers=None, raw=None):
        self.status_code = status
        self.url = url
        self.headers = CaseInsensitiveDict({"Content-Type": "application/pdf", "Content-Length": str(len(body))})
        self.headers.update(headers or {})
        self.raw = raw if raw is not None else RecordingRaw(body)
        self.close_count = 0

    @property
    def content(self):
        raise AssertionError("PDF response must not buffer upstream.content")

    @property
    def text(self):
        raise AssertionError("PDF response must not decode upstream.text")

    def close(self):
        self.close_count += 1
        self.raw.close()


def discover(*sources, problem_id=10):
    return statement_pdfs({"statement_html": "".join(
        f'<iframe src="{html.escape(source, quote=True)}"></iframe>' for source in sources
    )}, problem_id)


class StatementDiscoveryTests(unittest.TestCase):
    def test_advertised_query_order_revision_and_language_are_preserved(self):
        source = "/qoj/download.php?r=7&type=statement&id=10&lang=zh-cn"
        result = discover(source)
        url = "https://qoj.ac/download.php?r=7&type=statement&id=10&lang=zh-cn"
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], url)
        self.assertEqual(result[0]["id"], hashlib.sha256(url.encode()).hexdigest()[:24])
        self.assertTrue(result[0]["name"])

    def test_only_iframe_sources_are_discovered_and_duplicates_keep_order(self):
        source = '''<a href="/qoj/download.php?type=statement&amp;id=10">PDF link</a>
          <img src="/qoj/download.php?type=statement&amp;id=10">
          <iframe data-src="/qoj/download.php?type=statement&amp;id=10"></iframe>'''
        self.assertEqual(statement_pdfs({"statement_html": source}, 10), [])
        result = discover("/qoj/static/second.pdf", "/qoj/static/first.pdf", "/qoj/static/second.pdf")
        self.assertEqual([item["url"] for item in result],
                         ["https://qoj.ac/static/second.pdf", "https://qoj.ac/static/first.pdf"])

    def test_exact_problem_id_and_single_type_and_id_are_required(self):
        for source in (
            "/qoj/download.php?type=statement&id=11",
            "/qoj/download.php?type=statement&id=010",
            "/qoj/download.php?type=statement&id=10&id=10",
            "/qoj/download.php?type=statement&id=10&id=11",
            "/qoj/download.php?type=statement&id=10&%69d=11",
            "/qoj/download.php?type=statement&type=statement&id=10",
            "/qoj/download.php?type=statement&type=source&id=10",
            "/qoj/download.php?type=attachments&id=10",
            "/qoj/download.php?type=problem&id=10",
            "/qoj/download.php?type=statement",
            "/qoj/download.php?id=10",
        ):
            with self.subTest(source=source):
                self.assertEqual(discover(source), [])

    def test_foreign_origins_credentials_ports_and_ambiguous_urls_are_rejected(self):
        for source in (
            "https://example.org/static/task.pdf", "https://qoj.ac.example.org/static/task.pdf",
            "https://qoj.ac@example.org/static/task.pdf", "https://someone@qoj.ac/static/task.pdf",
            "https://qoj.ac:8443/static/task.pdf", "https://qoj.ac:invalid/static/task.pdf",
            "https://[qoj.ac/static/task.pdf", "http://qoj.ac/static/task.pdf",
            "//qoj.ac/static/task.pdf", "//example.org/static/task.pdf",
            "data:application/pdf;base64,JVBERi0=", "javascript:alert(1)",
            "/download.php?type=statement&id=10", "relative.pdf",
            "/qoj/static/task\n.pdf", "/qoj/static/task\\other.pdf",
        ):
            with self.subTest(source=source):
                self.assertEqual(discover(source), [])

    def test_static_pdf_paths_are_restricted_and_traversal_is_rejected(self):
        for source in (
            "/qoj/static/../task.pdf", "/qoj/static/./task.pdf",
            "/qoj/static/%2e%2e/task.pdf", "/qoj/static/%252e%252e/task.pdf",
            "/qoj/static/%5ctask.pdf", "/qoj/static/%2500task.pdf",
            "/qoj/logout/task.pdf", "/qoj/problem/10/task.pdf",
            "/qoj/static/task.html", "/qoj/static/task.pdf.html", "/qoj/static/task.svg",
            "/qoj/static/task.php/file.pdf;extension=.pdf",
        ):
            with self.subTest(source=source):
                self.assertEqual(discover(source), [])
        source = "https://qoj.ac/resources/contest-10/problem-A.PDF?v=3#page=2"
        self.assertEqual(discover(source)[0]["url"], "https://qoj.ac/resources/contest-10/problem-A.PDF?v=3")

    def test_display_title_is_preserved_and_missing_html_does_not_invent_pdf(self):
        info = {"statement_html": '<iframe title="题面（中文）" src="/qoj/static/task.pdf"></iframe>'}
        self.assertEqual(statement_pdfs(info, 10)[0]["name"], "题面（中文）")
        for info in ({}, {"statement_html": None}, {"statement_html": "<article>HTML statement</article>"}):
            self.assertEqual(statement_pdfs(info, 10), [])
        self.assertEqual(discover("/qoj/download.php?type=statement&id=True", problem_id=True), [])


class PDFResponseTests(unittest.TestCase):
    def reject(self, upstream, expected_status=502):
        with self.assertRaises(QOJError) as caught:
            pdf_response(upstream)
        self.assertEqual(caught.exception.status, expected_status)
        self.assertEqual(upstream.close_count, 1)
        self.assertTrue(upstream.raw.closed)

    def test_only_signature_is_read_before_streaming_and_all_bytes_are_preserved(self):
        upstream = Upstream()
        response = pdf_response(upstream)
        self.assertEqual(upstream.raw.bytes_read, 5)
        self.assertEqual(upstream.raw.reads, [(5, False)])
        self.assertEqual(upstream.raw.streams, [])
        self.assertEqual(upstream.close_count, 0)
        self.assertTrue(response.is_streamed)
        self.assertEqual(response.get_data(), PDF)
        self.assertEqual(upstream.raw.streams, [(64 * 1024, False)])
        self.assertTrue(all(0 < size <= 64 * 1024 and not decode for size, decode in upstream.raw.reads))
        self.assertEqual(upstream.close_count, 1)
        response.close()
        self.assertEqual(upstream.close_count, 1)

    def test_short_prefix_reads_are_completed_without_reading_extra_content(self):
        upstream = Upstream(raw=RecordingRaw(short_reads=2))
        response = pdf_response(upstream)
        self.assertEqual(upstream.raw.bytes_read, 5)
        self.assertEqual(upstream.raw.reads, [(5, False), (3, False), (1, False)])
        self.assertEqual(response.get_data(), PDF)
        response.close()
        self.assertEqual(upstream.close_count, 1)

    def test_plain_file_like_stream_is_supported_without_content_decoding(self):
        upstream = Upstream(raw=io.BytesIO(PDF))
        response = pdf_response(upstream)
        self.assertEqual(upstream.raw.tell(), 5)
        self.assertEqual(response.get_data(), PDF)
        response.close()
        self.assertTrue(upstream.raw.closed)
        self.assertEqual(upstream.close_count, 1)

    def test_pdf_and_octet_stream_require_magic_and_html_is_never_rendered(self):
        for kind in ("application/pdf", "application/octet-stream", "Application/PDF; charset=binary"):
            with self.subTest(kind=kind):
                upstream = Upstream(headers={"Content-Type": kind})
                response = pdf_response(upstream)
                self.assertEqual(response.content_type, "application/pdf")
                response.close()
        for kind in ("text/html", "application/xhtml+xml", "image/svg+xml", "text/plain", ""):
            with self.subTest(kind=kind):
                upstream = Upstream(headers={"Content-Type": kind})
                self.reject(upstream)
                self.assertEqual(upstream.raw.bytes_read, 0)
        for body in (b"", b"%PDF", b"<html>login</html>", b"\xef\xbb\xbf%PDF-1.7", b"NOTPDF-1.7"):
            with self.subTest(body=body):
                upstream = Upstream(body)
                self.reject(upstream)
                self.assertLessEqual(upstream.raw.bytes_read, 5)

    def test_login_redirects_and_unauthorized_status_return_401_and_close(self):
        self.reject(Upstream(url="https://qoj.ac/login?next=%2Fproblem%2F10"), 401)
        self.reject(Upstream(url="https://qoj.ac/login/2fa"), 401)
        self.reject(Upstream(status=401), 401)

    def test_non_200_statuses_and_compression_are_rejected_before_body_read(self):
        for status in (201, 204, 206, 302, 403, 404, 416, 429, 500):
            with self.subTest(status=status):
                upstream = Upstream(status=status)
                self.reject(upstream)
                self.assertEqual(upstream.raw.bytes_read, 0)
        for encoding in ("gzip", "br", "deflate", "identity, gzip"):
            with self.subTest(encoding=encoding):
                upstream = Upstream(headers={"Content-Encoding": encoding})
                self.reject(upstream)
                self.assertEqual(upstream.raw.bytes_read, 0)

    def test_unconsumed_response_and_head_release_upstream_once(self):
        upstream = Upstream()
        response = pdf_response(upstream)
        response.close()
        response.close()
        self.assertEqual(upstream.raw.bytes_read, 5)
        self.assertEqual(upstream.close_count, 1)
        upstream = Upstream()
        app = Flask(__name__)
        app.add_url_rule("/statement.pdf", view_func=lambda: pdf_response(upstream))
        result = app.test_client().head("/statement.pdf")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_data(), b"")
        result.close()
        self.assertEqual(upstream.raw.bytes_read, 5)
        self.assertEqual(upstream.close_count, 1)

    def test_disconnect_after_first_chunk_and_network_failure_release_upstream(self):
        upstream = Upstream()
        response = pdf_response(upstream)
        iterator = iter(response.response)
        self.assertEqual(next(iterator), b"%PDF-")
        iterator.close()
        response.close()
        self.assertEqual(upstream.close_count, 1)
        self.assertEqual(upstream.raw.bytes_read, 5)
        upstream = Upstream(raw=RecordingRaw(fail_at=5))
        response = pdf_response(upstream)
        iterator = iter(response.response)
        self.assertEqual(next(iterator), b"%PDF-")
        with self.assertRaises(OSError):
            next(iterator)
        response.close()
        self.assertEqual(upstream.close_count, 1)
        self.reject(Upstream(raw=RecordingRaw(fail_at=0)))

    def test_inline_unicode_filename_headers_allow_native_pdf_without_ranges(self):
        filename = "题面 A.pdf"
        for disposition in (f'attachment; filename="{filename}"',
                            f"attachment; filename*=UTF-8''{quote(filename)}"):
            with self.subTest(disposition=disposition):
                upstream = Upstream(headers={
                    "Content-Disposition": disposition,
                    "Content-Range": "bytes 0-99/999", "Accept-Ranges": "bytes",
                    "Content-Encoding": "identity", "ETag": '"pdf-fixture"',
                })
                response = pdf_response(upstream)
                header = response.headers["Content-Disposition"]
                self.assertTrue(header.startswith("inline;"))
                self.assertIn("filename*=UTF-8''" + quote(filename), header)
                header.encode("ascii")
                self.assertNotIn("sandbox", response.headers["Content-Security-Policy"])
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(response.headers["Content-Length"], str(len(PDF)))
                self.assertEqual(response.headers["ETag"], '"pdf-fixture"')
                for name in ("Content-Range", "Accept-Ranges", "Content-Encoding"):
                    self.assertNotIn(name, response.headers)
                response.close()

    def test_invalid_filename_header_is_not_forwarded(self):
        upstream = Upstream(headers={"Content-Disposition": 'attachment; filename="task.pdf"\r\nX-Evil: injected'})
        response = pdf_response(upstream)
        self.assertNotIn("\r", response.headers["Content-Disposition"])
        self.assertNotIn("\n", response.headers["Content-Disposition"])
        self.assertNotIn("X-Evil", response.headers)
        response.close()


if __name__ == "__main__":
    unittest.main()
