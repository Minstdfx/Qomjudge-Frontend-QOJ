"""Private, persistent virtual print files, scoped to an owner and VP run."""
from __future__ import annotations

import html
import secrets
import sqlite3
import time
from pathlib import Path

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name

from storage import Store

MAX_PRINT_SIZE = 2 * 1024 * 1024
MAX_RUN_BYTES = 20 * 1024 * 1024
MAX_RUN_FILES = 100
TEXT_EXTENSIONS = {'.txt', '.cpp', '.cc', '.cxx', '.c', '.h', '.hpp', '.py', '.java',
                   '.kt', '.rs', '.go', '.pas', '.js', '.cs', '.rb', '.md'}
CODE_LANGUAGES = {
    '.cpp': ('cpp', 'C++'), '.cc': ('cpp', 'C++'), '.cxx': ('cpp', 'C++'),
    '.h': ('cpp', 'C++'), '.hpp': ('cpp', 'C++'), '.c': ('c', 'C'),
    '.py': ('python', 'Python'), '.java': ('java', 'Java'), '.kt': ('kotlin', 'Kotlin'),
    '.rs': ('rust', 'Rust'), '.go': ('go', 'Go'), '.pas': ('pascal', 'Pascal'),
    '.js': ('javascript', 'JavaScript'), '.cs': ('csharp', 'C#'), '.rb': ('ruby', 'Ruby'),
}


def render_print(name: str, source: str) -> str:
    """Render escaped, self-contained source HTML; never execute uploaded markup."""
    title = html.escape(name)
    language = CODE_LANGUAGES.get(Path(name).suffix.lower())
    source = source.replace('\r\n', '\n').replace('\r', '\n')
    lines = source.count('\n') + (not source.endswith('\n'))
    syntax_css = ''
    if language:
        alias, label = language
        # Filename-selected built-in lexers only: no guessing, plugins, user options,
        # JavaScript, or external assets are needed to read the rendered document.
        lexer = get_lexer_by_name(alias, stripnl=False, ensurenl=False, tabsize=0)
        formatter = HtmlFormatter(nowrap=True, style='friendly')
        rendered = highlight(source, lexer, formatter)
        # The HTML formatter terminates its last line even when the file did not.
        # Keep the original newline/indentation; generated line numbers are not
        # selectable and do not become part of the copied source.
        if not source.endswith('\n') and rendered.endswith('\n'):
            rendered = rendered[:-1]
        rows = rendered.split('\n')
        if rendered.endswith('\n'):
            rows.pop()
        numbered = '\n'.join(f'<span class="linenos" aria-hidden="true">{index}</span>{line}'
                             for index, line in enumerate(rows, 1))
        if rendered.endswith('\n'):
            numbered += '\n'
        body = f'<div class="source"><pre><code>{numbered}</code></pre></div>'
        syntax_css = formatter.get_style_defs('.source')
        description = f'{label} · 语法高亮 · {lines} 行'
    else:
        body = f'<div class="source"><pre><code>{html.escape(source)}</code></pre></div>'
        description = f'纯文本 · {lines} 行'
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{title} · 虚拟打印</title><style>{syntax_css}'
        'body{margin:0;background:#edf2f6;color:#243647;font:15px/1.6 system-ui,sans-serif}'
        'main{max-width:1180px;margin:28px auto;background:#fff;border:1px solid #dce3e9;border-radius:8px;overflow:hidden}'
        'header{padding:20px 24px;border-bottom:1px solid #dce3e9;background:#f8fafc}'
        'h1{margin:0;font-size:20px;font-weight:650;overflow-wrap:anywhere}'
        '.metadata{margin:5px 0 0;color:#607383;font-size:13px}'
        '.source{padding:20px 24px;background:#fff}'
        '.source pre,.source code{margin:0;font:14px/1.65 Consolas,"Microsoft YaHei","微软雅黑",ui-monospace,monospace;tab-size:4}'
        '.source pre{white-space:pre-wrap;overflow-wrap:anywhere}'
        '.source .linenos{display:inline-block;min-width:3ch;margin-right:16px;padding-right:12px;'
        'border-right:1px solid #e2e8ee;color:#8493a0;background:transparent;user-select:none}'
        '@media(max-width:640px){main{margin:0;border:0;border-radius:0}header,.source{padding:16px 12px}}'
        '@page{margin:14mm}@media print{body{background:#fff}main{margin:0;max-width:none;border:0;border-radius:0;overflow:visible}'
        'header{background:#fff;padding:0 0 10px}h1{font-size:15px}.source{padding:12px 0 0}'
        '.source pre,.source code{font-size:10pt}.source .linenos{color:#666}}'
        f'</style></head><body><main><header><h1>{title}</h1>'
        f'<p class="metadata">{description}</p></header>{body}</main></body></html>'
    )


