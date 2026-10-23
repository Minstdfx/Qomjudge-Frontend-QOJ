"""Verify account transitions and state refresh races without a server or real credentials."""
import json
import os
import subprocess
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
from test_native_api import NativeClient, PRIVATE_PROBLEM, PRIVATE_STANDING
from playwright.sync_api import expect, sync_playwright


def check_store_races():
    """Exercise the actual Vue store with deferred, deliberately reordered responses."""
    source = r"""
import assert from 'node:assert/strict';
import { watch } from 'vue';
import { createContestStore } from './frontend/src/composables/useContest.js';
globalThis.location = { hash: '#participation' };
globalThis.history = { replaceState() {} };
globalThis.window = { addEventListener() {}, removeEventListener() {} };
globalThis.document = { addEventListener() {}, removeEventListener() {} };
const snapshot = username => ({ contest: { id: 1, problems: [] }, demo: false,
  user: { authenticated: true, username },
  team: { id: 'account-fixture-team', role: 'captain' },
  permissions: { manage: true, view_content: true, submit: true },
  participation: { mode: 'vp', status: 'running', content_available: true,
    can_submit: true, started_at: 1, ends_at: 9999999999 },
  run: { id: username }, scoreboard: [], server_time: Date.now() / 1000 });
const c = createContestStore();
const requests = [];
c.api = path => new Promise(resolve => requests.push({ path, resolve }));
try {
  const first = c.refreshState();
  const freshA = c.refreshState(true), freshB = c.refreshState(true);
  requests[0].resolve(snapshot('initial'));
  await first; await Promise.resolve();
  assert.equal(requests.length, 2, 'Concurrent refreshes must share one follow-up');
  requests[1].resolve(snapshot('latest'));
  await Promise.all([freshA, freshB]);
  assert.equal(c.state.user.username, 'latest');
  let contextChanges = 0;
  const stop = watch(() => c.context, () => contextChanges++, { flush: 'sync' });
  c.accountOpen = true;
  c.adoptState({ ...snapshot('latest'), scoreboard: [{ rank: 2 }], server_time: Date.now()/1000 });
  assert.equal(contextChanges, 0, 'Ordinary result polling must not clear form context');
  assert.equal(c.accountOpen, true);
  stop();

  const inFlight = c.refreshState();
  const pendingFresh = c.refreshState(true);
  const connecting = c.runAccountAction('connect', { cookie: 'fixture=only' });
  assert.equal(c.canRead, false, 'Account changes immediately close the content gate');
  assert.equal(requests.length, 3, 'Connect must wait for the old state request');
  requests[2].resolve(snapshot('latest'));
  await inFlight; await Promise.resolve();
  assert.equal(requests.length, 4);
  assert.equal(requests[3].path, '/api/connect');
  requests[3].resolve(snapshot('new-account'));
  await Promise.all([connecting, pendingFresh]);
  assert.equal(requests.length, 4, 'Account response should not trigger a redundant state request');
  assert.equal(c.state.user.username, 'new-account');
  assert.equal(c.canRead, true);

  // Demo API responses intentionally use the store's local participation fallback.
  c.api = async () => ({ ...snapshot('Demo'), demo: true, participation: undefined,
    run: { id: 'demo-run', status: 'running', started_at: 1, ends_at: 9999999999 } });
  await c.refreshContest();
  assert.equal(c.state.demo, true);
  assert.equal(c.part.status, 'running');
  assert.equal(c.canRead, true);
} finally { c.dispose(); }
console.log('state refresh races passed');
"""
    result = subprocess.run(["node", "--input-type=module"], input=source, text=True,
                            cwd=ROOT, capture_output=True)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)


class AccountClient(NativeClient):
    def __init__(self):
        super().__init__(time.time() - 100)
        self.connect_as("account-a")

    def connect_as(self, username):
        self.authenticated = True
        self.username = username
        self.contest_data["title"] = f"Private contest for {username}"
        self.set_status("vp", "running")

    def clear(self):
        super().clear()
        self.calls.append(("clear",))

    def import_cookies(self, cookie):
        self.clear()
        if cookie not in {"fixture=account-a", "fixture=account-b"}:
            raise QOJError("Fixture cookie rejected after upstream session changed", 401)
        self.connect_as(cookie.split("=", 1)[1])

    def participation(self, contest_id, mode="vp"):
        result = super().participation(contest_id, mode)
        if not self.authenticated:
            result.update(authenticated=False, username="", status="unavailable",
                          content_available=False, can_submit=False, started_at=None,
                          ends_at=None, actions=[], scoreboard=[], message="Fixture account disconnected")
        return result


