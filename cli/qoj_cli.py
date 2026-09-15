"""Standalone terminal clients for this proxy (Python 3.10+, standard library only)."""
from __future__ import annotations

import argparse
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_SOURCE = 256 * 1024
MAX_PRINT = 2 * 1024 * 1024
TEXT_EXTENSIONS = {'.txt', '.cpp', '.cc', '.cxx', '.c', '.h', '.hpp', '.py', '.java',
                   '.kt', '.rs', '.go', '.pas', '.js', '.cs', '.rb', '.md'}
LANGUAGE_PATTERNS = {
    'cpp': r'c\+\+|cpp|gnu\+\+', 'cc': r'c\+\+|cpp|gnu\+\+', 'cxx': r'c\+\+|cpp|gnu\+\+',
    'c': r'^(?:gnu\s*)?c(?:\d|\s|$)|clang\s*c\b',
    'py': r'python|pypy', 'java': r'java', 'kt': r'kotlin', 'rs': r'rust',
    'go': r'go(?:lang)?\b', 'pas': r'pascal', 'js': r'javascript|node',
    'cs': r'c#|csharp', 'hs': r'haskell', 'rb': r'ruby',
}
VERDICTS = {
    'AC': 'Accepted', 'WA': 'Wrong Answer', 'TLE': 'Time Limit Exceeded',
    'MLE': 'Memory Limit Exceeded', 'OLE': 'Output Limit Exceeded',
    'RE': 'Runtime Error', 'CE': 'Compile Error', 'PE': 'Presentation Error',
    'JE': 'Judgement Failed', 'PARTIAL': 'Partial Score', 'FAILED': 'Submission Failed',
}
ALIASES = {'TL': 'TLE', 'ML': 'MLE', 'OL': 'OLE', 'RTE': 'RE', 'JF': 'JE', 'IE': 'JE'}


class CLIError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def safe_text(value):
    """Do not allow server/file names to inject terminal control sequences."""
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


def load_config(argument=None):
    if argument or os.environ.get('QOJ_CLI_CONFIG'):
        path = Path(argument or os.environ['QOJ_CLI_CONFIG']).expanduser()
    elif Path('qoj-cli.json').is_file():
        path = Path('qoj-cli.json')
    else:
        path = Path.home() / '.config' / 'qoj-vp' / 'cli.json'
    try:
        with path.open('rb') as stream:
            data = stream.read(64 * 1024 + 1)
        if len(data) > 64 * 1024:
            raise ValueError()
        config = json.loads(data.decode('utf-8-sig'))
        if not isinstance(config, dict) or type(config.get('version')) is not int or config['version'] != 1:
            raise ValueError()
        token = config.get('token')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,4096}', token):
            raise ValueError()
        if type(config.get('contest_id')) is not int or config['contest_id'] <= 0:
            raise ValueError()
        if not isinstance(config.get('team_id'), str) or not config['team_id']:
            raise ValueError()
        server = config.get('server_url')
        if not isinstance(server, str) or any(ord(c) <= 32 for c in server) or '\\' in server:
            raise ValueError()
        parsed = urlsplit(server)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
            raise ValueError()
        _ = parsed.port  # Validate malformed/out-of-range ports before sending credentials.
        config['server_url'] = server.rstrip('/')
        return config
    except FileNotFoundError:
        raise CLIError('找不到连接配置。请在队长网页 Team → 命令行工具生成 qoj-cli.json，'
                       '放入当前目录，或使用 --config 指定路径。') from None
    except (OSError, UnicodeError, ValueError, TypeError):
        raise CLIError('连接配置无法读取或格式无效；请从队长网页重新下载。') from None


