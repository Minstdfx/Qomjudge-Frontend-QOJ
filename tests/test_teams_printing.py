"""Offline regressions for captain/member teams and run-scoped virtual printing."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import Mock

from bs4 import BeautifulSoup
from requests import Response

from app import create_app
from demo import DemoClient
from qoj import QOJClient, QOJError
from native_fixture import ActiveNativeFixture


MAX_FILE_SIZE = 2 * 1024 * 1024
PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


class TeamsPrintingTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        self.app = create_app(contest_id=1802, demo=True, data_dir=self.data_dir)
        self.app.config["TESTING"] = True
        self.captain = self.app.test_client()

    def state(self, client=None):
        response = (client or self.captain).get("/api/state")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def post(self, path, body=None, client=None):
        client = client or self.captain
        return client.post(path, json={} if body is None else body,
                           headers={"X-CSRF-Token": self.state(client)["csrf_token"]})

    def expect_ok(self, response):
        self.assertIn(response.status_code, (200, 201), response.get_data(as_text=True))
        return response.get_json()

    def create_team(self, name="测试队", device_name="Captain laptop"):
        return self.expect_ok(self.post("/api/team/create", {"name": name, "device_name": device_name}))

    def join(self, code, client=None, device_name="Reading laptop"):
        client = client or self.app.test_client()
        self.expect_ok(self.post("/api/team/join", {"code": code, "device_name": device_name}, client))
        return client

    def paired(self):
        team = self.create_team()["team"]
        member = self.join(team["join_code"])
        return member

    def start(self):
        return self.expect_ok(self.post("/api/start", {"team_name": "测试队"}))

    def files(self, client=None):
        response = (client or self.captain).get("/api/prints")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def upload(self, name="代码.cpp", content=b"int main() {}\n", client=None, csrf=True):
        client = client or self.captain
        headers = {"X-CSRF-Token": self.state(client)["csrf_token"]} if csrf else {}
        response = client.post("/api/prints", data={"file": (BytesIO(content), name)},
                               content_type="multipart/form-data", headers=headers)
        # Werkzeug's test builder may spool the outgoing multipart body to a
        # temporary file; that stream belongs to the test, not Flask's upload.
        response.request.input_stream.close()
        return response

    def new_file(self, name="代码.cpp", content=b"int main() {}\n"):
        self.expect_ok(self.upload(name, content))
        return next(item for item in self.files()["files"] if item["name"] == name)

    def test_team_creation_retains_captain_run_and_submission_history(self):
        initial = self.start()
        self.expect_ok(self.post("/api/submit", {"problem_id": 1, "language": "C++17",
                                               "filename": "main.cpp", "source": "int main() {}"}))
        paired = self.create_team()
        self.assertEqual(paired["run"]["id"], initial["run"]["id"])
        self.assertEqual(paired["run"]["started_at"], initial["run"]["started_at"])
        self.assertEqual(len(paired["submissions"]), 1)
        self.assertEqual(paired["team"]["role"], "captain")
        self.assertEqual(paired["team"]["max_members"], 2)
        self.assertEqual(paired["permissions"], {"manage": True, "submit": True, "print": True, "view_content": True})
        member = self.join(paired["team"]["join_code"])
        shared = self.state(member)
        self.assertEqual(shared["run"]["id"], initial["run"]["id"])
        self.assertEqual(shared["submissions"], [])
        self.assertEqual(shared["run"]["problem_stats"], self.state()["run"]["problem_stats"])
        self.assertEqual(shared["permissions"], {"manage": False, "submit": False, "print": False, "view_content": True})
        self.assertEqual(shared["team"]["member_count"], 1)
        self.assertNotIn("join_code", shared["team"])
        self.assertEqual(len(shared["scoreboard"]), 1)
        self.assertTrue(shared["scoreboard"][0]["is_self"])

    def assert_shared_team_name(self, member, expected):
        for client in (self.captain, member):
            state = self.state(client)
            self.assertEqual(state["team"]["name"], expected)
            self.assertEqual(state["run"]["team_name"], expected)
            self.assertEqual(len(state["scoreboard"]), 1)
            self.assertEqual(state["scoreboard"][0]["team_name"], expected)
            self.assertTrue(state["scoreboard"][0]["is_self"])

    def test_grouped_start_and_reset_use_saved_team_name_without_extra_input(self):
        name = "已创建的队伍"
        team = self.create_team(name=name)["team"]
        member = self.join(team["join_code"])
        started = self.expect_ok(self.post("/api/start"))
        self.assert_shared_team_name(member, name)
        reset = self.expect_ok(self.post("/api/reset"))
        self.assertNotEqual(reset["run"]["id"], started["run"]["id"])
        self.assert_shared_team_name(member, name)

    def test_grouped_start_and_reset_ignore_conflicting_or_invalid_payload_names(self):
        name = "Persisted Team"
        team = self.create_team(name=name)["team"]
        member = self.join(team["join_code"])
        for supplied_name in ("Different Team", "", None, ["invalid name type"]):
            with self.subTest(team_name=supplied_name):
                started = self.expect_ok(self.post("/api/start", {"team_name": supplied_name}))
                self.assert_shared_team_name(member, name)
                reset = self.expect_ok(self.post("/api/reset", {"team_name": supplied_name}))
                self.assertNotEqual(reset["run"]["id"], started["run"]["id"])
                self.assert_shared_team_name(member, name)
                self.expect_ok(self.post("/api/finish"))

    def test_two_member_limit_is_atomic_under_simultaneous_join_requests(self):
        code = self.create_team()["team"]["join_code"]
        candidates = [self.app.test_client() for _ in range(3)]
        tokens = [self.state(client)["csrf_token"] for client in candidates]
        barrier = Barrier(3)

        def attempt(index):
            barrier.wait(timeout=10)
            response = candidates[index].post("/api/team/join", json={"code": code, "device_name": f"Member {index}"},
                                               headers={"X-CSRF-Token": tokens[index]})
            return response.status_code

        with ThreadPoolExecutor(max_workers=3) as executor:
            statuses = list(executor.map(attempt, range(3)))
        self.assertEqual(sum(status in (200, 201) for status in statuses), 2, statuses)
        self.assertEqual(sum(status == 409 for status in statuses), 1, statuses)
        self.assertEqual(self.state()["team"]["member_count"], 2)

    def test_member_reads_shared_problem_and_prints_but_cannot_mutate_or_proxy(self):
        member = self.paired()
        self.start()
        printed = self.new_file()
        for path in ("/api/state", "/api/problems/1", "/api/problems/1/statement", "/api/prints",
                     printed["view_url"], printed["download_url"]):
            with self.subTest(allowed=path):
                response = member.get(path)
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                response.close()
        mutations = {
            "/api/team/create": {"name": "Escape", "device_name": "x"},
            "/api/team/join": {"code": "another", "device_name": "x"},
            "/api/team/rotate": {}, "/api/team/revoke": {"device_id": self.state()["team"]["device_id"]},
            "/api/start": {"team_name": "Escape"}, "/api/reset": {"team_name": "Escape"},
            "/api/finish": {}, "/api/connect": {"cookie": "fixture=value"},
            "/api/logout": {}, "/api/refresh": {},
            "/api/submit": {"problem_id": 1, "language": "C++17", "source": "int main() {}", "filename": "main.cpp"},
            "/api/prints": {"name": "escape.txt", "content": "Escape"},
        }
        for path, body in mutations.items():
            with self.subTest(denied=path):
                self.assertEqual(self.post(path, body, member).status_code, 403)
        self.assertEqual(self.upload(client=member).status_code, 403)
        for path in ("/api/attachments", "/api/attachments/demo-samples/download", "/qoj/login", "/qoj/logout",
                     "/qoj/contest/1802", "/qoj/contest/1802/problem/1", "/qoj/problem/999", "/qoj/submission/1"):
            with self.subTest(proxy=path):
                self.assertEqual(member.get(path).status_code, 403)
        self.assertEqual(member.post("/qoj/login", data={"username": "fixture"},
                                      headers={"Origin": "http://localhost"}).status_code, 403)
        self.assertEqual(self.state()["run"]["status"], "running")
        self.assertEqual(len(self.files()["files"]), 1)

    def test_rotating_join_code_invalidates_old_code(self):
        original = self.create_team()["team"]["join_code"]
        rotated = self.expect_ok(self.post("/api/team/rotate"))["team"]["join_code"]
        self.assertNotEqual(original, rotated)
        candidate = self.app.test_client()
        self.assertIn(self.post("/api/team/join", {"code": original, "device_name": "Old code"}, candidate).status_code,
                      (400, 404, 409))
        self.join(rotated, candidate)
        self.assertEqual(self.state(candidate)["team"]["role"], "member")

    def test_malformed_revoke_device_ids_are_validation_errors(self):
        member = self.paired()
        for invalid in (None, [], {}, "too-short"):
            with self.subTest(device_id=invalid):
                self.assertEqual(self.post("/api/team/revoke", {"device_id": invalid}).status_code, 400)
        self.assertEqual(self.state(member)["team"]["role"], "member")

    def test_revocation_blocks_shared_data_and_releases_member_slot(self):
        member = self.paired()
        self.start()
        printed = self.new_file()
        device_id = self.state(member)["team"]["device_id"]
        self.expect_ok(self.post("/api/team/revoke", {"device_id": device_id}))
        revoked = self.state(member)
        self.assertEqual(revoked["team"]["role"], "revoked")
        self.assertEqual(revoked["permissions"], {"manage": False, "submit": False, "print": False, "view_content": False})
        self.assertIsNone(revoked["run"])
        self.assertEqual(revoked["submissions"], [])
        self.assertEqual(revoked["scoreboard"], [])
        self.assertFalse((revoked.get("contest") or {}).get("problems"))
        for path in ("/api/problems/1", "/api/prints", printed["view_url"], printed["download_url"]):
            with self.subTest(path=path):
                self.assertEqual(member.get(path).status_code, 403)
        self.assertEqual(self.state()["team"]["member_count"], 0)
        replacement = self.join(self.state()["team"]["join_code"])
        self.assertEqual(self.state(replacement)["team"]["role"], "member")

    def test_member_leave_rotates_browser_identity_and_detaches_shared_run(self):
        member = self.paired()
        self.start()
        printed = self.new_file()
        with member.session_transaction() as before:
            old_sid = before["sid"]
        result = self.expect_ok(self.post("/api/team/leave", client=member))
        with member.session_transaction() as after:
            self.assertNotEqual(after["sid"], old_sid)
        self.assertIsNone(result["team"])
        self.assertIsNone(result["run"])
        self.assertEqual(result["permissions"], {"manage": True, "submit": True, "print": True, "view_content": True})
        self.assertEqual(member.get(printed["download_url"]).status_code, 404)
        self.assertEqual(self.state()["team"]["member_count"], 0)

    def test_upload_requires_active_run_and_multipart_csrf(self):
        self.assertEqual(self.upload().status_code, 409)
        self.start()
        self.assertEqual(self.upload(csrf=False).status_code, 403)
        token = self.state()["csrf_token"]
        cross_origin = self.captain.post("/api/prints", data={"file": (BytesIO(b"safe"), "safe.txt")},
                                          headers={"X-CSRF-Token": token, "Origin": "https://attacker.example"})
        self.assertEqual(cross_origin.status_code, 403)
        self.assertEqual(self.files()["files"], [])
        self.expect_ok(self.post("/api/finish"))
        self.assertEqual(self.upload().status_code, 409)

    def test_utf8_source_download_is_exact_and_preview_escapes_active_markup(self):
        self.start()
        source = '// 中文测试\r\n<script>window.pwned=true</script>\n<img src=x onerror="attack()">\n'.encode("utf-8")
        printed = self.new_file("题解.cpp", source)
        self.assertEqual(printed["size"], len(source))
        self.assertIn("created_at", printed)
        self.assertEqual(self.files()["max_file_size"], MAX_FILE_SIZE)
        downloaded = self.captain.get(printed["download_url"])
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.data, source)
        self.assertTrue(downloaded.headers["Content-Disposition"].startswith("attachment;"))
        downloaded.close()
        preview = self.captain.get(printed["view_url"])
        self.assertEqual(preview.status_code, 200)
        self.assertIn("text/html", preview.content_type)
        soup = BeautifulSoup(preview.get_data(as_text=True), "html.parser")
        self.assertIsNotNone(soup.pre)
        self.assertIn("<script>window.pwned=true</script>", soup.pre.get_text())
        self.assertIsNone(soup.pre.find("script"))
        self.assertIsNone(soup.pre.find("img"))
        preview.close()

    def test_pasted_code_and_pdf_have_usable_preview_and_download_urls(self):
        self.start()
        self.expect_ok(self.post("/api/prints", {"name": "solution.py", "content": 'print("你好")\n'}))
        pasted = next(item for item in self.files()["files"] if item["name"] == "solution.py")
        response = self.captain.get(pasted["download_url"])
        self.assertEqual(response.data, 'print("你好")\n'.encode("utf-8"))
        response.close()
        pdf = self.new_file("notes.pdf", PDF_BYTES)
        preview = self.captain.get(pdf["view_url"])
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.data, PDF_BYTES)
        self.assertIn("application/pdf", preview.content_type)
        self.assertTrue(preview.headers["Content-Disposition"].startswith("inline;"))
        preview.close()

    def test_team_member_reads_highlighted_code_with_original_download(self):
        member = self.paired()
        self.start()
        source = b'\n\tint main() { return 0; }\n\n'
        printed = self.new_file('main.cpp', source)
        preview = member.get(printed['view_url'])
        self.assertEqual(preview.status_code, 200)
        self.assertIn("sandbox; default-src 'none'", preview.headers['Content-Security-Policy'])
        soup = BeautifulSoup(preview.get_data(as_text=True), 'html.parser')
        self.assertTrue(soup.select('.source .k'))
        self.assertTrue(soup.select('.linenos'))
        for line in soup.select('.linenos'):
            line.decompose()
        self.assertEqual(soup.code.get_text(), source.decode('utf-8'))
        preview.close()
        download = member.get(printed['download_url'])
        self.assertEqual(download.data, source)
        download.close()

    def test_invalid_extensions_encodings_pdf_magic_and_oversize_are_rejected(self):
        self.start()
        for name, content in (("payload.exe", b"MZ executable"), ("page.html", b"<script>attack()</script>"),
                              ("broken.txt", b"\xff\xfe\x00"), ("fake.pdf", b"this is not PDF")):
            with self.subTest(name=name):
                self.assertEqual(self.upload(name, content).status_code, 400)
        self.assertEqual(self.upload("large.txt", b"x" * (MAX_FILE_SIZE + 1)).status_code, 413)
        self.assertEqual(self.files()["files"], [])

    def test_files_are_shared_with_member_but_hidden_from_other_team_and_old_run(self):
        member = self.paired()
        old_run = self.start()["run"]["id"]
        printed = self.new_file()
        self.assertEqual(self.files(member)["run_id"], old_run)
        self.assertEqual([item["id"] for item in self.files(member)["files"]], [printed["id"]])
        outsider = self.app.test_client()
        for path in (printed["view_url"], printed["download_url"]):
            self.assertEqual(outsider.get(path).status_code, 404)
        self.expect_ok(self.post("/api/reset", {"team_name": "Retry"}))
        self.assertNotEqual(self.files()["run_id"], old_run)
        self.assertEqual(self.files()["files"], [])
        self.assertEqual(self.files(member)["files"], [])
        for client in (self.captain, member):
            for path in (printed["view_url"], printed["download_url"]):
                self.assertEqual(client.get(path).status_code, 404)

    def test_print_file_count_quota_rejects_101st_file(self):
        self.start()
        for index in range(100):
            self.expect_ok(self.upload(f"file-{index}.txt", b"x"))
        response = self.upload("file-101.txt", b"x")
        self.assertIn(response.status_code, (409, 413))
        self.assertEqual(len(self.files()["files"]), 100)

    def test_exact_two_mebibyte_files_are_allowed_until_twenty_mebibyte_quota(self):
        self.start()
        content = b"x" * MAX_FILE_SIZE
        for index in range(10):
            self.expect_ok(self.upload(f"large-{index}.txt", content))
        response = self.upload("extra.txt", b"x")
        self.assertIn(response.status_code, (409, 413))
        self.assertEqual(sum(item["size"] for item in self.files()["files"]), 20 * 1024 * 1024)

    def test_membership_run_and_printed_files_survive_server_restart(self):
        member = self.paired()
        run_id = self.start()["run"]["id"]
        printed = self.new_file("persist.txt", "重启以后仍可下载\n".encode("utf-8"))
        cookies = [client.get_cookie("qoj_vp_session").value for client in (self.captain, member)]
        restarted = create_app(contest_id=1802, demo=True, data_dir=self.data_dir)
        restarted.config["TESTING"] = True
        for cookie, role in zip(cookies, ("captain", "member")):
            client = restarted.test_client()
            client.set_cookie("qoj_vp_session", cookie)
            state = self.state(client)
            self.assertEqual(state["team"]["role"], role)
            self.assertEqual(state["run"]["id"], run_id)
            response = client.get(printed["download_url"])
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, "重启以后仍可下载\n".encode("utf-8"))
            response.close()


class StatementResourceClient(ActiveNativeFixture, DemoClient):
    def __init__(self):
        super().__init__()
        self.calls = []

    def problem(self, contest_id, problem_id):
        result = super().problem(contest_id, problem_id)
        result["statement_html"] = '''<p>Untrusted statement content</p>
          <img src="/qoj/logout?_token=fixture"><img src="/qoj/images/figure.png">
          <iframe src="/qoj/download.php?type=statement&id=1"></iframe>
          <iframe src="/qoj/download.php?type=problem&id=1"></iframe>
          <iframe src="/qoj/download.php?type=statement&id=999"></iframe>
          <img src="/qoj/download.php?type=source&id=1">
          <img src="/qoj/download.php?type=statement&id=1&id=999">'''
        return result

    def fetch(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if not kwargs["url_guard"]("https://qoj.ac" + path):
            raise QOJError("Fixture guard rejected resource", 403)
        response = Response()
        response.status_code = 200
        response.url = "https://qoj.ac" + path
        response.headers["Content-Type"] = "application/pdf" if path.startswith("/download.php") else "image/png"
        response._content = PDF_BYTES if path.startswith("/download.php") else b"fixture image"
        return response


class MemberStatementResourceTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.upstream = StatementResourceClient()
        self.app = create_app(contest_id=1802, data_dir=Path(directory.name), client_factory=lambda: self.upstream)
        self.app.config["TESTING"] = True
        self.captain = self.app.test_client()
        token = self.captain.get("/api/state").get_json()["csrf_token"]
        created = self.captain.post("/api/team/create", json={"name": "Resource test", "device_name": "Captain"},
                                    headers={"X-CSRF-Token": token})
        self.assertEqual(created.status_code, 200)
        self.member = self.app.test_client()
        token = self.member.get("/api/state").get_json()["csrf_token"]
        joined = self.member.post("/api/team/join", json={"code": created.get_json()["team"]["join_code"], "device_name": "Member"},
                                  headers={"X-CSRF-Token": token})
        self.assertEqual(joined.status_code, 200)
        self.assertEqual(self.member.get("/api/problems/1").status_code, 200)

    def test_referenced_logout_url_is_rejected_before_fetch(self):
        response = self.member.get("/qoj/logout?_token=fixture")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.upstream.calls, [])

    def test_referenced_pdf_and_image_resources_are_readable_with_redirect_guard(self):
        for path in ("/qoj/download.php?type=statement&id=1", "/qoj/download.php?type=problem&id=1", "/qoj/images/figure.png"):
            with self.subTest(path=path):
                response = self.member.get(path)
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                self.assertTrue(response.data)
                self.assertTrue(callable(self.upstream.calls[-1][2]["url_guard"]))
        self.assertEqual(len(self.upstream.calls), 3)

    def test_unknown_problem_wrong_download_type_duplicate_id_and_unlisted_image_are_rejected(self):
        for path in ("/qoj/download.php?type=statement&id=999", "/qoj/download.php?type=source&id=1",
                     "/qoj/download.php?type=statement&id=1&id=999", "/qoj/images/unlisted.png"):
            with self.subTest(path=path):
                self.assertEqual(self.member.get(path).status_code, 403)
        self.assertEqual(self.upstream.calls, [])


class MemberResourceRedirectTests(unittest.TestCase):
    def test_allowed_statement_resource_cannot_redirect_to_logout(self):
        client = QOJClient()
        self.addCleanup(client.session.close)
        response = Response()
        response.status_code = 302
        response.url = "https://qoj.ac/download.php?type=statement&id=1"
        response.headers["Location"] = "/logout?_token=fixture"
        response._content = b""
        client.session.request = Mock(return_value=response)
        permitted = lambda url: url == "https://qoj.ac/download.php?type=statement&id=1"
        with self.assertRaises(QOJError) as raised:
            client.fetch("GET", response.url, url_guard=permitted)
        self.assertEqual(raised.exception.status, 403)
        client.session.request.assert_called_once()

    def test_unlisted_initial_resource_is_rejected_before_network_access(self):
        client = QOJClient()
        self.addCleanup(client.session.close)
        client.session.request = Mock()
        with self.assertRaises(QOJError) as raised:
            client.fetch("GET", "/contest/1802/problem/999", url_guard=lambda url: False)
        self.assertEqual(raised.exception.status, 403)
        client.session.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
