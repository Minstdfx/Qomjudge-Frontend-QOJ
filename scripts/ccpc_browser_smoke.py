"""Exercise progressive CCPC scoreboards with synthetic Flask/Edge requests.

All requests are served in memory. No listener, QOJ session, or real contest is
used. Set QOJ_FRONTEND_STATIC=frontend-dist to check an unpublished Vite build.
"""
import json
import os
import sys
import time
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT / "tests"))

from app import create_app
from test_native_api import NativeClient
from playwright.sync_api import expect, sync_playwright


class CCPCFixture(NativeClient):
    def __init__(self):
        super().__init__(time.time() - 600)
        self.contest_data.update(title="Synthetic CCPC Preview", attachments=[], problems=[
            {"id": 11, "label": "A", "title": "Synthetic Alpha", "color": "#ff0000"},
            {"id": 12, "label": "B", "title": "Synthetic Beta", "color": "#00ff00"},
            {"id": 13, "label": "C", "title": "Synthetic Gamma", "color": "#0000ff"},
        ])
        self.stage = "initial"
        self.refresh("unregistered")

    def refresh(self, status="running", source="qoj-external", frozen=False):
        self.set_status("vp", status)
        state = self.snapshots["vp"]
        own = {
            "rank": 1, "team_name": "CCPC Team Alpha", "username": self.username,
            "is_self": True, "solved": 1, "penalty": 20,
            "problem_stats": {
                "11": {"solved": True, "display": "+\n0:20", "time": 20, "attempts": 1, "first_blood": True},
                "12": {"failed": True, "display": "-2", "attempts": 2},
                "13": {"pending": True, "display": "?3", "attempts": 3},
            },
        }
        rival = {
            "rank": 2, "team_name": "CCPC Team Beta", "username": "synthetic-beta",
            "is_self": False, "solved": 1, "penalty": 30,
            "problem_stats": {
                "11": {"failed": True, "display": "-1", "attempts": 1},
                "12": {"solved": True, "display": "+1\n0:10", "time": 10, "attempts": 2, "first_blood": True},
            },
        }
        if self.stage == "threshold":
            rival.update(solved=2, penalty=70)
            rival["problem_stats"]["11"] = {
                "solved": True, "display": "+1\n0:20", "time": 20, "attempts": 2,
            }
        state.update(scoreboard=[own, rival], scoreboard_source=source, ruleset="ICPC",
                     scoreboard_available=True, scoreboard_status="available",
                     scoreboard_frozen=frozen, scoreboard_url="https://qoj.ac/contest/1802/standings")