class Client:
    def __init__(self, config):
        self.config = config
        # No CookieJar, automatic redirects, or retry handler.
        self.opener = build_opener(NoRedirect())

    def request(self, path, *, body=None, content_type=None, timeout=30):
        headers = {'Authorization': 'Bearer ' + self.config['token'],
                   'Accept': 'application/json', 'User-Agent': 'QOJ-VP-CLI/1'}
        if content_type:
            headers['Content-Type'] = content_type
        req = Request(self.config['server_url'] + path, data=body, headers=headers,
                      method='GET' if body is None else 'POST')
        try:
            with self.opener.open(req, timeout=timeout) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise CLIError('服务响应过大，请在网页检查。')
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except HTTPError as exc:
            try:
                data = json.loads(exc.read(64 * 1024))
                detail = data.get('error', '') if isinstance(data, dict) else ''
                if not isinstance(detail, str):
                    detail = ''
            except (ValueError, OSError, HTTPException):
                detail = ''
            finally:
                exc.close()
            if 300 <= exc.code < 400:
                detail = '服务返回重定向；请将配置中的 server_url 改为反代的直接访问地址。'
            elif exc.code == 401 and not detail:
                detail = '终端授权无效或 QOJ 连接失效，请回到队长网页检查。'
            raise CLIError(f'HTTP {exc.code}: {detail or "请求未成功，请在网页检查。"}') from None
        except (URLError, OSError, TimeoutError, HTTPException):
            raise CLIError('无法连接服务或请求超时；请检查服务器地址及网络。') from None
        except (ValueError, UnicodeError):
            raise CLIError('服务没有返回有效 JSON；请检查 server_url 是否为本项目的反代地址。') from None

    def json_post(self, path, data):
        return self.request(path, body=json.dumps(data, ensure_ascii=False).encode('utf-8'),
                            content_type='application/json; charset=utf-8')

    def state(self, action=None, timeout=30):
        state = self.request('/api/state', timeout=timeout)
        team = state.get('team') or {}
        contest = state.get('contest') or {}
        if (str(contest.get('id')) != str(self.config['contest_id'])
                or team.get('id') != self.config['team_id'] or team.get('role') != 'captain'):
            raise CLIError('当前队伍或比赛与配置不符；请在对应的队长网页重新生成配置。')
        if not state.get('demo') and not (state.get('user') or {}).get('authenticated'):
            raise CLIError('QOJ 尚未连接，请先在队长网页连接账号。')
        if action:
            run, participant = state.get('run') or {}, state.get('participation') or {}
            if (not (state.get('permissions') or {}).get(action) or run.get('status') != 'running'
                    or (not state.get('demo') and
                        (participant.get('status') != 'running' or not participant.get('can_submit')))):
                raise CLIError('当前不能提交或虚拟打印：请先在网页开始参赛，并确认比赛正在进行。')
        return state


def read_file(name, limit):
    path = Path(name).expanduser()
    try:
        if not path.is_file():
            raise OSError()
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
    except OSError:
        raise CLIError('文件不存在或无法读取：' + safe_text(path)) from None
    if not data:
        raise CLIError('文件不能为空。')
    if len(data) > limit:
        raise CLIError(f'文件超过大小限制（{limit // 1024} KiB）。')
    return path, data


def decode_source(data):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        raise CLIError('文本文件必须使用 UTF-8 编码。') from None
    if '\0' in text:
        raise CLIError('请选择源代码或文本，不要上传二进制程序。')
    return text


def select_problem(state, hint):
    problems = (state.get('contest') or {}).get('problems') or []
    matches = [p for p in problems if str(p.get('id')) == hint
               or str(p.get('label', '')).casefold() == hint.casefold()]
    if len(matches) != 1:
        available = ', '.join(str(p.get('label', p.get('id'))) for p in problems)
        raise CLIError(f'无法唯一识别题目 {safe_text(hint)}；请使用 -p 指定题号。可选：{safe_text(available)}')
    return matches[0]


