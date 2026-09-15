"""Verify local team dissolution with real offline-demo APIs and isolated Edge.

All browsers use in-memory Flask routes and temporary synthetic team records.
No listening service, real QOJ connection, account, or existing data is accessed.
Set QOJ_FRONTEND_STATIC=frontend-dist to test a staged Vue build.
"""
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

ORIGIN = "http://qoj-dissolve.test"


def assert_team_entry_only(page):
    expect(page.locator(".main-nav [data-tab]:visible")).to_have_count(2)
    for tab in ("participation", "team"):
        expect(page.locator(f'[data-tab="{tab}"]')).to_be_visible()
    expect(page.locator("#submit-button")).not_to_be_visible()
    expect(page.locator("#team-dissolve-button")).to_have_count(0)
    expect(page.locator("#cli-tools-panel")).to_have_count(0)


def main():
    with TemporaryDirectory() as temporary, sync_playwright() as playwright:
        app = create_app(contest_id=1, demo=True, data_dir=Path(temporary))
        app.config["TESTING"] = True
        static_override = os.environ.get("QOJ_FRONTEND_STATIC")
        if static_override:
            app.static_folder = str((ROOT / static_override).resolve())
        clients = [app.test_client() for _ in range(3)]
        captain_client = clients[0]

        def post(client, path, body):
            state = client.get("/api/state", base_url=ORIGIN).get_json()
            response = client.post(path, json=body, headers={"X-CSRF-Token": state["csrf_token"]}, base_url=ORIGIN)
            assert 200 <= response.status_code < 300, response.get_data(as_text=True)
            return response.get_json()

        created = post(captain_client, "/api/team/create", {"name": "Dissolve offline team", "device_name": "Captain"})
        old_code = created["team"]["join_code"]
        old_team = created["team"]["id"]
        for index, client in enumerate(clients[1:], 1):
            post(client, "/api/team/join", {"code": old_code, "device_name": f"Member {index}"})
        old_run = post(captain_client, "/api/start", {})["run"]["id"]
        post(captain_client, "/api/prints", {"name": "preserved-notes.txt", "content": "Offline history survives dissolution."})
        print_list = captain_client.get("/api/prints", base_url=ORIGIN).get_json()["files"]
        old_print_url = print_list[0]["view_url"]
        cli_token = post(captain_client, "/api/cli/token", {})["config"]["token"]
        assert app.test_client().get("/api/state", base_url=ORIGIN,
                                     headers={"Authorization": "Bearer " + cli_token}).status_code == 200
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        seen, deferred, errors = [], [], []
        delay_dissolve = {"enabled": True}

        def make_handler(client, device):
            def handler(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.netloc != "qoj-dissolve.test":
                    route.abort()
                    return
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
                seen.append((device, request.method, path))
                response = client.open(path, method=request.method, data=request.post_data_buffer, base_url=ORIGIN,
                                       headers={key: value for key, value in request.headers.items()
                                                if key.lower() not in ("host", "content-length", "cookie")})
                try:
                    reply = {"status": response.status_code, "body": response.get_data(),
                             "headers": {key: value for key, value in response.headers.items()
                                         if key.lower() not in ("content-length", "set-cookie")}}
                    if path == "/api/team/dissolve" and delay_dissolve["enabled"]:
                        deferred.append((route, reply))
                    else:
                        route.fulfill(**reply)
                finally:
                    response.close()
            return handler

        try:
            pages = []
            for index, client in enumerate(clients):
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.route("**/*", make_handler(client, index))
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(ORIGIN + "/#team", wait_until="networkidle")
                page.locator('[data-tab="team"]').click()
                pages.append(page)
            captain, member_one, member_two = pages
            expect(captain.locator("#team-dissolve-button")).to_be_visible()
            for member in (member_one, member_two):
                expect(member.locator("#team-dissolve-button")).to_have_count(0)
                member.locator('[data-tab="printing"]').click()
                expect(member.locator("#prints-table")).to_contain_text("preserved-notes.txt")

            captain.locator("#team-dissolve-button").click()
            expect(captain.locator("#confirm-title")).to_have_text("解散队伍？")
            for warning in ("本地队伍", "不会结束 QOJ", "命令行配置", "历史参赛、提交和打印记录保留"):
                expect(captain.locator("#confirm-message")).to_contain_text(warning)
            captain.locator('#confirm-dialog button[data-close="confirm-dialog"]').last.click()
            assert not any(path == "/api/team/dissolve" for _, _, path in seen)
            expect(captain.locator("#team-join-code")).to_have_value(old_code)

            captain.locator("#team-dissolve-button").click()
            captain.locator("#confirm-action").click()
            expect(captain.locator("#confirm-action")).to_be_disabled()
            expect(captain.locator("#team-dissolve-button")).to_be_disabled()
            captain.evaluate("document.querySelector('#confirm-action').click()")
            assert len(deferred) == 1
            assert sum(path == "/api/team/dissolve" for _, _, path in seen) == 1
            pending, reply = deferred.pop()
            pending.fulfill(**reply)
            delay_dissolve["enabled"] = False
            expect(captain).to_have_url(ORIGIN + "/#participation")
            expect(captain.locator("#confirm-dialog")).not_to_be_visible()
            assert_team_entry_only(captain)

            for member in (member_one, member_two):
                member.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                assert_team_entry_only(member)
                expect(member.locator("#prints-table")).to_have_text("")
                member.locator('[data-tab="team"]').click()
                expect(member.locator("#team-create-button")).to_be_visible()
            for client in clients:
                assert client.get("/api/state", base_url=ORIGIN).get_json()["team"] is None
            assert app.test_client().get("/api/state", base_url=ORIGIN,
                                         headers={"Authorization": "Bearer " + cli_token}).status_code == 401
            assert clients[1].get(old_print_url, base_url=ORIGIN).status_code == 404

            member_one.locator("#team-join-input").fill(old_code)
            member_one.locator("#team-join-button").click()
            expect(member_one.locator("#team-error")).to_contain_text("加入码无效")
            expect(member_one.locator("#team-current")).not_to_be_visible()
            member_two.locator("#team-create-name").fill("New member-owned offline team")
            member_two.locator("#team-create-button").click()
            expect(member_two.locator("#team-display-name")).to_have_text("New member-owned offline team")
            expect(member_two.locator("#team-dissolve-button")).to_be_visible()

            captain.locator('[data-tab="team"]').click()
            captain.locator("#team-create-name").fill("Recreated offline team")
            captain.locator("#team-create-button").click()
            expect(captain.locator("#team-display-name")).to_have_text("Recreated offline team")
            new_state = captain_client.get("/api/state", base_url=ORIGIN).get_json()
            assert new_state["team"]["id"] != old_team
            assert new_state["team"]["join_code"] != old_code
            assert new_state["run"]["id"] == old_run
            captain.locator('[data-tab="printing"]').click()
            expect(captain.locator("#prints-table")).to_contain_text("preserved-notes.txt")

            # Another captain tab can dissolve while this tab has an old confirm
            # open. Refresh must close it and an old button must not POST again.
            captain.locator('[data-tab="team"]').click()
            captain.locator("#team-dissolve-button").click()
            captain.evaluate("window.oldDissolveConfirm = document.querySelector('#confirm-action')")
            post(captain_client, "/api/team/dissolve", {})
            captain.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            expect(captain.locator("#confirm-dialog")).not_to_be_visible()
            assert_team_entry_only(captain)
            captain.evaluate("window.oldDissolveConfirm.click()")
            assert sum(path == "/api/team/dissolve" for _, _, path in seen) == 1
            assert not errors, errors
            print("Team dissolution browser smoke passed: captain confirmation, cancel, one POST, device detachment, invalid credentials, private history retained, new teams, and stale confirmation isolation.")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
