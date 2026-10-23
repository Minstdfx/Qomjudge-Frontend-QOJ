"""Check the Joint VP UI with synthetic JSON and local built assets only.

No listener, QOJ request, account cookie, or existing application data is used.
Set QOJ_FRONTEND_STATIC=frontend-dist to check an unpublished Vue build.
"""
import copy
import json
import mimetypes
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".venv" / "Lib" / "site-packages"))

from playwright.sync_api import expect, sync_playwright

ORIGIN = "http://joint-vp-ui.test"
CODE = "synthetic-joint-code"
PLAN = {
    "fingerprint": "synthetic-form-v1",
    "message": "Synthetic QOJ registration declaration. Please confirm before starting.",
    "action": {"id": "register_vp", "kind": "start", "label": "Start",
               "fields": [{"name": "agree", "type": "checkbox", "required": True,
                           "label": "I accept the synthetic contest rules", "default": True}]},
}


def main():
    static = (ROOT / os.environ.get("QOJ_FRONTEND_STATIC", "static")).resolve()
    assert (static / "index.html").is_file()
    fixture = {"room": None, "native": {}, "posts": [], "requests": [], "external": [], "errors": [], "fail_start": False}
    now = int(time.time())

    def state(team_id, role):
        room = fixture["room"]
        own = fixture["native"].get(team_id)
        if own is None:
            own = next((item for item in room["members"] if item["team_id"] == team_id), None) if room else None
        started = own and own["status"] == "started"
        return {
            "server_time": time.time(), "poll_interval": 120, "demo": False, "csrf_token": "fixture-csrf",
            "contest": {"id": 9, "title": "Synthetic Joint VP", "problems": [], "attachments": []},
            "user": {"authenticated": True, "username": f"fixture-account-{team_id}"},
            "team": {"id": team_id, "name": f"Fixture team {team_id}", "role": role,
                     "device_id": f"{team_id}-{role}", "device_name": role, "devices": []},
            "permissions": {"manage": role == "captain", "view_content": bool(started),
                            "submit": bool(started) and role == "captain", "print": bool(started) and role == "captain"},
            "participation": {"mode": "vp", "status": "running" if started else "unregistered",
                              "content_available": bool(started), "can_submit": bool(started),
                              "started_at": own.get("started_at") if started else None,
                              "ends_at": own.get("ends_at") if started else None,
                              "actions": [] if started else [{"id": "regvir", "kind": "start", "label": "Start", "fields": []}]},
            "run": None, "submissions": [], "scoreboard": [], "languages": [],
        }

    def member(team_id):
        return {"team_id": team_id, "name": f"Fixture team {team_id}", "status": "waiting",
                "started_at": None, "ends_at": None, "message": ""}

    def room_view(team_id, role):
        stored = fixture["room"]
        if not stored or not any(item["team_id"] == team_id for item in stored["members"]):
            return {"joint_vp": None}
        room = copy.deepcopy(stored)
        room["is_host"] = team_id == 1
        own = next(item for item in room["members"] if item["team_id"] == team_id)
        room["self"] = {"status": own["status"], "username": f"fixture-account-{team_id}"}
        if role == "captain":
            room["code"] = CODE
            if team_id in stored["plans"]:
                room["self"]["plan"] = copy.deepcopy(PLAN)
        room["can_start"] = room["status"] == "waiting" and len(room["members"]) >= 2 and all(item["status"] == "ready" for item in room["members"])
        room.pop("plans", None)
        return {"joint_vp": room}

    def handler_for(team_id, role):
        def handler(route):
            request = route.request
            parsed = urlsplit(request.url)
            if f"{parsed.scheme}://{parsed.netloc}" != ORIGIN:
                fixture["external"].append(request.url)
                route.abort()
                return
            path = parsed.path
            fixture["requests"].append((team_id, role, request.method, path))
            if path == "/api/state":
                route.fulfill(json=state(team_id, role))
            elif path == "/api/joint-vp":
                route.fulfill(json=room_view(team_id, role))
            elif path.startswith("/api/joint-vp/"):
                assert role == "captain", "Members must not send Joint VP mutations"
                assert request.method == "POST"
                assert request.headers.get("x-csrf-token") == "fixture-csrf"
                body = request.post_data_json
                action = path.rsplit("/", 1)[-1]
                fixture["posts"].append((team_id, action, body))
                stored = fixture["room"]
                if action == "create":
                    assert not stored
                    fixture["room"] = {"id": 1, "status": "waiting", "members": [member(team_id)], "plans": []}
                elif action == "join":
                    assert body["code"] == CODE
                    stored["members"].append(member(team_id))
                elif action == "prepare":
                    if team_id not in stored["plans"]:
                        stored["plans"].append(team_id)
                    next(item for item in stored["members"] if item["team_id"] == team_id)["status"] = "needs_confirmation"
                elif action in ("ready", "unready"):
                    own = next(item for item in stored["members"] if item["team_id"] == team_id)
                    if action == "ready":
                        assert body == {"fingerprint": PLAN["fingerprint"], "action": PLAN["action"]["id"],
                                        "values": {"agree": True}, "consent": True}
                    own["status"] = "ready" if action == "ready" else "waiting"
                    stored["plans"] = [item for item in stored["plans"] if item != team_id]
                elif action == "start":
                    assert team_id == 1 and all(item["status"] == "ready" for item in stored["members"])
                    stored["status"] = "starting"
                    for item in stored["members"]:
                        item["status"] = "starting"
                    if fixture["fail_start"]:
                        # The request took effect, but the response was lost. Only
                        # a subsequent GET may reconcile it; never re-send Start.
                        route.abort("failed")
                        return
                elif action == "refresh":
                    pass
                elif action == "cancel":
                    fixture["room"] = None
                elif action == "leave":
                    stored["members"] = [item for item in stored["members"] if item["team_id"] != team_id]
                else:
                    raise AssertionError(f"Unexpected Joint VP mutation: {action}")
                route.fulfill(status=202 if action == "start" else 200, json=room_view(team_id, role))
            elif path == "/api/logout":
                assert role == "captain" and request.method == "POST"
                assert request.headers.get("x-csrf-token") == "fixture-csrf"
                route.fulfill(status=409, json={"error": "已准备联合 VP，请先取消准备。", "code": "joint_vp_locked"})
            elif path.startswith("/api/"):
                # Ancillary Team/CLI views have no state in this UI fixture.
                assert request.method == "GET", path
                route.fulfill(json={"tokens": []})
            else:
                relative = "index.html" if path == "/" else path.removeprefix("/static/").lstrip("/")
                target = (static / relative).resolve()
                assert target.is_relative_to(static)
                if target.is_file():
                    route.fulfill(body=target.read_bytes(), content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                else:
                    route.fulfill(status=404, body="Not found")
        return handler

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            pages = []
            for team_id, role in ((1, "captain"), (2, "captain"), (1, "member")):
                context = browser.new_context(viewport={"width": 1440, "height": 1100})
                context.route("**/*", handler_for(team_id, role))
                page = context.new_page()
                page.on("pageerror", lambda failure: fixture["errors"].append(str(failure)))
                page.goto(ORIGIN + (f"/?joint={CODE}#participation" if team_id == 2 else "/#participation"), wait_until="networkidle")
                pages.append(page)
            host, guest, viewer = pages
            assert not fixture["errors"], fixture["errors"]
            assert not fixture["posts"], "Opening an invitation must not automatically join"
            expect(guest.locator("#joint-vp-join-code")).to_have_value(CODE)
            expect(viewer.locator("#joint-vp-create")).not_to_be_visible()
            host.locator("#joint-vp-create").click()
            expect(host.locator("#joint-vp-code")).to_have_text(CODE)
            expect(host.locator("#joint-vp-start")).to_be_disabled()
            guest.locator("#joint-vp-join").click()
            expect(guest.locator("#joint-vp-members tbody tr")).to_have_count(2)
            expect(guest.locator("#joint-vp-start")).not_to_be_visible()

            for page in (host, guest):
                page.locator("#joint-vp-prepare").click()
                expect(page.locator("#joint-vp-ready-form")).to_be_visible()
                expect(page.locator('[data-joint-field="agree"]')).not_to_be_checked()
                expect(page.locator("#joint-vp-consent")).not_to_be_checked()
                expect(page.locator("#joint-vp-ready")).to_be_disabled()
                page.locator("#joint-vp-consent").check()
                count = len(fixture["posts"])
                page.locator("#joint-vp-ready").click()
                assert len(fixture["posts"]) == count, "Required QOJ checkbox must remain explicit"
                page.locator('[data-joint-field="agree"]').check()
                page.locator("#joint-vp-refresh").click()
                expect(page.locator("#joint-vp-refresh")).to_be_enabled()
                expect(page.locator('[data-joint-field="agree"]')).to_be_checked()
                expect(page.locator("#joint-vp-consent")).to_be_checked()
                page.locator("#joint-vp-ready").click()
                expect(page.locator("#joint-vp-unready")).to_be_visible()
                expect(page.locator("#participation-start-button")).to_be_disabled()
                expect(page.locator("#participation-mode")).to_be_disabled()

            # A pre-mutation guard rejection leaves the connected account and
            # prepared state intact, instead of clearing the authenticated UI.
            host.locator("#account-button").click()
            host.locator("#logout-button").click()
            expect(host.locator("#account-error")).to_contain_text("请先取消准备")
            expect(host.locator("#account-status")).to_contain_text("已连接账号：fixture-account-1")
            expect(host.locator("#account-label")).to_have_text("fixture-account-1")
            host.locator('[data-close="account-dialog"]').click()
            expect(host.locator("#joint-vp-unready")).to_be_visible()
            expect(host.locator("#participation-start-button")).to_be_disabled()

            viewer.locator("#joint-vp-refresh").click()
            expect(viewer.locator("#joint-vp-members tbody tr")).to_have_count(2)
            for selector in ("#joint-vp-invite", "#joint-vp-code", "#joint-vp-prepare", "#joint-vp-ready-form", "#joint-vp-start", "#joint-vp-cancel", "#joint-vp-leave"):
                expect(viewer.locator(selector)).not_to_be_visible()
            assert CODE not in viewer.content()
            viewer.set_viewport_size({"width": 390, "height": 844})
            assert viewer.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile overflow"

            # Cancel preparation and verify the native controls unlock; checking
            # readiness again still requires explicit consent.
            guest.locator("#joint-vp-unready").click()
            expect(guest.locator("#participation-start-button")).to_be_enabled()
            guest.locator("#joint-vp-prepare").click()
            expect(guest.locator("#joint-vp-consent")).not_to_be_checked()
            guest.locator('[data-joint-field="agree"]').check()
            guest.locator("#joint-vp-consent").check()
            guest.locator("#joint-vp-ready").click()
            expect(guest.locator("#joint-vp-unready")).to_be_visible()
            host.locator("#joint-vp-refresh").click()
            expect(host.locator("#joint-vp-start")).to_be_enabled()

            # A lost Start response does not cause a second POST. The local GET
            # still discovers that the background launch is underway.
            fixture["fail_start"] = True
            host.locator("#joint-vp-start").click()
            expect(host.locator("#confirm-dialog")).to_be_visible()
            assert not any(action == "start" for _, action, _ in fixture["posts"])
            host.locator("#confirm-action").click()
            expect(host.locator("#joint-vp-status")).to_have_text("正在启动各队 VP")
            expect(host.locator("#joint-vp-error")).to_be_visible()
            expect(host.locator("#joint-vp-start")).not_to_be_visible()
            assert len([item for item in fixture["posts"] if item[1] == "start"]) == 1

            fixture["room"]["status"] = "started"
            for index, item in enumerate(fixture["room"]["members"]):
                item.update(status="started", started_at=now + index * 3, ends_at=now + index * 3 + 18000)
                fixture["native"][item["team_id"]] = copy.deepcopy(item)
            host.locator("#joint-vp-refresh").click()
            expect(host.locator("#joint-vp-status")).to_have_text("启动流程已完成")
            expect(host.locator("#joint-vp-skew")).to_contain_text("3 秒")
            expect(host.locator("#run-status")).to_have_text("比赛进行中")
            expect(host.locator("#joint-vp-start")).not_to_be_visible()
            assert len([item for item in fixture["posts"] if item[1] == "start"]) == 1

            # The Joint VP component does not poll while another tab is active.
            host.locator('[data-tab="team"]').click()
            before = len([item for item in fixture["requests"] if item[:2] == (1, "captain") and item[3] == "/api/joint-vp"])
            host.wait_for_timeout(5500)
            after = len([item for item in fixture["requests"] if item[:2] == (1, "captain") and item[3] == "/api/joint-vp"])
            assert before == after, "Joint VP polling must pause outside Contest"
            host.locator('[data-tab="participation"]').click()
            expect(host.locator("#joint-vp-cancel")).to_have_text("结束联合组织")
            expect(host.locator("#joint-vp-leave")).not_to_be_visible()
            host.locator("#joint-vp-cancel").click()
            host.locator("#confirm-action").click()
            expect(host.locator("#joint-vp-create")).to_be_visible()
            expect(host.locator("#run-status")).to_have_text("比赛进行中")
            assert fixture["room"] is None and len(fixture["native"]) == 2
            assert not fixture["errors"], fixture["errors"]
            assert not fixture["external"], fixture["external"]
            print(json.dumps({"joint_vp_ui": "passed", "browsers": 3, "explicit_consent": True,
                              "member_read_only": True, "lost_start_not_retried": True,
                              "native_timing": True, "mobile": True, "inactive_polling_paused": True,
                              "end_organization_keeps_native_vp": True, "guarded_logout_keeps_account": True}))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