def select_language(detail, hint, extension):
    languages = [item for item in detail.get('languages', [])
                 if isinstance(item, dict) and isinstance(item.get('value'), str) and item['value']]
    available = ', '.join(item['value'] for item in languages)
    if hint:
        exact = [item for item in languages if item['value'].casefold() == hint.casefold()
                 or str(item.get('label', '')).casefold() == hint.casefold()]
        if len(exact) == 1:
            return exact[0]
        extension = hint.lower().lstrip('.')
    pattern = LANGUAGE_PATTERNS.get(extension)
    matches = [item for item in languages if pattern and re.search(
        pattern, str(item.get('label', '')) + ' ' + item['value'], re.I)]
    if len(matches) > 1 and extension in {'cpp', 'cc', 'cxx'}:
        preferred = [item for item in matches if re.search(r'(?:c\+\+|gnu\+\+|cpp)\s*17\b',
                     str(item.get('label', '')) + ' ' + item['value'], re.I)]
        if len(preferred) == 1:
            matches = preferred
    if len(matches) != 1:
        raise CLIError('无法唯一确定语言，请用 -l 指定完整语言值。可选：' + safe_text(available))
    return matches[0]


def verdict_code(submission):
    value = str(submission.get('verdict') or 'UNKNOWN').strip().upper()
    value = ALIASES.get(value, value)
    for code, label in VERDICTS.items():
        if value == label.upper():
            return code
    return value


def show_submission(submission, demo=False):
    identifier = f'本地 #{submission.get("id", "?")}'
    if submission.get('qoj_id'):
        identifier += f' / QOJ #{submission["qoj_id"]}'
    elif demo:
        identifier += ' / 离线模拟'
    else:
        identifier += ' / QOJ 编号待确认'
    code = verdict_code(submission)
    if code in VERDICTS:
        print(safe_text(f'{identifier}: {code} {VERDICTS[code]}'), flush=True)
        return True
    print(safe_text(f'{identifier}: {code} — 等待 QOJ 结果' if not demo
                    else f'{identifier}: {code} — 等待模拟结果'), flush=True)
    return False


def poll_interval(value):
    try:
        result = float(value)
        return max(3.0, result) if math.isfinite(result) else 8.0
    except (ValueError, TypeError):
        return 8.0


def wait_for_result(client, state, submission, args):
    deadline = time.monotonic() + args.timeout
    run_id = (state.get('run') or {}).get('id')
    last = (verdict_code(submission), submission.get('qoj_id'))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CLIError('等待结果超时；提交已保存，请在网页继续查看，不要重复提交。')
        delay = max(poll_interval(state.get('poll_interval', 8)), args.interval)
        if delay >= remaining:
            time.sleep(remaining)
            continue
        time.sleep(delay)
        state = client.state(timeout=min(30, max(0.1, deadline - time.monotonic())))
        if (state.get('run') or {}).get('id') != run_id:
            raise CLIError('参赛场次已变化，停止等待。原提交请在网页核对。')
        found = next((item for item in state.get('submissions', []) if item.get('id') == submission['id']), None)
        if found is None:
            raise CLIError('当前记录中找不到该提交，请在网页核对；不会重复提交。')
        current = (verdict_code(found), found.get('qoj_id'))
        if current != last or current[0] in VERDICTS:
            if show_submission(found, state.get('demo')):
                return
            last = current


def submit(client, args):
    path, data = read_file(args.file, MAX_SOURCE)
    source = decode_source(data)
    if not source.strip():
        raise CLIError('源代码不能为空。')
    state = client.state('submit')
    problem = select_problem(state, args.problem or path.stem)
    detail = client.request(f'/api/problems/{problem["id"]}')
    if not detail.get('submission_available'):
        raise CLIError(detail.get('message') or '该题当前不允许提交。')
    language = select_language(detail, args.language, path.suffix.lower().lstrip('.'))
    filename = path.name
    if len(filename) > 200 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+\-]*', filename):
        suffix = path.suffix if re.fullmatch(r'\.[A-Za-z0-9]{1,16}', path.suffix) else '.txt'
        filename = 'solution' + suffix
    print(safe_text(f'Contest {client.config["contest_id"]} | {state["team"].get("name", "")} | '
                    f'Problem {problem.get("label", problem["id"])} | {language["value"]} | {filename}'), flush=True)
    try:
        result = client.json_post('/api/submit', {'problem_id': problem['id'], 'language': language['value'],
                                               'source': source, 'filename': filename})
        submission = result.get('submission')
        if not isinstance(submission, dict) or type(submission.get('id')) is not int:
            raise CLIError('服务没有返回可确认的本地提交编号。')
    except CLIError as exc:
        raise CLIError(f'{exc} 提交请求未重试；请先在网页核对记录，避免重复提交。') from None
    complete = show_submission(submission, state.get('demo'))
    if args.wait and not complete:
        wait_for_result(client, state, submission, args)