def main():
    artifacts = ROOT / "artifacts" / "ccpc"
    artifacts.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as temporary, sync_playwright() as playwright:
        upstream = CCPCFixture()
        app = create_app(contest_id=1802, data_dir=Path(temporary), client_factory=lambda: upstream,
                         scoreboard_style="ccpc", ccpc_unlock=2, ccpc_unlock_overrides="B=1")
        static_override = os.environ.get("QOJ_FRONTEND_STATIC")
        if static_override:
            app.static_folder = str((ROOT / static_override).resolve())
        app.config.update(TESTING=True)
        client = app.test_client()
        token = client.get("/api/state", base_url="http://qoj-ccpc.test").get_json()["csrf_token"]
        result = client.post("/api/team/create", json={"name": "CCPC Team Alpha", "device_name": "Captain"},
                             headers={"X-CSRF-Token": token}, base_url="http://qoj-ccpc.test")
        assert result.status_code in (200, 201), result.data
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        failures = []
        response_mode = {"value": "ccpc"}
        latest_state = {}

        def handle(route):
            request = route.request
            url = urlsplit(request.url)
            if url.netloc != "qoj-ccpc.test":
                failures.append("Unexpected external request: " + request.url)
                route.abort()
                return
            path = url.path + ("?" + url.query if url.query else "")
            response = client.open(path, method=request.method, data=request.post_data_buffer,
                                   base_url="http://qoj-ccpc.test",
                                   headers={key: value for key, value in request.headers.items()
                                            if key.lower() not in ("host", "content-length", "cookie")})
            try:
                body = response.get_data()
                if url.path == "/api/state":
                    payload = response.get_json()
                    if response_mode["value"] == "standard":
                        payload.pop("scoreboard_display", None)
                        payload["scoreboard"] = deepcopy(upstream.snapshots["vp"]["scoreboard"])
                    elif response_mode["value"] == "error":
                        payload["scoreboard_display"] = {"style": "ccpc", "error": "Synthetic projection error"}
                    elif response_mode["value"] == "locked-leak":
                        # Guard stale/invalid server payloads at the component boundary too.
                        payload["scoreboard_display"] = deepcopy(latest_state["scoreboard_display"])
                        payload["scoreboard"] = deepcopy(latest_state["scoreboard"])
                    if response_mode["value"] == "ccpc":
                        latest_state.clear()
                        latest_state.update(deepcopy(payload))
                    body = json.dumps(payload)
                route.fulfill(status=response.status_code, body=body,
                              headers={key: value for key, value in response.headers.items()
                                       if key.lower() not in ("content-length", "set-cookie")})
            finally:
                response.close()

        context.route("**/*", handle)
        page = context.new_page()
        page.on("pageerror", lambda error: failures.append(str(error)))
        try:
            page.goto("http://qoj-ccpc.test/#scoreboard", wait_until="networkidle")
            expect(page.locator('[data-tab="scoreboard"]')).not_to_be_visible()
            expect(page.locator(".ccpc-scoreboard-table")).to_have_count(0)
            expect(page.locator("#ccpc-revealed-summary")).to_have_count(0)

            upstream.refresh()
            page.reload(wait_until="networkidle")
            page.locator('[data-tab="scoreboard"]').click()
            board = page.locator("#scoreboard-table")
            expect(board.locator(".ccpc-scoreboard-table")).to_be_visible()
            expect(board.locator(".ccpc-unrevealed")).to_have_count(3)
            expect(board.locator(".ccpc-unrevealed a")).to_have_count(0)
            expect(board.locator(".ccpc-unrevealed .ccpc-problem-label").first).to_have_text("?")
            expect(board.locator(".problem-letter")).to_have_count(0)
            expect(board.locator(".cell-first-blood")).to_have_count(2)
            expect(page.locator('[data-revealed-problem="12"]')).to_contain_text("1 / 4")
            expect(page.locator('[data-revealed-problem="11"]')).to_have_count(0)
            expect(page.locator('[data-revealed-problem="13"]')).to_have_count(0)
            page.screenshot(path=str(artifacts / "anonymous-scoreboard.png"), full_page=True)
            assert latest_state["scoreboard_display"]["hidden_count"] == 2
            for row in latest_state["scoreboard"]:
                assert "problem_stats" not in row
                for cell in row["ccpc_cells"]:
                    if not cell["revealed"]:
                        assert "problem_id" not in cell and "label" not in cell

            page.locator('[data-tab="overview"]').click()
            own = page.locator("#team-scoreboard")
            expect(own.locator(".ccpc-scoreboard-table")).to_have_count(0)
            expect(own.locator(".problem-letter")).to_have_count(3)
            expect(own.locator(".cell-accepted")).to_contain_text("+")
            expect(own.locator('[data-problem-totals="11"]')).to_have_text("未揭晓")
            expect(own.locator('[data-problem-totals="12"]')).to_have_text("1 / 4")
            expect(own.locator('[data-problem-totals="13"]')).to_have_text("未揭晓")

            page.locator('[data-tab="scoreboard"]').click()
            upstream.stage = "threshold"
            upstream.refresh()
            page.locator("#scoreboard-refresh").click()
            expect(page.locator('[data-revealed-problem="11"]')).to_contain_text("2 / 3")
            expect(board.locator('.ccpc-problem-label[href="#problems/11"]')).to_have_count(2)
            expect(board.locator(".ccpc-unrevealed")).to_have_count(1)
            expect(board.locator(".self-row .score-cell")).to_have_text("1")
            expect(board.locator(".self-row .penalty-cell")).to_have_text("20")

            upstream.refresh(frozen=True)
            page.locator("#scoreboard-refresh").click()
            expect(page.locator("#ccpc-revealed-summary")).to_contain_text("全部题号已揭晓")
            expect(board.locator(".ccpc-unrevealed")).to_have_count(0)
            expect(board.locator(".cell-pending")).to_contain_text("?3")
            expect(board.locator(".cell-pending .ccpc-problem-label")).to_have_text("C")
            expect(board.locator(".self-row .penalty-cell")).to_have_text("20")
            page.screenshot(path=str(artifacts / "frozen-revealed-scoreboard.png"), full_page=True)

            upstream.refresh(source="qoj-pc2", frozen=True)
            page.locator("#scoreboard-refresh").click()
            expect(page.locator("#scoreboard-source-label")).to_contain_text("PC2")
            expect(page.locator(".cell-first-blood")).to_have_count(0)
            expect(page.locator("#first-blood-legend")).not_to_be_visible()

            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile page overflows"
            assert board.evaluate("el => el.scrollWidth > el.clientWidth"), "Small screen table should scroll locally"
            page.set_viewport_size({"width": 1440, "height": 1000})

            response_mode["value"] = "error"
            page.locator("#scoreboard-refresh").click()
            expect(page.locator("#scoreboard-source-message")).to_contain_text("Synthetic projection error")
            expect(board.locator("table")).to_have_count(0)
            expect(page.locator("#ccpc-revealed-summary")).to_have_count(0)

            response_mode["value"] = "standard"
            page.locator("#scoreboard-refresh").click()
            expect(board.locator("table.scoreboard-table")).to_be_visible()
            expect(board.locator(".ccpc-scoreboard-table")).to_have_count(0)
            expect(board.locator(".problem-letter")).to_have_count(3)
            expect(page.locator("#ccpc-revealed-summary")).to_have_count(0)

            response_mode["value"] = "locked-leak"
            upstream.refresh(status="unregistered")
            page.reload(wait_until="networkidle")
            expect(page.locator('[data-tab="scoreboard"]')).not_to_be_visible()
            expect(page.locator(".ccpc-scoreboard-table")).to_have_count(0)
            expect(page.locator("#ccpc-revealed-summary")).to_have_count(0)
            assert "Synthetic Alpha" not in page.locator("body").inner_text()
            assert "CCPC Team Beta" not in page.locator("body").inner_text()
            expect(page.locator("#team-scoreboard")).to_have_text("")
            assert not failures, failures
            print("CCPC browser smoke passed: hidden/revealed cells, private details, frozen ?3, PC2, mobile, errors, gates, standard view.")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
