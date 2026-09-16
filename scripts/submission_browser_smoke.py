"""Check saved submission code in Edge through Flask in-memory routes.

No listener, real QOJ request, or system clipboard change is made.
"""
import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from native_browser_smoke import BrowserNative, ROOT, create_app, expect, sync_playwright


ORIGIN = "http://qoj-vp.test"
SOURCE = ('// 中文 <script>window.sourceExecuted = true</script>\n'
          '// </textarea><img src=x onerror="window.sourceExecuted = true">\n'
          '#include <iostream>\nint main() { std::cout << "<&>"; }\n')
MESSAGE = "Fixture: QOJ accepted the request; checking the submission number."


class SubmissionNative(BrowserNative):
    def submit(self, *args, **kwargs):
        self.calls.append(("submit", args, kwargs))
        return {"qoj_id": None, "message": MESSAGE}


def main():
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    with TemporaryDirectory() as temporary, sync_playwright() as p:
        upstream = SubmissionNative(time.time() - 100)
        upstream.set_status("vp", "running")
        app = create_app(contest_id=1802, data_dir=Path(temporary), client_factory=lambda: upstream)
        if os.environ.get("QOJ_FRONTEND_STATIC"):
            app.static_folder = str(Path(os.environ["QOJ_FRONTEND_STATIC"]).resolve())
        app.config["TESTING"] = True
        captain_client, member_client, other_captain_client = [app.test_client() for _ in range(3)]
        store = app.extensions["store"]

        def post(client, path, data):
            token = client.get("/api/state", base_url=ORIGIN).get_json()["csrf_token"]
            response = client.post(path, json=data, headers={"X-CSRF-Token": token}, base_url=ORIGIN)
            assert response.status_code in (200, 201), response.data
            return response.get_json()

        team = post(captain_client, "/api/team/create", {"name": "Code browser team", "device_name": "Captain"})
        post(member_client, "/api/team/join", {"code": team["team"]["join_code"], "device_name": "Member"})
        other_team = post(other_captain_client, "/api/team/create",
                          {"name": "Independent code browser team", "device_name": "Other captain"})
        assert other_team["team"]["id"] != team["team"]["id"]
        payload = {"problem_id": 1, "language": "C++17", "source": SOURCE, "filename": "main.cpp"}
        pending = post(captain_client, "/api/submit", payload)["submission"]
        known = post(captain_client, "/api/submit", {**payload, "filename": "known.cpp"})["submission"]
        not_sent = post(captain_client, "/api/submit", {**payload, "filename": "not-sent.cpp"})["submission"]
        store.update_submission(not_sent["id"], verdict="FAILED", message="代码尚未发送到 QOJ：提交表单校验失败。")
        store.update_submission(known["id"], verdict="UNKNOWN", qoj_id=9001,
                                qoj_url="https://qoj.ac/submission/9001", checked_at=time.time() + 3600,
                                message="Fixture: verdict not available yet.")
        run = captain_client.get("/api/state", base_url=ORIGIN).get_json()["run"]
        legacy_id = store.add_submission(run, upstream.contest_data["problems"][0], "C++17")
        store.update_submission(legacy_id, verdict="AC")
        other_submission = post(other_captain_client, "/api/submit", payload)["submission"]
        for client, forbidden_id in ((other_captain_client, pending["id"]), (captain_client, other_submission["id"])):
            response = client.get(f"/api/submissions/{forbidden_id}/source", base_url=ORIGIN)
            assert response.status_code == 404, response.get_data(as_text=True)
        browser = p.chromium.launch(channel="msedge", headless=True)
        errors, requests, contexts, pages, delayed = [], [], [], [], []
        source_options = {}

        def handler_for(client):
            def handler(route):
                request = route.request
                parsed = urlsplit(request.url)
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
                requests.append((client, request.method, path))
                if path.startswith("/api/submissions/") and path.endswith("/source"):
                    if source_options.get("delay"):
                        delayed.append(route)
                        return
                    if source_options.get("error"):
                        route.fulfill(status=503, content_type="application/json",
                                      body=json.dumps({"error": source_options["error"]}))
                        return
                response = client.open(path, method=request.method, data=request.post_data_buffer, base_url=ORIGIN,
                                       headers={key: value for key, value in request.headers.items()
                                                if key.lower() not in ("host", "content-length", "cookie")})
                try:
                    route.fulfill(status=response.status_code, body=response.get_data(),
                                  headers={key: value for key, value in response.headers.items()
                                           if key.lower() not in ("content-length", "set-cookie")})
                finally:
                    response.close()
            return handler

        def show_source(page, local_id):
            page.locator(f'#submissions-table [data-submission-source="{local_id}"]').click()
            expect(page.locator("#submission-source-dialog")).to_be_visible()

        def close_source(page):
            page.locator('[data-close="submission-source-dialog"]').click()
            expect(page.locator("#submission-source-dialog")).not_to_be_visible()
            expect(page.locator("#submission-source-code")).to_have_value("")
            assert not page.locator("#submission-source-download").get_attribute("href")

        try:
            for client in (captain_client, member_client, other_captain_client):
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
                contexts.append(context)
                context.route(ORIGIN + "/**", handler_for(client))
                # HTTP LAN copying uses selection + execCommand; intercept only the clipboard write.
                context.add_init_script("""(() => {
                    const exec = document.execCommand.bind(document);
                    document.execCommand = function(command, ...args) {
                        if (command === 'copy') { window.copiedSource = document.activeElement.value; return true; }
                        return exec(command, ...args);
                    };
                })();""")
                page = context.new_page()
                pages.append(page)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(ORIGIN + "/#submissions", wait_until="networkidle")
            captain, member, other_captain = pages
            expect(captain.locator(".main-nav .nav-label-en:visible")).to_have_count(8)
            expect(captain.locator(".main-nav .nav-label-zh:visible")).to_have_count(0)
            expect(captain.locator("#connection-label")).to_have_text("Connected")
            captain.set_viewport_size({"width": 1180, "height": 1000})
            expect(captain.locator(".main-nav .nav-label-en:visible")).to_have_count(0)
            expect(captain.locator(".main-nav .nav-label-zh:visible")).to_have_count(8)
            captain.set_viewport_size({"width": 1440, "height": 1000})
            assert not any(path.endswith("/source") for _, _, path in requests), requests
            pending_row = captain.locator(f'#submissions-table [data-submission-id="{pending["id"]}"]')
            known_row = captain.locator(f'#submissions-table [data-submission-id="{known["id"]}"]')
            expect(pending_row).to_contain_text("编号待确认")
            expect(pending_row).to_contain_text(MESSAGE)
            expect(known_row).to_contain_text("等待 QOJ 结果")
            expect(known_row).to_contain_text("Fixture: verdict not available yet.")
            not_sent_row = captain.locator(f'#submissions-table [data-submission-id="{not_sent["id"]}"]')
            expect(not_sent_row.locator(".verdict-final.verdict-wrong")).to_contain_text("提交失败")
            expect(not_sent_row).to_contain_text("代码尚未发送到 QOJ")
            expect(not_sent_row).not_to_contain_text("编号待确认")
            expect(captain.locator("#submissions-table")).not_to_contain_text("Unknown")
            expect(member.locator('[data-tab="submissions"]')).not_to_be_visible()
            expect(member.locator("[data-submission-source]")).to_have_count(0)
            denied = member_client.get(f'/api/submissions/{pending["id"]}/source', base_url=ORIGIN)
            assert denied.status_code == 403

            show_source(captain, pending["id"])
            expect(captain.locator("#submission-source-code")).to_have_value(SOURCE)
            expect(captain.locator("#submission-source-name")).to_have_text("main.cpp")
            expect(captain.locator("#submission-source-code")).to_have_attribute("readonly", "")
            expect(captain.locator("#submission-source-dialog img, #submission-source-dialog script")).to_have_count(0)
            assert captain.evaluate("window.sourceExecuted || false") is False
            captain.locator("#submission-source-copy").click()
            expect(captain.locator("#submission-source-status")).to_have_text("代码已复制。")
            assert captain.evaluate("window.copiedSource") == SOURCE
            with captain.expect_download() as download_info:
                captain.locator("#submission-source-download").click()
            download = download_info.value
            assert download.suggested_filename == "main.cpp"
            assert Path(download.path()).read_bytes() == SOURCE.encode("utf-8")
            assert sum(path.endswith("/source") for _, _, path in requests) == 1
            captain.screenshot(path=str(artifacts / "submission-source-desktop.png"), full_page=True)
            close_source(captain)

            show_source(captain, not_sent["id"])
            expect(captain.locator("#submission-source-code")).to_have_value(SOURCE)
            expect(captain.locator("#submission-source-meta")).to_contain_text("未生成 QOJ 提交编号")
            expect(captain.locator("#submission-source-meta")).not_to_contain_text("编号待确认")
            close_source(captain)

            show_source(captain, legacy_id)
            expect(captain.locator("#submission-source-error")).to_contain_text("历史提交没有保存源代码")
            expect(captain.locator("#submission-source-code")).not_to_be_visible()
            expect(captain.locator("#submission-source-copy")).to_be_disabled()
            expect(captain.locator("#submission-source-download")).not_to_be_visible()
            close_source(captain)
            source_options["error"] = "Fixture: source request failed; please retry."
            show_source(captain, pending["id"])
            expect(captain.locator("#submission-source-error")).to_contain_text(source_options["error"])
            source_options.clear()
            captain.locator("#submission-source-retry").click()
            expect(captain.locator("#submission-source-code")).to_have_value(SOURCE)
            expect(captain.locator("#submission-source-error")).not_to_be_visible()
            store.update_submission(pending["id"], verdict="AC", qoj_id=9100,
                                    qoj_url="https://qoj.ac/submission/9100", message="QOJ score: 100/100",
                                    time_text="12 ms", memory_text="512 KB")
            store.update_submission(known["id"], verdict="WA", message="QOJ: Wrong Answer",
                                    time_text="7 ms", memory_text="256 KB")
            source_reads = sum(path.endswith("/source") for _, _, path in requests)
            with captain.expect_response(lambda response: response.url.endswith("/api/state")):
                captain.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            expect(pending_row).to_contain_text("Accepted")
            expect(pending_row).to_contain_text("#9100")
            expect(pending_row.locator(".verdict-final")).to_contain_text("AC · Accepted")
            expect(pending_row.locator(".submission-message")).to_have_count(0)
            expect(pending_row).not_to_contain_text("100/100")
            expect(captain.locator("[data-submission-resource], .submission-resources")).to_have_count(0)
            for hidden_text in ("用时", "内存", "12 ms", "512 KB", "7 ms", "256 KB"):
                expect(captain.locator("#submissions-table")).not_to_contain_text(hidden_text)
                expect(captain.locator("#overview-submissions")).not_to_contain_text(hidden_text)
            expect(known_row.locator(".verdict-final.verdict-wrong")).to_contain_text("Wrong Answer")
            expect(captain.locator("#submissions-table .verdict-pending")).to_have_count(0)
            expect(captain.locator("#submissions-table")).not_to_contain_text("等待 QOJ 结果")
            expect(captain.locator("#pending-badge")).not_to_be_visible()
            expect(captain.locator("#submission-source-code")).to_have_value(SOURCE)
            assert sum(path.endswith("/source") for _, _, path in requests) == source_reads
            close_source(captain)
            for duplicate in ("AC ✓", "AC ?", "Accepted ✔", "100 / 100"):
                store.update_submission(pending["id"], message=duplicate)
                with captain.expect_response(lambda response: response.url.endswith("/api/state")):
                    captain.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                expect(pending_row.locator(".submission-message")).to_have_count(0)
                expect(captain.locator(f'#overview-submissions [data-submission-id="{pending["id"]}"] .submission-message')).to_have_count(0)
            store.update_submission(pending["id"], message="Accepted; source verification completed.")
            store.update_submission(known["id"], message="AC ✓")
            with captain.expect_response(lambda response: response.url.endswith("/api/state")):
                captain.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            expect(pending_row.locator(".submission-message")).to_have_text("Accepted; source verification completed.")
            expect(known_row.locator(".submission-message")).to_have_text("AC ✓")
            store.update_submission(pending["id"], message="AC ✓")
            store.update_submission(known["id"], message="QOJ: Wrong Answer")
            with captain.expect_response(lambda response: response.url.endswith("/api/state")):
                captain.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            expect(pending_row.locator(".submission-message")).to_have_count(0)
            expect(known_row.locator(".submission-message")).to_have_count(0)
            error_cases = [
                ("WA", "WA", "Wrong Answer"), ("TLE", "TLE", "Time Limit Exceeded"),
                ("MLE", "MLE", "Memory Limit Exceeded"), ("OLE", "OLE", "Output Limit Exceeded"),
                ("RE", "RE", "Runtime Error"), ("CE", "CE", "Compile Error"),
                ("PE", "PE", "Presentation Error"), ("JE", "JE", "Judgement Failed"),
                ("TL", "TLE", "Time Limit Exceeded"), ("ML", "MLE", "Memory Limit Exceeded"),
                ("OL", "OLE", "Output Limit Exceeded"), ("RTE", "RE", "Runtime Error"),
                ("JF", "JE", "Judgement Failed"), ("IE", "JE", "Judgement Failed"),
                ("Judgement Failed", "JE", "Judgement Failed"),
                ("Compile Time Error", "CE", "Compile Error"), ("Compiler Error", "CE", "Compile Error"),
                ("Compilation Failed", "CE", "Compile Error"), ("Run Time Error", "RE", "Runtime Error"),
                ("Run-error", "RE", "Runtime Error"), ("Judgment Failed", "JE", "Judgement Failed"),
                ("Internal Error", "JE", "Judgement Failed"),
            ]
            for raw, code, explanation in error_cases:
                diagnostic = f"QOJ original result: {raw}; fixture diagnostic preserved."
                store.update_submission(known["id"], verdict=raw, message=diagnostic)
                with captain.expect_response(lambda response: response.url.endswith("/api/state")):
                    captain.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                badge = known_row.locator(".verdict-final.verdict-wrong")
                expect(badge).to_contain_text(f"{code} · {explanation}")
                expect(badge).to_have_css("background-color", "rgb(253, 233, 229)")
                expect(known_row.locator(".submission-message")).to_have_text(diagnostic)
                expect(known_row.locator(".verdict-pending, .pending-pulse")).to_have_count(0)
                expect(captain.locator("#pending-badge")).not_to_be_visible()
                expect(known_row.locator("[data-submission-source]")).to_be_enabled()
                pure_messages = [f"[ {raw.lower()} ✗ ]"]
                if raw == code:
                    pure_messages.append(f"× {explanation.lower()}")
                for pure in pure_messages:
                    store.update_submission(known["id"], message=pure)
                    with captain.expect_response(lambda response: response.url.endswith("/api/state")):
                        captain.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                    expect(known_row.locator(".submission-message")).to_have_count(0)
                    expect(badge).to_contain_text(f"{code} · {explanation}")
            store.update_submission(known["id"], verdict="CE", message="Compiler Error: main.cpp:3: expected ';'")
            with captain.expect_response(lambda response: response.url.endswith("/api/state")):
                captain.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            expect(known_row.locator(".submission-message")).to_have_text("Compiler Error: main.cpp:3: expected ';'")
            store.update_submission(known["id"], verdict="TLE", message="TL")
            with captain.expect_response(lambda response: response.url.endswith("/api/state")):
                captain.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            expect(known_row.locator(".verdict-final")).to_contain_text("TLE · Time Limit Exceeded")
            expect(known_row.locator(".submission-message")).to_have_count(0)
            show_source(captain, known["id"])
            expect(captain.locator("#submission-source-code")).to_have_value(SOURCE)
            expect(captain.locator("#submission-source-name")).to_have_text("known.cpp")
            close_source(captain)
            captain.screenshot(path=str(artifacts / "submission-results-desktop.png"), full_page=True)

            captain.set_viewport_size({"width": 390, "height": 844})
            expect(captain.locator(".main-nav .nav-label-en:visible")).to_have_count(0)
            expect(captain.locator(".main-nav .nav-label-zh:visible")).to_have_count(8)
            assert captain.evaluate("document.documentElement.scrollWidth <= innerWidth")
            action = captain.locator(f'#submissions-table [data-submission-source="{pending["id"]}"]')
            bounds = action.bounding_box()
            assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390, bounds
            for hidden_text in ("用时", "内存", "12 ms", "512 KB", "7 ms", "256 KB"):
                expect(captain.locator("#submissions-table")).not_to_contain_text(hidden_text)
            captain.screenshot(path=str(artifacts / "submission-results-mobile.png"), full_page=True)
            show_source(captain, pending["id"])
            expect(captain.locator("#submission-source-code")).to_have_value(SOURCE)
            captain.screenshot(path=str(artifacts / "submission-source-mobile.png"), full_page=True)
            close_source(captain)
            show_source(other_captain, other_submission["id"])
            expect(other_captain.locator("#submission-source-code")).to_have_value(SOURCE)
            close_source(other_captain)

            # A slow old source response must not restore code after a mode/context change.
            source_options["delay"] = True
            show_source(captain, pending["id"])
            expect(captain.locator("#submission-source-loading")).to_be_visible()
            assert delayed
            upstream.set_status("official", "scheduled")
            post(captain_client, "/api/mode", {"mode": "official"})
            with captain.expect_response(lambda response: response.url.endswith("/api/state")):
                captain.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            expect(captain.locator("#view-participation")).to_be_visible()
            expect(captain.locator("#submission-source-dialog")).not_to_be_visible()
            expect(captain.locator("#submission-source-code")).to_have_value("")
            assert not captain.locator("#submission-source-download").get_attribute("href")
            for route in delayed:
                try:
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps({"source": SOURCE, "filename": "stale.cpp", "language": "C++17", "qoj_id": 9100}))
                except Exception:
                    pass  # AbortController may already have cancelled the old browser request.
            source_options.clear()
            captain.wait_for_timeout(100)
            expect(captain.locator("#submission-source-code")).to_have_value("")

            # Exercise the Vue submit form itself, including its isolated drafts.
            other_captain.locator("#submit-button").click()
            expect(other_captain.locator("#submit-language")).to_have_value("C++17")
            other_captain.locator("#source-file").set_input_files({
                "name": "from-browser.cpp", "mimeType": "text/plain", "buffer": SOURCE.encode("utf-8")})
            expect(other_captain.locator("#source-code")).to_have_value(SOURCE)
            expect(other_captain.locator("#file-picker-label")).to_have_text("from-browser.cpp")
            other_captain.locator('[data-close="submit-dialog"]').first.click()
            expect(other_captain.locator("#submit-dialog")).not_to_be_visible()
            other_captain.locator("#submit-button").click()
            expect(other_captain.locator("#source-code")).to_have_value(SOURCE)
            expect(other_captain.locator("#submit-language")).to_have_value("C++17")
            with other_captain.expect_response(lambda response: response.url.endswith("/api/submit")) as submitted:
                other_captain.locator("#send-button").click()
            browser_submission = submitted.value.json()["submission"]
            expect(other_captain.locator("#submit-dialog")).not_to_be_visible()
            expect(other_captain.locator(f'#submissions-table [data-submission-id="{browser_submission["id"]}"]')).to_be_visible()
            show_source(other_captain, browser_submission["id"])
            expect(other_captain.locator("#submission-source-code")).to_have_value(SOURCE)
            expect(other_captain.locator("#submission-source-name")).to_have_text("from-browser.cpp")
            close_source(other_captain)

            other_captain.locator("#submit-button").click()
            expect(other_captain.locator("#source-code")).to_have_value(SOURCE)
            post(other_captain_client, "/api/mode", {"mode": "official"})
            with other_captain.expect_response(lambda response: response.url.endswith("/api/state")):
                other_captain.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            expect(other_captain.locator("#submit-dialog")).not_to_be_visible()
            expect(other_captain.locator("#source-code")).to_have_value("")
            post(other_captain_client, "/api/mode", {"mode": "vp"})
            with other_captain.expect_response(lambda response: response.url.endswith("/api/state")):
                other_captain.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            other_captain.locator("#submit-button").click()
            expect(other_captain.locator("#source-code")).to_have_value(SOURCE)
            expect(other_captain.locator("#file-picker-label")).to_have_text("from-browser.cpp")
            other_captain.locator('[data-close="submit-dialog"]').first.click()
            assert not errors, errors
            print(json.dumps({"submission_browser": "passed", "roles": 3, "listener_started": False,
                              "checks": ["on-demand source", "plain text", "HTTP clipboard fallback", "download bytes",
                                         "legacy error", "network retry", "Unknown recovery", "member denied",
                                         "independent team source", "cross-team source isolation", "context cleanup", "stale response ignored", "mobile actions",
                                         "final verdict badges", "runtime and memory omitted", "mobile results",
                                         "duplicate AC notes omitted", "extra and error notes preserved", "responsive nav labels",
                                         "standard error labels", "legacy error aliases", "error results are final", "error source view",
                                         "duplicate error notes omitted", "decorated and full verdict names",
                                         "Vue submit form", "source file upload", "draft restored on reopen",
                                         "draft context cleared", "original context draft restored"]}))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
