"""Project an ICPC scoreboard into the optional CCPC progressive-reveal view.

This layer never calculates ranks, verdicts, penalties or freeze results. It
only reveals problem identities once enough *published* teams have solved them.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import re
import sqlite3
from pathlib import Path


_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_MARKER = re.compile(r"([+?−-])([0-9]*)(?:\s|$)")
_PC2 = re.compile(r"([0-9]+)/([0-9]+|--|\?+)$")
_ROW_FIELDS = ("rank", "team_name", "username", "solved", "penalty", "score", "penalty_seconds")


def _integer(value, maximum):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class CCPCConfig:
    """Validate command-line values before the server accepts requests."""

    def __init__(self, style="standard", unlock=None, overrides=None, reveal_after=None):
        if style not in ("standard", "ccpc"):
            raise ValueError("榜单样式必须为 standard 或 ccpc。")
        if style == "standard" and any(value is not None for value in (unlock, overrides, reveal_after)):
            raise ValueError("CCPC 参数需要同时指定 --scoreboard-style ccpc。")
        if style == "ccpc" and not _integer(unlock, 20000):
            raise ValueError("--ccpc-unlock 必须为 0 到 20000 的整数。")
        if reveal_after is not None and not _integer(reveal_after, 10080):
            raise ValueError("--ccpc-reveal-after 必须为 0 到 10080 的整数分钟数。")
        parsed = {}
        if isinstance(overrides, str):
            if not overrides.strip():
                raise ValueError("--ccpc-unlock-overrides 不能为空。")
            for entry in overrides.split(","):
                pieces = entry.strip().split("=")
                if len(pieces) != 2 or not re.fullmatch(r"[0-9]+", pieces[1].strip()):
                    raise ValueError("单题阈值格式应为 A=10,C=20。")
                label, value = pieces[0].strip(), int(pieces[1].strip())
                if label in parsed:
                    raise ValueError("单题阈值不能重复指定同一题号。")
                parsed[label] = value
        elif isinstance(overrides, dict):
            parsed = dict(overrides)
        elif overrides is not None:
            raise ValueError("单题阈值格式应为 A=10,C=20。")
        if any(not isinstance(label, str) or not _LABEL.fullmatch(label)
               or not _integer(value, 20000) for label, value in parsed.items()):
            raise ValueError("单题阈值需要有效题号和 0 到 20000 的整数。")
        self.style = style
        self.unlock = unlock
        self.overrides = parsed
        self.reveal_after = reveal_after

    def validate_problems(self, problems):
        if not isinstance(problems, list) or len(problems) > 1000:
            raise ValueError("CCPC 榜单题目列表无效。")
        labels, identifiers = set(), set()
        for problem in problems:
            if not isinstance(problem, dict):
                raise ValueError("CCPC 榜单题目列表无效。")
            label, identifier = problem.get("label"), problem.get("id")
            if (not isinstance(label, str) or not _LABEL.fullmatch(label) or label in labels
                    or not isinstance(identifier, int) or isinstance(identifier, bool)
                    or not 0 < identifier <= 2**63 - 1 or identifier in identifiers):
                raise ValueError("CCPC 榜单题号或题目 ID 无效。")
            labels.add(label)
            identifiers.add(identifier)
        unknown = set(self.overrides) - labels
        if unknown:
            raise ValueError("CCPC 阈值指定了不存在的题号：" + ", ".join(sorted(unknown)))


def _attempts(stat, display):
    value = stat.get("attempts")
    if _integer(value, 10**9):
        return value
    marker = _MARKER.match(display)
    numeric = _PC2.fullmatch(display)
    if numeric:
        return int(numeric[1]) if len(numeric[1]) <= 9 else None
    if marker and len(marker[2]) <= 9:
        if marker[1] == "+":
            return int(marker[2] or "0") + 1
        if marker[2]:
            return int(marker[2])
    if not display or display == "-":
        if not stat.get("solved") and not stat.get("pending") and not stat.get("failed"):
            return 0
    return None


def _clock(value):
    """Return only a recognized elapsed time, never arbitrary upstream text."""
    if _integer(value, 10**9):
        return value * 60, str(value)
    if not isinstance(value, str) or len(value) > 20:
        return None, ""
    if re.fullmatch(r"[0-9]{1,9}", value):
        return int(value) * 60, value
    match = re.fullmatch(r"([0-9]{1,7}):([0-5][0-9])(?::([0-5][0-9]))?", value)
    if match:
        seconds = int(match[1]) * 3600 + int(match[2]) * 60 + int(match[3] or 0)
        return seconds, value
    return None, ""


def _cell_data(stat, demo):
    display = stat.get("display", stat.get("text", ""))
    if display is None:
        display = ""
    if not isinstance(display, (str, int, float)) or isinstance(display, bool):
        raise ValueError("CCPC 榜单成绩格式无效。")
    display = str(display).strip()
    if len(display) > 4096:
        raise ValueError("CCPC 榜单成绩过长。")
    attempts = _attempts(stat, display)
    numeric = _PC2.fullmatch(display)
    pending = (bool(stat.get("pending")) or display.startswith("?")
               or bool(numeric and numeric[2].startswith("?")))
    solved = stat.get("solved") is True and not pending and not stat.get("hacked")
    wrong = bool(stat.get("failed") or stat.get("hacked")) or bool(
        re.match(r"^[−-][0-9]+(?:\s|$)", display))
    if demo and _integer(stat.get("wrong"), 10**9) and stat["wrong"]:
        wrong = True
    status = "pending" if pending else "accepted" if solved else "wrong" if wrong else "neutral"
    placeholder = display in ("", "-") or bool(numeric and numeric[1] == "0" and numeric[2] == "--")
    empty = not (pending or solved or wrong or attempts or not placeholder)
    seconds, clock = _clock(stat.get("time"))
    if not clock:
        lines = display.splitlines()
        if len(lines) == 2 and _MARKER.fullmatch(lines[0]):
            seconds, clock = _clock(lines[1])
        if numeric and numeric[2].isdigit():
            seconds, clock = _clock(numeric[2])
    # Preserve published pending counts, not any hidden pre-freeze success time.
    if status == "accepted":
        wrong_attempts = max(0, attempts - 1) if attempts is not None else None
        text = "+" + (str(wrong_attempts) if wrong_attempts else "")
        if clock:
            text += "\n" + clock
    elif status == "pending":
        text = "?" + (str(attempts) if attempts else "")
    elif status == "wrong":
        text = "−" + (str(attempts) if attempts else "")
        if clock:
            text += "\n" + clock
    else:
        text = f"{attempts} 次提交" if attempts else "—"
    submissions = attempts
    if demo and "submissions" in stat:
        submissions = stat["submissions"] if _integer(stat["submissions"], 10**9) else None
    return {"solved": solved, "attempts": submissions, "status": status,
            "display": text, "seconds": seconds if solved else None, "empty": empty}


class CCPCScoreboard:
    def __init__(self, db_path, config, secret: bytes):
        if not isinstance(config, CCPCConfig):
            raise ValueError("CCPC 榜单配置无效。")
        if not isinstance(secret, bytes) or len(secret) < 16:
            raise ValueError("CCPC 匿名成绩块需要至少 16 字节的服务器密钥。")
        self.path, self.config, self.secret = str(db_path), config, secret
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS ccpc_reveals (
                scope TEXT NOT NULL, problem_id INTEGER NOT NULL, revealed_at REAL NOT NULL,
                PRIMARY KEY (scope, problem_id))""")

    def _key(self, *values):
        data = json.dumps(values, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return hmac.new(self.secret, data, hashlib.sha256).hexdigest()

    def project(self, rows, problems, *, scope: str, now: float, started_at: float | None,
                finished=False, freeze_at=None, frozen=False, demo=False, ruleset="ICPC",
                source="qoj", published_totals=None):
        if self.config.style == "standard":
            return {"rows": copy.deepcopy(rows), "meta": {"style": "standard"}}
        self.config.validate_problems(problems)
        if ruleset != "ICPC":
            raise ValueError("CCPC 动态榜单仅支持可确认的 ICPC 赛制。")
        if (not isinstance(scope, str) or not scope or len(scope) > 16384 or not _finite(now)
                or any(value is not None and not _finite(value) for value in (started_at, freeze_at))):
            raise ValueError("CCPC 榜单参赛范围或时间无效。")
        if not isinstance(rows, list) or len(rows) > 20000:
            raise ValueError("CCPC 榜单行格式无效。")
        config_key = {"unlock": self.config.unlock, "overrides": self.config.overrides,
                      "reveal_after": self.config.reveal_after}
        namespace = self._key("ccpc-v1", scope, config_key,
                              sorted((problem["id"], problem["label"]) for problem in problems))
        prepared = []
        counts = {problem["id"]: {} for problem in problems}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("CCPC 榜单行格式无效。")
            public_row = {}
            for name in _ROW_FIELDS:
                value = row.get(name)
                if value is not None and not (isinstance(value, str) or _finite(value)):
                    raise ValueError("CCPC 榜单行字段无效。")
                if name in row:
                    public_row[name] = value
            public_row["is_self"] = row.get("is_self") is True
            username, team_name = row.get("username"), row.get("team_name")
            if isinstance(username, str) and username.strip():
                identity = ("username", username)
            elif isinstance(team_name, str) and team_name.strip():
                identity = ("team_name", team_name)
            else:
                raise ValueError("CCPC 榜单缺少可确认的队伍标识。")
            stats = row.get("problem_stats")
            if stats is None:
                stats = {}
            if not isinstance(stats, dict):
                raise ValueError("CCPC 榜单题目成绩格式无效。")
            cells = []
            for problem in problems:
                identifier = problem["id"]
                stat = stats.get(str(identifier), stats.get(identifier, {}))
                if not isinstance(stat, dict):
                    raise ValueError("CCPC 榜单题目成绩格式无效。")
                data = _cell_data(stat, demo)
                team_count = counts[identifier].setdefault(identity, {"solved": False, "attempts": 0})
                team_count["solved"] |= data["solved"]
                if team_count["attempts"] is None or data["attempts"] is None:
                    team_count["attempts"] = None
                else:
                    team_count["attempts"] = max(team_count["attempts"], data["attempts"])
                if data["empty"]:
                    continue
                key = self._key("cell", namespace, identity, identifier)
                cells.append((problem, data, key, source in ("qoj", "qoj-external")
                              and not demo and data["solved"] and stat.get("first_blood") is True))
            # Accepted records form one time-ordered group. All other records
            # are ordered independently per team, including ties and unknown times.
            cells.sort(key=lambda item: (0 if item[1]["solved"] else 1,
                                         item[1]["seconds"] if item[1]["seconds"] is not None else math.inf,
                                         item[2]))
            prepared.append((public_row, cells))
        totals = {}
        for identifier, teams in counts.items():
            totals[identifier] = {"solved": sum(team["solved"] for team in teams.values()),
                                  "submissions": None if any(team["attempts"] is None for team in teams.values())
                                  else sum(team["attempts"] for team in teams.values())}
            # Static results pages have no unofficial filtering. Their own
            # footer may include submissions after the first AC, which the
            # displayed scoring attempts omit. Use that total only when its
            # solve count agrees with the full visible roster and it covers
            # every known visible attempt. It never drives the reveal threshold.
            if source in ("qoj-external", "qoj-pc2") and isinstance(published_totals, dict):
                published = published_totals.get(str(identifier), published_totals.get(identifier))
                visible_attempts = totals[identifier]["submissions"]
                if (isinstance(published, dict) and _integer(published.get("solved"), 20000)
                        and published["solved"] == totals[identifier]["solved"]
                        and _integer(published.get("submissions"), 2**53 - 1)
                        and visible_attempts is not None and published["submissions"] >= visible_attempts):
                    totals[identifier]["submissions"] = published["submissions"]
        reveal_all = (finished is True or frozen is True or freeze_at is not None and now >= freeze_at
                      or self.config.reveal_after is not None and started_at is not None
                      and now >= started_at + self.config.reveal_after * 60)
        eligible = [problem["id"] for problem in problems if reveal_all or totals[problem["id"]]["solved"]
                    >= self.config.overrides.get(problem["label"], self.config.unlock)]
        # An atomic insert/read keeps concurrent requests monotonic. The scope
        # includes the visible source, run and account supplied by the caller,
        # plus our own configuration and problem mapping.
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany("INSERT OR IGNORE INTO ccpc_reveals(scope,problem_id,revealed_at) VALUES (?,?,?)",
                           [(namespace, identifier, now) for identifier in eligible])
            revealed = {row[0] for row in db.execute("SELECT problem_id FROM ccpc_reveals WHERE scope=?", (namespace,))}
        result = []
        for row, cells in prepared:
            row["ccpc_cells"] = []
            for problem, data, key, first_blood in cells:
                public_cell = {"key": key, "status": data["status"], "display": data["display"],
                               "first_blood": first_blood, "revealed": problem["id"] in revealed}
                if public_cell["revealed"]:
                    public_cell.update(problem_id=problem["id"], label=problem["label"])
                row["ccpc_cells"].append(public_cell)
            result.append(row)
        revealed_problems = [{"id": problem["id"], "label": problem["label"],
                              "title": problem.get("title", "") if isinstance(problem.get("title", ""), str) else "",
                              "threshold": self.config.overrides.get(problem["label"], self.config.unlock),
                              **totals[problem["id"]]} for problem in problems if problem["id"] in revealed]
        hidden_count = len(problems) - len(revealed_problems)
        message = "题号按当前完整榜单公开的通过队伍数逐步揭晓；排名、罚时和封榜成绩沿用 QOJ。"
        if not hidden_count:
            message = "全部题号已揭晓；排名、罚时和封榜成绩沿用 QOJ，题号揭晓不会解封评测结果。"
        elif freeze_at is None and not frozen and self.config.reveal_after is None:
            message += " 上游未提供可确认的封榜起点；可使用 --ccpc-reveal-after 设置全部题号的揭晓时间。"
        return {"rows": result, "meta": {"style": "ccpc", "default_unlock": self.config.unlock,
                "overrides": dict(self.config.overrides), "reveal_after": self.config.reveal_after,
                "revealed_problems": revealed_problems, "hidden_count": hidden_count,
                "all_revealed": hidden_count == 0, "message": message}}
