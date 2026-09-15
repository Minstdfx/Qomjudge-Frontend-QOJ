"""Attachment metadata and streaming downloads, using only offline upstreams."""
from copy import deepcopy
import gzip
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote
import unittest
from unittest.mock import Mock
from zipfile import ZipFile

from requests import Response

from app import create_app
from demo import DemoClient
from qoj import QOJClient, QOJError
from native_fixture import ActiveNativeFixture


ATTACHMENT_ID = "01abcdef2345678901abcdef"
EXTERNAL_ID = "02abcdef2345678902abcdef"
ATTACHMENT = {"id": ATTACHMENT_ID, "name": "比赛附件.zip", "type": "attachment", "size": None,
              "external": False, "url": "https://qoj.ac/contest/1802/download/samples.zip"}


class TrackingRaw(BytesIO):
    """Record real consumed bytes and whether requests asked to decompress them."""
    def __init__(self, body):
        super().__init__(body)
        self.bytes_read = 0
        self.read_sizes = []
        self.decode_requests = []
        self.release_count = 0

    def read(self, amount=-1, decode_content=None):
        self.read_sizes.append(amount)
        if decode_content is not None:
            self.decode_requests.append(decode_content)
        result = super().read(amount)
        self.bytes_read += len(result)
        return result

    def stream(self, amount=65536, decode_content=False):
        self.decode_requests.append(decode_content)
        while True:
            chunk = self.read(amount)
            if not chunk:
                break
            yield chunk

    def release_conn(self):
        self.release_count += 1


class StreamingOnlyResponse(Response):
    @property
    def content(self):
        raise AssertionError("Attachment response must not eagerly materialize .content")

    @property
    def text(self):
        raise AssertionError("Attachment response must not decode/rewrite .text")


def upstream(body=b"fixture", status=200, url=None, headers=None):
    result = StreamingOnlyResponse()
    result.status_code = status
    result.url = url or ATTACHMENT["url"]
    result.headers.update({"Content-Type": "application/octet-stream", "Content-Length": str(len(body))})
    result.headers.update(headers or {})
    result.raw = TrackingRaw(body)
    return result


class AttachmentClient(ActiveNativeFixture, DemoClient):
    def __init__(self, attachments=None):
        super().__init__()
        self.attachments = deepcopy([ATTACHMENT] if attachments is None else attachments)
        self.contest_calls = 0
        self.fetch_calls = []
        self.next_response = None
        self.contest_error = None

    def contest(self, contest_id):
        self.contest_calls += 1
        if self.contest_error:
            raise self.contest_error
        result = super().contest(contest_id)
        result["attachments"] = deepcopy(self.attachments)
        return result

    def fetch(self, method, url, **kwargs):
        self.fetch_calls.append((method, url, kwargs))
        if isinstance(self.next_response, Exception):
            raise self.next_response
        if self.next_response is None:
            raise AssertionError("Unexpected upstream attachment fetch")
        return self.next_response


class AttachmentDownloadTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        self.upstreams = []

        def factory():
            client = AttachmentClient()
            self.upstreams.append(client)
            return client

        self.app = create_app(contest_id=1802, data_dir=self.data_dir, client_factory=factory)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def listing(self, client=None, refresh=False):
        result = (client or self.client).get("/api/attachments" + ("?refresh=1" if refresh else ""))
        self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
        return result.get_json()

    def ready(self, response, attachment=None):
        self.listing()
        client = self.upstreams[0]
        if attachment is not None:
            client.attachments = [attachment]
            self.listing(refresh=True)
        client.next_response = response
        return client

    @staticmethod
    def download_path(identifier=ATTACHMENT_ID):
        return f"/api/attachments/{identifier}/download"

    def test_listing_uses_fresh_native_authorization_without_starting_or_downloading(self):
        first = self.listing()
        self.assertEqual(first["source_url"], "/qoj/contest/1802")
        self.assertFalse(first["demo"])
        self.assertEqual(first["attachments"][0]["name"], ATTACHMENT["name"])
        self.assertEqual(first["attachments"][0]["download_url"], self.download_path())
        fixture = self.upstreams[0]
        fixture.attachments[0]["name"] = "Updated name.zip"
        self.assertEqual(self.listing()["attachments"][0]["name"], "Updated name.zip")
        self.assertGreaterEqual(len(fixture.participation_calls), 2)
        before_refresh = len(fixture.participation_calls)
        self.assertEqual(self.listing(refresh=True)["attachments"][0]["name"], "Updated name.zip")
        self.assertGreater(len(fixture.participation_calls), before_refresh)
        self.assertEqual(fixture.fetch_calls, [])
        self.assertEqual(self.client.get("/api/state").get_json()["participation"]["status"], "running")

    def test_attachment_cache_and_ids_are_scoped_to_current_browser(self):
        self.listing()
        other = self.app.test_client()
        self.listing(other)
        self.assertEqual(len(self.upstreams), 2)
        other_id = "03abcdef23456789"
        self.upstreams[1].attachments = [{**ATTACHMENT, "id": other_id, "name": "Other account.zip"}]
        self.assertEqual(self.listing(other, refresh=True)["attachments"][0]["id"], other_id)
        self.assertEqual(self.listing()["attachments"][0]["id"], ATTACHMENT_ID)
        self.assertEqual(other.get(self.download_path()).status_code, 404)
        self.assertEqual(self.client.get(self.download_path(other_id)).status_code, 404)
        self.assertTrue(all(not item.fetch_calls for item in self.upstreams))

    def test_unknown_external_and_arbitrary_url_requests_never_fetch_files(self):
        self.listing()
        fixture = self.upstreams[0]
        fixture.attachments.append({**ATTACHMENT, "id": EXTERNAL_ID, "external": True,
                                    "url": "https://files.example/sample.zip"})
        listing = self.listing(refresh=True)
        self.assertIsNone(next(a for a in listing["attachments"] if a["external"])["download_url"])
        self.assertEqual(self.client.get(self.download_path("unknown")).status_code, 404)
        self.assertEqual(self.client.get(self.download_path(EXTERNAL_ID)).status_code, 400)
        self.assertEqual(self.client.get("/api/attachments/download?url=https://files.example/private.zip").status_code, 404)
        self.assertEqual(fixture.fetch_calls, [])

    def test_refresh_failure_does_not_allow_stale_private_attachment_download(self):
        self.listing()
        fixture = self.upstreams[0]
        fixture.contest_error = QOJError("Fixture session expired", 401)
        refreshed = self.client.get("/api/attachments?refresh=1")
        self.assertIn(refreshed.status_code, (401, 409))
        downloaded = self.client.get(self.download_path())
        self.assertIn(downloaded.status_code, (401, 409))
        self.assertNotIn("Content-Disposition", downloaded.headers)
        self.assertEqual(fixture.fetch_calls, [])

    def test_download_streams_bounded_chunks_and_closes_after_complete_consumption(self):
        body = bytes(range(256)) * 8192
        response = upstream(body)
        fixture = self.ready(response)
        result = self.client.get(self.download_path(), buffered=False)
        self.assertEqual(result.status_code, 200)
        self.assertLess(response.raw.bytes_read, len(body))
        self.assertFalse(response.raw.closed)
        self.assertEqual(result.get_data(), body)
        result.close()
        self.assertTrue(response.raw.closed)
        self.assertNotIn(True, response.raw.decode_requests)
        method, url, kwargs = fixture.fetch_calls[0]
        self.assertEqual((method, url), ("GET", ATTACHMENT["url"]))
        self.assertIs(kwargs["stream"], True)
        self.assertEqual(kwargs["headers"]["Accept-Encoding"], "identity")
        self.assertEqual(kwargs["headers"]["Referer"], "https://qoj.ac/contest/1802")
        self.assertEqual(result.headers["Content-Length"], str(len(body)))
        self.assertEqual(result.headers["Content-Type"], "application/octet-stream")
        disposition = result.headers["Content-Disposition"]
        self.assertTrue(disposition.startswith("attachment;"))
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertIn(ATTACHMENT["name"], unquote(disposition))

    def test_disconnected_downloader_closes_upstream_without_reading_rest(self):
        body = b"x" * (2 * 1024 * 1024)
        response = upstream(body)
        self.ready(response)
        result = self.client.get(self.download_path(), buffered=False)
        self.assertLess(response.raw.bytes_read, len(body))
        result.close()
        self.assertTrue(response.raw.closed)
        self.assertLess(response.raw.bytes_read, len(body))

    def test_interrupted_upstream_stream_closes_response_and_is_not_retried(self):
        class InterruptedRaw(TrackingRaw):
            def stream(self, amount=65536, decode_content=False):
                yield b"first chunk"
                raise OSError("Fixture upstream connection closed")

        response = upstream()
        response.raw = InterruptedRaw(b"")
        fixture = self.ready(response)
        result = self.client.get(self.download_path(), buffered=False)
        with self.assertRaises(OSError):
            result.get_data()
        result.close()
        self.assertTrue(response.raw.closed)
        self.assertEqual(len(fixture.fetch_calls), 1)

    def test_head_closes_unconsumed_upstream_and_preserves_download_headers(self):
        response = upstream(b"fixture bytes")
        self.ready(response)
        result = self.client.head(self.download_path())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data, b"")
        self.assertEqual(result.headers["Content-Length"], "13")
        result.close()
        self.assertTrue(response.raw.closed)
        self.assertEqual(response.raw.bytes_read, 0)

    def test_marked_html_and_javascript_downloads_preserve_every_byte(self):
        for kind, filename, body in (
            ("text/html", "参考.html", b'<a href="https://qoj.ac/contest/1802">Original</a>\r\n'),
            ("application/javascript", "checker.js", b"$.post('/login', data); const path='/';\r\n"),
        ):
            with self.subTest(kind=kind):
                response = upstream(body, headers={"Content-Type": kind,
                                                    "Content-Disposition": 'attachment; filename="original.txt"'})
                self.ready(response, {**ATTACHMENT, "name": filename})
                result = self.client.get(self.download_path())
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.data, body)
                self.assertEqual(result.headers["Content-Type"], kind)
                self.assertTrue(result.headers["Content-Disposition"].startswith("attachment;"))
                result.close()
                self.assertTrue(response.raw.closed)

    def test_compressed_upstream_bytes_and_encoding_are_kept_consistent(self):
        body = gzip.compress(b"original archive bytes\n" * 100, mtime=0)
        response = upstream(body, headers={"Content-Encoding": "gzip"})
        self.ready(response)
        result = self.client.get(self.download_path())
        self.assertEqual(result.data, body)
        self.assertEqual(result.headers["Content-Encoding"], "gzip")
        self.assertEqual(result.headers["Content-Length"], str(len(body)))
        self.assertNotIn(True, response.raw.decode_requests)
        result.close()

    def test_upstream_unicode_filename_is_preserved_while_inline_becomes_attachment(self):
        disposition = "inline; filename=sample.zip; filename*=UTF-8''%E6%A0%B7%E4%BE%8B.zip"
        response = upstream(headers={"Content-Disposition": disposition})
        self.ready(response)
        result = self.client.get(self.download_path())
        self.assertEqual(result.headers["Content-Disposition"], disposition.replace("inline;", "attachment;", 1))
        self.assertEqual(result.data, b"fixture")
        result.close()

    def test_partial_download_forwards_range_and_preserves_partial_response_headers(self):
        response = upstream(b"345", status=206, headers={"Content-Range": "bytes 3-5/10", "Accept-Ranges": "bytes",
                                                       "ETag": '"fixture-etag"', "Last-Modified": "Mon, 14 Sep 2026 12:00:00 GMT"})
        fixture = self.ready(response)
        result = self.client.get(self.download_path(), headers={"Range": "bytes=3-5", "If-Range": '"fixture-etag"'})
        self.assertEqual(result.status_code, 206)
        self.assertEqual(result.data, b"345")
        self.assertEqual(result.headers["Content-Length"], "3")
        self.assertEqual(result.headers["Content-Range"], "bytes 3-5/10")
        self.assertEqual(result.headers["Accept-Ranges"], "bytes")
        self.assertEqual(fixture.fetch_calls[0][2]["headers"]["Range"], "bytes=3-5")
        self.assertEqual(fixture.fetch_calls[0][2]["headers"]["If-Range"], '"fixture-etag"')
        result.close()

    def test_unsatisfiable_range_preserves_416_and_file_length(self):
        response = upstream(b"", status=416, headers={"Content-Range": "bytes */10", "Accept-Ranges": "bytes"})
        self.ready(response)
        result = self.client.get(self.download_path(), headers={"Range": "bytes=100-200"})
        self.assertEqual(result.status_code, 416)
        self.assertEqual(result.headers["Content-Range"], "bytes */10")
        result.close()
        self.assertTrue(response.raw.closed)

    def test_login_redirect_and_unmarked_html_error_are_not_delivered_as_files(self):
        for response in (
            upstream(b'<form id="form-login"></form>', url="https://qoj.ac/login?return=contest", headers={"Content-Type": "text/html"}),
            upstream(b"<html><h1>Permission denied</h1></html>", headers={"Content-Type": "text/html"}),
        ):
            with self.subTest(url=response.url):
                if self.upstreams:
                    self.upstreams[0].authenticated = True
                    self.upstreams[0].username = "demo-team"
                self.ready(response)
                result = self.client.get(self.download_path())
                self.assertIn(result.status_code, (401, 403, 409, 502))
                self.assertTrue(result.is_json)
                self.assertNotIn("Content-Disposition", result.headers)
                result.close()
                self.assertTrue(response.raw.closed)

    def test_upstream_auth_failure_is_json_error_without_file_headers(self):
        fixture = self.ready(QOJError("Fixture forbidden", 403))
        result = self.client.get(self.download_path())
        self.assertEqual(result.status_code, 403)
        self.assertTrue(result.is_json)
        self.assertNotIn("Content-Disposition", result.headers)
        self.assertEqual(len(fixture.fetch_calls), 1)


