"""Capture shareable previews using exclusively fresh, offline demo fixtures.

Usage: python scripts/capture_demo_previews.py
Requires Playwright, Microsoft Edge and the Python application dependencies.
Never connects to the running application or loads its data/browser profile.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
from urllib.parse import urlsplit
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / '.venv' / 'Lib' / 'site-packages'))

from app import create_app
from demo import DemoClient
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server, WSGIRequestHandler


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


SOURCE = '''// Offline preview fixture: A + B. No code is executed.
#include <iostream>
using namespace std;

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    long long a, b;
    cin >> a >> b;
    cout << a + b << '\\n';
    return 0;
}
'''


def main():
    output = ROOT / 'previews' / 'demo'
    output.mkdir(parents=True, exist_ok=True)
    images = []
    with sync_playwright() as playwright, ExitStack() as cleanup:
        temporary = cleanup.enter_context(TemporaryDirectory(prefix='qoj-preview-'))
        app = create_app(contest_id=1, demo=True, data_dir=Path(temporary))
        if os.environ.get('QOJ_FRONTEND_STATIC'):
            app.static_folder = str((ROOT / os.environ['QOJ_FRONTEND_STATIC']).resolve())
        frontend_entry = Path(app.static_folder) / 'index.html'
        assert frontend_entry.is_file()
        frontend_hash = hashlib.sha256(frontend_entry.read_bytes()).hexdigest()
        app.config['TESTING'] = True
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cleanup.callback(server.server_close)
        cleanup.callback(thread.join, 5)
        cleanup.callback(server.shutdown)
        origin = f'http://127.0.0.1:{server.server_port}'
        browser = playwright.chromium.launch(headless=True, channel='msedge')
        cleanup.callback(browser.close)
        errors, blocked = [], []

        def isolate(route):
            url = urlsplit(route.request.url)
            if f'{url.scheme}://{url.netloc}' == origin:
                route.continue_()
            else:
                blocked.append(route.request.url)
                route.abort()

        def new_context():
            context = browser.new_context(viewport={'width': 1440, 'height': 1000},
                                          device_scale_factor=1, locale='zh-CN',
                                          timezone_id='Asia/Shanghai', reduced_motion='reduce')
            context.route('**/*', isolate)
            return context

        def state(context):
            response = context.request.get(origin + '/api/state')
            assert response.ok
            result = response.json()
            assert result['demo'] is True and result['contest']['id'] == 1
            return result

        def post(context, path, body):
            csrf = state(context)['csrf_token']
            response = context.request.post(origin + path, data=body,
                                            headers={'X-CSRF-Token': csrf, 'Origin': origin})
            assert response.ok, (path, response.status)
            return response.json()

        captain = new_context()
        post(captain, '/api/team/create', {'name': 'Aurora', 'device_name': 'Captain PC'})
        # This invitation is only for the disposable preview database.
        with app.extensions['store'].connect() as db:
            db.execute('UPDATE teams SET join_code=?', ('DEMO-PREVIEW-ONLY',))
        members = [new_context(), new_context()]
        for number, context in enumerate(members, 1):
            post(context, '/api/team/join', {'code': 'DEMO-PREVIEW-ONLY', 'device_name': f'Member {number} PC'})
        post(captain, '/api/start', {})
        run = state(captain)['run']
        started_at = time.time() - 84 * 60
        store = app.extensions['store']
        with store.connect() as db:
            db.execute('UPDATE runs SET started_at=?,ends_at=? WHERE id=?',
                       (started_at, started_at + 18000, run['id']))
        run['started_at'] = started_at
        problems = DemoClient().contest(1)['problems']

        def submission(target, problem, minute, verdict):
            submission_id = store.add_submission(target, problems[problem - 1], 'C++17',
                                                 now=started_at + minute * 60)
            store.save_submission_payload(submission_id, SOURCE, f'{chr(64 + problem)}.cpp')
            store.update_submission(submission_id, verdict=verdict,
                                    message=f'离线测试数据：模拟 {verdict}，未编译或执行代码。',
                                    checked_at=time.time())
            return submission_id

        submission(run, 1, 12, 'AC')
        submission(run, 2, 24, 'WA')
        submission(run, 2, 31, 'AC')
        submission(run, 4, 47, 'TLE')
        last_id = submission(run, 4, 62, 'AC')
        submission(run, 6, 76, 'WA')
        # Additional fictitious teams make the demo scoreboard easier to inspect.
        for index, (name, solved) in enumerate([('Blue Horizon', [1, 2, 3, 5]),
                                                ('Cedar', [1, 2]), ('Delta', [1]),
                                                ('Orbit', [3]), ('Willow', [])]):
            other = store.start(f'preview-team-{index}', 'demo:1', name, 18000, now=started_at)
            for offset, problem in enumerate(solved):
                submission(other, problem, 8 + index * 4 + offset * 16, 'AC')
            submission(other, 7, 61 + index, 'WA')

        post(captain, '/api/prints', {'name': 'team-notes.txt', 'content':
             'OFFLINE PREVIEW — fictional team notes\n\nA / B / D: sample solved problems.\n'
             'F: review boundary cases together.\nG: discuss the graph model.\n\n'
             'Only the captain submits code. Members can read shared print files.\n'})
        post(captain, '/api/prints', {'name': 'A.cpp', 'content': SOURCE})

        page = captain.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(origin + '/#overview', wait_until='networkidle')
        expect(page.locator('#demo-banner')).to_be_visible()
        expect(page.locator('#team-name-display')).to_have_text('Aurora')
        expect(page.locator('#metric-solved')).to_have_text('3 / 8')

        def capture(name, caption, target=page):
            expect(target.locator('#demo-banner')).to_be_visible()
            expect(target.locator('#contest-id')).to_have_text('CONTEST #1')
            # Keep the visible demo banner in every unmodified browser screenshot.
            for frame in target.frames:
                visible_text = frame.locator('body').inner_text()
                assert '3962' not in visible_text
                assert 'domjudge' not in visible_text.casefold(), 'Unexpected branding in preview'
            assert 'domjudge' not in target.title().casefold()
            target.evaluate('document.fonts.ready')
            path = output / name
            target.screenshot(path=str(path), full_page=True, animations='disabled')
            images.append({'file': name, 'caption': caption,
                           'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})

        capture('01-overview.png', '队伍概览：模拟 AC / WA / TLE 与本队成绩')
        page.locator('[data-tab="problems"]').click()
        expect(page.frame_locator('#statement-frame').locator('h1')).to_contain_text('A + B')
        capture('02-problem.png', '题面：离线 A + B 示例')
        page.locator('[data-tab="scoreboard"]').click()
        expect(page.locator('#scoreboard-table')).to_contain_text('Blue Horizon')
        capture('03-scoreboard.png', '榜单：六支虚构队伍的模拟成绩')
        page.locator('[data-tab="submissions"]').click()
        page.locator(f'#submissions-table [data-submission-source="{last_id}"]').click()
        expect(page.locator('#submission-source-code')).to_have_value(SOURCE)
        capture('04-source-code.png', '提交记录与代码查看：仅含生成的示例代码')
        page.locator('[data-close="submission-source-dialog"]').click()
        page.locator('[data-tab="printing"]').click()
        expect(page.locator('#prints-table')).to_contain_text('team-notes.txt')
        capture('05-printing.png', '虚拟打印：队长上传的测试文件')
        page.locator('[data-tab="team"]').click()
        page.reload(wait_until='networkidle')
        expect(page.locator('#team-devices')).to_contain_text('Member 2 PC')
        expect(page.locator('#team-dissolve-button')).to_be_visible()
        expect(page.locator('#cli-tools-panel')).to_be_visible()
        expect(page.locator('#cli-status')).to_have_text('尚未启用')
        capture('06-team.png', '队伍协作：一名队长、两名队员；邀请码仅用于临时演示')
        mobile = members[0].new_page()
        mobile.on('pageerror', lambda error: errors.append(str(error)))
        mobile.set_viewport_size({'width': 390, 'height': 844})
        mobile.goto(origin + '/#printing', wait_until='networkidle')
        expect(mobile.locator('#prints-table')).to_contain_text('team-notes.txt')
        assert mobile.evaluate('document.documentElement.scrollWidth <= innerWidth')
        capture('07-member-mobile.png', '手机窄屏：队员查看共享打印文件', mobile)
        page.locator('[data-tab="attachments"]').click()
        expect(page.locator('#attachments-table')).to_contain_text('samples.zip')
        expect(page.locator('#attachments-count')).to_have_text('共 2 项附件')
        capture('08-attachments.png', '比赛附件：离线样例压缩包与说明文件')
        page.locator('[data-tab="problems"]').click()
        expect(page.frame_locator('#statement-frame').locator('h1')).to_contain_text('A + B')
        page.locator('#submit-button').click()
        expect(page.locator('#submit-dialog')).to_be_visible()
        expect(page.locator('#submit-language')).to_be_enabled()
        page.locator('#source-code').fill(SOURCE)
        expect(page.locator('#send-button')).to_be_enabled()
        capture('09-submit.png', '提交代码：离线示例源码与语言选择；未发送提交')
        page.locator('[data-close="submit-dialog"]').first.click()
        assert not errors, errors
        assert not blocked, 'An unexpected non-demo request was blocked'
        assert all(isinstance(runtime['client'], DemoClient) for runtime in app.extensions['clients'].values())

    version = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))['version']
    captured_at = datetime.now(timezone.utc).isoformat()
    manifest = {'mode': 'offline-demo', 'contest_id': 1, 'private_data_used': False,
                'captured_at': captured_at, 'project_version': version,
                'frontend_entry_sha256': frontend_hash, 'branding_check_passed': True,
                'external_requests': 0, 'images': images}
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    note = (f'截图时间（UTC）：{captured_at}；项目版本：v{version}。\n'
            '这些截图均由新建的离线演示实例生成，contest ID 为 1。\n'
            '队名、成绩、提交代码、打印文件和邀请码均为测试数据。\n'
            '未读取现有比赛数据、账号 Cookie 或已有浏览器窗口，未请求 QOJ。\n'
            '截图保留页面 DEMO 标识；临时服务及浏览器在完成后已关闭。\n\n'
            + '\n'.join(f"{item['file']}  {item['caption']}" for item in images) + '\n')
    (output / 'README.txt').write_text(note, encoding='utf-8')
    archive_path = ROOT / 'previews' / 'demo-previews.zip'
    with ZipFile(archive_path, 'w', ZIP_DEFLATED) as archive:
        for name in [item['file'] for item in images] + ['README.txt', 'manifest.json']:
            archive.write(output / name, name)
    print(json.dumps({'screenshots': len(images), 'directory': str(output),
                      'archive': str(archive_path), 'external_requests': 0,
                      'browser_errors': errors, 'temporary_service_closed': True}))


if __name__ == '__main__':
    main()
