"""Coordinate native QOJ VP starts; never own clocks, standings or results."""
from __future__ import annotations

import copy
import json
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path


class JointVPError(Exception):
    def __init__(self, message, status=400, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class JointVPService:
    """Callbacks receive (team_id, owner_sid), or those and a private grant.

    observe is GET-only. prepare is GET-only and returns a public form descriptor
    and a fingerprint which covers account, mode and form schema. launch must
    recheck that grant before posting, and must never automatically retry a POST.
    """

    def __init__(self, db_path, contest_key, observe, prepare, launch, *, max_workers=8):
        self.path = str(db_path)
        self.contest_key = str(contest_key)
        self.observe_callback = observe
        self.prepare_callback = prepare
        self.launch_callback = launch
        self._plans = {}
        self._lock = threading.RLock()
        self._futures = set()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="joint-vp")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect(immediate=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS joint_vp_rooms (
                    id TEXT PRIMARY KEY, contest_key TEXT NOT NULL,
                    host_team_id TEXT NOT NULL, invite_code TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS joint_vp_members (
                    team_id TEXT PRIMARY KEY REFERENCES teams(id) ON DELETE CASCADE,
                    room_id TEXT NOT NULL REFERENCES joint_vp_rooms(id) ON DELETE CASCADE,
                    status TEXT NOT NULL, username TEXT NOT NULL DEFAULT '',
                    started_at REAL, ends_at REAL, message TEXT NOT NULL DEFAULT '',
                    grant_json TEXT, revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS joint_vp_roster ON joint_vp_members(room_id);
            """)
            # Browser/QOJ clients do not survive restart. Read-only recovery is
            # safe; replaying a persisted authorization or POST is not.
            db.execute("""UPDATE joint_vp_members SET status=CASE
                    WHEN status='starting' THEN 'uncertain' ELSE 'waiting' END,
                    grant_json=NULL, revision=revision+1,
                    message='服务已重启，请重新连接 QOJ 并核查状态；不会自动发送开赛请求。'
                WHERE room_id IN (SELECT id FROM joint_vp_rooms WHERE contest_key=?)
                    AND status IN ('ready','starting','needs_confirmation')""", (self.contest_key,))
            db.execute("UPDATE joint_vp_rooms SET status='started' WHERE contest_key=? AND status='starting'",
                       (self.contest_key,))
            self._cleanup(db)

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

    def close(self, wait=True):
        self._executor.shutdown(wait=wait)

    @staticmethod
    def _cleanup(db):
        # Dissolving a Team cascades its membership. An orphaned room has no
        # organizer and cannot be started; release all its surviving members.
        db.execute("""DELETE FROM joint_vp_rooms WHERE NOT EXISTS
            (SELECT 1 FROM teams WHERE teams.id=joint_vp_rooms.host_team_id)""")

    def _team(self, db, team, *, captain=True):
        if not isinstance(team, dict) or (captain and team.get("role") != "captain"):
            raise JointVPError("只有队长机可以操作联合 VP。", 403)
        actual = db.execute("SELECT id,owner_sid,name FROM teams WHERE id=? AND contest_key=?",
                            (team.get("id"), self.contest_key)).fetchone()
        if actual is None or actual["owner_sid"] != team.get("owner_sid"):
            raise JointVPError("队伍身份已失效，请刷新页面。", 403)
        if team.get("role") not in ("captain", "member"):
            raise JointVPError("此设备已无权访问队伍。", 403)
        return dict(actual)

    @staticmethod
    def _member(db, team_id):
        row = db.execute("SELECT * FROM joint_vp_members WHERE team_id=?", (team_id,)).fetchone()
        if row is None:
            raise JointVPError("当前队伍尚未加入联合 VP。", 404)
        return dict(row)

    @staticmethod
    def _room(db, member):
        return dict(db.execute("SELECT * FROM joint_vp_rooms WHERE id=?", (member["room_id"],)).fetchone())

    @staticmethod
    def _waiting(room):
        if room["status"] != "waiting":
            raise JointVPError("联合开赛已开始处理，不能修改准备名单。", 409)

    @staticmethod
    def _started(snapshot):
        return snapshot.get("mode") == "vp" and snapshot.get("status") in ("scheduled", "running", "finished", "started")

    @staticmethod
    def _identity(snapshot):
        username = snapshot.get("username")
        if not snapshot.get("authenticated") or not isinstance(username, str) or not username.strip():
            raise JointVPError("请先连接自己的 QOJ 账号。", 409)
        if snapshot.get("mode") != "vp":
            raise JointVPError("联合开赛仅支持 VP，请先切换参赛模式。", 409)
        return username.strip()

    def _project(self, db, team, captain):
        row = db.execute("SELECT * FROM joint_vp_members WHERE team_id=?", (team["id"],)).fetchone()
        if row is None:
            return {"room": None}
        member = dict(row)
        room = self._room(db, member)
        roster = [dict(r) for r in db.execute("""SELECT t.id AS team_id,t.name,m.status,m.started_at,m.ends_at,m.message
            FROM joint_vp_members m JOIN teams t ON t.id=m.team_id
            WHERE m.room_id=? ORDER BY t.created_at,t.id""", (room["id"],))]
        public = {"id": room["id"], "status": room["status"],
                  "is_host": room["host_team_id"] == team["id"], "members": roster,
                  "can_start": (captain and room["status"] == "waiting" and room["host_team_id"] == team["id"]
                                and len(roster) >= 2 and all(m["status"] == "ready" for m in roster)),
                  "self": {key: member[key] for key in ("status", "username", "started_at", "ends_at", "message")}}
        # Account names, upstream errors and form selections are only visible
        # within that team. Other teams get the intentionally minimal roster.
        if captain:
            public["code"] = room["invite_code"]
            with self._lock:
                cached = self._plans.get(team["id"])
                if cached and cached[0] == member["revision"]:
                    public["self"]["plan"] = copy.deepcopy(cached[1])
        return {"room": public}

    def view(self, team, captain=False):
        with self.connect(immediate=True) as db:
            actual = self._team(db, team, captain=False)
            self._cleanup(db)
            return self._project(db, actual, captain and team.get("role") == "captain")

    def create(self, team):
        with self.connect(immediate=True) as db:
            actual = self._team(db, team)
            self._cleanup(db)
            if db.execute("SELECT 1 FROM joint_vp_members WHERE team_id=?", (actual["id"],)).fetchone():
                raise JointVPError("当前队伍已加入联合 VP，请先退出。", 409)
            room_id = secrets.token_hex(16)
            db.execute("INSERT INTO joint_vp_rooms VALUES(?,?,?,?,?,?)",
                       (room_id, self.contest_key, actual["id"], secrets.token_urlsafe(18), "waiting", time.time()))
            db.execute("INSERT INTO joint_vp_members(team_id,room_id,status) VALUES(?,?,'waiting')",
                       (actual["id"], room_id))
            return self._project(db, actual, True)

    def join(self, team, code):
        if not isinstance(code, str) or not 1 <= len(code.strip()) <= 128:
            raise JointVPError("请输入有效的联合 VP 邀请码。")
        with self.connect(immediate=True) as db:
            actual = self._team(db, team)
            self._cleanup(db)
            room = db.execute("SELECT * FROM joint_vp_rooms WHERE contest_key=? AND invite_code=?",
                              (self.contest_key, code.strip())).fetchone()
            if room is None:
                raise JointVPError("联合 VP 邀请码无效。", 404)
            self._waiting(room)
            existing = db.execute("SELECT room_id FROM joint_vp_members WHERE team_id=?", (actual["id"],)).fetchone()
            if existing:
                if existing["room_id"] == room["id"]:
                    return self._project(db, actual, True)
                raise JointVPError("当前队伍已加入其他联合 VP，请先退出。", 409)
            if db.execute("SELECT COUNT(*) FROM joint_vp_members WHERE room_id=?", (room["id"],)).fetchone()[0] >= 20:
                raise JointVPError("联合 VP 最多支持 20 支队伍。", 409)
            db.execute("INSERT INTO joint_vp_members(team_id,room_id,status) VALUES(?,?,'waiting')",
                       (actual["id"], room["id"]))
            return self._project(db, actual, True)

    def leave(self, team):
        with self.connect(immediate=True) as db:
            actual = self._team(db, team)
            member = self._member(db, actual["id"])
            room = self._room(db, member)
            if room["status"] == "starting":
                raise JointVPError("正在处理联合开赛，请等待结果后退出。", 409)
            if room["host_team_id"] == actual["id"]:
                raise JointVPError("组织者请使用取消联合 VP；不会取消已开始的 QOJ VP。", 409)
            db.execute("DELETE FROM joint_vp_members WHERE team_id=?", (actual["id"],))
            with self._lock:
                self._plans.pop(actual["id"], None)
            return {"room": None}

    def cancel(self, team):
        with self.connect(immediate=True) as db:
            actual = self._team(db, team)
            member = self._member(db, actual["id"])
            room = self._room(db, member)
            if room["host_team_id"] != actual["id"]:
                raise JointVPError("只有组织者可以取消联合 VP。", 403)
            if room["status"] == "starting":
                raise JointVPError("正在处理联合开赛，请等待结果后取消。", 409)
            members = [r[0] for r in db.execute("SELECT team_id FROM joint_vp_members WHERE room_id=?", (room["id"],))]
            db.execute("DELETE FROM joint_vp_rooms WHERE id=?", (room["id"],))
            with self._lock:
                for team_id in members:
                    self._plans.pop(team_id, None)
            return {"room": None}

    def guard_mutation(self, team, *, start=False):
        """Call before account/mode/individual-start/team-dissolution changes."""
        with self.connect() as db:
            actual = self._team(db, team)
            row = db.execute("SELECT status FROM joint_vp_members WHERE team_id=?", (actual["id"],)).fetchone()
            if row and row["status"] in ("ready", "starting"):
                raise JointVPError("已准备联合 VP，请先取消准备；正在开赛时请等待结果。", 409, "joint_vp_locked")
            if start and row and row["status"] == "uncertain":
                raise JointVPError("上次开赛请求结果未确认，请先刷新联合 VP 状态核查，不能直接重发。", 409, "joint_vp_locked")

    def detach(self, team):
        """Called before Team dissolution; never roll back any native VP."""
        self.guard_mutation(team)
        view = self.view(team, captain=True)
        if not view["room"]:
            return view
        return self.cancel(team) if view["room"]["is_host"] else self.leave(team)

    def _context(self, team):
        with self.connect() as db:
            actual = self._team(db, team)
            member = self._member(db, actual["id"])
            self._waiting(self._room(db, member))
            if member["status"] == "starting":
                raise JointVPError("正在开赛，请等待结果。", 409)
            return actual, member

    def prepare(self, team):
        actual, before = self._context(team)
        snapshot = self.observe_callback(actual["id"], actual["owner_sid"])
        username = self._identity(snapshot)
        if self._started(snapshot):
            raise JointVPError("这支队伍已经开始 QOJ VP，请刷新状态。", 409)
        plan = self.prepare_callback(actual["id"], actual["owner_sid"])
        if not isinstance(plan, dict) or not isinstance(plan.get("fingerprint"), str) or not plan.get("action"):
            raise JointVPError("QOJ 未提供可确认的开赛表单，请刷新或在原站检查。", 409)
        with self.connect(immediate=True) as db:
            current = self._member(db, actual["id"])
            self._waiting(self._room(db, current))
            if current["room_id"] != before["room_id"] or current["revision"] != before["revision"]:
                raise JointVPError("准备状态已变化，请重新读取报名选项。", 409)
            db.execute("""UPDATE joint_vp_members SET status='needs_confirmation',username=?,grant_json=NULL,
                message='',revision=revision+1 WHERE team_id=?""", (username, actual["id"]))
            with self._lock:
                self._plans[actual["id"]] = (current["revision"] + 1, copy.deepcopy(plan))
            return self._project(db, actual, True)

    def ready(self, team, fingerprint, action, values):
        if not isinstance(fingerprint, str) or not fingerprint or not isinstance(action, str) or not isinstance(values, dict):
            raise JointVPError("准备确认参数无效。")
        if len(json.dumps(values, ensure_ascii=False)) > 16384:
            raise JointVPError("报名选项过长。")
        actual, before = self._context(team)
        snapshot = self.observe_callback(actual["id"], actual["owner_sid"])
        username = self._identity(snapshot)
        if self._started(snapshot):
            raise JointVPError("这支队伍已经开始 QOJ VP，请刷新状态。", 409)
        current_plan = self.prepare_callback(actual["id"], actual["owner_sid"])
        descriptor = current_plan.get("action", {})
        if current_plan.get("fingerprint") != fingerprint or not isinstance(descriptor, dict) or descriptor.get("id") != action:
            raise JointVPError("QOJ 账号或报名选项已变化，请重新准备。", 409)
        # Use exactly the same validation as a native entry. Checkboxes must be
        # explicitly supplied, so server-provided defaults cannot grant consent.
        for field in descriptor.get("fields", []):
            if field.get("type") == "checkbox" and field.get("name") not in values:
                raise JointVPError("请明确选择每一项报名确认选项。")
            if field.get("required") and field.get("name") not in values:
                raise JointVPError("请填写或选择所有必填报名选项。")
        from joint_entry import validate_preparation
        from qoj import QOJError
        try:
            validate_preparation(current_plan, action, values)
        except QOJError as exc:
            raise JointVPError(str(exc), exc.status) from exc
        grant = {"username": username, "fingerprint": fingerprint, "action": action, "values": values}
        with self.connect(immediate=True) as db:
            current = self._member(db, actual["id"])
            self._waiting(self._room(db, current))
            if (current["room_id"] != before["room_id"] or current["revision"] != before["revision"]
                    or current["status"] != "needs_confirmation"):
                raise JointVPError("请先读取本次报名选项，再确认准备。", 409)
            with self._lock:
                cached = self._plans.get(actual["id"])
            if not cached or cached[0] != current["revision"] or cached[1].get("fingerprint") != fingerprint:
                raise JointVPError("报名确认已失效，请重新准备。", 409)
            duplicate = db.execute("""SELECT 1 FROM joint_vp_members WHERE room_id=? AND team_id<>?
                AND lower(username)=lower(?) AND status IN ('ready','starting','started')""",
                (current["room_id"], actual["id"], username)).fetchone()
            if duplicate:
                raise JointVPError("此 QOJ 账号已被本次联合 VP 的其他队伍使用，请使用独立账号。", 409)
            db.execute("""UPDATE joint_vp_members SET status='ready',username=?,grant_json=?,message='',
                revision=revision+1 WHERE team_id=?""", (username, json.dumps(grant, ensure_ascii=False), actual["id"]))
            with self._lock:
                self._plans.pop(actual["id"], None)
            return self._project(db, actual, True)

    def unready(self, team):
        with self.connect(immediate=True) as db:
            actual = self._team(db, team)
            member = self._member(db, actual["id"])
            self._waiting(self._room(db, member))
            db.execute("""UPDATE joint_vp_members SET status='waiting',grant_json=NULL,message='',
                revision=revision+1 WHERE team_id=?""", (actual["id"],))
            with self._lock:
                self._plans.pop(actual["id"], None)
            return self._project(db, actual, True)

    def start(self, team):
        with self.connect(immediate=True) as db:
            actual = self._team(db, team)
            member = self._member(db, actual["id"])
            room = self._room(db, member)
            if room["host_team_id"] != actual["id"]:
                raise JointVPError("只有组织者可以统一开始 VP。", 403)
            self._waiting(room)
            members = [dict(row) for row in db.execute("""SELECT m.*,t.owner_sid FROM joint_vp_members m
                JOIN teams t ON t.id=m.team_id WHERE m.room_id=?""", (room["id"],))]
            if len(members) < 2 or any(m["status"] != "ready" or not m["grant_json"] for m in members):
                raise JointVPError("至少两支队伍加入，且所有队长确认准备后，才能统一开赛。", 409)
            db.execute("UPDATE joint_vp_rooms SET status='starting' WHERE id=?", (room["id"],))
            db.execute("UPDATE joint_vp_members SET status='starting',revision=revision+1 WHERE room_id=?", (room["id"],))
        # No network requests or waits in the request handling transaction.
        for member in members:
            try:
                future = self._executor.submit(self._launch_one, member)
            except RuntimeError:
                self._complete(member, "error", message="开赛任务未发送，请刷新状态后由队长单独操作。")
                continue
            with self._lock:
                self._futures.add(future)
            future.add_done_callback(self._forget_future)
        return self.view(team, captain=True)

    def _forget_future(self, future):
        with self._lock:
            self._futures.discard(future)

    def _launch_one(self, member):
        sent = False
        try:
            grant = json.loads(member["grant_json"])
            snapshot = self.observe_callback(member["team_id"], member["owner_sid"])
            username = self._identity(snapshot)
            if username != grant["username"]:
                raise JointVPError("QOJ 账号已变化，未发送开赛请求，请重新确认。", 409)
            if self._started(snapshot):
                self._complete(member, "started", snapshot)
                return
            plan = self.prepare_callback(member["team_id"], member["owner_sid"])
            if plan.get("fingerprint") != grant["fingerprint"]:
                self._complete(member, "needs_confirmation", message="QOJ 报名选项已变化，未发送开赛请求，请由队长重新确认。")
                return
            sent = True
            snapshot = self.launch_callback(member["team_id"], member["owner_sid"], grant)
            if self._started(snapshot):
                self._complete(member, "started", snapshot)
            elif snapshot.get("entry_pending") or snapshot.get("entry_stage") == "confirmation":
                self._complete(member, "needs_confirmation", snapshot,
                               message="QOJ 要求进一步确认，请由队长在参赛页面完成。")
            else:
                self._complete(member, "uncertain", snapshot,
                               message="QOJ 尚未确认已开赛，请刷新核查；不会自动重发。")
        except Exception:
            # Exception strings may contain a URL, account details or tokens.
            # Only the integration's own safe diagnostics belong in its logs.
            self._complete(member, "uncertain" if sent else "error", message=(
                "开赛请求结果未确认，请刷新核查；不会自动重发。" if sent else
                "开赛前检查未通过，未发送开赛请求。请检查 QOJ 登录、VP 模式及报名选项。"))

    def _complete(self, member, status, snapshot=None, message=""):
        snapshot = snapshot or {}
        with self.connect(immediate=True) as db:
            db.execute("""UPDATE joint_vp_members SET status=?,started_at=?,ends_at=?,message=?,grant_json=NULL,
                revision=revision+1 WHERE team_id=? AND room_id=? AND status='starting'""",
                (status, snapshot.get("started_at"), snapshot.get("ends_at"), message,
                 member["team_id"], member["room_id"]))
            if not db.execute("SELECT 1 FROM joint_vp_members WHERE room_id=? AND status='starting'", (member["room_id"],)).fetchone():
                db.execute("UPDATE joint_vp_rooms SET status='started' WHERE id=? AND status='starting'", (member["room_id"],))

    def refresh(self, team, *, snapshot=None):
        with self.connect() as db:
            actual = self._team(db, team)
            before = self._member(db, actual["id"])
            if before["status"] == "starting":
                return self._project(db, actual, True)
        # An ordinary /api/start can supply the fresh observation it already
        # obtained, so a manual recovery updates the roster without extra GETs.
        if snapshot is None:
            snapshot = self.observe_callback(actual["id"], actual["owner_sid"])
        username = self._identity(snapshot)
        with self.connect(immediate=True) as db:
            member = self._member(db, actual["id"])
            if member["room_id"] != before["room_id"] or member["revision"] != before["revision"]:
                return self._project(db, actual, True)
            if member["username"] and username != member["username"]:
                if member["status"] in ("ready", "needs_confirmation"):
                    db.execute("""UPDATE joint_vp_members SET status='waiting',grant_json=NULL,revision=revision+1,
                        message='QOJ 账号已变化，请重新准备。' WHERE team_id=?""", (actual["id"],))
                else:
                    raise JointVPError("QOJ 账号与本次参赛记录不一致，请连接原账号后核查。", 409)
            elif self._started(snapshot):
                db.execute("""UPDATE joint_vp_members SET status='started',username=?,started_at=?,ends_at=?,
                    message='',grant_json=NULL,revision=revision+1 WHERE team_id=?""",
                    (username, snapshot.get("started_at"), snapshot.get("ends_at"), actual["id"]))
            elif (member["status"] in ("uncertain", "error", "needs_confirmation")
                  and self._room(db, member)["status"] != "waiting"):
                if snapshot.get("status") == "unregistered" and not snapshot.get("entry_uncertain"):
                    db.execute("""UPDATE joint_vp_members SET status='error',revision=revision+1,
                        message='已查询 QOJ，当前尚未开赛；请由队长在参赛页面确认报名选项后单独开始。'
                        WHERE team_id=?""", (actual["id"],))
                else:
                    db.execute("""UPDATE joint_vp_members SET message='本次查询尚未确认开赛；请继续核查 QOJ 状态，不会自动重发。'
                        WHERE team_id=?""", (actual["id"],))
            return self._project(db, actual, True)