class PrintError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class PrintStore:
    def __init__(self, store: Store, directory):
        self.store = store
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS print_files (
                    id TEXT PRIMARY KEY, sid TEXT NOT NULL, contest_key TEXT NOT NULL,
                    run_id INTEGER NOT NULL REFERENCES runs(id), name TEXT NOT NULL,
                    size INTEGER NOT NULL, kind TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS print_owner ON print_files(sid, contest_key, run_id);
            ''')

    def listing(self, sid, contest_key, run_id):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT id,name,size,kind,created_at FROM print_files WHERE sid=? AND contest_key=? AND run_id=? ORDER BY created_at DESC,id',
                (sid, contest_key, run_id))]

    def get(self, sid, contest_key, run_id, file_id):
        with self.store.connect() as db:
            row = db.execute('SELECT id,name,size,kind,created_at FROM print_files WHERE id=? AND sid=? AND contest_key=? AND run_id=?',
                             (file_id, sid, contest_key, run_id)).fetchone()
        if not row:
            raise PrintError('本轮 VP 中没有此打印文件，或你没有访问权限。', 404)
        path = self.directory / (row['id'] + '.bin')
        if not path.is_file():
            raise PrintError('打印文件已不在服务器上。', 404)
        return dict(row), path

    def create(self, sid, contest_key, run_id, name, content):
        if not isinstance(name, str) or not name.strip() or len(name) > 180:
            raise PrintError('打印文件名须为 1–180 个字符。')
        name = name.strip()
        if name in {'.', '..'} or any(c in name for c in '/\\') or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in name):
            raise PrintError('打印文件名不能包含路径或控制字符。')
        if not isinstance(content, bytes) or not content:
            raise PrintError('打印内容不能为空。')
        if len(content) > MAX_PRINT_SIZE:
            raise PrintError('每份打印文件最多 2 MiB。', 413)
        extension = Path(name).suffix.lower()
        if extension == '.pdf':
            if not content.startswith(b'%PDF-'):
                raise PrintError('文件不是有效的 PDF 格式。')
            kind = 'pdf'
        elif extension in TEXT_EXTENSIONS:
            try:
                decoded = content.decode('utf-8-sig')
            except UnicodeDecodeError:
                raise PrintError('文本打印须使用 UTF-8 编码。') from None
            if '\0' in decoded:
                raise PrintError('请选择文本源码，不要上传二进制程序。')
            kind = 'text'
        else:
            raise PrintError('仅支持 PDF、UTF-8 文本及常见源码文件。')
        file_id = secrets.token_hex(16)
        path = self.directory / (file_id + '.bin')
        created = time.time()
        written = False
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                quota = db.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM print_files WHERE sid=? AND contest_key=? AND run_id=?',
                                   (sid, contest_key, run_id)).fetchone()
                if quota[0] >= MAX_RUN_FILES or quota[1] + len(content) > MAX_RUN_BYTES:
                    raise PrintError('本轮 VP 的打印额度已用完（最多 100 份、合计 20 MiB）。', 409)
                with path.open('xb') as output:
                    written = True
                    output.write(content)
                db.execute('INSERT INTO print_files(id,sid,contest_key,run_id,name,size,kind,created_at) VALUES(?,?,?,?,?,?,?,?)',
                           (file_id, sid, contest_key, run_id, name, len(content), kind, created))
        except (OSError, sqlite3.Error):
            if written:
                path.unlink(missing_ok=True)
            raise PrintError('服务器无法保存打印文件，请检查存储空间后重试。', 507) from None
        return {'id': file_id, 'name': name, 'size': len(content), 'kind': kind, 'created_at': created}
