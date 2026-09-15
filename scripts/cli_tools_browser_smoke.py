"""Exercise the CLI configuration panel with synthetic in-memory demo routes.

No listener, QOJ connection, real credentials, or system clipboard is used.
Set QOJ_FRONTEND_STATIC=frontend-dist to test a staged frontend build.
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

ORIGIN = "http://qoj-cli.test"


def main():
    with TemporaryDirectory() as temporary, sync_playwright() as playwright:
        app = create_app(contest_id=1, demo=True, data_dir=Path(temporary))
        app.config.update(TESTING=True)
        static_override = os.environ.get("QOJ_FRONTEND_STATIC")
        if static_override:
            app.static_folder = str((ROOT / static_override).resolve())
        client = app.test_client()
        fixture = {"enabled": False, "created_at": None, "generation": 0,
                   "delay": False, "fail_generation": False, "role": None, "real_api": False}
        requests, deferred, errors = [], [], []
        browser = playwright.chromium.launch(channel="msedge", headless=True)

        def handler(route):
            request = route.request
            parsed = urlsplit(request.url)
            if parsed.netloc != "qoj-cli.test":
                route.abort()
                return
            path = parsed.path
            requests.append((request.method, path))
            if path == "/api/cli" and not fixture["real_api"]:
                route.fulfill(json={"enabled": fixture["enabled"], "created_at": fixture["created_at"]})
                return
            if path == "/api/cli/token" and not fixture["real_api"]:
                if fixture["fail_generation"]:
                    route.fulfill(status=500, json={"error": "fixture-only-credential-must-not-be-rendered"})
                    return
                fixture["generation"] += 1
                fixture["enabled"] = True
                fixture["created_at"] = 1700000000 + fixture["generation"]
                state = client.get("/api/state", base_url=ORIGIN).get_json()
                fixture["config"] = {
                    "version": 1, "server_url": ORIGIN,
                    "token": f"fixture-only-token-{fixture['generation']}",
                    "contest_id": state["contest"]["id"],
                    "team_id": state["team"]["id"], "team_name": state["team"]["name"],
                }
                response = {"config": fixture["config"], "created_at": fixture["created_at"]}
                if fixture["delay"]:
                    deferred.append((route, response))
                else:
                    route.fulfill(json=response)
                return
            if path == "/api/cli/revoke" and not fixture["real_api"]:
                fixture["enabled"], fixture["created_at"] = False, None
                route.fulfill(json={"enabled": False})
                return
            result = client.open(path + ("?" + parsed.query if parsed.query else ""),
                                 method=request.method, data=request.post_data_buffer,
                                 base_url=ORIGIN, headers={key: value for key, value in request.headers.items()
                                 if key.lower() not in ("host", "content-length", "cookie")})
            try:
                body = result.get_data()
                if path == "/api/state" and fixture["role"]:
                    payload = result.get_json()
                    payload["team"]["role"] = fixture["role"]
                    payload["permissions"].update(manage=False, submit=False, print=False)
                    payload["team"].pop("join_code", None)
                    body = json.dumps(payload)
                route.fulfill(status=result.status_code, body=body,
                              headers={key: value for key, value in result.headers.items()
                                       if key.lower() not in ("content-length", "set-cookie")})
            finally:
                result.close()

        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
            context.route("**/*", handler)
            context.add_init_script("""(() => {
                window.cliBlobRevocations = [];
                const revoke = URL.revokeObjectURL.bind(URL);
                URL.revokeObjectURL = value => { window.cliBlobRevocations.push(value); revoke(value); };
                Object.defineProperty(navigator, 'clipboard', { value: {
                    writeText: () => { throw new Error('CLI setup must not write to the clipboard'); }
                }});
            })();""")
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(ORIGIN + "/#team", wait_until="networkidle")
            page.locator('[data-tab="team"]').click()
            expect(page.locator("#team-create-button")).to_be_visible()
            expect(page.locator("#cli-tools-panel")).to_have_count(0)
            assert not any(path.startswith("/api/cli") for _, path in requests)

            page.locator("#team-create-name").fill("CLI offline test team")
            page.locator("#team-create-button").click()
            expect(page.locator("#cli-status")).to_have_text("尚未启用")
            expect(page.locator("#cli-generate")).to_be_enabled()
            expect(page.locator("#cli-revoke")).to_be_disabled()
            expect(page.locator("#cli-tools-panel")).to_contain_text("虚拟打印不会连接实体打印机")

            fixture["delay"] = True
            page.locator("#cli-generate").click()
            expect(page.locator("#cli-generate")).to_be_disabled()
            assert len(deferred) == 1
            assert requests.count(("POST", "/api/cli/token")) == 1
            expect(page.locator("#confirm-dialog")).not_to_be_visible()
            delayed_route, response = deferred.pop()
            delayed_route.fulfill(json=response)
            fixture["delay"] = False
            expect(page.locator("#cli-download")).to_be_visible()
            token = fixture["config"]["token"]
            assert token not in page.content()
            assert page.evaluate("Object.keys(localStorage).length") == 0
            first_blob = page.locator("#cli-download").get_attribute("href")
            with page.expect_download() as downloaded:
                page.locator("#cli-download").click()
            download = downloaded.value
            assert download.suggested_filename == "qoj-cli.json"
            assert json.loads(Path(download.path()).read_text(encoding="utf-8")) == fixture["config"]

            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.locator('[data-tab="participation"]').click()
            page.locator('[data-tab="team"]').click()
            expect(page.locator("#cli-generate")).to_be_enabled()
            expect(page.locator("#cli-download")).to_have_attribute("href", first_blob)

            page.locator("#cli-generate").click()
            expect(page.locator("#confirm-message")).to_contain_text("旧配置将立即失效")
            page.locator('#confirm-dialog button[data-close="confirm-dialog"]').last.click()
            assert requests.count(("POST", "/api/cli/token")) == 1
            expect(page.locator("#cli-download")).to_have_attribute("href", first_blob)
            page.locator("#cli-generate").click()
            page.locator("#confirm-action").click()
            expect(page.locator("#cli-download")).not_to_have_attribute("href", first_blob)
            assert first_blob in page.evaluate("window.cliBlobRevocations")
            assert requests.count(("POST", "/api/cli/token")) == 2

            # An HTTP error containing synthetic credential text must remain private.
            fixture["fail_generation"] = True
            page.locator("#cli-generate").click()
            page.locator("#confirm-action").click()
            expect(page.locator("#confirm-error")).to_be_visible()
            expect(page.locator("#cli-download")).to_have_count(0)
            assert "fixture-only-credential-must-not-be-rendered" not in page.content()
            page.locator('#confirm-dialog button[data-close="confirm-dialog"]').last.click()
            fixture["fail_generation"] = False
            page.locator("#cli-revoke").click()
            expect(page.locator("#confirm-message")).to_contain_text("无法再使用 submit 或 printfile")
            page.locator("#confirm-action").click()
            expect(page.locator("#cli-status")).to_have_text("尚未启用")
            expect(page.locator("#cli-revoke")).to_be_disabled()

            # A new response must not bring the token back after captain access is lost.
            fixture["delay"] = True
            page.locator("#cli-generate").click()
            expect(page.locator("#cli-generate")).to_be_disabled()
            assert len(deferred) == 1
            fixture["role"] = "member"
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            expect(page.locator("#cli-tools-panel")).to_have_count(0)
            delayed_route, response = deferred.pop()
            try:
                delayed_route.fulfill(json=response)
            except Exception as failure:
                assert "already handled" in str(failure).lower() or "closed" in str(failure).lower(), str(failure)
            expect(page.locator("#cli-download")).to_have_count(0)
            assert fixture["config"]["token"] not in page.content()
            cli_count = sum(path.startswith("/api/cli") for _, path in requests)
            page.reload(wait_until="networkidle")
            page.locator('[data-tab="team"]').click()
            expect(page.locator("#cli-tools-panel")).to_have_count(0)
            assert cli_count == sum(path.startswith("/api/cli") for _, path in requests)

            # Verify the real Flask credential routes and CSRF contract as well.
            # The backend is still an isolated demo with only synthetic team data.
            fixture.update(real_api=True, role=None, delay=False)
            page.reload(wait_until="networkidle")
            expect(page.locator("#cli-status")).to_have_text("尚未启用")
            page.locator("#cli-generate").click()
            expect(page.locator("#cli-download")).to_be_visible()
            with page.expect_download() as downloaded:
                page.locator("#cli-download").click()
            config = json.loads(Path(downloaded.value.path()).read_text(encoding="utf-8"))
            assert config["server_url"] == ORIGIN
            assert config["version"] == 1 and config["contest_id"] == 1
            assert config["team_name"] == "CLI offline test team"
            assert config["token"].startswith("qojcli_")
            assert config["token"] not in page.content()
            page.reload(wait_until="networkidle")
            expect(page.locator("#cli-status")).to_have_text("已启用")
            expect(page.locator("#cli-download")).to_have_count(0)
            page.locator("#cli-revoke").click()
            page.locator("#confirm-action").click()
            expect(page.locator("#cli-status")).to_have_text("尚未启用")
            assert client.get("/api/cli", base_url=ORIGIN).get_json()["enabled"] is False
            assert not errors, errors
            print("CLI configuration browser smoke passed: captain-only setup, download, rotation, revocation, redacted errors, mobile layout, stale-response isolation, and real offline-demo credential endpoints.")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
