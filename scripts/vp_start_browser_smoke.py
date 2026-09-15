"""Verify native VP Start and paginated registration through real adapter HTML.

Edge connects exclusively to in-memory Flask routes backed by a synthetic QOJ
fixture. No listener, real QOJ request, account, or existing data directory is used.
Set QOJ_FRONTEND_STATIC=frontend-dist to test a staged frontend build.
"""
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT / "tests"))

from app import create_app
from qoj import QOJClient
from playwright.sync_api import expect, sync_playwright
from test_native_adapter_api import PaginatedVPClient
from test_native_entry_transport import NativeEntryTransport

ORIGIN = "http://qoj-vp-start.test"
CONTENT_TABS = ("overview", "problems", "attachments", "submissions", "scoreboard", "printing")


def assert_locked(page):
    expect(page.locator("#view-participation")).to_be_visible()
    expect(page.locator("#contest-title")).to_have_text("Contest #9")
    expect(page.locator("#participation-locked-note")).to_be_visible()
    for tab in CONTENT_TABS:
        expect(page.locator(f'[data-tab="{tab}"]')).not_to_be_visible()
    for content in ("Fixture Contest", "Hidden A", "Hidden B", "Hidden statements"):
        assert content not in page.locator("body").inner_text(), content
    expect(page.locator("#scoreboard-table")).to_have_text("")
    expect(page.locator("#team-scoreboard")).to_have_text("")
    assert not page.locator("#statement-frame").get_attribute("src")
    assert not page.locator("#statement-frame").get_attribute("srcdoc")


def exercise_rejections(playwright):
    """Real request serialization must surface rejected/unconfirmed native starts."""
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    try:
        for rejection, expected_status in (("This page has expired.", 422), (None, 409)):
            with TemporaryDirectory() as temporary:
                transport = NativeEntryTransport()
                transport.rejection = rejection
                transport.ignore_post = rejection is None
                upstream = QOJClient()
                upstream.session.adapters.clear()
                upstream.session.mount("https://qoj.ac/", transport)
                upstream.import_cookies("UOJSESSID=offline-captain-session")
                app = create_app(contest_id=9, data_dir=Path(temporary), mode="vp", client_factory=lambda: upstream)
                app.config["TESTING"] = True
                static_override = os.environ.get("QOJ_FRONTEND_STATIC")
                if static_override:
                    app.static_folder = str((ROOT / static_override).resolve())
                client = app.test_client()
                initial = client.get("/api/state", base_url=ORIGIN).get_json()
                created = client.post("/api/team/create", json={"name": "Rejected VP offline team"},
                                      headers={"X-CSRF-Token": initial["csrf_token"]}, base_url=ORIGIN)
                assert created.status_code == 200, created.get_data(as_text=True)
                seen, errors, states = [], [], []

                def handler(route):
                    request = route.request
                    parsed = urlsplit(request.url)
                    if parsed.netloc != "qoj-vp-start.test":
                        route.abort()
                        return
                    path = parsed.path + ("?" + parsed.query if parsed.query else "")
                    seen.append((request.method, path))
                    response = client.open(path, method=request.method, data=request.post_data_buffer, base_url=ORIGIN,
                                           headers={key: value for key, value in request.headers.items()
                                                    if key.lower() not in ("host", "content-length", "cookie")})
                    try:
                        if path == "/api/state" and response.status_code == 200:
                            states.append(response.get_json())
                        route.fulfill(status=response.status_code, body=response.get_data(),
                                      headers={key: value for key, value in response.headers.items()
                                               if key.lower() not in ("content-length", "set-cookie")})
                    finally:
                        response.close()

                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.route("**/*", handler)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                try:
                    page.goto(ORIGIN + "/#participation", wait_until="networkidle")
                    assert_locked(page)
                    with page.expect_response(ORIGIN + "/api/start") as started:
                        page.locator("#participation-start-button").click()
                    assert started.value.status == expected_status
                    returned = started.value.json()
                    assert returned.get("error")
                    expect(page.locator("#participation-error")).to_be_visible()
                    expect(page.locator("#participation-error")).to_have_attribute("role", "alert")
                    expect(page.locator("#participation-error")).to_have_text(returned["error"])
                    if rejection:
                        expect(page.locator("#participation-error")).to_contain_text(rejection)
                    expect(page).to_have_url(ORIGIN + "/#participation")
                    expect(page.locator("#view-overview")).not_to_be_visible()
                    assert_locked(page)
                    assert not transport.entered
                    assert sum(request.method == "POST" for request in transport.requests) == 1
                    assert all(state["run"] is None and not state["permissions"]["view_content"] for state in states)

                    with page.expect_response(ORIGIN + "/api/state"):
                        page.locator("#participation-refresh").click()
                    expect(page.locator("#participation-refresh")).to_be_enabled()
                    assert_locked(page)
                    assert states[-1]["run"] is None
                    assert sum(request.method == "POST" for request in transport.requests) == 1
                    assert seen.count(("POST", "/api/start")) == 1
                    assert not errors, errors
                finally:
                    context.close()
                    upstream.session.close()
        print("Native VP rejection browser smoke passed: HTTP 422/409 display inline alerts, remain at Contest, and never create a run or retry the POST.")
    finally:
        browser.close()


