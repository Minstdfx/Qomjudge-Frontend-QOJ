"""DOMjudge-style client for QOJ participation, with local team devices and printing."""
from __future__ import annotations

import argparse
import hmac
import html
import json
import math
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from bs4 import BeautifulSoup
from flask import Flask, Response, g, jsonify, request, send_file, send_from_directory, session
from werkzeug.exceptions import HTTPException

from cli_auth import CLIAuthError, CLITokenStore
from demo import DemoClient
from downloads import attachment_response
from printing import MAX_PRINT_SIZE, MAX_RUN_BYTES, MAX_RUN_FILES, PrintError, PrintStore
from qoj import QOJClient, QOJError, rewrite_proxy
from storage import Store, score
from statements import pdf_response, statement_pdfs
from teams import TeamError, TeamStore

ROOT = Path(__file__).resolve().parent


class AppError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def create_app(contest_id=1802, demo=False, duration_minutes=None, data_dir=None, client_factory=None, freeze_minutes=0, mode="vp", scoreboard="auto"):
    if isinstance(contest_id, bool) or not isinstance(contest_id, int) or contest_id < 1:
        raise ValueError("contest_id 必须是正整数")
    if duration_minutes is not None and not 1 <= duration_minutes <= 10080:
        raise ValueError("duration_minutes 必须在 1 到 10080 之间")
    if isinstance(freeze_minutes, bool) or not isinstance(freeze_minutes, int) or not 0 <= freeze_minutes <= 10080:
        raise ValueError("freeze_minutes 必须是 0 到 10080 之间的整数")
    if mode not in {"vp", "official"}:
        raise ValueError("mode 必须为 vp 或 official")
    if not demo and (duration_minutes is not None or freeze_minutes):
        raise ValueError("真实比赛的时间与封榜由 QOJ 决定，不能在本地覆盖。")
    if scoreboard not in {"auto", "pc2"}:
        raise ValueError("scoreboard 必须为 auto 或 pc2")
    data_dir = Path(data_dir or ROOT / ".data")
    data_dir.mkdir(parents=True, exist_ok=True)
    secret_path = data_dir / "server-secret.key"
    if not secret_path.exists():
        try:
            with secret_path.open("xb") as secret_file:
                secret_file.write(secrets.token_bytes(32))
            if os.name != "nt":
                secret_path.chmod(0o600)
        except FileExistsError:
            pass
    app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
    app.config.update(SECRET_KEY=secret_path.read_bytes(), MAX_CONTENT_LENGTH=3 * 1024 * 1024,
                      SESSION_COOKIE_NAME="qoj_vp_session", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=30 * 86400)
    store = Store(data_dir / "vp.sqlite3")
    teams = TeamStore(data_dir / "vp.sqlite3")
    cli_tokens = CLITokenStore(data_dir / "vp.sqlite3")
    prints = PrintStore(store, data_dir / "prints")
    app.extensions["store"] = store
    app.extensions["teams"] = teams
    app.extensions["cli_tokens"] = cli_tokens
    app.extensions["prints"] = prints
    contest_key = ("demo:" if demo else "qoj:") + str(contest_id)
    factory = client_factory or (DemoClient if demo else QOJClient)
    clients = {}
    registry_lock = threading.Lock()
    app.extensions["clients"] = clients

    def runtime():
        cli = getattr(g, "cli", None)
        if cli is not None:
            sid = cli["owner_sid"]
        elif "sid" not in session:
            session["sid"] = secrets.token_urlsafe(32)
            session["csrf"] = secrets.token_urlsafe(32)
            session.permanent = True
            sid = session["sid"]
        else:
            sid = session["sid"]
        g.team = teams.access(sid, contest_key)
        g.role = g.team["role"] if g.team else "solo"
        if cli is not None and (not g.team or g.role != "captain" or g.team["id"] != cli["team_id"]
                                or g.team["owner_sid"] != sid):
            raise CLIAuthError("终端授权绑定的队长设备已失效，请重新配置。")
        g.owner_sid = g.team["owner_sid"] if g.team and g.role != "revoked" else sid
        with registry_lock:
            sid = g.owner_sid
            if sid not in clients:
                client = factory()
                client.scoreboard_mode = scoreboard
                clients[sid] = {"client": client, "contest": None, "contest_error": None, "contest_error_status": None,
                                "loaded": False, "problems": {}, "last_poll": 0}
            return clients[sid]

    @app.before_request
    def protect_mutations():
        if "Authorization" in request.headers:
            match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]+)", request.headers["Authorization"], re.I)
            if not match:
                raise CLIAuthError("Authorization 须使用有效的 Bearer 终端授权。")
            g.cli = cli_tokens.authorize(match[1], contest_key)
            readable = request.method == "GET" and (request.path in {"/api/state", "/api/prints"}
                                                        or re.fullmatch(r"/api/problems/\d+", request.path))
            writable = request.method == "POST" and request.path in {"/api/submit", "/api/prints"}
            if not readable and not writable:
                raise AppError("此终端授权仅用于读取参赛状态、提交代码和虚拟打印。", 403)
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                raise AppError("请求来源不匹配，请使用当前服务地址。", 403)
        if request.path.startswith("/static/"):
            return
        g.runtime = runtime()
        if getattr(g, "cli", None) is not None and not demo and not g.runtime["client"].authenticated:
            raise AppError("QOJ 账号尚未连接或连接已失效；请在队长网页连接账号后重试。", 401)
        # Device roles come from persistent server records, never client flags.
        if g.role in {"member", "revoked"}:
            common = (request.method in {"GET", "HEAD"} and request.path in {"/", "/healthz", "/api/state", "/api/team"}) or (request.method == "POST" and request.path == "/api/team/leave")
            readable = g.role == "member" and request.method in {"GET", "HEAD"} and (
                re.fullmatch(r"/api/problems/\d+(?:/statement|/pdf/[a-f0-9]{24})?", request.path)
                or re.fullmatch(r"/api/prints(?:/[a-f0-9]{32}/(?:view|download))?", request.path)
                or request.path.startswith("/qoj/"))
            if not common and not readable:
                raise AppError("此设备只有查看权限；该操作须在队长机上进行。", 403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                raise AppError("请求来源不匹配，请从当前服务页面操作。", 403)
            if request.path.startswith("/api/"):
                if getattr(g, "cli", None) is None and not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session["csrf"]):
                    raise AppError("页面会话已过期，请刷新后重试。", 403)
                if not request.is_json and not (request.path == "/api/prints" and request.mimetype == "multipart/form-data"):
                    raise AppError("请求必须使用 JSON。", 415)
            elif request.path.startswith("/qoj"):
                referer = request.headers.get("Referer", "")
                if not origin and urlsplit(referer).netloc != request.host:
                    raise AppError("请从代理登录页提交表单。", 403)

    @app.after_request
    def response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        if not request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(AppError)
    @app.errorhandler(QOJError)
    @app.errorhandler(TeamError)
    @app.errorhandler(PrintError)
    @app.errorhandler(CLIAuthError)
    def known_error(exc):
        return jsonify(error=str(exc)), exc.status

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=exc.description), exc.code

    def payload():
        body = request.get_json()
        if not isinstance(body, dict):
            raise AppError("请求必须是 JSON 对象。")
        return body

    def participation_mode():
        return store.participation_mode(g.owner_sid, contest_key, mode)

    def invalidate_native():
        g.runtime.update(loaded=False, contest=None, problems={})
        g.pop("native_snapshot", None)
        g.pop("native_run", None)

    def native_snapshot(force=False):
        """One fresh upstream observation per HTTP request; errors revoke cached content."""
        if not force and hasattr(g, "native_snapshot"):
            return g.native_snapshot
        rt = g.runtime
        selected_mode = participation_mode()
        try:
            if not callable(getattr(rt["client"], "participation", None)):
                raise AppError("QOJ 适配器不支持原生参赛状态。", 502)
            with rt["client"].lock:
                snapshot = dict(rt["client"].participation(contest_id, selected_mode))
                if snapshot.get("mode", selected_mode) != selected_mode:
                    raise AppError("QOJ 返回了不同参赛模式的状态，请刷新连接。", 502)
                snapshot["mode"] = selected_mode
                entry_notice = rt.get("entry_notice")
                if entry_notice and entry_notice[:2] == (selected_mode, rt["client"].username):
                    if snapshot.get("status") in {"scheduled", "running", "finished"}:
                        rt.pop("entry_notice", None)
                    else:
                        snapshot.update(entry_uncertain=True, message=entry_notice[2])
                visible = (bool(snapshot.get("content_available")) and rt["client"].authenticated
                           and snapshot.get("status") in {"running", "finished"})
                snapshot["content_available"] = visible
                snapshot["can_submit"] = bool(snapshot.get("can_submit")) and snapshot.get("status") == "running" and visible
                contest = snapshot.get("contest")
                if visible and contest is None:
                    contest = rt["client"].contest(contest_id)
                if not visible:
                    contest = {"id": contest_id, "title": f"QOJ Contest #{contest_id}", "duration_seconds": 0,
                               "problems": [], "attachments": []}
                    snapshot["scoreboard"] = []
                elif not isinstance(contest, dict) or not isinstance(contest.get("problems"), list):
                    raise AppError("无法确认 QOJ 当前比赛的题目列表。", 502)
                snapshot["contest"] = contest
                scope = (selected_mode, rt["client"].username, snapshot.get("started_at"), visible)
                if rt.get("native_scope") != scope:
                    rt["problems"].clear()
                rt.update(native_scope=scope, contest=contest, loaded=True, contest_error=None, contest_error_status=None)
                g.native_snapshot = snapshot
                g.pop("native_run", None)
                return snapshot
        except (QOJError, AppError) as exc:
            invalidate_native()
            rt.update(contest_error=str(exc), contest_error_status=exc.status)
            g.native_snapshot = {"mode": selected_mode, "status": "unavailable", "content_available": False,
                                 "can_submit": False, "started_at": None, "ends_at": None, "actions": [],
                                 "scoreboard": [], "message": str(exc), "error_status": exc.status}
            return g.native_snapshot

    def require_content():
        if not demo:
            snapshot = native_snapshot()
            if not snapshot.get("content_available"):
                raise AppError(snapshot.get("message") or "比赛尚未开始，比赛内容暂不可访问。",
                               snapshot.get("error_status") or 403)

    def native_run():
        if hasattr(g, "native_run"):
            return g.native_run
        snapshot = native_snapshot()
        start, end = snapshot.get("started_at"), snapshot.get("ends_at")
        username = g.runtime["client"].username
        valid_times = all(isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t)
                          for t in (start, end))
        if not snapshot.get("content_available") or not username or not valid_times or end <= start:
            g.native_run = None
            return None
        # Account and upstream start identify the run across refresh/restart.
        key = json.dumps([snapshot["mode"], username, start], ensure_ascii=True, separators=(",", ":"))
        name = g.team["name"] if g.team else username
        g.native_run = store.sync_native_run(g.owner_sid, contest_key, key, name, start, end,
                                            finished=snapshot["status"] == "finished")
        return g.native_run

    def load_contest(force=False):
        rt = g.runtime
        if not demo:
            native_snapshot(force=force)
            return rt["contest"]
        if force or not rt["loaded"]:
            rt["loaded"] = True
            try:
                contest = rt["client"].contest(contest_id)
                if not contest.get("problems"):
                    raise AppError("没有找到可访问的比赛题目，请检查比赛 ID 和账号权限。", 502)
                if duration_minutes is not None:
                    contest["duration_seconds"] = duration_minutes * 60
                contest["duration_seconds"] = max(60, int(contest.get("duration_seconds") or 18000))
                rt["contest"] = contest
                rt["contest_error"] = None
                rt["contest_error_status"] = None
                rt["problems"].clear()
            except (QOJError, AppError) as exc:
                rt["contest_error"] = str(exc)
                rt["contest_error_status"] = exc.status
        return rt["contest"]

    def require_contest():
        contest = load_contest()
        if not contest or g.runtime["contest_error"]:
            raise AppError(g.runtime["contest_error"] or "比赛尚未加载。", 409)
        return contest

    def contest_attachments(force=False):
        if not demo and force:
            native_snapshot(force=True)
        require_content()
        contest = load_contest(force=force and demo)
        if not contest or g.runtime["contest_error"]:
            raise AppError(g.runtime["contest_error"] or "比赛尚未加载。",
                           g.runtime["contest_error_status"] or 409)
        return contest.get("attachments", [])

    def find_problem(problem_id):
        require_content()
        contest = require_contest()
        p = next((p for p in contest["problems"] if p["id"] == problem_id), None)
        if p is None:
            raise AppError("该题目不属于当前比赛。", 404)
        return p

    def problem_info(problem_id, force=False):
        p = find_problem(problem_id)
        rt = g.runtime
        if force:
            rt["problems"].pop(problem_id, None)
        if problem_id not in rt["problems"]:
            rt["problems"][problem_id] = rt["client"].problem(contest_id, problem_id)
        return {**p, **rt["problems"][problem_id], "label": p["label"]}

    def current_run():
        if not demo:
            return native_run()
        return store.current(g.owner_sid, contest_key)

    def public_team():
        team = g.team
        if not team:
            return None
        devices = team["devices"] if g.role != "revoked" else []
        result = {"id": team["id"], "name": team["name"], "role": g.role,
                  "device_id": team["device_id"], "device_name": team["device_name"],
                  "devices": devices, "member_count": sum(d["role"] == "member" for d in devices),
                  "max_members": 2}
        if g.role == "captain" and getattr(g, "cli", None) is None:
            result["join_code"] = team["join_code"]
        return result

    def active_run():
        if not demo:
            snapshot = native_snapshot()
            if not snapshot.get("can_submit"):
                raise AppError(snapshot.get("message") or "QOJ 当前未允许参赛提交。", snapshot.get("error_status") or 409)
            run = native_run()
            if run is None:
                raise AppError("无法确认 QOJ 本轮参赛时间，暂不能提交或打印；请刷新连接。", 409)
            return run
        run = current_run()
        if not run or run["finished_at"] is not None or time.time() >= run["ends_at"]:
            raise AppError("VP 尚未开始或已结束，请先开始新一轮 VP。", 409)
        return run

    def submission_retry_delay(exc):
        delay = 60 if exc.status == 429 else 30 if exc.status >= 500 else 0
        retry_after = getattr(exc, "retry_after", None)
        if (isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool)
                and math.isfinite(retry_after) and retry_after >= 0):
            delay = max(delay, retry_after)
        return delay

    def poll(run):
        now = time.time()
        if now - g.runtime["last_poll"] < (1 if demo else 8):
            return
        g.runtime["last_poll"] = now
        client = g.runtime["client"]
        expected_username = client.username
        submissions = store.submissions(run["id"])
        excluded = {sub["qoj_id"] for sub in submissions if sub["qoj_id"] is not None}
        checked = 0
        for sub in sorted(submissions, key=lambda s: s["checked_at"]):
            if sub["verdict"] not in {"PENDING", "UNKNOWN"}:
                continue
            if now < sub["next_poll_at"]:
                continue
            if demo:
                if now - sub["submitted_at"] >= 2:
                    store.update_submission(sub["id"], verdict="AC", checked_at=now,
                                            message="演示结果：模拟 AC，未编译或执行代码。")
            elif now - sub["checked_at"] >= 8:
                receipt = None
                reconciler = getattr(client, "reconcile_submission", None)
                if sub["qoj_id"] is None:
                    saved = store.submission_payload(sub["id"])
                    receipt = saved["receipt"] if saved else None
                    if sub["verdict"] != "UNKNOWN" or receipt is None or not callable(reconciler):
                        continue
                # Keep page refresh latency bounded; rotate fairly through a queue.
                if checked >= 1:
                    break
                checked += 1
                try:
                    qoj_id = sub["qoj_id"]
                    if qoj_id is None:
                        recovered = reconciler(contest_id, sub["problem_id"], sub["language"], receipt,
                                               exclude_ids=tuple(sorted(excluded)))
                        if client.username != expected_username:
                            raise QOJError("QOJ 账号在确认提交编号时发生变化，请重新连接后刷新。", 409)
                        qoj_id = recovered.get("qoj_id")
                        if qoj_id is None:
                            store.update_submission(sub["id"], checked_at=now,
                                                    next_poll_at=0,
                                                    message=recovered.get("message") or "尚未唯一确认 QOJ 提交编号，将继续只读核查。")
                            continue
                        if isinstance(qoj_id, bool) or not isinstance(qoj_id, int) or qoj_id <= 0 or qoj_id in excluded:
                            raise QOJError("QOJ 返回的提交编号无效或已关联其它提交，暂不绑定。", 502)
                        # Persist the confirmed ID before fetching its verdict,
                        # so a later network failure does not lose the binding.
                        store.update_submission(sub["id"], qoj_id=qoj_id,
                                                qoj_url=f"https://qoj.ac/submission/{qoj_id}", verdict="PENDING",
                                                message=recovered.get("message", ""))
                        excluded.add(qoj_id)
                    result = client.verdict(qoj_id)
                    if client.username != expected_username:
                        raise QOJError("QOJ 账号在读取评测结果时发生变化，请重新连接后刷新。", 409)
                    store.update_submission(sub["id"], verdict=result["verdict"],
                                            message=result.get("message", ""), checked_at=now, next_poll_at=0,
                                            time_text=result.get("time_text", ""), memory_text=result.get("memory_text", ""))
                except QOJError as exc:
                    delay = submission_retry_delay(exc)
                    store.update_submission(sub["id"], message=str(exc), checked_at=now,
                                            next_poll_at=now + delay if delay else 0)
                    if exc.status in (401, 409):
                        invalidate_native()
                        raise

    def state():
        rt = g.runtime
        can_manage = g.role in {"solo", "captain"}
        csrf_token = "" if getattr(g, "cli", None) is not None else session["csrf"]
        if g.role == "revoked":
            return {"csrf_token": csrf_token, "demo": demo, "server_time": time.time(),
                    "contest": {"id": contest_id, "title": "设备已离开队伍", "duration_seconds": 0, "problems": []},
                    "contest_error": "队长已移除此设备，请退出后使用新的加入码加入。",
                    "user": {"authenticated": False, "username": ""}, "run": None,
                    "submissions": [], "scoreboard": [], "poll_interval": 8,
                    "team": public_team(), "permissions": {"manage": False, "submit": False, "print": False, "view_content": False},
                    "participation": {"mode": participation_mode(), "status": "unavailable", "content_available": False, "actions": []}}
        if not demo:
            snapshot = native_snapshot()
            visible = snapshot.get("content_available", False)
            contest = snapshot.get("contest") or {"id": contest_id, "title": f"QOJ Contest #{contest_id}",
                                                   "duration_seconds": 0, "problems": []}
            board = snapshot.get("scoreboard", []) if visible else []
            run = current_run() if visible else None
            submissions = []
            if run:
                poll(run)
                submissions = store.submissions(run["id"])
                own_row = next((row for row in board if row.get("is_self")), {})
                run = {k: v for k, v in run.items() if k not in {"sid", "contest_key", "upstream_key", "freeze_seconds"}}
                run.update(status=snapshot["status"], mode=snapshot["mode"],
                           solved=own_row.get("solved"), penalty=own_row.get("penalty"),
                           problem_stats=own_row.get("problem_stats", {}))
            participant = {k: v for k, v in snapshot.items() if k not in {"contest", "scoreboard", "error_status"}}
            allowed = can_manage and snapshot.get("can_submit", False) and run is not None
            return {"csrf_token": csrf_token, "demo": False, "server_time": snapshot.get("server_time") or time.time(),
                    "contest": contest if can_manage else {k: v for k, v in contest.items() if k != "attachments"},
                    "contest_error": rt["contest_error"], "user": {"authenticated": rt["client"].authenticated,
                    "username": rt["client"].username}, "run": run, "submissions": submissions if can_manage else [],
                    "scoreboard": board, "participation": participant, "poll_interval": 8, "team": public_team(),
                    "permissions": {"manage": can_manage, "submit": allowed, "print": allowed, "view_content": visible}}
        contest = load_contest() or {"id": contest_id, "title": f"QOJ Contest #{contest_id}",
                                   "duration_seconds": (duration_minutes or 300) * 60, "problems": []}
        run = current_run()
        submissions = []
        if run:
            poll(run)
            submissions = store.submissions(run["id"])
            run = {k: v for k, v in run.items() if k not in {"sid", "contest_key"}}
            run["status"] = "finished" if run["finished_at"] is not None or time.time() >= run["ends_at"] else "running"
            run.update(score(contest["problems"], submissions))
        board = store.scoreboard(contest_key, g.owner_sid, contest["problems"])
        own_row = next((row for row in board if row["is_self"]), None)
        freeze_at = own_row.get("freeze_at") if own_row else None
        unfreeze_at = own_row.get("unfreeze_at") if own_row else None
        freeze_status = ("frozen" if own_row and own_row.get("frozen") else
                         "unfrozen" if unfreeze_at is not None and time.time() >= unfreeze_at else
                         "scheduled" if freeze_at is not None else "disabled")
        return {"csrf_token": csrf_token, "demo": demo, "server_time": time.time(),
                "contest": contest if can_manage else {k: v for k, v in contest.items() if k != "attachments"}, "contest_error": rt["contest_error"],
                "user": {"authenticated": rt["client"].authenticated, "username": rt["client"].username},
                "run": run, "submissions": submissions if can_manage else [],
                "scoreboard": board,
                "scoreboard_freeze": {"status": freeze_status, "freeze_at": freeze_at, "unfreeze_at": unfreeze_at,
                                      "frozen_teams": sum(bool(row.get("frozen")) for row in board),
                                      "configured_minutes": freeze_minutes, "auto_unfreeze": True},
                "poll_interval": 8, "team": public_team(),
                "permissions": {"manage": can_manage, "submit": can_manage, "print": can_manage, "view_content": True}}

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/healthz")
    def health():
        return jsonify(status="ok", contest_id=contest_id, demo=demo)

    @app.get("/api/state")
    def get_state():
        with g.runtime["client"].lock:
            return jsonify(state())

    @app.get("/api/team")
    def get_team():
        return jsonify(team=public_team())

    def require_cli_captain():
        if not g.team or g.role != "captain" or g.team["owner_sid"] != g.owner_sid:
            raise AppError("请先在本机创建队伍；只有队长机可以管理终端授权。", 403)

    @app.get("/api/cli")
    def get_cli():
        require_cli_captain()
        return jsonify(cli_tokens.status(g.owner_sid, g.team["id"], contest_key))

    @app.post("/api/cli/token")
    def issue_cli_token():
        payload()
        require_cli_captain()
        issued = cli_tokens.issue(g.owner_sid, g.team["id"], contest_key)
        return jsonify(config={"version": 1, "server_url": request.host_url.rstrip("/"),
                               "token": issued["token"], "contest_id": contest_id,
                               "team_id": g.team["id"], "team_name": g.team["name"]},
                       created_at=issued["created_at"])

    @app.post("/api/cli/revoke")
    def revoke_cli_token():
        payload()
        require_cli_captain()
        return jsonify(cli_tokens.revoke(g.owner_sid, g.team["id"], contest_key))

    def rebound_state():
        g.runtime = runtime()
        with g.runtime["client"].lock:
            return jsonify(state())

    @app.post("/api/team/create")
    def create_team():
        body = payload()
        teams.create(session["sid"], contest_key, body.get("name", "My Team"), body.get("device_name", "队长机"))
        return rebound_state()

    @app.post("/api/team/join")
    def join_team():
        body = payload()
        if demo and not g.team:
            run = current_run()
            if run and run["finished_at"] is None and time.time() < run["ends_at"]:
                raise AppError("请先结束当前独立 VP，再加入队伍。", 409)
        teams.join(session["sid"], contest_key, body.get("code"), body.get("device_name", "队员机"))
        return rebound_state()

    @app.post("/api/team/rotate")
    def rotate_team():
        teams.rotate(session["sid"], contest_key)
        return rebound_state()

    @app.post("/api/team/revoke")
    def revoke_device():
        teams.revoke(session["sid"], contest_key, payload().get("device_id"))
        return rebound_state()

    @app.post("/api/team/leave")
    def leave_team():
        teams.leave(session["sid"], contest_key)
        session.clear()
        return rebound_state()

    @app.post("/api/team/dissolve")
    def dissolve_team():
        payload()
        teams.dissolve(session["sid"], contest_key)
        return rebound_state()

    @app.post("/api/refresh")
    def refresh():
        with g.runtime["client"].lock:
            load_contest(force=True)
            return jsonify(state())

    @app.post("/api/mode")
    def select_mode():
        selected = payload().get("mode")
        if selected not in {"vp", "official"}:
            raise AppError("参赛模式须为 vp 或 official。")
        if demo and selected != "vp":
            raise AppError("离线演示仅提供 VP 示例。", 409)
        with g.runtime["client"].lock:
            store.set_participation_mode(g.owner_sid, contest_key, selected)
            invalidate_native()
            return jsonify(state())

    @app.post("/api/connect")
    def connect():
        if demo:
            raise AppError("离线演示无需连接 QOJ。")
        body = payload()
        cookie = body.get("cookie")
        if not isinstance(cookie, str) or not cookie.strip() or len(cookie) > 16384:
            raise AppError("请输入有效的 QOJ Cookie（最多 16 KB）。")
        with g.runtime["client"].lock:
            g.runtime["client"].import_cookies(cookie)
            invalidate_native()
            load_contest(force=True)
            if g.runtime["contest_error"]:
                raise AppError(g.runtime["contest_error"], 401)
            return jsonify(state())

    @app.post("/api/logout")
    def logout():
        with g.runtime["client"].lock:
            g.runtime["client"].clear()
            invalidate_native()
            g.runtime.update(loaded=False, contest=None, contest_error=None, problems={})
            return jsonify(ok=True)

    def start_run(reset=False):
        body = payload()
        team = g.team["name"] if g.team else body.get("team_name", "My Team")
        if not isinstance(team, str) or not team.strip() or len(team.strip()) > 80:
            raise AppError("队名长度须为 1–80 个字符。")
        with g.runtime["client"].lock:
            if not demo:
                if reset:
                    raise AppError("原生参赛不能在本地重新计时；请使用 QOJ 提供的参赛操作。", 409)
                selected = body.get("mode", participation_mode())
                if selected not in {"vp", "official"}:
                    raise AppError("参赛模式须为 vp 或 official。")
                store.set_participation_mode(g.owner_sid, contest_key, selected)
                invalidate_native()
                snapshot = native_snapshot()
                if snapshot.get("error_status"):
                    raise AppError(snapshot.get("message") or "无法确认 QOJ 参赛状态。", snapshot["error_status"])
                if not g.runtime["client"].authenticated:
                    raise AppError("请先连接 QOJ 账号。", 401)
                selection = body.get("selection") or {}
                if not isinstance(selection, dict):
                    raise AppError("请选择有效的 QOJ 参赛选项。")
                # A new explicit attempt replaces any notice from an older one.
                # A rejected POST must not keep masquerading as an earlier
                # request that may have succeeded.
                g.runtime.pop("entry_notice", None)
                try:
                    entered = g.runtime["client"].enter(contest_id, selected,
                                                        selection={"action": body.get("action"), "values": selection})
                    if entered and entered.get("entry_uncertain"):
                        g.runtime["entry_notice"] = (selected, g.runtime["client"].username,
                                                     entered.get("message") or "参赛请求已发出，但状态尚未确认；请刷新检查，勿重复开始。")
                finally:
                    # A failed/uncertain POST may still have reached QOJ. Never
                    # retry or manufacture a local success; refresh on next GET.
                    invalidate_native()
                updated = state()
                participant = updated.get("participation", {})
                if (entered and entered.get("entry_pending") and participant.get("entry_stage") == "confirmation"
                        and participant.get("actions")):
                    return jsonify(updated)
                if participant.get("status") not in {"scheduled", "running", "finished"}:
                    message = participant.get("message") or "QOJ 尚未确认开始参赛，请刷新状态核查。"
                    return jsonify(error=message, state=updated), 409
                return jsonify(updated)
            contest = require_contest()
            if not g.runtime["client"].authenticated:
                raise AppError("请先连接 QOJ 账号。", 401)
            run = current_run()
            if not reset and run and run["finished_at"] is None and time.time() < run["ends_at"]:
                raise AppError("当前 VP 已经开始。", 409)
            store.start(g.owner_sid, contest_key, team.strip(), contest["duration_seconds"],
                        freeze_seconds=freeze_minutes * 60)
            return jsonify(state())

    @app.post("/api/start")
    def start():
        return start_run()

    @app.post("/api/reset")
    def reset():
        return start_run(reset=True)

    @app.post("/api/finish")
    def finish():
        with g.runtime["client"].lock:
            if not demo:
                raise AppError("原生参赛由 QOJ 管理结束时间，不能在本地提前结束。", 409)
            run = active_run()
            store.finish(run["id"])
            return jsonify(state())

    @app.get("/api/problems/<int:problem_id>")
    def get_problem(problem_id):
        with g.runtime["client"].lock:
            info = problem_info(problem_id, force=request.args.get("refresh") == "1")
            visible = {k: v for k, v in info.items() if k != "statement_html"}
            pdfs = statement_pdfs(info, problem_id)
            document = BeautifulSoup(info["statement_html"], "html.parser")
            body = document.body or document
            for node in body.select("iframe, script, style"):
                node.decompose()
            pdf_only = bool(pdfs) and not body.get_text(strip=True) and not body.select("img")
            visible["statement_pdfs"] = [{"id": item["id"], "name": item["name"],
                                          "url": f"/api/problems/{problem_id}/pdf/{item['id']}"} for item in pdfs]
            visible["statement_format"] = "pdf" if pdf_only else "html"
            if g.role == "member":
                visible.update(languages=[], submission_available=False, message="此设备为队员机，提交须在队长机进行。")
            return jsonify({**visible,
                            "statement_url": f"/api/problems/{problem_id}/statement"})

    @app.get("/api/problems/<int:problem_id>/pdf/<pdf_id>")
    def problem_pdf(problem_id, pdf_id):
        client = g.runtime["client"]
        with client.lock:
            info = problem_info(problem_id)
            candidate = next((item for item in statement_pdfs(info, problem_id) if item["id"] == pdf_id), None)
            if candidate is None:
                raise AppError("本题没有此 PDF 题面。", 404)
            try:
                upstream = client.fetch("GET", candidate["url"], stream=True,
                                        headers={"Referer": f"https://qoj.ac/contest/{contest_id}/problem/{problem_id}",
                                                 "Accept-Encoding": "identity"})
                return pdf_response(upstream)
            except QOJError as exc:
                if exc.status == 401:
                    client.authenticated = False
                    client.username = ""
                    g.runtime["loaded"] = False
                raise

    def public_print(item):
        return {**item, "view_url": f"/api/prints/{item['id']}/view",
                "download_url": f"/api/prints/{item['id']}/download"}

    @app.get("/api/prints")
    def list_prints():
        require_content()
        run = current_run()
        files = prints.listing(g.owner_sid, contest_key, run["id"]) if run else []
        return jsonify(files=[public_print(item) for item in files], run_id=run["id"] if run else None,
                       max_file_size=MAX_PRINT_SIZE, max_files=MAX_RUN_FILES, max_total_size=MAX_RUN_BYTES)

    @app.post("/api/prints")
    def create_print():
        with g.runtime["client"].lock:
            active_run()
            if request.is_json:
                body = payload()
                name, content = body.get("name"), body.get("content")
                if not isinstance(content, str):
                    raise AppError("打印内容须为文本。")
                try:
                    content = content.encode("utf-8")
                except UnicodeError:
                    raise AppError("打印内容须为有效的 UTF-8 文本。") from None
            else:
                files = request.files.getlist("file")
                if len(files) != 1 or len(request.files) != 1:
                    raise AppError("每次请选择一份打印文件。")
                name = files[0].filename
                content = files[0].stream.read(MAX_PRINT_SIZE + 1)
            run = active_run()
            item = prints.create(g.owner_sid, contest_key, run["id"], name, content)
            return jsonify(file=public_print(item)), 201

    def print_file(file_id):
        require_content()
        run = current_run()
        if not run:
            raise AppError("本轮 VP 中没有此打印文件。", 404)
        return prints.get(g.owner_sid, contest_key, run["id"], file_id)

    @app.get("/api/prints/<file_id>/download")
    def download_print(file_id):
        item, path = print_file(file_id)
        response = send_file(path, download_name=item["name"], as_attachment=True,
                             mimetype="application/pdf" if item["kind"] == "pdf" else "text/plain")
        response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
        return response

    @app.get("/api/prints/<file_id>/view")
    def view_print(file_id):
        item, path = print_file(file_id)
        if item["kind"] == "pdf":
            response = send_file(path, download_name=item["name"], mimetype="application/pdf")
        else:
            title = html.escape(item["name"])
            body = html.escape(path.read_text(encoding="utf-8-sig"))
            response = Response('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                                f'<title>{title} · 虚拟打印</title><style>body{{margin:24px;font:15px/1.6 system-ui,sans-serif;color:#243647}}'
                                'pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.6 ui-monospace,monospace;border-top:1px solid #ccd3d9;padding-top:16px}'
                                f'</style><h1>{title}</h1><pre>{body}</pre></html>', content_type="text/html; charset=utf-8")
        response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'"
        return response

    @app.get("/api/attachments")
    def get_attachments():
        with g.runtime["client"].lock:
            items = contest_attachments(force=request.args.get("refresh") == "1")
            return jsonify(attachments=[{
                **item,
                "download_url": None if item.get("external") else f"/api/attachments/{item['id']}/download"
            } for item in items], source_url=f"/qoj/contest/{contest_id}", demo=demo)

    @app.get("/api/attachments/<attachment_id>/download")
    def download_attachment(attachment_id):
        client = g.runtime["client"]
        with client.lock:
            item = next((item for item in contest_attachments() if item["id"] == attachment_id), None)
            if item is None:
                raise AppError("该附件不在当前比赛的可访问附件列表中，请刷新列表。", 404)
            if item.get("external"):
                raise AppError("此附件是外部链接，请从列表打开原始链接。", 400)
            if demo:
                content, filename, content_type = client.attachment(attachment_id)
                return send_file(content, download_name=filename, mimetype=content_type,
                                 as_attachment=True, conditional=True)
            headers = {"Referer": f"https://qoj.ac/contest/{contest_id}", "Accept-Encoding": "identity"}
            for key in ("Range", "If-Range"):
                if key in request.headers:
                    headers[key] = request.headers[key]
            try:
                upstream = client.fetch("GET", item["url"], headers=headers, stream=True)
                # Transfer only the independent response stream; the session lock
                # is released so a large download does not block submissions.
                return attachment_response(upstream, item["name"])
            except QOJError as exc:
                if exc.status == 401:
                    client.authenticated = False
                    client.username = ""
                    g.runtime["loaded"] = False
                raise

    @app.get("/api/problems/<int:problem_id>/statement")
    def statement(problem_id):
        with g.runtime["client"].lock:
            html = problem_info(problem_id)["statement_html"]
        response = Response(html, content_type="text/html; charset=utf-8")
        response.headers["Content-Security-Policy"] = "default-src 'none'; script-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; font-src 'self'; frame-src 'self'; object-src 'self'; base-uri 'none'; form-action 'none'"
        return response

    @app.get("/api/submissions/<int:sub_id>/source")
    def submission_source(sub_id):
        with g.runtime["client"].lock:
            require_content()
            run = current_run()
            sub = next((row for row in store.submissions(run["id"]) if row["id"] == sub_id), None) if run else None
            if sub is None:
                raise AppError("当前参赛记录中没有这条提交。", 404)
            saved = store.submission_payload(sub_id)
            if saved is None:
                raise AppError("这条历史提交没有保存源代码，无法在本地查看。", 404)
            return jsonify(source=saved["source"], filename=saved["filename"],
                           language=sub["language"], qoj_id=sub["qoj_id"])

    @app.post("/api/submit")
    def submit():
        body = payload()
        problem_id, language, source = body.get("problem_id"), body.get("language"), body.get("source")
        if isinstance(problem_id, bool) or not isinstance(problem_id, int):
            raise AppError("题目 ID 必须是整数。")
        if not isinstance(source, str) or not source.strip() or len(source.encode("utf-8")) > 256 * 1024:
            raise AppError("源代码不能为空，且不能超过 256 KiB。")
        if not isinstance(language, str):
            raise AppError("请选择编程语言。")
        filename = body.get("filename") or "solution.txt"
        if not isinstance(filename, str) or len(filename) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+\-]*", filename):
            raise AppError("文件名只能包含字母、数字、下划线、点、加号和减号。")
        with g.runtime["client"].lock:
            initial_run = active_run()
            info = problem_info(problem_id)
            if not info.get("submission_available"):
                raise AppError(info.get("message") or "此题暂不支持从本界面提交。", 409)
            if language not in [lang["value"] for lang in info["languages"]]:
                raise AppError("该题不支持所选语言，请重新选择。")
            # Recheck after fetching the form, which may have taken several seconds.
            if not demo:
                native_snapshot(force=True)
            run = active_run()
            if not demo:
                if run["id"] != initial_run["id"]:
                    raise AppError("QOJ 账号或参赛场次在读取题目时发生变化，请刷新后重新提交。", 409)
                find_problem(problem_id)
            sub_id = store.add_submission(run, info, language)
            store.save_submission_payload(sub_id, source, filename)
            try:
                client = g.runtime["client"]
                options = {}
                if callable(getattr(client, "reconcile_submission", None)):
                    options["on_prepared"] = lambda receipt: store.save_submission_receipt(sub_id, receipt)
                result = client.submit(contest_id, problem_id, language, source, filename, **options)
                store.update_submission(sub_id, verdict="PENDING" if demo or result.get("qoj_id") else "UNKNOWN", qoj_id=result.get("qoj_id"),
                                        qoj_url=result.get("qoj_url"), message=result.get("message", ""))
            except QOJError as exc:
                failure = {"verdict": "UNKNOWN", "message": str(exc)}
                if exc.status == 429:
                    now = time.time()
                    failure.update(checked_at=now, next_poll_at=now + submission_retry_delay(exc))
                store.update_submission(sub_id, **failure)
                raise
            sub = next(s for s in store.submissions(run["id"]) if s["id"] == sub_id)
            return jsonify(submission=sub), 201

    def member_resource(target):
        """Only assets actually referenced by this contest's sanitized statements."""
        parsed = urlsplit(target)
        if parsed.netloc and (parsed.scheme != "https" or parsed.netloc != "qoj.ac"):
            return False
        path = parsed.path
        decoded = unquote(unquote(path))
        if any(part in {".", ".."} for part in decoded.split("/")) or "\\" in decoded or any(ord(c) < 32 for c in decoded):
            return False
        if path == decoded and re.fullmatch(r"/js/mathjax/[A-Za-z0-9_./-]+", path):
            return True
        # A problem author can put an action URL in an <img>. Being referenced
        # is necessary, but does not turn a QOJ account action into a safe asset.
        if decoded == "/download.php":
            query = parse_qs(parsed.query, keep_blank_values=True)
            accessible_ids = {str(p["id"]) for p in (g.runtime["contest"] or {}).get("problems", [])}
            if query.get("type") not in (["statement"], ["problem"]) or len(query.get("id", [])) != 1 or query["id"][0] not in accessible_ids:
                return False
        elif not re.fullmatch(r"/(?:images|pictures|uploads|source|storage|resources|static)/[A-Za-z0-9_%./@+\-]+\.(?:png|jpe?g|gif|svg|webp|avif|bmp|ico|pdf)", path, re.I):
            return False
        local = "/qoj" + path + ("?" + parsed.query if parsed.query else "")
        for problem in g.runtime["problems"].values():
            soup = BeautifulSoup(problem.get("statement_html", ""), "html.parser")
            if any(node.get("src") == local for node in soup.select("img[src], iframe[src]")):
                return True
        return False

    @app.route("/qoj", defaults={"path": ""}, methods=["GET", "POST"])
    @app.route("/qoj/<path:path>", methods=["GET", "POST"])
    def proxy(path):
        target = "/" + path
        if request.query_string:
            target += "?" + request.query_string.decode("ascii", errors="strict")
        if g.role == "member" and not member_resource(target):
            raise AppError("队员机只能通过题目页查看本次比赛内容，不能使用 QOJ 操作页面。", 403)
        if demo:
            raise AppError("离线演示模式不访问 QOJ，请以 --contest ID 启动真实比赛。", 409)
        def login_resource(url):
            parsed_url = urlsplit(url)
            if parsed_url.netloc and (parsed_url.scheme != "https" or parsed_url.netloc != "qoj.ac"):
                return False
            resource_path = parsed_url.path
            if unquote(unquote(resource_path)) != resource_path or "\\" in resource_path or ".." in resource_path.split("/"):
                return False
            return bool(re.fullmatch(r"/(?:login(?:/2fa)?|logout|login-2fa|two-factor-auth|captcha)(?:/)?", resource_path)
                        or re.fullmatch(r"/(?:js|css|fonts)/[A-Za-z0-9_./@+\-]+\.(?:js|css|woff2?|ttf|eot|svg)", resource_path)
                        or re.fullmatch(r"/cdn-cgi/scripts/[a-f0-9]+/cloudflare-static/email-decode\.min\.js", resource_path)
                        or resource_path in {"/favicon.ico", "/images/logo.png", "/images/logo_small.png"})

        authentication_asset = g.role in {"solo", "captain"} and login_resource(target)
        if not authentication_asset:
            require_content()
        if g.role == "member" and not member_resource(target):
            raise AppError("队员机只能通过题目页查看本次比赛内容，不能使用 QOJ 操作页面。", 403)
        headers = {}
        for name in ("Content-Type", "Accept", "Range", "X-Requested-With"):
            if name in request.headers:
                headers[name] = request.headers[name]
        headers["Origin"] = "https://qoj.ac"
        referer = request.headers.get("Referer", "")
        parsed = urlsplit(referer)
        headers["Referer"] = "https://qoj.ac" + (parsed.path[4:] if parsed.path.startswith("/qoj/") else "/")
        if parsed.query:
            headers["Referer"] += "?" + parsed.query
        with g.runtime["client"].lock:
            options = ({"url_guard": member_resource} if g.role == "member" else
                       {"url_guard": login_resource} if authentication_asset else {})
            upstream = g.runtime["client"].fetch(request.method, target, headers=headers,
                                               data=request.get_data() if request.method == "POST" else None, **options)
            kind = upstream.headers.get("Content-Type", "application/octet-stream")
            if g.role == "member" and any(t in kind.lower() for t in ("text/html", "application/xhtml+xml")):
                upstream.close()
                raise AppError("题面资源返回了操作网页，请由队长检查 QOJ 连接。", 403)
            body = upstream.content
            if any(t in kind for t in ("text/html", "javascript", "text/css")):
                upstream.encoding = "utf-8"
                body = rewrite_proxy(upstream.text, kind, upstream.url).encode("utf-8")
                if "text/html" in kind:
                    banner = '<div style="padding:10px;background:#173453;color:white;text-align:center"><a style="color:white" href="/" target="_top">← 返回 VP 界面，点击“刷新连接”完成登录同步</a></div>'
                    body = re.sub(rb"(<body[^>]*>)", lambda m: m[0] + banner.encode("utf-8"), body, count=1, flags=re.I)
            response = Response(body, status=upstream.status_code, content_type=kind)
            for name in ("Content-Disposition", "Content-Range", "Accept-Ranges"):
                if name in upstream.headers:
                    response.headers[name] = upstream.headers[name]
            return response

    return app


def main():
    parser = argparse.ArgumentParser(description="QOJ → DOMjudge 风格原生参赛服务")
    parser.add_argument("--contest", type=int, help="QOJ 比赛 ID，例如 1802")
    parser.add_argument("--demo", action="store_true", help="离线演示，不访问 QOJ，也不真实评测")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址；局域网使用 0.0.0.0")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--mode", choices=("vp", "official"), default="vp", help="默认参赛模式：vp 或 official（正式比赛）；也可在页面选择")
    parser.add_argument("--scoreboard", choices=("auto", "pc2"), default="auto", help="榜单来源：自动跟随 QOJ 入口，或指定 Training Mode 的 PC2 results 页面")
    parser.add_argument("--duration", type=int, help="仅离线演示：指定演示时长（分钟）")
    parser.add_argument("--data-dir", default=str(ROOT / ".data"), help="SQLite 和本地会话签名密钥目录")
    parser.add_argument("--secure-cookie", action="store_true", help="在 HTTPS 部署时启用 Secure Cookie")
    args = parser.parse_args()
    if not args.demo and args.contest is None:
        parser.error("请指定 --contest ID，或使用 --demo 查看离线演示。")
    if args.contest is not None and args.contest < 1:
        parser.error("--contest 必须是正整数。")
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1–65535 之间。")
    if args.duration is not None and not 1 <= args.duration <= 10080:
        parser.error("--duration 必须在 1–10080 分钟之间。")
    if not args.demo and args.duration is not None:
        parser.error("真实参赛的赛长由 QOJ 决定，--duration 仅用于 --demo。")
    app = create_app(args.contest or 1, args.demo, args.duration, args.data_dir, mode=args.mode, scoreboard=args.scoreboard)
    app.config["SESSION_COOKIE_SECURE"] = args.secure_cookie
    from waitress import serve
    print(f"QOJ VP: http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}", flush=True)
    print("Mode: offline demo (simulated verdicts)" if args.demo else f"Contest: https://qoj.ac/contest/{args.contest}", flush=True)
    serve(app, host=args.host, port=args.port, threads=12, max_request_body_size=3 * 1024 * 1024)


if __name__ == "__main__":
    main()