def printfile(client, args):
    path, data = read_file(args.file, MAX_PRINT)
    name = (args.name or path.name).strip()
    if (not name or len(name) > 180 or name in {'.', '..'} or any(c in name for c in '/\\')
            or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in name)):
        raise CLIError('虚拟打印文件名无效：请使用 1–180 个字符且不含路径或控制字符。')
    extension = Path(name).suffix.lower()
    if extension == '.pdf':
        if not data.startswith(b'%PDF-'):
            raise CLIError('文件不是有效的 PDF 格式。')
        mime = 'application/pdf'
    elif extension in TEXT_EXTENSIONS:
        decode_source(data)
        mime = 'text/plain; charset=utf-8'
    else:
        raise CLIError('虚拟打印仅支持 PDF、UTF-8 文本及常见源代码文件。')
    client.state('print')
    boundary = 'qojcli' + secrets.token_hex(24)
    escaped_name = name.replace('"', '\\"')
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{escaped_name}"\r\n'
            f'Content-Type: {mime}\r\n\r\n').encode('utf-8') + data + f'\r\n--{boundary}--\r\n'.encode('ascii')
    try:
        result = client.request('/api/prints', body=body, content_type=f'multipart/form-data; boundary={boundary}')
        item = result.get('file')
        if not isinstance(item, dict) or not item.get('id'):
            raise CLIError('服务没有返回虚拟打印文件编号。')
    except CLIError as exc:
        raise CLIError(f'{exc} 上传未重试，请先到网页 Printing 核对文件。') from None
    print(safe_text(f'虚拟打印已上传：{item.get("name", name)} / #{item["id"]}'), flush=True)
    print('队员可在网页 Printing 查看：' + client.config['server_url'] + '/#printing')


def positive_seconds(value):
    try:
        result = float(value)
        if not math.isfinite(result) or result <= 0:
            raise ValueError()
        return result
    except ValueError:
        raise argparse.ArgumentTypeError('请输入大于零的秒数。') from None


def main(command, argv=None):
    parser = argparse.ArgumentParser(prog=command, description=(
        '向当前队伍的 QOJ 比赛提交单个 UTF-8 源文件。' if command == 'submit' else
        '上传虚拟打印文件到反代服务器，供队员在网页查看。'))
    parser.add_argument('--config', metavar='PATH', help='队长网页下载的 qoj-cli.json；也可设置 QOJ_CLI_CONFIG')
    parser.add_argument('file', metavar='FILE', help='要上传的本地文件')
    if command == 'submit':
        parser.add_argument('-p', '--problem', help='题目字母或 QOJ 题目 ID；默认取文件名去掉扩展名')
        parser.add_argument('-l', '--language', help='QOJ 语言值，或 cpp、py 等扩展名；默认按扩展名识别')
        parser.add_argument('--wait', action='store_true', help='等待评测结果；得到终态后停止')
        parser.add_argument('--timeout', type=positive_seconds, default=300, metavar='SECONDS', help='最长等待结果时间（默认 300 秒）')
        parser.add_argument('--interval', type=positive_seconds, default=3, metavar='SECONDS', help='等待时的间隔（最少 3 秒，同时遵守服务器间隔）')
    else:
        parser.add_argument('--name', help='虚拟打印文件名，默认保留本地文件名')
    args = parser.parse_args(argv)
    config = None
    try:
        config = load_config(args.config)
        client = Client(config)
        (submit if command == 'submit' else printfile)(client, args)
        return 0
    except CLIError as exc:
        message = str(exc)
        if config:
            message = message.replace(config['token'], '[redacted]')
        print('Error: ' + safe_text(message), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\n已停止；若请求已发出，请先在网页核对记录。', file=sys.stderr)
        return 130
