"""Optional browser check: python scripts/browser_smoke.py [demo_url] [live_url].

Requires Playwright and its Chromium browser, independently of server dependencies.
Only the explicitly labelled demo receives source code. Live checks are read-only.
QOJ_FRONTEND_STATIC=frontend-dist starts a temporary localhost demo automatically
and shuts it down after the checks, including on failure.
"""
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from zipfile import ZipFile

from playwright.sync_api import sync_playwright, expect

demo_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
live_url = sys.argv[2] if len(sys.argv) > 2 else None
artifacts = Path(__file__).resolve().parents[1] / "artifacts"
artifacts.mkdir(exist_ok=True)

with ExitStack() as fixtures, sync_playwright() as p:
    static_override = os.environ.get('QOJ_FRONTEND_STATIC')
    if static_override:
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))
        sys.path.insert(0, str(root / '.venv' / 'Lib' / 'site-packages'))
        from app import create_app
        temporary = fixtures.enter_context(TemporaryDirectory())
        app = create_app(contest_id=1, demo=True, data_dir=Path(temporary))
        app.config['TESTING'] = True
        folder = Path(static_override)
        if not folder.is_absolute():
            folder = root / folder
        assert (folder / 'index.html').is_file(), 'Frontend build is missing'
        app.static_folder = str(folder.resolve())
        from werkzeug.serving import make_server, WSGIRequestHandler
        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        fixtures.callback(server.server_close)
        fixtures.callback(thread.join, 5)
        fixtures.callback(server.shutdown)
        demo_url = f'http://127.0.0.1:{server.server_port}'
        live_url = None
    browser = p.chromium.launch(headless=True, **({'channel': 'msedge'} if static_override else {}))
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(demo_url, wait_until="networkidle")
    expect(page.locator("#demo-banner")).to_be_visible()
    expect(page.locator("#view-participation")).to_be_visible()
    expect(page.locator('[data-tab="problems"]')).not_to_be_visible()
    expect(page.locator('[data-tab="attachments"]')).not_to_be_visible()
    page.locator('[data-tab="team"]').click()
    page.locator("#team-create-name").fill("Browser Test Team")
    page.locator("#team-create-device").fill("Browser test captain")
    page.locator("#team-create-button").click()
    expect(page.locator("#team-join-code")).to_be_visible()
    page.locator('[data-tab="participation"]').click()
    expect(page.locator("#start-team-name")).to_have_text("队伍：Browser Test Team")
    page.locator("#start-button").click()
    expect(page.locator("#run-status")).to_have_text("比赛进行中")
    expect(page.locator("#team-scoreboard table")).to_be_visible()
    expect(page.locator("#overview-submissions thead")).to_be_visible()
    page.screenshot(path=str(artifacts / "overview-desktop.png"), full_page=True)
    page.locator('[data-tab="attachments"]').click()
    expect(page.locator("#attachments-table")).to_contain_text("samples.zip")
    expect(page.locator("#attachments-table")).to_contain_text("比赛说明.txt")
    with page.expect_download() as download_event:
        page.get_by_role("link", name="下载 samples.zip", exact=True).click()
    downloaded_zip = download_event.value
    assert downloaded_zip.suggested_filename == "samples.zip", downloaded_zip.suggested_filename
    downloaded_zip.save_as(str(artifacts / "downloaded-samples.zip"))
    with ZipFile(artifacts / "downloaded-samples.zip") as archive:
        assert archive.read("A/sample.in") == b"3 5\n"
        assert archive.read("A/sample.ans") == b"8\n"
    with page.expect_download() as download_event:
        page.get_by_role("link", name="下载 比赛说明.txt", exact=True).click()
    downloaded_notes = download_event.value
    assert downloaded_notes.suggested_filename == "比赛说明.txt"
    downloaded_notes.save_as(str(artifacts / "downloaded-notes.txt"))
    assert "离线演示附件" in (artifacts / "downloaded-notes.txt").read_text(encoding="utf-8")
    page.locator("#attachments-refresh").click()
    expect(page.locator("#attachments-table")).to_contain_text("samples.zip")
    page.screenshot(path=str(artifacts / "attachments-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#submit-button")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Attachments overflow on mobile"
    mobile_download = page.get_by_role("link", name="下载 samples.zip", exact=True)
    expect(mobile_download).to_be_in_viewport(ratio=1)
    page.screenshot(path=str(artifacts / "attachments-mobile.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.locator('[data-tab="overview"]').click()
    expect(page.locator("#view-overview")).to_be_visible()
    page.locator('[data-tab="problems"]').click()
    expect(page.locator("#statement-frame")).to_be_visible()
    expect(page.frame_locator("#statement-frame").locator("h1")).to_contain_text("A + B")
    page.locator("#submit-button").click()
    expect(page.locator("#submit-language")).to_have_value("C++17")
    page.locator("#source-code").fill("#include <iostream>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b;}")
    page.locator("#send-button").click()
    expect(page.locator("#submit-dialog")).not_to_be_visible()
    page.locator('[data-tab="submissions"]').click()
    expect(page.locator("#submissions-table")).to_contain_text("Accepted", timeout=20000)
    expect(page.locator("#submissions-table")).to_contain_text("模拟")
    page.locator('[data-tab="overview"]').click()
    expect(page.locator("#overview-submissions")).to_contain_text("Accepted")
    expect(page.locator("#team-scoreboard .cell-accepted")).to_have_count(1)
    expect(page.locator("#team-scoreboard .cell-accepted")).to_contain_text("1 try")
    page.screenshot(path=str(artifacts / "overview-active-desktop.png"), full_page=True)
    page.locator('[data-tab="submissions"]').click()
    page.reload(wait_until="networkidle")
    expect(page.locator("#submissions-table")).to_contain_text("Accepted")
    page.locator('[data-tab="scoreboard"]').click()
    expect(page.locator("#scoreboard-table")).to_contain_text("Browser Test Team")
    expect(page.locator("#countdown")).to_be_visible()
    page.screenshot(path=str(artifacts / "scoreboard-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator('[data-tab="overview"]').click()
    expect(page.locator("#view-overview")).to_be_visible()
    page.screenshot(path=str(artifacts / "overview-mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile page overflows viewport"
    page.locator("#finish-button").click()
    page.locator("#confirm-action").click()
    expect(page.locator("#run-status")).to_have_text("比赛已结束")
    assert not errors, errors
    print(json.dumps({"demo": "passed", "browser_errors": errors, "screenshots": str(artifacts)}))
    if live_url:
        live = context.new_page()
        live_errors, failed = [], []
        live.on("pageerror", lambda error: live_errors.append(str(error)))
        live.on("response", lambda response: failed.append({"url": response.url, "status": response.status}) if response.status >= 400 else None)
        live.goto(live_url + "/qoj/login", wait_until="networkidle", timeout=90000)
        expect(live.locator("#input-username")).to_be_visible()
        expect(live.locator("#input-password")).to_be_visible()
        live.screenshot(path=str(artifacts / "qoj-login.png"), full_page=True)
        print(json.dumps({"live_login": "loaded", "browser_errors": live_errors, "failed_resources": failed}))
    browser.close()