def main():
    check_store_races()
    with TemporaryDirectory() as temporary, sync_playwright() as playwright:
        upstream = AccountClient()
        app = create_app(contest_id=1802, data_dir=Path(temporary), client_factory=lambda: upstream)
        static_override = os.environ.get("QOJ_FRONTEND_STATIC")
        if static_override:
            app.static_folder = str((ROOT / static_override).resolve())
        app.config["TESTING"] = True
        client = app.test_client()
        state_failure = {"enabled": False}
        requests, errors = [], []
        # The development proxy preserves this browser Host and Origin pair.
        origin = "http://127.0.0.1:5173"
        initial = client.get("/api/state", base_url=origin).get_json()
        created = client.post("/api/team/create", base_url=origin,
                              json={"name": "Account browser team", "device_name": "Account test captain"},
                              headers={"X-CSRF-Token": initial["csrf_token"], "Origin": origin})
        assert created.status_code == 200, created.get_data(as_text=True)
        assert created.get_json()["team"]["role"] == "captain"

        def handler(route):
            request = route.request
            parsed = urlsplit(request.url)
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
            requests.append((request.method, path))
            if path == "/api/state" and state_failure["enabled"]:
                route.fulfill(status=503, content_type="application/json",
                              body=json.dumps({"error": "Fixture state unavailable after account mutation"}))
                return
            result = client.open(path, method=request.method, data=request.post_data_buffer, base_url=origin,
                                 headers={key: value for key, value in request.headers.items()
                                          if key.lower() not in {"host", "content-length", "cookie"}})
            try:
                route.fulfill(status=result.status_code, body=result.get_data(),
                              headers={key: value for key, value in result.headers.items()
                                       if key.lower() not in {"content-length", "set-cookie"}})
            finally:
                result.close()

        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            context.route(origin + "/**", handler)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/#overview", wait_until="networkidle")
            expect(page.locator("#contest-title")).to_have_text("Private contest for account-a")
            page.locator("#account-button").click()
            page.locator(".cookie-details summary").click()
            page.locator("#cookie-input").fill("fixture=account-b")
            before = len(requests)
            with page.expect_response(lambda response: urlsplit(response.url).path == "/api/state"):
                page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            expect(page.locator("#cookie-input")).to_have_value("fixture=account-b")
            expect(page.locator("#account-dialog")).to_be_visible()
            assert requests[before:] == [("GET", "/api/state")]

            state_failure["enabled"] = True
            before = len(requests)
            page.locator("#connect-button").click()
            expect(page.locator("#account-dialog")).not_to_be_visible()
            expect(page.locator("#account-label")).to_have_text("account-b")
            expect(page.locator("#contest-title")).to_have_text("Private contest for account-b")
            assert requests[before:] == [("POST", "/api/connect")], requests[before:]

            page.locator("#account-button").click()
            before = len(requests)
            page.locator("#account-refresh").click()
            expect(page.locator("#account-dialog")).not_to_be_visible()
            expect(page.locator("#contest-title")).to_have_text("Private contest for account-b")
            assert requests[before:] == [("POST", "/api/refresh")], requests[before:]

            page.locator("#account-button").click()
            before = len(requests)
            page.locator("#logout-button").click()
            expect(page.locator("#account-error")).to_contain_text("Fixture state unavailable")
            expect(page.locator("#account-dialog")).to_be_visible()
            expect(page.locator("#contest-title")).to_have_text("Contest #1802")
            expect(page.locator("#account-label")).to_have_text("Connect QOJ")
            for tab in ("overview", "problems", "attachments", "submissions", "scoreboard", "printing"):
                expect(page.locator(f'[data-tab="{tab}"]')).not_to_be_visible()
            assert page.locator("#scoreboard-table").inner_text() == ""
            assert page.locator("#team-scoreboard").inner_text() == ""
            assert not page.locator("#scoreboard-original").get_attribute("href")
            for private in ("Private contest for account-b", PRIVATE_PROBLEM, PRIVATE_STANDING):
                assert private not in page.locator("body").inner_text()
            # Returning to Contest may also read the local joint-VP panel.
            # Account verification still consists of one logout and one state GET.
            account_requests = [item for item in requests[before:] if item != ("GET", "/api/joint-vp")]
            assert account_requests == [("POST", "/api/logout"), ("GET", "/api/state")], requests[before:]

            page.locator("#cookie-input").fill("fixture=account-a")
            page.locator("#connect-button").click()
            expect(page.locator("#account-dialog")).not_to_be_visible()
            expect(page.locator("#contest-title")).to_have_text("Private contest for account-a")
            page.locator("#account-button").click()
            page.locator("#cookie-input").fill("fixture=invalid")
            page.locator("#connect-button").click()
            expect(page.locator("#account-error")).to_contain_text("Fixture cookie rejected")
            expect(page.locator("#contest-title")).to_have_text("Contest #1802")
            expect(page.locator("#account-label")).to_have_text("Connect QOJ")
            assert page.locator("#team-scoreboard").inner_text() == ""
            expect(page.locator("#cookie-input")).to_have_value("")
            assert not errors, errors
            print(json.dumps({"account_browser": "passed", "listener_started": False,
                              "checks": ["stable polling preserves cookie input", "connect adopts returned state",
                                         "refresh adopts returned state", "failed logout verification clears old account",
                                         "failed cookie import clears old content", "reconnect after failure",
                                         "same Host and Origin CSRF", "concurrent refresh coalescing",
                                         "account mutation waits for old poll"]}))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
