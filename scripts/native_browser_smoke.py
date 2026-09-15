"""Exercise native participation in Edge using Flask in-memory routes only.

No listener, real QOJ connection, or native participation request is created.
Requires Playwright and Microsoft Edge, plus the project's Python dependencies.
"""
import json
import os
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT / "tests"))

from app import create_app
from qoj import QOJError
from test_native_api import NativeClient, PRIVATE_ATTACHMENT, PRIVATE_PROBLEM, PRIVATE_STANDING
from playwright.sync_api import expect, sync_playwright


class BrowserNative(NativeClient):
    def snapshot(self, mode, status):
        result = super().snapshot(mode, status)
        result["scoreboard"][0]["penalty"] = "5:40"
        result["scoreboard"][0]["problem_stats"]["1"].update(
            display="?3", text="?3", solved=False, pending=3)
        result.update(scoreboard_source="qoj-pc2" if mode == "official" else "qoj",
                      scoreboard_url="https://qoj.ac/results/QOJ1802?mode=pc2" if mode == "official"
                      else "https://qoj.ac/contest/1802/standings")
        result["actions"] = [] if status in ("running", "finished", "scheduled") else [{
            "id": "enter-vp" if mode == "vp" else "register",
            "label": "Start QOJ VP" if mode == "vp" else "Register official contest",
            "fields": [
                {"name": "choice", "label": "Time window", "type": "select", "default": "current",
                 "required": True, "options": [{"value": "current", "label": "Current contest"}]},
                {"name": "notify", "label": "Notify setting", "type": "checkbox", "default": True},
                {"name": "planned", "label": "Planned time", "type": "datetime-local",
                 "default": "2026-10-01T10:30", "min": "2026-10-01T00:00",
                 "max": "2026-10-02T00:00", "step": "60"},
                {"name": "count", "label": "Number setting", "type": "number",
                 "default": 2, "min": 1, "max": 5, "step": 1},
            ],
        }]
        if status == "scheduled":
            result.update(started_at=time.time() + 60, ends_at=time.time() + 3660)
        return result

    def problem(self, contest_id, problem_id):
        self.calls.append(("problem", contest_id, problem_id))
        return {**self.contest_data["problems"][0],
                "languages": [{"value": "C++17", "label": "C++17"}], "submission_available": True,
                "statement_html": "<!doctype html><html><head></head><body><h1>"
                                  + PRIVATE_PROBLEM + "</h1></body></html>"}


def assert_locked(page):
    expect(page.locator("#view-participation")).to_be_visible()
    expect(page.locator("#contest-title")).to_have_text("Contest #1802")
    for tab in ("overview", "problems", "attachments", "submissions", "scoreboard", "printing"):
        expect(page.locator(f'[data-tab="{tab}"]')).not_to_be_visible()
    for secret in (PRIVATE_PROBLEM, PRIVATE_ATTACHMENT, PRIVATE_STANDING, "Native QOJ Fixture"):
        assert secret not in page.locator("body").inner_text(), secret
        assert secret not in page.title(), secret
    assert not page.locator("#statement-frame").get_attribute("srcdoc")
    assert not page.locator("#statement-frame").get_attribute("src")
    assert page.locator("#attachments-table").inner_text() == ""
    assert page.locator("#prints-table").inner_text() == ""
    assert page.locator("#scoreboard-table").inner_text() == ""
    assert page.locator("#team-scoreboard").inner_text() == ""
    assert page.locator("#scoreboard-source-message").inner_text() == ""
    expect(page.locator("#first-blood-legend")).not_to_be_visible()
    expect(page.locator(".cell-first-blood")).to_have_count(0)
    expect(page.locator("#scoreboard-original")).not_to_be_visible()
    assert not page.locator("#scoreboard-original").get_attribute("href")


def assert_scoreboard_error(page, reason):
    expect(page.locator("#view-scoreboard")).to_be_visible()
    expect(page.locator("#scoreboard-source-message")).to_be_visible()
    expect(page.locator("#scoreboard-source-message")).to_contain_text("无法读取 QOJ 榜单")
    expect(page.locator("#scoreboard-source-message")).to_contain_text(reason)
    expect(page.locator("#scoreboard-table")).not_to_be_visible()
    expect(page.locator("#scoreboard-table")).to_have_text("")
    expect(page.locator("#scoreboard-refresh")).to_be_enabled()
    expect(page.locator('[data-tab="problems"]')).to_be_visible()
    expect(page.locator("#run-status")).to_have_text("比赛进行中")
    assert "暂无参赛队伍" not in page.locator("body").inner_text()
    assert "QOJ 榜单暂无本队记录" not in page.locator("body").inner_text()
    assert "[object Object]" not in page.locator("body").inner_text()
    assert "undefined" not in page.locator("body").inner_text()


