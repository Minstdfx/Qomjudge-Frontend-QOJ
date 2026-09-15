"""Persistent three-device teams; browser session IDs never become device IDs."""
from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class TeamError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _name(value, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TeamError(f"{label}必须是文字。")
    if any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value):
        raise TeamError(f"{label}不能包含换行或控制字符。")
    value = value.strip()
    if not 1 <= len(value) <= maximum:
        raise TeamError(f"{label}长度须为 1–{maximum} 个字符。")
    return value


class TeamStore:
    def __init__(self, db_path):
        self.path = str(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS teams (
                    id TEXT PRIMARY KEY,
                    owner_sid TEXT NOT NULL,
                    contest_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    join_code TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    UNIQUE(owner_sid, contest_key),
                    UNIQUE(id, contest_key)
                );
                CREATE TABLE IF NOT EXISTS team_devices (
                    id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL,
                    sid TEXT NOT NULL,
                    contest_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('captain', 'member', 'revoked')),
                    created_at REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    UNIQUE(sid, contest_key),
                    FOREIGN KEY(team_id, contest_key) REFERENCES teams(id, contest_key)
                );
                CREATE INDEX IF NOT EXISTS team_device_roster ON team_devices(team_id, role);
                CREATE UNIQUE INDEX IF NOT EXISTS team_captain ON team_devices(team_id)
                    WHERE role='captain';
            """)

    @contextmanager
    def connect(self, *, immediate: bool = False):
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
    def _access(db, sid: str, contest_key: str, *, touch: bool = True):
        row = db.execute("""SELECT t.id, t.owner_sid, t.name, t.join_code, t.created_at,
                    d.role, d.id AS device_id, d.name AS device_name
                FROM teams t JOIN team_devices d ON d.team_id=t.id
                WHERE d.sid=? AND d.contest_key=?""", (sid, contest_key)).fetchone()
        if row is None:
            return None
        result = dict(row)
        if touch:
            db.execute("UPDATE team_devices SET last_seen=? WHERE id=?", (time.time(), result["device_id"]))
        result["devices"] = [dict(device) for device in db.execute("""
                SELECT id, name, role, last_seen FROM team_devices
                WHERE team_id=? AND role IN ('captain', 'member')
                ORDER BY CASE role WHEN 'captain' THEN 0 ELSE 1 END, created_at, id
            """, (result["id"],))]
        # owner_sid is private integration data for the existing VP runs table.
        # Public roster entries intentionally contain only random device IDs.
        return result

    def access(self, sid: str, contest_key: str):
        with self.connect() as db:
            return self._access(db, sid, contest_key)

    @staticmethod
    def _captain(db, owner_sid: str, contest_key: str):
        team = db.execute("SELECT * FROM teams WHERE owner_sid=? AND contest_key=?",
                          (owner_sid, contest_key)).fetchone()
        if team is None:
            raise TeamError("只有队长可以管理队伍。", 403)
        return dict(team)

    def create(self, sid: str, contest_key: str, name: str, device_name: str = "队长机"):
        name = _name(name, "队伍名称", 80)
        device_name = _name(device_name, "设备名称", 40)
        with self.connect(immediate=True) as db:
            existing = self._access(db, sid, contest_key, touch=False)
            if existing is not None:
                if existing["role"] == "revoked":
                    raise TeamError("此设备的队伍访问已撤销，请先退出队伍。", 403)
                raise TeamError("当前设备已在队伍中。", 409)
            now = time.time()
            team_id = secrets.token_hex(16)
            db.execute("INSERT INTO teams(id,owner_sid,contest_key,name,join_code,created_at) VALUES(?,?,?,?,?,?)",
                       (team_id, sid, contest_key, name, secrets.token_urlsafe(24), now))
            db.execute("""INSERT INTO team_devices(id,team_id,sid,contest_key,name,role,created_at,last_seen)
                          VALUES(?,?,?,?,?,'captain',?,?)""",
                       (secrets.token_hex(16), team_id, sid, contest_key, device_name, now, now))
            return self._access(db, sid, contest_key)

    def join(self, sid: str, contest_key: str, code: str, device_name: str):
        device_name = _name(device_name, "设备名称", 40)
        if not isinstance(code, str) or not 1 <= len(code.strip()) <= 128:
            raise TeamError("请输入有效的加入码。")
        code = code.strip()
        with self.connect(immediate=True) as db:
            existing = self._access(db, sid, contest_key, touch=False)
            if existing is not None and existing["role"] == "revoked":
                raise TeamError("此设备的队伍访问已撤销，请先退出队伍。", 403)
            team = db.execute("SELECT id FROM teams WHERE contest_key=? AND join_code=?",
                              (contest_key, code)).fetchone()
            if team is None:
                raise TeamError("加入码无效或已更新，请向队长获取当前比赛的加入码。", 404)
            if existing is not None:
                if existing["id"] != team["id"]:
                    raise TeamError("当前设备已在其他队伍中，请先退出。", 409)
                return self._access(db, sid, contest_key)
            count = db.execute("SELECT COUNT(*) FROM team_devices WHERE team_id=? AND role IN ('captain','member')",
                               (team["id"],)).fetchone()[0]
            if count >= 3:
                raise TeamError("队伍已达到 3 台设备上限（1 台队长机和 2 台队员机）。", 409)
            now = time.time()
            db.execute("""INSERT INTO team_devices(id,team_id,sid,contest_key,name,role,created_at,last_seen)
                          VALUES(?,?,?,?,?,'member',?,?)""",
                       (secrets.token_hex(16), team["id"], sid, contest_key, device_name, now, now))
            return self._access(db, sid, contest_key)

    def leave(self, sid: str, contest_key: str):
        with self.connect(immediate=True) as db:
            existing = self._access(db, sid, contest_key, touch=False)
            if existing is None:
                raise TeamError("当前设备未加入队伍。", 404)
            if existing["role"] == "captain":
                raise TeamError("队长机不能退出自己的队伍。", 403)
            db.execute("UPDATE team_devices SET role='revoked', last_seen=? WHERE id=?",
                       (time.time(), existing["device_id"]))
            return self._access(db, sid, contest_key)

    def dissolve(self, captain_sid: str, contest_key: str):
        """Detach all devices and invalidate credentials without deleting VP data."""
        with self.connect(immediate=True) as db:
            existing = self._access(db, captain_sid, contest_key, touch=False)
            if existing is None or existing["role"] != "captain" or existing["owner_sid"] != captain_sid:
                raise TeamError("只有当前队长可以解散队伍。", 403)
            identity = (existing["id"], contest_key)
            # TeamStore also works independently, before optional CLI support
            # has initialized its table. Remove the FK dependent credentials
            # first when present, in the same transaction as team deletion.
            has_cli = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cli_tokens'").fetchone()
            if has_cli:
                db.execute("DELETE FROM cli_tokens WHERE team_id=? AND contest_key=?", identity)
            db.execute("DELETE FROM team_devices WHERE team_id=? AND contest_key=?", identity)
            db.execute("DELETE FROM teams WHERE id=? AND contest_key=?", identity)

    def revoke(self, owner_sid: str, contest_key: str, device_id: str):
        if not isinstance(device_id, str) or len(device_id) != 32 or any(char not in "0123456789abcdef" for char in device_id):
            raise TeamError("设备编号无效。")
        with self.connect(immediate=True) as db:
            team = self._captain(db, owner_sid, contest_key)
            changed = db.execute("""UPDATE team_devices SET role='revoked'
                                  WHERE id=? AND team_id=? AND role='member'""",
                                 (device_id, team["id"])).rowcount
            if not changed:
                raise TeamError("未找到可以移除的队员设备。", 404)
            db.execute("UPDATE teams SET join_code=? WHERE id=?", (secrets.token_urlsafe(24), team["id"]))
            return self._access(db, owner_sid, contest_key)

    def rotate(self, owner_sid: str, contest_key: str):
        with self.connect(immediate=True) as db:
            team = self._captain(db, owner_sid, contest_key)
            db.execute("UPDATE teams SET join_code=? WHERE id=?", (secrets.token_urlsafe(24), team["id"]))
            return self._access(db, owner_sid, contest_key)
