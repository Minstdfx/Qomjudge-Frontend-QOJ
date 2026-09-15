"""Four isolated browsers exercise captain + two members and a rejected extra device.

Usage: python scripts/teams_browser_smoke.py http://127.0.0.1:8093
The target must be an explicitly labelled offline demo.
Set QOJ_FRONTEND_STATIC=frontend-dist to use an isolated temporary demo instead.
It listens only on a random localhost port and closes automatically at exit;
the test never accesses a QOJ account or changes the system clipboard.
"""
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / '.venv' / 'Lib' / 'site-packages'))

from playwright.sync_api import expect, sync_playwright

url = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8080'
artifacts = ROOT / 'artifacts'
artifacts.mkdir(exist_ok=True)


def assert_team_entry_only(page):
    expect(page.locator('.main-nav [data-tab]:visible')).to_have_count(2)
    for tab in ('participation', 'team'):
        expect(page.locator(f'[data-tab="{tab}"]')).to_be_visible()
    expect(page.locator('#submit-button')).not_to_be_visible()


with ExitStack() as fixtures, sync_playwright() as p:
    static_override = os.environ.get('QOJ_FRONTEND_STATIC')
    if static_override:
        from app import create_app
        from werkzeug.serving import make_server, WSGIRequestHandler

        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        temporary = fixtures.enter_context(TemporaryDirectory())
        app = create_app(contest_id=1, demo=True, data_dir=Path(temporary))
        app.config['TESTING'] = True
        folder = Path(static_override)
        if not folder.is_absolute():
            folder = ROOT / folder
        folder = folder.resolve()
        assert (folder / 'index.html').is_file(), f'Frontend build is missing: {folder}'
        app.static_folder = str(folder)
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        def stop_server():
            server.shutdown()
            server_thread.join(timeout=5)
            server.server_close()

        fixtures.callback(stop_server)
        url = f'http://127.0.0.1:{server.server_port}'

    browser = p.chromium.launch(headless=True, **({'channel': 'msedge'} if static_override else {}))
    contexts = [browser.new_context(viewport={'width': 1440, 'height': 1000}) for _ in range(4)]
    delayed_prints = []

    for context in contexts if static_override else []:
        context.add_init_script("""(() => {
            Object.defineProperty(navigator, 'clipboard', { value: {
                writeText: async value => { window.fixtureCopiedCode = value; }
            }});
            const exec = document.execCommand.bind(document);
            document.execCommand = function(command, ...args) {
                if (command === 'copy') { window.fixtureCopiedCode = document.activeElement.value; return true; }
                return exec(command, ...args);
            };
        })();""")

    captain, member_one, member_two, extra = [context.new_page() for context in contexts]
    errors = []
    for page in (captain, member_one, member_two, extra):
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url, wait_until='networkidle')
        expect(page.locator('#demo-banner')).to_be_visible()
        assert_team_entry_only(page)
        expect(page.locator('#start-button')).not_to_be_visible()

    captain.locator('[data-tab="team"]').click()
    captain.locator('#team-create-name').fill('Three-device browser team')
    captain.locator('#team-create-device').fill('Captain computer')
    captain.locator('#team-create-button').click()
    expect(captain.locator('#team-join-code')).to_be_visible()
    code = captain.locator('#team-join-code').input_value()
    assert code
    if static_override:
        captain.locator('#team-code-copy').click()
        assert captain.evaluate('window.fixtureCopiedCode') == code
        captain.locator('#team-code-rotate').click()
        captain.locator('#confirm-action').click()
        expect(captain.locator('#team-join-code')).not_to_have_value(code)
        code = captain.locator('#team-join-code').input_value()
        assert code
    for page, device in ((member_one, 'Member one'), (member_two, 'Member two')):
        page.locator('[data-tab="team"]').click()
        page.locator('#team-join-input').fill(code)
        page.locator('#team-join-device').fill(device)
        page.locator('#team-join-button').click()
        expect(page.locator('#team-leave-button')).to_be_visible()
        expect(page.locator('#submit-button')).not_to_be_visible()
        expect(page.locator('[data-tab="attachments"]')).not_to_be_visible()
        expect(page.locator('[data-tab="submissions"]')).not_to_be_visible()
        expect(page.locator('[data-tab="problems"]')).not_to_be_visible()
        expect(page.locator('#statement-submit')).not_to_be_visible()
    extra.locator('[data-tab="team"]').click()
    extra.locator('#team-join-input').fill(code)
    extra.locator('#team-join-device').fill('Fourth computer')
    extra.locator('#team-join-button').click()
    expect(extra.locator('#team-error')).to_contain_text('3 台')

    captain.locator('[data-tab="participation"]').click()
    expect(captain.locator('#team-name')).not_to_be_visible()
    expect(captain.locator('#start-team-name')).to_have_text('队伍：Three-device browser team')
    captain.locator('#start-button').click()
    expect(captain.locator('#run-status')).to_have_text('比赛进行中')
    captain.locator('[data-tab="printing"]').click()
    print_bytes = '// 中文共享代码\n#include <iostream>\n<script>window.printXSS = true</script>\n'.encode('utf-8')
    captain.locator('#print-file').set_input_files({'name': '共享源码.cpp', 'mimeType': 'text/plain', 'buffer': print_bytes})
    captain.locator('#print-upload-button').click()
    expect(captain.locator('#prints-table')).to_contain_text('共享源码.cpp')
    captain.locator('#print-mode').select_option('text')
    captain.locator('#print-name').fill('notes.txt')
    captain.locator('#print-content').fill('Shared notes for this VP.')
    if static_override:
        # Routine polling must not invalidate a draft when its scope is stable.
        with captain.expect_response(url + '/api/state', timeout=15000) as polled:
            pass
        polled.value.body()
        captain.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        expect(captain.locator('#print-content')).to_have_value('Shared notes for this VP.')
    captain.locator('#print-upload-button').click()
    expect(captain.locator('#prints-table')).to_contain_text('notes.txt')
    captain.screenshot(path=str(artifacts / 'printing-captain.png'), full_page=True)

    for page in (member_one, member_two):
        expect(page.locator('#run-status')).to_have_text('比赛进行中', timeout=15000)
        page.locator('[data-tab="problems"]').click()
        expect(page.frame_locator('#statement-frame').locator('h1')).to_contain_text('A + B')
        page.locator('[data-tab="printing"]').click()
        expect(page.locator('#prints-table')).to_contain_text('共享源码.cpp', timeout=15000)
        expect(page.locator('#print-upload-form')).not_to_be_visible()
        page.locator('[data-tab="scoreboard"]').click()
        expect(page.locator('#scoreboard-table')).to_contain_text('Three-device browser team')
        page.locator('[data-tab="printing"]').click()

    source_row = member_one.locator('#prints-table tr').filter(has_text='共享源码.cpp')
    with member_one.expect_popup() as popup_event:
        source_row.locator('a[href$="/view"]').click()
    preview = popup_event.value
    expect(preview.locator('pre')).to_contain_text('<script>window.printXSS = true</script>')
    assert preview.evaluate('window.printXSS === undefined')
    preview.close()
    with member_one.expect_download() as download_event:
        source_row.locator('a[href$="/download"]').click()
    download = download_event.value
    assert download.suggested_filename == '共享源码.cpp', repr(download.suggested_filename)
    download.save_as(str(artifacts / 'team-print-download.cpp'))
    assert (artifacts / 'team-print-download.cpp').read_bytes() == print_bytes
    member_two.set_viewport_size({'width': 390, 'height': 844})
    assert member_two.evaluate('document.documentElement.scrollWidth <= innerWidth')
    expect(member_two.locator('#prints-table a[href$="/view"]').first).to_be_in_viewport(ratio=1)
    member_two.screenshot(path=str(artifacts / 'printing-member-mobile.png'), full_page=True)

    captain.locator('[data-tab="team"]').click()
    captain.reload(wait_until='networkidle')
    expect(captain.locator('#team-devices')).to_contain_text('Member one')
    captain.screenshot(path=str(artifacts / 'team-captain.png'), full_page=True)
    if static_override:
        # Keep a response containing the old team's print names in flight while
        # the captain revokes this device; it must never repopulate the new UI.
        def delay_print_list(route):
            if route.request.method != 'GET':
                route.continue_()
                return
            response = route.fetch()
            try:
                reply = {'status': response.status, 'body': response.body(),
                         'headers': {key: value for key, value in response.headers.items()
                                     if key.lower() not in ('content-length', 'set-cookie')}}
                delayed_prints.append((route, reply))
            finally:
                response.dispose()

        contexts[1].route(url + '/api/prints', delay_print_list)
        member_one.locator('#prints-refresh').click()
        expect(member_one.locator('#prints-refresh')).to_be_disabled()
    captain.locator('#team-devices tr').filter(has_text='Member one').get_by_role('button', name='移除').click()
    captain.locator('#confirm-action').click()
    expect(captain.locator('#team-devices')).not_to_contain_text('Member one')
    if static_override:
        expect(member_one.locator('#team-leave-button')).to_be_visible(timeout=15000)
        assert delayed_prints
        for delayed_route, reply in delayed_prints:
            try:
                delayed_route.fulfill(**reply)
            except Exception as error:
                # Chromium can discard an interception after AbortController
                # cancels the request. A successful fulfil is safe to test too.
                assert any(word in str(error).lower() for word in ('interception', 'request', 'cancel', 'abort')), error
        expect(member_one.locator('#prints-table')).to_have_text('')
        expect(member_one.locator('#print-content')).to_have_value('')
    else:
        member_one.reload(wait_until='networkidle')
    expect(member_one.locator('#team-leave-button')).to_be_visible()
    expect(member_one.locator('[data-tab="printing"]')).not_to_be_visible()
    assert_team_entry_only(member_one)
    member_one.screenshot(path=str(artifacts / 'team-revoked.png'), full_page=True)
    if static_override:
        member_one.locator('#team-leave-button').click()
        member_one.locator('#confirm-action').click()
        expect(member_one.locator('#team-solo')).to_be_visible()
        expect(member_one.locator('#team-join-input')).to_have_value('')
        assert_team_entry_only(member_one)
    assert not errors, errors
    print(json.dumps({'teams': 'passed', 'browsers': 4, 'browser_errors': errors, 'screenshots': str(artifacts)}))
    browser.close()
