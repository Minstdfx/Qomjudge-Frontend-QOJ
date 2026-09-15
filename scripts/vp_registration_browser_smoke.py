"""Exercise a two-step native VP registration with a synthetic requests adapter.

The default fixture uses the register_vp form structure supplied by the user.
A separate synthetic checkbox variant tests explicit choices without assuming
those extra fields are present on the actual QOJ registration page.
Edge uses in-memory Flask routes only. No real QOJ registration, existing account,
listening service, or persistent user data is touched.
Set QOJ_FRONTEND_STATIC=frontend-dist to test a staged Vue build.
"""
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT / "tests"))

from app import create_app
from qoj import QOJClient
from playwright.sync_api import expect, sync_playwright
from test_vp_registration_flow import VirtualRegistrationAdapter

ORIGIN = "http://qoj-vp-register.test"
CONTENT_TABS = ("overview", "problems", "attachments", "submissions", "scoreboard", "printing")


def assert_locked(page):
    expect(page.locator("#view-participation")).to_be_visible()
    expect(page.locator("#contest-title")).to_have_text("Contest #9")
    for tab in CONTENT_TABS:
        expect(page.locator(f'[data-tab="{tab}"]')).not_to_be_visible()
    expect(page.locator("#submit-button")).not_to_be_visible()
    expect(page.locator("#scoreboard-table")).to_have_text("")
    expect(page.locator("#team-scoreboard")).to_have_text("")
    assert not page.locator("#statement-frame").get_attribute("src")
    assert not page.locator("#statement-frame").get_attribute("srcdoc")