class DemoAttachmentTests(unittest.TestCase):
    def test_demo_zip_and_text_download_without_starting_vp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        app = create_app(contest_id=1802, demo=True, data_dir=Path(directory.name))
        app.config["TESTING"] = True
        client = app.test_client()
        response = client.get("/api/attachments")
        self.assertEqual(response.status_code, 200)
        listing = response.get_json()
        self.assertTrue(listing["demo"])
        attachments = listing["attachments"]
        archives = [item for item in attachments if item["name"].lower().endswith(".zip")]
        documents = [item for item in attachments if item["name"].lower().endswith(".txt")]
        self.assertTrue(archives)
        self.assertTrue(documents)
        archive = client.get(archives[0]["download_url"])
        self.assertEqual(archive.status_code, 200)
        self.assertTrue(archive.headers["Content-Disposition"].startswith("attachment;"))
        with ZipFile(BytesIO(archive.data)) as zip_file:
            self.assertTrue(zip_file.namelist())
            self.assertIsNone(zip_file.testzip())
        text = client.get(documents[0]["download_url"])
        self.assertEqual(text.status_code, 200)
        self.assertTrue(text.data.decode("utf-8").strip())
        self.assertTrue(text.headers["Content-Disposition"].startswith("attachment;"))
        self.assertIsNone(client.get("/api/state").get_json()["run"])
        archive.close()
        text.close()


