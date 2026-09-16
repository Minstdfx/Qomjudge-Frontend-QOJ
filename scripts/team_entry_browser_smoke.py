"""Check Team entry gates with isolated offline data and Flask in-memory routes.

No listener, live service, QOJ account, or network request is used. Run with
python scripts/team_entry_browser_smoke.py; QOJ_FRONTEND_STATIC may select an
unpublished frontend build. Requires Playwright and Microsoft Edge.
"""
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "Lib" / "site-packages"))

from app import create_app
from playwright.sync_api import expect, sync_playwright

ORIGIN = "http://team-entry.test"
RESTRICTED = ("overview", "problems", "attachments", "submissions", "scoreboard", "printing")


def visible_tabs(page, tabs):
    expect(page.locator(".main-nav [data-tab]:visible")).to_have_count(len(tabs))
    assert page.locator(".main-nav [data-tab]:visible").evaluate_all(
        "nodes => nodes.map(node => node.dataset.tab)"
    ) == list(tabs)


def assert_locked(page, tab="participation"):
    visible_tabs(page, ("participation", "team"))
    expect(page.locator(f"#view-{tab}")).to_be_visible()
    expect(page.locator("#submit-button")).not_to_be_visible()
    expect(page.locator("#participation-enter")).not_to_be_visible()
    expect(page.locator("#start-button")).not_to_be_visible()
    expect(page.locator("#participation-start-form")).not_to_be_visible()
    expect(page.locator("#contest-title")).to_have_text("Contest #1")
    for name in RESTRICTED:
        expect(page.locator(f"#view-{name}")).not_to_be_visible()
    frame = page.locator("#statement-frame")
    assert not frame.get_attribute("src")
    assert not frame.get_attribute("srcdoc")
    for name in ("attachments-table", "prints-table", "scoreboard-table", "team-scoreboard"):
        expect(page.locator(f"#{name}")).to_have_text("")
    expect(page.locator("#print-content")).to_have_value("")
    for text in ("A + B Problem", "QOJ Practice Contest", "team-only-note.txt"):
        assert text not in page.locator("body").inner_text(), text


