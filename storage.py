"""Persistent local virtual contests. Times are UTC Unix seconds; penalty is minutes."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

PENALIZED = {"WA", "TLE", "MLE", "RE", "OLE", "PE"}
PENDING = {"PENDING", "SUBMITTING", "UNKNOWN"}


def score(problems, submissions):
    stats = {str(p["id"]): {"attempts": 0, "wrong": 0, "pending": 0,
                             "solved": False, "time": None} for p in problems}
    solved = penalty = 0
    for sub in sorted(submissions, key=lambda s: (s["elapsed_seconds"], s["id"])):
        item = stats.get(str(sub["problem_id"]))
        if item is None or item["solved"]:
            continue
        item["attempts"] += 1
        verdict = sub["verdict"]
        if verdict == "AC":
            item["solved"] = True
            item["time"] = int(sub["elapsed_seconds"] // 60)
            solved += 1
            penalty += item["time"] + item["wrong"] * 20
        elif verdict in PENALIZED:
            item["wrong"] += 1
        elif verdict in PENDING:
            item["pending"] += 1
    return {"solved": solved, "penalty": penalty, "problem_stats": stats}


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, sid TEXT NOT NULL, contest_key TEXT NOT NULL,
                    team_name TEXT NOT NULL, started_at REAL NOT NULL,
                    ends_at REAL NOT NULL, duration_seconds INTEGER NOT NULL,
                    finished_at REAL, freeze_seconds INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS run_owner ON runs(sid, contest_key, id);
                CREATE TABLE IF NOT EXISTS participation_preferences (
                    sid TEXT NOT NULL, contest_key TEXT NOT NULL, mode TEXT NOT NULL,
                    PRIMARY KEY(sid, contest_key)
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
                    problem_id INTEGER NOT NULL, label TEXT NOT NULL, language TEXT NOT NULL,
                    submitted_at REAL NOT NULL, elapsed_seconds REAL NOT NULL,
                    verdict TEXT NOT NULL, qoj_id INTEGER, qoj_url TEXT,
                    message TEXT NOT NULL DEFAULT '', checked_at REAL NOT NULL DEFAULT 0,
                    next_poll_at REAL NOT NULL DEFAULT 0,
                    time_text TEXT NOT NULL DEFAULT '', memory_text TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS submission_payloads (
                    submission_id INTEGER PRIMARY KEY REFERENCES submissions(id),
                    source TEXT NOT NULL, filename TEXT NOT NULL, receipt TEXT
                );
            """)
            # Existing databases and older runs keep the original, unfrozen
            # behavior. Serialize migration when two app instances start up.
            db.execute("BEGIN IMMEDIATE")
            if "freeze_seconds" not in {row["name"] for row in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN freeze_seconds INTEGER NOT NULL DEFAULT 0")
            if "upstream_key" not in {row["name"] for row in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN upstream_key TEXT")
            if "next_poll_at" not in {row["name"] for row in db.execute("PRAGMA table_info(submissions)")}:
                db.execute("ALTER TABLE submissions ADD COLUMN next_poll_at REAL NOT NULL DEFAULT 0")
            for field in ("time_text", "memory_text"):
                if field not in {row["name"] for row in db.execute("PRAGMA table_info(submissions)")}:
                    db.execute(f"ALTER TABLE submissions ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS native_run ON runs(sid, contest_key, upstream_key) WHERE upstream_key IS NOT NULL")
            # A process killed during a POST must never blindly resubmit it.
            db.execute("UPDATE submissions SET verdict='UNKNOWN', message=? WHERE verdict='SUBMITTING'",
                       ("服务重启前的提交结果未确认；请在 QOJ 核查，程序不会自动重发。",))
            # Older versions mislabeled this exact pre-POST validation error
            # as uncertain. Change only rows with saved code and no receipt;
            # all other UNKNOWN records may represent an actual remote POST.
            rejected_action = "QOJ 提交表单地址与所选题目不一致。"
            db.execute("""UPDATE submissions SET verdict='FAILED', message=?, next_poll_at=0
                WHERE verdict='UNKNOWN' AND qoj_id IS NULL AND message=?
                AND EXISTS (SELECT 1 FROM submission_payloads
                    WHERE submission_id=submissions.id AND receipt IS NULL)""",
                       ("代码尚未发送到 QOJ：" + rejected_action, rejected_action))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def current(self, sid, contest_key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE sid=? AND contest_key=? ORDER BY id DESC LIMIT 1",
                             (sid, contest_key)).fetchone()
        return dict(row) if row else None

    def participation_mode(self, sid, contest_key, default="vp"):
        with self.connect() as db:
            row = db.execute("SELECT mode FROM participation_preferences WHERE sid=? AND contest_key=?",
                             (sid, contest_key)).fetchone()
        return row["mode"] if row else default

    def set_participation_mode(self, sid, contest_key, mode):
        if mode not in {"vp", "official"}:
            raise ValueError("Invalid participation mode")
        with self.connect() as db:
            db.execute("INSERT INTO participation_preferences(sid,contest_key,mode) VALUES(?,?,?) "
                       "ON CONFLICT(sid,contest_key) DO UPDATE SET mode=excluded.mode", (sid, contest_key, mode))

    def sync_native_run(self, sid, contest_key, upstream_key, team_name, started_at, ends_at, finished=False):
        """Bind logs/prints to an upstream participation; never start a local clock."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO runs(sid,contest_key,upstream_key,team_name,started_at,ends_at,duration_seconds) "
                       "VALUES(?,?,?,?,?,?,?)", (sid, contest_key, upstream_key, team_name, started_at, ends_at, int(ends_at - started_at)))
            db.execute("UPDATE runs SET team_name=?,ends_at=?,duration_seconds=?,finished_at=? "
                       "WHERE sid=? AND contest_key=? AND upstream_key=?",
                       (team_name, ends_at, int(ends_at - started_at), ends_at if finished else None, sid, contest_key, upstream_key))
            row = db.execute("SELECT * FROM runs WHERE sid=? AND contest_key=? AND upstream_key=?",
                             (sid, contest_key, upstream_key)).fetchone()
        return dict(row)

    def start(self, sid, contest_key, team_name, duration, now=None, freeze_seconds=0):
        if isinstance(freeze_seconds, bool) or not isinstance(freeze_seconds, int) or not 0 <= freeze_seconds <= 604800:
            raise ValueError("freeze_seconds must be an integer between 0 and 604800")
        freeze_seconds = min(duration, freeze_seconds)
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("UPDATE runs SET finished_at=? WHERE sid=? AND contest_key=? AND finished_at IS NULL",
                       (now, sid, contest_key))
            db.execute("INSERT INTO runs(sid,contest_key,team_name,started_at,ends_at,duration_seconds,freeze_seconds) VALUES(?,?,?,?,?,?,?)",
                       (sid, contest_key, team_name, now, now + duration, duration, freeze_seconds))
        return self.current(sid, contest_key)

    def finish(self, run_id, now=None):
        with self.connect() as db:
            db.execute("UPDATE runs SET finished_at=COALESCE(finished_at,?) WHERE id=?",
                       (time.time() if now is None else now, run_id))

    def submissions(self, run_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM submissions WHERE run_id=? ORDER BY id DESC", (run_id,))]

    def add_submission(self, run, problem, language, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            cursor = db.execute("""INSERT INTO submissions(run_id,problem_id,label,language,
                submitted_at,elapsed_seconds,verdict) VALUES(?,?,?,?,?,?,'SUBMITTING')""",
                (run["id"], problem["id"], problem["label"], language, now, now - run["started_at"]))
            return cursor.lastrowid

    def update_submission(self, sub_id, **fields):
        allowed = {"verdict", "qoj_id", "qoj_url", "message", "checked_at", "next_poll_at", "time_text", "memory_text"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid submission fields")
        with self.connect() as db:
            db.execute("UPDATE submissions SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                       (*fields.values(), sub_id))

    def save_submission_payload(self, sub_id, source, filename):
        """Keep submitted code separate from public submission metadata."""
        with self.connect() as db:
            db.execute("INSERT INTO submission_payloads(submission_id,source,filename) VALUES(?,?,?)",
                       (sub_id, source, filename))

    def save_submission_receipt(self, sub_id, receipt):
        if not isinstance(receipt, dict):
            raise ValueError("Submission receipt must be a JSON object")
        encoded = json.dumps(receipt, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        with self.connect() as db:
            cursor = db.execute("UPDATE submission_payloads SET receipt=? WHERE submission_id=?", (encoded, sub_id))
            if cursor.rowcount != 1:
                raise ValueError("Submission source must be saved before its receipt")

    def submission_payload(self, sub_id):
        with self.connect() as db:
            row = db.execute("SELECT source,filename,receipt FROM submission_payloads WHERE submission_id=?",
                             (sub_id,)).fetchone()
        if row is None:
            return None
        return {"source": row["source"], "filename": row["filename"],
                "receipt": json.loads(row["receipt"]) if row["receipt"] is not None else None}

    def scoreboard(self, contest_key, sid, problems, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            runs = db.execute("""SELECT * FROM runs WHERE id IN
                (SELECT MAX(id) FROM runs WHERE contest_key=? GROUP BY sid)""", (contest_key,)).fetchall()
        result = []
        for row in runs:
            freeze_at = max(row["started_at"], row["ends_at"] - row["freeze_seconds"]) if row["freeze_seconds"] else None
            unfreeze_at = row["ends_at"] if freeze_at is not None else None
            # A manual finish must not reveal a team's frozen verdicts before
            # its scheduled end. Each independent VP uses its own time window.
            frozen = freeze_at is not None and freeze_at <= now < unfreeze_at
            submissions = self.submissions(row["id"])
            if frozen:
                cutoff = freeze_at - row["started_at"]
                # Filter every verdict before scoring. In particular a hidden
                # AC must not suppress later attempts and disclose its result;
                # hidden CE and failed runs are indistinguishable as pending.
                submissions = [{**sub, "verdict": "PENDING"} if sub["elapsed_seconds"] >= cutoff else sub
                               for sub in submissions]
            scored = score(problems, submissions)
            # Header totals count every visible record, including submissions
            # after the first AC; scoring attempts and penalties keep their
            # existing first-acceptance semantics. Never branch on a verdict.
            for item in scored["problem_stats"].values():
                item["submissions"] = 0
            for sub in submissions:
                item = scored["problem_stats"].get(str(sub["problem_id"]))
                if item is not None:
                    item["submissions"] += 1
            result.append({"team_name": row["team_name"], "is_self": row["sid"] == sid,
                           "frozen": frozen, "freeze_at": freeze_at, "unfreeze_at": unfreeze_at,
                           **scored})
        result.sort(key=lambda r: (-r["solved"], r["penalty"], r["team_name"].casefold()))
        rank = 0
        previous = None
        for index, row in enumerate(result, 1):
            key = (row["solved"], row["penalty"])
            if key != previous:
                rank = index
            row["rank"] = rank
            previous = key
        return result