class AttachmentTransportTests(unittest.TestCase):
    def setUp(self):
        self.client = QOJClient()
        self.addCleanup(self.client.session.close)
        self.client.session.request = Mock()

    def test_successful_stream_is_returned_without_consuming_body(self):
        response = upstream(b"binary archive bytes")
        self.client.session.request.return_value = response
        result = self.client.fetch("GET", ATTACHMENT["url"], stream=True)
        self.assertIs(result, response)
        self.assertEqual(response.raw.bytes_read, 0)
        self.assertFalse(response.raw.closed)
        result.close()

    def test_unsatisfiable_range_stream_is_returned_for_route_to_handle(self):
        response = upstream(status=416, headers={"Content-Range": "bytes */20"})
        self.client.session.request.return_value = response
        result = self.client.fetch("GET", ATTACHMENT["url"], stream=True)
        self.assertEqual(result.status_code, 416)
        self.assertEqual(result.headers["Content-Range"], "bytes */20")
        self.assertFalse(response.raw.closed)
        result.close()

    def test_redirect_streams_close_before_following_or_rejecting_destination(self):
        redirect = upstream(status=302, headers={"Location": "/download.php?type=attachment&id=10"})
        final = upstream(b"archive")
        self.client.session.request.side_effect = [redirect, final]
        result = self.client.fetch("GET", ATTACHMENT["url"], stream=True)
        self.assertTrue(redirect.raw.closed)
        self.assertEqual(redirect.raw.bytes_read, 0)
        self.assertFalse(final.raw.closed)
        result.close()
        external = upstream(status=302, headers={"Location": "https://external.example/file.zip"})
        self.client.session.request.reset_mock(side_effect=True)
        self.client.session.request.return_value = external
        with self.assertRaises(QOJError):
            self.client.fetch("GET", ATTACHMENT["url"], stream=True)
        self.assertTrue(external.raw.closed)
        self.assertEqual(self.client.session.request.call_count, 1)

    def test_redirect_limit_and_http_error_close_all_streams(self):
        redirects = [upstream(status=302, headers={"Location": ATTACHMENT["url"]}) for _ in range(8)]
        self.client.session.request.side_effect = redirects
        with self.assertRaises(QOJError):
            self.client.fetch("GET", ATTACHMENT["url"], stream=True)
        self.assertTrue(all(result.raw.closed for result in redirects))
        missing = upstream(status=404)
        self.client.session.request.reset_mock(side_effect=True)
        self.client.session.request.return_value = missing
        with self.assertRaises(QOJError) as raised:
            self.client.fetch("GET", ATTACHMENT["url"], stream=True)
        self.assertEqual(raised.exception.status, 404)
        self.assertTrue(missing.raw.closed)


if __name__ == "__main__":
    unittest.main()
