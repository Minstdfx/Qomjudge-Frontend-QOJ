"""Private, persistent virtual print files, scoped to an owner and VP run."""
from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path

from storage import Store

MAX_PRINT_SIZE = 2 * 1024 * 1024
MAX_RUN_BYTES = 20 * 1024 * 1024
MAX_RUN_FILES = 100
TEXT_EXTENSIONS = {'.txt', '.cpp', '.cc', '.cxx', '.c', '.h', '.hpp', '.py', '.java',
                   '.kt', '.rs', '.go', '.pas', '.js', '.cs', '.rb', '.md'}


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
