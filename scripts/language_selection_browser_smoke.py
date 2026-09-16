"""Check automatic and explicit language selection in Edge using offline routes.

No listener or QOJ request is created. Build the Vue frontend before running.
"""
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from native_browser_smoke import BrowserNative, ROOT, create_app, expect, sync_playwright


ORIGIN = "http://qoj-language.test"
LANGUAGES = [{"value": "C++98", "label": "GNU C++98 (GCC 15)"},
             {"value": "C++26", "label": "GNU C++26 (GCC 14)"},
             {"value": "C++23", "label": "GNU C++23 (GCC 14)"},
             {"value": "Python3", "label": "Python 3"}]
SOURCE = "#include <iostream>\nint main() { std::cout << 1; }\n"


class LanguageNative(BrowserNative):
    def problem(self, contest_id, problem_id):
        result = super().problem(contest_id, problem_id)
        result["languages"] = LANGUAGES
        return result


def main():
    with TemporaryDirectory() as temporary, sync_playwright() as playwright:
        upstream = LanguageNative(time.time() - 100)
        upstream.set_status("vp", "running")
        app = create_app(contest_id=1802, data_dir=Path(temporary), client_factory=lambda: upstream)
        app.static_folder = str((ROOT / os.environ.get("QOJ_FRONTEND_STATIC", "frontend-dist")).resolve())
        app.config["TESTING"] = True
        client = app.test_client()
        token = client.get("/api/state", base_url=ORIGIN).get_json()["csrf_token"]
        response = client.post("/api/team/create", json={"name": "Language fixture", "device_name": "Captain"},
                               headers={"X-CSRF-Token": token}, base_url=ORIGIN)
        assert response.status_code == 200
        posts, errors = [], []

        def handler(route):
            request = route.request
            parsed = urlsplit(request.url)
            assert parsed.netloc == "qoj-language.test", "Unexpected external request"
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
            if request.method == "POST":
                posts.append(path)
            result = client.open(path, method=request.method, data=request.post_data_buffer, base_url=ORIGIN,
                                 headers={key: value for key, value in request.headers.items()
                                          if key.lower() not in ("host", "content-length", "cookie")})
            try:
                route.fulfill(status=result.status_code, body=result.get_data(),
                              headers={key: value for key, value in result.headers.items()
                                       if key.lower() not in ("content-length", "set-cookie")})
            finally:
                result.close()

        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            context = browser.new_context(viewport={"width": 1400, "height": 1100})
            context.route("**/*", handler)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(ORIGIN + "/#overview", wait_until="networkidle")
            page.locator("#submit-button").click()
            language = page.locator("#submit-language")
            expect(language).to_have_value("C++26")
            page.locator("#source-file").set_input_files({"name": "main.cpp", "mimeType": "text/plain", "buffer": SOURCE.encode()})
            expect(language).to_have_value("C++26")
            expect(page.locator("#source-code")).to_have_value(SOURCE)

            language.select_option("C++98")
            page.locator("#source-file").set_input_files({"name": "main.cpp", "mimeType": "text/plain", "buffer": (SOURCE + "// edited\n").encode()})
            expect(language).to_have_value("C++98")
            page.locator('[data-close="submit-dialog"]').first.click()
            page.locator("#submit-button").click()
            expect(language).to_have_value("C++98")
            page.locator("#source-code").fill(SOURCE)
            expect(language).to_have_value("C++98")

            # Reset this test's draft to exercise pasted code and extension detection.
            page.locator('[data-close="submit-dialog"]').first.click()
            page.reload(wait_until="networkidle")
            page.evaluate("sessionStorage.clear()")
            page.locator("#submit-button").click()
            expect(language).to_have_value("C++26")
            page.locator("#source-code").fill(SOURCE)
            expect(language).to_have_value("C++26")
            page.locator("#source-file").set_input_files({"name": "main.py", "mimeType": "text/plain", "buffer": b"print(1)\n"})
            expect(language).to_have_value("Python3")
            page.locator("#source-file").set_input_files({"name": "main.cpp", "mimeType": "text/plain", "buffer": SOURCE.encode()})
            expect(language).to_have_value("C++26")
            page.locator('[data-close="submit-dialog"]').first.click()
            page.locator("#submit-button").click()
            expect(language).to_have_value("C++26")
            assert not posts, posts
            assert not errors, errors
            print("PASS: C++26 default, uploads and pasted source, explicit C++98 preserved, draft restore; no submissions sent.")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