def main():
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    with TemporaryDirectory() as temporary, sync_playwright() as p:
        upstream = BrowserNative(time.time() - 100)
        upstream.set_status("official", "unregistered")
        app = create_app(contest_id=1802, data_dir=Path(temporary), client_factory=lambda: upstream)
        static_override = os.environ.get("QOJ_FRONTEND_STATIC")
        if static_override:
            app.static_folder = str((ROOT / static_override).resolve())
        app.config.update(TESTING=True)
        captain_client, member_client = app.test_client(), app.test_client()

        def post(client, path, body):
            token = client.get("/api/state", base_url="http://qoj-vp.test").get_json()["csrf_token"]
            result = client.post(path, json=body, headers={"X-CSRF-Token": token}, base_url="http://qoj-vp.test")
            assert result.status_code == 200, result.data
            return result.get_json()

        team = post(captain_client, "/api/team/create", {"name": "Native browser team", "device_name": "Captain"})
        post(member_client, "/api/team/join", {"code": team["team"]["join_code"], "device_name": "Member"})
        browser = p.chromium.launch(channel="msedge", headless=True)
        seen, errors, pages = [], [], []
        state_response = {}

        def make_handler(client):
            def handler(route):
                request = route.request
                parsed = urlsplit(request.url)
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
                seen.append((request.method, path))
                if path == "/api/state" and state_response.get("error"):
                    route.fulfill(status=503, content_type="application/json",
                                  body=json.dumps({"error": state_response["error"]}))
                    return
                result = client.open(path, method=request.method, data=request.post_data_buffer, base_url="http://qoj-vp.test",
                                     headers={key: value for key, value in request.headers.items()
                                              if key.lower() not in ("host", "content-length", "cookie")})
                try:
                    body = result.get_data()
                    if path == "/api/state" and any(key in state_response for key in ("scoreboard", "demo")):
                        payload = result.get_json()
                        for key in ("scoreboard", "demo"):
                            if key in state_response:
                                payload[key] = state_response[key]
                        body = json.dumps(payload)
                    route.fulfill(status=result.status_code, body=body,
                                  headers={key: value for key, value in result.headers.items()
                                           if key.lower() not in ("content-length", "set-cookie")})
                finally:
                    result.close()
            return handler

        try:
            for client in (captain_client, member_client):
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.route("http://qoj-vp.test/**", make_handler(client))
                page = context.new_page()
                pages.append(page)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto("http://qoj-vp.test/#problems/1", wait_until="networkidle")
            captain, member = pages
            for page in pages:
                assert_locked(page)
            assert not any(path.startswith(("/api/problems/", "/api/attachments", "/api/prints"))
                           for _, path in seen), seen
            expect(member.locator("#participation-mode")).to_be_disabled()
            expect(member.locator("#participation-start-form")).not_to_be_visible()
            expect(captain.locator('[data-participation-field="choice"]')).to_have_value("current")
            expect(captain.locator('[data-participation-field="notify"]')).to_be_checked()
            expect(captain.locator('[data-participation-field="planned"]')).to_have_value("2026-10-01T10:30")
            expect(captain.locator('[data-participation-field="count"]')).to_have_value("2")
            expect(captain.locator('[data-participation-field="count"]')).to_have_attribute("min", "1")
            captain.locator('[data-participation-field="notify"]').uncheck()
            upstream.snapshots["vp"].update(entry_uncertain=True, message="Verify the previous request before retrying.")
            captain.locator("#participation-refresh").click()
            expect(captain.locator('[data-participation-field="notify"]')).not_to_be_checked()
            expect(captain.locator(".participation-uncertain")).to_be_visible()
            expect(captain.locator("#participation-start-button")).to_be_enabled()
            assert not any(call[0] == "enter" for call in upstream.calls)
            captain.locator("#participation-start-button").click()
            try:
                expect(captain.locator("#view-overview")).to_be_visible()
            except AssertionError:
                print(json.dumps({"start_error": captain.locator("#participation-error").text_content(),
                                  "message": captain.locator("#participation-message").text_content(),
                                  "browser_errors": errors,
                                  "enter_calls": [call for call in upstream.calls if call[0] == "enter"]}))
                raise
            entered = [call for call in upstream.calls if call[0] == "enter"]
            assert entered[-1][3]["values"]["notify"] is False, entered
            assert entered[-1][3]["values"]["choice"] == "current"
            expect(captain.locator("#metric-penalty")).to_have_text("5:40")
            expect(captain.locator("#finish-button")).not_to_be_visible()
            expect(captain.locator("#start-panel")).not_to_be_visible()
            captain.locator('[data-tab="scoreboard"]').click()
            expect(captain.locator("#scoreboard-table .upstream-score")).to_have_text("?3")
            expect(captain.locator("#scoreboard-table .penalty-cell")).to_have_text("5:40")
            expect(captain.locator("#scoreboard-table tbody td").first).to_have_text("?x")
            expect(captain.locator("#scoreboard-original")).to_have_attribute("href", "https://qoj.ac/contest/1802/standings")
            expect(captain.locator("#scoreboard-original")).to_have_attribute("rel", "noopener noreferrer")
            expect(captain.locator("#scoreboard-source-label")).to_have_text("QOJ 榜单")
            captain.screenshot(path=str(artifacts / "native-scoreboard-desktop.png"), full_page=True)
            normal_vp_board = upstream.snapshots["vp"]["scoreboard"]
            first_blood_cases = [
                ("First solve", "+1\n00:14", True, 0, True, False),
                ("Ordinary AC", "+1\n00:05", True, 0, False, False),
                ("Pending flag", "+2\n00:20", True, 1, True, False),
                ("Frozen text", "?1", True, 0, True, False),
                ("No submissions", "-", False, 0, False, False),
                ("Failed attempt", "-1", False, 0, False, True),
            ]
            first_blood_rows = [{**normal_vp_board[0], "team_name": name, "is_self": index == 0,
                                 "problem_stats": {"1": {"display": raw, "text": raw, "solved": solved,
                                                           "pending": pending, "first_blood": first, "failed": failed,
                                                           "attempts": 0 if raw == "-" else 1}}}
                                for index, (name, raw, solved, pending, first, failed) in enumerate(first_blood_cases)]
            upstream.snapshots["vp"].update(scoreboard=first_blood_rows, scoreboard_source="qoj-external",
                                             scoreboard_url="https://qoj.ac/results/QOJ1802")
            captain.locator("#scoreboard-refresh").click()
            expect(captain.locator("#scoreboard-source-label")).to_have_text("QOJ External Standings")
            expect(captain.locator("#scoreboard-original")).to_have_attribute("href", "https://qoj.ac/results/QOJ1802")
            expect(captain.locator("#first-blood-legend")).to_be_visible()
            blood = captain.locator("#scoreboard-table .cell-first-blood")
            expect(blood).to_have_count(1)
            expect(blood.locator(".upstream-score")).to_have_text("+1\n00:14")
            expect(blood).to_have_css("background-color", "rgb(39, 128, 57)")
            expect(blood).to_have_css("color", "rgb(255, 255, 255)")
            expect(blood).to_have_attribute("aria-label", "+1\n00:14\nFirst to solve / 一血")
            expect(captain.locator("#scoreboard-table .cell-accepted:not(.cell-first-blood)")).to_have_text("+1\n00:05")
            expect(captain.locator("#scoreboard-table .cell-accepted:not(.cell-first-blood)")).to_have_css("background-color", "rgb(136, 217, 112)")
            expect(captain.locator("#scoreboard-table .cell-pending")).to_have_text(["+2\n00:20", "?1"])
            for cell in captain.locator("#scoreboard-table .cell-pending").all():
                expect(cell).to_have_css("background-color", "rgb(158, 203, 242)")
            expect(captain.locator("#scoreboard-table .cell-empty")).to_have_text("-")
            expect(captain.locator("#scoreboard-table .cell-empty")).to_have_css("background-color", "rgb(255, 255, 255)")
            expect(captain.locator("#scoreboard-table .cell-wrong")).to_have_text("-1")
            expect(captain.locator("#scoreboard-table .cell-wrong")).to_have_css("background-color", "rgb(244, 170, 165)")
            expect(captain.locator("#team-scoreboard .cell-first-blood")).to_have_count(1)
            expect(captain.locator("#team-scoreboard .cell-first-blood")).to_have_css("background-color", "rgb(39, 128, 57)")
            captain.screenshot(path=str(artifacts / "native-first-blood-desktop.png"), full_page=True)
            captain.set_viewport_size({"width": 390, "height": 844})
            captain.locator("#scoreboard-table").evaluate("element => { element.scrollLeft = element.scrollWidth; }")
            assert captain.evaluate("document.documentElement.scrollWidth <= innerWidth")
            for item in captain.locator("#scoreboard-footer .legend-item:visible").all():
                bounds = item.bounding_box()
                assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390, bounds
            captain.screenshot(path=str(artifacts / "native-first-blood-mobile.png"), full_page=True)
            captain.set_viewport_size({"width": 1440, "height": 1000})
            first_blood_rows[-1]["problem_stats"]["1"]["failed"] = False
            upstream.snapshots["vp"].update(scoreboard_source="qoj-pc2", scoreboard_url="https://qoj.ac/results/QOJ1802?mode=pc2")
            captain.locator("#scoreboard-refresh").click()
            expect(captain.locator("#scoreboard-source-label")).to_have_text("QOJ PC2 榜单")
            expect(captain.locator("#scoreboard-original")).to_have_attribute("href", "https://qoj.ac/results/QOJ1802?mode=pc2")
            expect(captain.locator(".cell-first-blood")).to_have_count(0)
            expect(captain.locator("#first-blood-legend")).not_to_be_visible()
            expect(captain.locator("#scoreboard-table .cell-accepted")).to_have_count(2)
            expect(captain.locator("#scoreboard-table .cell-wrong")).to_have_text(["-", "-1"])
            for cell in captain.locator("#scoreboard-table .cell-accepted").all():
                expect(cell).to_have_css("background-color", "rgb(136, 217, 112)")
            upstream.snapshots["vp"].update(scoreboard_source="qoj", scoreboard_url="https://qoj.ac/contest/1802/standings")
            captain.locator("#scoreboard-refresh").click()
            expect(captain.locator("#scoreboard-source-label")).to_have_text("QOJ 榜单")
            expect(captain.locator("#scoreboard-table .cell-wrong")).to_have_text(["-", "-1"])
            expect(captain.locator("#scoreboard-table .cell-first-blood")).to_have_count(1)
            expect(captain.locator("#team-scoreboard .cell-first-blood")).to_have_count(1)
            expect(captain.locator("#scoreboard-table .cell-first-blood")).to_have_css("background-color", "rgb(39, 128, 57)")
            expect(captain.locator("#first-blood-legend")).to_be_visible()
            state_response["demo"] = True
            captain.locator("#scoreboard-refresh").click()
            expect(captain.locator(".cell-first-blood")).to_have_count(0)
            expect(captain.locator("#first-blood-legend")).not_to_be_visible()
            state_response.clear()
            upstream.snapshots["vp"]["scoreboard"] = normal_vp_board
            captain.locator("#scoreboard-refresh").click()
            expect(captain.locator("#scoreboard-table .upstream-score")).to_have_text("?3")
            member.locator("#participation-refresh").click()
            member.locator('[data-tab="problems"]').click()
            expect(member.frame_locator("#statement-frame").locator("h1")).to_have_text(PRIVATE_PROBLEM)
            expect(member.locator("#submit-button")).not_to_be_visible()
            captain.locator('[data-tab="attachments"]').click()
            expect(captain.locator("#attachments-table")).to_contain_text(PRIVATE_ATTACHMENT)
            captain.locator('[data-tab="printing"]').click()
            captain.locator("#print-mode").select_option("text")
            captain.locator("#print-name").fill("vp-private.txt")
            captain.locator("#print-content").fill("Private VP notes")
            captain.locator("#print-upload-button").click()
            expect(captain.locator("#prints-table")).to_contain_text("vp-private.txt")
            captain.locator('[data-tab="participation"]').click()
            captain.locator("#participation-mode").select_option("official")
            expect(captain.locator("#participation-mode")).to_have_value("official")
            assert_locked(captain)
            assert len([call for call in upstream.calls if call[0] == "enter"]) == 1
            member.locator('[data-tab="participation"]').click()
            member.locator("#participation-refresh").click()
            assert_locked(member)
            captain.locator("#participation-start-button").click()
            expect(captain.locator("#run-status")).to_have_text("等待开赛")
            assert_locked(captain)
            upstream.set_status("official", "running")
            captain.locator("#participation-refresh").click()
            expect(captain.locator('[data-tab="printing"]')).to_be_visible()
            captain.locator('[data-tab="printing"]').click()
            expect(captain.locator("#prints-empty")).to_be_visible()
            expect(captain.locator("#prints-table")).not_to_contain_text("vp-private.txt")
            captain.locator('[data-tab="scoreboard"]').click()
            reason = "QOJ fixture standings failed (HTTP 403): <b>restricted</b>"
            upstream.snapshots["official"].update(scoreboard=[], scoreboard_available=False,
                                                   scoreboard_message=reason)
            captain.locator("#scoreboard-refresh").click()
            assert_scoreboard_error(captain, reason)
            expect(captain.locator("#scoreboard-source-message b")).to_have_count(0)
            captain.locator('[data-tab="overview"]').click()
            expect(captain.locator("#team-scoreboard")).to_contain_text("无法读取 QOJ 榜单")
            expect(captain.locator("#team-scoreboard")).to_contain_text(reason)
            expect(captain.locator("#metric-penalty")).to_have_text("—")
            captain.locator("#team-scoreboard-refresh").click()
            expect(captain.locator("#team-scoreboard")).to_contain_text(reason)
            member.locator("#participation-refresh").click()
            member.locator('[data-tab="scoreboard"]').click()
            assert_scoreboard_error(member, reason)
            expect(member.locator("#scoreboard-original")).to_be_visible()
            expect(member.locator("#scoreboard-original")).to_have_attribute("href", "https://qoj.ac/results/QOJ1802?mode=pc2")
            expect(member.locator("#scoreboard-source-label")).to_have_text("QOJ PC2 榜单")
            request_count = len(seen)
            member.locator("#scoreboard-refresh").click()
            expect(member.locator("#scoreboard-refresh")).to_be_enabled()
            assert ("GET", "/api/state") in seen[request_count:]
            assert all(method == "GET" for method, _ in seen[request_count:]), seen[request_count:]
            member.set_viewport_size({"width": 390, "height": 844})
            assert member.evaluate("document.documentElement.scrollWidth <= innerWidth")
            member.screenshot(path=str(artifacts / "native-scoreboard-error-mobile.png"), full_page=True)
            captain.locator('[data-tab="scoreboard"]').click()
            captain.screenshot(path=str(artifacts / "native-scoreboard-error-desktop.png"), full_page=True)

            upstream.set_status("official", "running", scoreboard_available=True, scoreboard_message="")
            captain.locator("#scoreboard-refresh").click()
            expect(captain.locator("#scoreboard-source-message")).not_to_be_visible()
            expect(captain.locator("#scoreboard-table .upstream-score")).to_have_text("?3")
            expect(captain.locator("#metric-penalty")).to_have_text("5:40")
            normal_board = upstream.snapshots["official"]["scoreboard"]
            pc2_cases = [("7/--", "wrong", "rgb(244, 170, 165)", False, 0, True),
                         ("3/--", "pending", "rgb(158, 203, 242)", False, 3, False),
                         ("2/14", "accepted", "rgb(136, 217, 112)", True, 0, False)]
            pc2_rows = [{**normal_board[0], "team_name": f"PC2 {kind}", "is_self": index == 0,
                         "problem_stats": {"1": {"display": raw, "text": raw, "solved": solved,
                                                   "pending": pending, "failed": failed, "first_blood": True}}}
                        for index, (raw, kind, _, solved, pending, failed) in enumerate(pc2_cases)]
            upstream.snapshots["official"]["scoreboard"] = pc2_rows
            captain.locator("#scoreboard-refresh").click()
            expect(captain.locator(".cell-first-blood")).to_have_count(0)
            expect(captain.locator("#first-blood-legend")).not_to_be_visible()
            for raw, kind, background, *_ in pc2_cases:
                cell = captain.locator(f"#scoreboard-table .cell-{kind}")
                expect(cell).to_have_text(raw)
                expect(cell).to_have_css("background-color", background)
            captain.screenshot(path=str(artifacts / "native-pc2-scoreboard-desktop.png"), full_page=True)
            pc2_rows[1]["problem_stats"]["1"]["failed"] = True
            pc2_rows[2]["problem_stats"]["1"].update(failed=True, pending=1)
            captain.locator("#scoreboard-refresh").click()
            expect(captain.locator("#scoreboard-table .cell-pending")).to_have_text(["3/--", "2/14"])
            expect(captain.locator("#scoreboard-table .cell-accepted")).to_have_count(0)
            upstream.snapshots["official"]["scoreboard"] = normal_board
            for unsafe_url in (None, "javascript:alert(1)", "http://qoj.ac/contest/1802/standings",
                               "https://qoj.ac.example/results/QOJ1802?mode=pc2",
                               "https://user:pass@qoj.ac/results/QOJ1802?mode=pc2",
                               "https://qoj.ac:444/results/QOJ1802?mode=pc2"):
                upstream.snapshots["official"]["scoreboard_url"] = unsafe_url
                captain.locator("#scoreboard-refresh").click()
                expect(captain.locator("#scoreboard-original")).not_to_be_visible()
                assert not captain.locator("#scoreboard-original").get_attribute("href")
            upstream.snapshots["official"]["scoreboard_url"] = "https://qoj.ac/results/QOJ1802?mode=pc2"
            captain.locator("#scoreboard-refresh").click()
            expect(captain.locator("#scoreboard-original")).to_be_visible()
            for malformed in ({}, [None], [{"rank": []}], [{"problem_stats": []}],
                              [{"problem_stats": {"1": {"display": {}}}}]):
                state_response["scoreboard"] = malformed
                captain.locator("#scoreboard-refresh").click()
                assert_scoreboard_error(captain, "榜单格式不正确")
            state_response.clear()
            state_response["error"] = "Offline fixture: state request unavailable"
            captain.locator("#scoreboard-refresh").click()
            assert_scoreboard_error(captain, state_response["error"])
            state_response.clear()
            captain.locator("#scoreboard-refresh").click()
            expect(captain.locator("#scoreboard-table .upstream-score")).to_have_text("?3")
            expect(captain.locator("#scoreboard-source-message")).not_to_be_visible()
            upstream.snapshots["official"]["scoreboard"] = []
            captain.locator("#scoreboard-refresh").click()
            expect(captain.locator("#scoreboard-table")).to_contain_text("暂无参赛队伍")
            expect(captain.locator("#scoreboard-source-message")).not_to_be_visible()
            captain.locator('[data-tab="overview"]').click()
            expect(captain.locator("#team-scoreboard")).to_contain_text("QOJ 榜单暂无本队记录")
            expect(captain.locator("#metric-penalty")).to_have_text("—")
            assert "NaN" not in captain.locator("body").inner_text()
            upstream.error = QOJError("Offline fixture status unavailable", 502)
            captain.locator('[data-tab="participation"]').click()
            captain.locator("#participation-refresh").click()
            expect(captain.locator("#participation-status-title")).to_contain_text("无法确认")
            assert_locked(captain)
            captain.set_viewport_size({"width": 390, "height": 844})
            assert captain.evaluate("document.documentElement.scrollWidth <= innerWidth")
            captain.screenshot(path=str(artifacts / "native-waiting-mobile.png"), full_page=True)
            assert not errors, errors
            print(json.dumps({"native_browser": "passed", "roles": 2, "listener_started": False,
                              "checks": ["prestart gate", "field defaults", "unchecked checkbox false",
                                         "query-only mode switch", "upstream scores", "scheduled gate",
                                         "member permissions", "old cache cleared", "print mode isolation",
                                         "null scores", "scoreboard-only failure", "member read-only retry",
                                         "scoreboard recovery", "malformed scoreboard", "state request failure",
                                         "upstream scoreboard links", "unsafe source URLs rejected",
                                         "PC2 raw results and colors", "PC2 status color precedence",
                                         "external and native first blood", "default external URL preserved", "pending overrides first blood", "overview first blood",
                                         "external empty dash", "external failed marker", "legacy negative result text",
                                         "PC2 and demo ignore first blood", "first blood source switching", "mobile legend",
                                         "successful empty scoreboard", "upstream failure gate", "mobile layout"]}))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