def main():
    with TemporaryDirectory() as temporary, sync_playwright() as p:
        app = create_app(contest_id=1, demo=True, data_dir=Path(temporary))
        app.config.update(TESTING=True)
        if os.environ.get("QOJ_FRONTEND_STATIC"):
            app.static_folder = str((ROOT / os.environ["QOJ_FRONTEND_STATIC"]).resolve())
        assert (Path(app.static_folder) / "index.html").is_file()
        clients = [app.test_client() for _ in range(3)]
        seen = [[] for _ in clients]
        errors = []

        def state(client):
            return client.get("/api/state", base_url=ORIGIN).get_json()

        def post(client, path, body=None):
            response = client.post(path, json=body or {}, base_url=ORIGIN,
                                   headers={"X-CSRF-Token": state(client)["csrf_token"]})
            assert 200 <= response.status_code < 300, response.data
            return response.get_json()

        # Reproduce returning solo sessions, including backend-visible content:
        # these must still enter through Team before the UI reveals that content.
        post(clients[0], "/api/start", {"team_name": "Legacy running solo"})
        post(clients[1], "/api/start", {"team_name": "Legacy finished solo"})
        post(clients[1], "/api/finish")
        assert state(clients[0])["run"]["status"] == "running"
        assert state(clients[1])["run"]["status"] == "finished"
        assert not state(clients[2])["run"]

        def handler_for(index):
            def handler(route):
                request = route.request
                parsed = urlsplit(request.url)
                assert f"{parsed.scheme}://{parsed.netloc}" == ORIGIN, request.url
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
                seen[index].append((request.method, path))
                response = clients[index].open(
                    path, method=request.method, data=request.post_data_buffer, base_url=ORIGIN,
                    headers={key: value for key, value in request.headers.items()
                             if key.lower() not in ("host", "content-length", "cookie")})
                try:
                    route.fulfill(status=response.status_code, body=response.get_data(),
                                  headers={key: value for key, value in response.headers.items()
                                           if key.lower() not in ("content-length", "set-cookie")})
                finally:
                    response.close()
            return handler

        browser = p.chromium.launch(channel="msedge", headless=True)
        try:
            pages = []
            for index in range(len(clients)):
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.route("**/*", handler_for(index))
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                pages.append(page)
                page.goto(ORIGIN + "/#problems/1", wait_until="networkidle")
                assert_locked(page)
                expect(page.locator("#participation-team-setup")).to_be_visible()
                for tab in RESTRICTED:
                    page.evaluate("hash => { location.hash = hash; }", f"#{tab}/1")
                    expect(page).to_have_url(ORIGIN + "/#participation")
                    assert_locked(page)
                assert not any(path.startswith(("/api/problems/", "/api/attachments", "/api/prints"))
                               for _, path in seen[index]), seen[index]

            captain, member, other = pages
            # Small screens use the same gate rather than hiding overflow only.
            other.set_viewport_size({"width": 390, "height": 844})
            assert_locked(other)
            assert other.evaluate("document.documentElement.scrollWidth <= innerWidth")

            captain.locator("#participation-team-setup").click()
            expect(captain.locator("#team-solo")).to_be_visible()
            captain.locator("#team-create-name").fill("Team entry fixture")
            captain.locator("#team-create-button").click()
            expect(captain.locator("#team-join-code")).to_be_visible()
            code = captain.locator("#team-join-code").input_value()
            visible_tabs(captain, ("participation", "overview", "problems", "attachments",
                                   "submissions", "scoreboard", "printing", "team"))
            expect(captain.locator("#submit-button")).to_be_visible()
            captain.locator('[data-tab="problems"]').click()
            expect(captain.frame_locator("#statement-frame").locator("h1")).to_contain_text("A + B")
            post(clients[0], "/api/prints", {"name": "team-only-note.txt", "content": "Team-only fixture note"})

            member.locator("#participation-team-setup").click()
            member.locator("#team-join-input").fill(code)
            member.locator("#team-join-button").click()
            expect(member.locator("#team-leave-button")).to_be_visible()
            visible_tabs(member, ("participation", "problems", "scoreboard", "printing", "team"))
            expect(member.locator("#submit-button")).not_to_be_visible()
            member.locator('[data-tab="problems"]').click()
            expect(member.frame_locator("#statement-frame").locator("h1")).to_contain_text("A + B")
            member.locator('[data-tab="scoreboard"]').click()
            # Creating Team keeps the existing VP's stored scoreboard name.
            expect(member.locator("#scoreboard-table")).to_contain_text("Legacy running solo")
            member.locator('[data-tab="printing"]').click()
            expect(member.locator("#prints-table")).to_contain_text("team-only-note.txt")
            member.locator('[data-tab="team"]').click()
            member.locator("#team-leave-button").click()
            member.locator("#confirm-action").click()
            expect(member.locator("#team-solo")).to_be_visible()
            assert_locked(member, "team")
            member.evaluate("location.hash = '#printing'")
            expect(member).to_have_url(ORIGIN + "/#participation")
            assert_locked(member)

            # Rejoin and verify a server-side revocation also invalidates content.
            member.locator("#participation-team-setup").click()
            member.locator("#team-join-input").fill(code)
            member.locator("#team-join-button").click()
            expect(member.locator("#team-leave-button")).to_be_visible()
            member.locator('[data-tab="problems"]').click()
            expect(member.frame_locator("#statement-frame").locator("h1")).to_contain_text("A + B")
            device = next(item for item in state(clients[0])["team"]["devices"] if item["role"] == "member")
            post(clients[0], "/api/team/revoke", {"device_id": device["id"]})
            member.locator('[data-tab="participation"]').click()
            member.locator("#participation-refresh").click()
            expect(member.locator("#team-role-label")).to_have_text("访问已撤销")
            # Existing routing keeps an allowed Contest page; both entry pages
            # remain accessible while all contest content has been cleared.
            assert_locked(member)
            member.evaluate("location.hash = '#scoreboard'")
            expect(member).to_have_url(ORIGIN + "/#team")
            assert_locked(member, "team")
            assert not errors, errors
            print(json.dumps({"team_entry": "passed", "solo_statuses": ["unregistered", "running", "finished"],
                              "browsers": 3, "hash_guards": len(RESTRICTED) * 3,
                              "member_leave": "passed", "revocation": "passed", "browser_errors": errors}))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