def main(with_checkbox=False):
    with TemporaryDirectory() as temporary, sync_playwright() as playwright:
        transport = VirtualRegistrationAdapter(with_checkbox=with_checkbox)
        upstream = QOJClient()
        upstream.session.adapters.clear()
        upstream.session.mount("https://qoj.ac/", transport)
        upstream.authenticated, upstream.username = True, transport.username
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

        team = post(captain_client, "/api/team/create", {"name": "Two-step VP offline team", "device_name": "Captain"})
        post(member_client, "/api/team/join", {"code": team["team"]["join_code"], "device_name": "Member"})
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        seen, errors, states = [], [], {}

        def make_handler(client, role):
            def handler(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.netloc != "qoj-vp-register.test":
                    route.abort()
                    return
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
                seen.append((role, request.method, path))
                response = client.open(path, method=request.method, data=request.post_data_buffer, base_url=ORIGIN,
                                       headers={key: value for key, value in request.headers.items()
                                                if key.lower() not in ("host", "content-length", "cookie")})
                try:
                    if parsed.path in ("/api/state", "/api/start") and response.status_code == 200:
                        result = response.get_json()
                        if result and "participation" in result:
                            states[role] = result
                    route.fulfill(status=response.status_code, body=response.get_data(),
                                  headers={key: value for key, value in response.headers.items()
                                           if key.lower() not in ("content-length", "set-cookie")})
                finally:
                    response.close()
            return handler

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
            assert not transport.posts

            with captain.expect_response(ORIGIN + "/api/start") as started:
                captain.locator("#participation-start-button").click()
            assert started.value.status == 200, started.value.text()
            confirmation = started.value.json()
            assert confirmation["participation"]["status"] == "unregistered"
            assert confirmation["participation"]["entry_stage"] == "confirmation"
            assert confirmation["run"] is None
            assert not confirmation["permissions"]["view_content"]
            assert len(transport.posts) == 1 and not transport.entered
            assert urlsplit(transport.posts[0].url).path == "/contest/9"
            expect(captain).to_have_url(ORIGIN + "/#participation")
            expect(captain.locator("#participation-status-title")).to_have_text("确认 QOJ VP 报名")
            expect(captain.locator("#participation-error")).not_to_be_visible()
            expect(captain.locator("#participation-start-form")).to_be_visible()
            assert_locked(captain)
            action = confirmation["participation"]["actions"][0]
            checkbox_fields = [field for field in action["fields"] if field["type"] == "checkbox"]
            if with_checkbox:
                assert checkbox_fields, "The synthetic variant must require an explicit checkbox choice"
            else:
                assert not action["fields"], "The user-supplied register_vp form has no visible fields"
            for field in checkbox_fields:
                expect(captain.locator(f'[data-participation-field="{field["name"]}"]')).not_to_be_checked()
            assert transport.latest_confirmation_token not in captain.content()

            if any(field.get("required") for field in checkbox_fields):
                # The browser must not silently agree or submit a required unchecked field.
                captain.locator("#participation-start-button").click()
                assert len(transport.posts) == 1
                assert len([item for item in seen if item[1:] == ("POST", "/api/start")]) == 1
                expect(captain.locator("#participation-fields input:invalid")).not_to_have_count(0)

            member.locator("#participation-refresh").click()
            expect(member.locator("#participation-status-title")).to_have_text("确认 QOJ VP 报名")
            expect(member.locator("#participation-message")).to_contain_text("队长正在确认")
            expect(member.locator("#participation-start-form")).not_to_be_visible()
            assert_locked(member)
            assert states["member"]["run"] is None
            assert not any(path.startswith(("/api/problems/", "/api/attachments", "/api/prints")) for _, _, path in seen)

            for field in checkbox_fields:
                captain.locator(f'[data-participation-field="{field["name"]}"]').check()
            with captain.expect_response(ORIGIN + "/api/state"):
                captain.locator("#participation-refresh").click()
            expect(captain.locator("#participation-refresh")).to_be_enabled()
            expect(captain.locator("#participation-status-title")).to_have_text("确认 QOJ VP 报名")
            for field in checkbox_fields:
                expect(captain.locator(f'[data-participation-field="{field["name"]}"]')).to_be_checked()
            assert len(transport.posts) == 1 and not transport.entered

            with captain.expect_response(ORIGIN + "/api/start") as confirmed:
                captain.locator("#participation-start-button").click()
            assert confirmed.value.status == 200, confirmed.value.text()
            running = confirmed.value.json()
            assert running["participation"]["status"] == "running"
            assert running["run"] is not None
            assert transport.entered and len(transport.posts) == 2
            assert urlsplit(transport.posts[1].url).path == "/contest/9/virtual/register"
            raw = transport.posts[1].body
            posted = parse_qs(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            assert posted.get("submit-register_vp") == ["register_vp"]
            if with_checkbox:
                assert posted.get("agree") == ["yes"]  # This extra field belongs only to the synthetic variant.
            else:
                assert set(posted) == {"_token", "submit-register_vp"}
            expect(captain.locator("#view-overview")).to_be_visible()
            expect(captain).to_have_url(ORIGIN + "/#overview")
            expect(captain.locator("#run-status")).to_have_text("比赛进行中")
            expect(captain.locator("#participation-start-form")).not_to_be_visible()
            expect(captain.locator("#submit-button")).to_be_visible()
            run_id = running["run"]["id"]

            member.locator("#participation-refresh").click()
            expect(member.locator("#participation-enter")).to_be_visible()
            expect(member.locator('[data-tab="problems"]')).to_be_visible()
            expect(member.locator('[data-tab="scoreboard"]')).to_be_visible()
            expect(member.locator("#submit-button")).not_to_be_visible()
            assert states["member"]["run"]["id"] == run_id
            member.locator('[data-tab="scoreboard"]').click()
            expect(member.locator("#scoreboard-table")).to_contain_text("?4")
            assert len(transport.posts) == 2
            assert [(role, method) for role, method, path in seen if path == "/api/start"] == [("captain", "POST"), ("captain", "POST")]
            assert not errors, errors
            variant = "synthetic checkbox" if with_checkbox else "user-supplied register_vp structure"
            print(f"Offline two-step VP browser smoke passed ({variant}): Start opens confirmation, no automatic registration, read-only member gating, and confirmed shared run after the second POST.")
        finally:
            browser.close()
            upstream.session.close()


if __name__ == "__main__":
    main()
    main(with_checkbox=True)