def main():
    with TemporaryDirectory() as temporary, sync_playwright() as playwright:
        upstream = PaginatedVPClient()
        app = create_app(contest_id=9, data_dir=Path(temporary), mode="vp", client_factory=lambda: upstream)
        app.config["TESTING"] = True
        static_override = os.environ.get("QOJ_FRONTEND_STATIC")
        if static_override:
            app.static_folder = str((ROOT / static_override).resolve())
        captain_client, member_client = app.test_client(), app.test_client()

        def post(client, path, body):
            state = client.get("/api/state", base_url=ORIGIN).get_json()
            response = client.post(path, json=body, headers={"X-CSRF-Token": state["csrf_token"]}, base_url=ORIGIN)
            assert response.status_code == 200, response.get_data(as_text=True)
            return response.get_json()

        team = post(captain_client, "/api/team/create", {"name": "Paginated VP browser team", "device_name": "Captain"})
        post(member_client, "/api/team/join", {"code": team["team"]["join_code"], "device_name": "Member"})
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        seen, errors, states = [], [], {}

        def make_handler(client, role):
            def handler(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.netloc != "qoj-vp-start.test":
                    route.abort()
                    return
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
                seen.append((role, request.method, path))
                response = client.open(path, method=request.method, data=request.post_data_buffer, base_url=ORIGIN,
                                       headers={key: value for key, value in request.headers.items()
                                                if key.lower() not in ("host", "content-length", "cookie")})
                try:
                    if parsed.path in ("/api/state", "/api/start") and response.status_code == 200:
                        payload = response.get_json()
                        if payload and "participation" in payload:
                            states[role] = payload
                    route.fulfill(status=response.status_code, body=response.get_data(),
                                  headers={key: value for key, value in response.headers.items()
                                           if key.lower() not in ("content-length", "set-cookie")})
                finally:
                    response.close()
            return handler

        def upstream_posts():
            return [call for call in upstream.posts if call[0].upper() == "POST"]

        try:
            pages = []
            for client, role in ((captain_client, "captain"), (member_client, "member")):
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.route("**/*", make_handler(client, role))
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(ORIGIN + "/#participation", wait_until="networkidle")
                pages.append(page)
            captain, member = pages
            for page in pages:
                assert_locked(page)
            expect(member.locator("#participation-start-form")).not_to_be_visible()
            expect(member.locator("#participation-mode")).to_be_disabled()
            expect(captain.locator("#participation-start-button")).to_have_text("Start!")
            expect(captain.locator("#participation-start-button")).to_be_enabled()
            assert not upstream_posts()
            assert not any(path.startswith(("/api/problems/", "/api/attachments", "/api/prints"))
                           for _, _, path in seen), seen

            captain.locator("#participation-start-button").click()
            expect(captain.locator("#view-overview")).to_be_visible()
            expect(captain).to_have_url(ORIGIN + "/#overview")
            expect(captain.locator("#contest-title")).to_have_text("Fixture Contest")
            expect(captain.locator("#run-status")).to_have_text("比赛进行中")
            expect(captain.locator("#participation-error")).not_to_be_visible()
            expect(captain.locator("#participation-start-form")).not_to_be_visible()
            expect(captain.locator("#team-scoreboard")).to_contain_text("Fixture Team")
            expect(captain.locator("#submit-button")).to_be_visible()
            assert len(upstream_posts()) == 1, upstream_posts()
            _, url, kwargs = upstream_posts()[0]
            assert url == "https://qoj.ac/contest/9", url
            assert kwargs["data"] == {"_token": "fresh-token", "submit-regvir": "regvir"}, kwargs
            run_id = states["captain"]["run"]["id"]
            assert states["captain"]["participation"]["status"] == "running"
            assert states["captain"]["run"]["started_at"] == upstream.start
            assert states["captain"]["run"]["ends_at"] == upstream.start + 18000
            assert "/contest/9/registrants?page=2" in upstream.gets

            # The member never submits a native entry form. Refreshing the shared
            # state opens the same confirmed run and makes its content available.
            member.locator("#participation-refresh").click()
            expect(member.locator("#participation-enter")).to_be_visible()
            expect(member.locator("#contest-title")).to_have_text("Fixture Contest")
            expect(member.locator('[data-tab="problems"]')).to_be_visible()
            expect(member.locator('[data-tab="scoreboard"]')).to_be_visible()
            expect(member.locator("#submit-button")).not_to_be_visible()
            expect(member.locator("#participation-start-form")).not_to_be_visible()
            assert states["member"]["run"]["id"] == run_id
            member.locator('[data-tab="scoreboard"]').click()
            expect(member.locator("#scoreboard-table")).to_contain_text("Fixture Team")
            expect(member.locator("#scoreboard-table")).to_contain_text("?4")
            assert len(upstream_posts()) == 1

            for page, role in ((captain, "captain"), (member, "member")):
                page.locator('[data-tab="participation"]').click()
                with page.expect_response(ORIGIN + "/api/state"):
                    page.locator("#participation-refresh").click()
                expect(page.locator("#participation-refresh")).to_be_enabled()
                expect(page.locator("#participation-status-title")).to_have_text("比赛正在进行")
                assert states[role]["run"]["id"] == run_id
                assert states[role]["participation"]["started_at"] == upstream.start
                assert states[role]["run"]["ends_at"] == upstream.start + 18000
            assert len(upstream_posts()) == 1, upstream_posts()
            assert [(role, method) for role, method, path in seen if path == "/api/start"] == [("captain", "POST")]
            assert not errors, errors
            print("Paginated native VP browser smoke passed: one upstream Start POST, captain overview, member content gating, and stable shared run after refresh.")
        finally:
            browser.close()
        exercise_rejections(playwright)


if __name__ == "__main__":
    main()
