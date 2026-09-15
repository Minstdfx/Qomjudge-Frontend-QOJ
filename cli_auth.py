"""Revocable, contest-scoped terminal credentials; only SHA-256 digests persist."""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager


class CLIAuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


class CLITokenStore:
    """One independently revocable credential per captain, team and contest.

    TeamStore must initialize the shared database first. Credentials never create
    a team device or a browser session, and captain ownership is rechecked in the
    database on every authorization, including after a process restart.
    """

    token_pattern = re.compile(r"qojcli_[A-Za-z0-9_-]{43}\Z")

    def __init__(self, db_path):
        self.path = str(db_path)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS cli_tokens (
                team_id TEXT NOT NULL,
                contest_key TEXT NOT NULL,
                owner_sid TEXT NOT NULL,
                token_digest TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                PRIMARY KEY(team_id, contest_key),
                FOREIGN KEY(team_id, contest_key) REFERENCES teams(id, contest_key)
            )""")

    @contextmanager
    def connect(self, *, immediate=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                if immediate:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    @staticmethod
    def _captain(db, owner_sid, team_id, contest_key):
        found = db.execute("""SELECT t.id FROM teams t JOIN team_devices d
                ON d.team_id=t.id AND d.contest_key=t.contest_key
                WHERE t.id=? AND t.contest_key=? AND t.owner_sid=?
                  AND d.sid=t.owner_sid AND d.role='captain'""",
                           (team_id, contest_key, owner_sid)).fetchone()
        if found is None:
            raise CLIAuthError("请在已创建队伍的队长机上管理终端授权。", 403)

    def status(self, owner_sid, team_id, contest_key):
        with self.connect() as db:
            self._captain(db, owner_sid, team_id, contest_key)
            row = db.execute("""SELECT created_at FROM cli_tokens
                WHERE owner_sid=? AND team_id=? AND contest_key=?""",
                             (owner_sid, team_id, contest_key)).fetchone()
            return {"enabled": row is not None, "created_at": row["created_at"] if row else None}

    def issue(self, owner_sid, team_id, contest_key):
        token = "qojcli_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = time.time()
        with self.connect(immediate=True) as db:
            self._captain(db, owner_sid, team_id, contest_key)
            db.execute("""INSERT INTO cli_tokens(team_id,contest_key,owner_sid,token_digest,created_at)
                VALUES(?,?,?,?,?) ON CONFLICT(team_id,contest_key) DO UPDATE SET
                owner_sid=excluded.owner_sid, token_digest=excluded.token_digest,
                created_at=excluded.created_at""", (team_id, contest_key, owner_sid, digest, now))
        return {"token": token, "created_at": now}

    def revoke(self, owner_sid, team_id, contest_key):
        with self.connect(immediate=True) as db:
            self._captain(db, owner_sid, team_id, contest_key)
            db.execute("DELETE FROM cli_tokens WHERE owner_sid=? AND team_id=? AND contest_key=?",
                       (owner_sid, team_id, contest_key))
        return {"enabled": False, "created_at": None}

    def authorize(self, token, contest_key):
        if not isinstance(token, str) or not self.token_pattern.fullmatch(token):
            raise CLIAuthError("终端授权无效；请在队长网页重新生成并导入配置。")
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self.connect() as db:
            row = db.execute("""SELECT c.owner_sid, c.team_id, c.contest_key
                FROM cli_tokens c JOIN teams t
                  ON t.id=c.team_id AND t.contest_key=c.contest_key AND t.owner_sid=c.owner_sid
                JOIN team_devices d
                  ON d.team_id=c.team_id AND d.contest_key=c.contest_key AND d.sid=c.owner_sid
                WHERE c.token_digest=? AND c.contest_key=? AND d.role='captain'""",
                             (digest, contest_key)).fetchone()
        if row is None:
            raise CLIAuthError("终端授权已失效、已撤销或不属于本场比赛；请在队长网页重新生成并导入配置。")
        return dict(row)
