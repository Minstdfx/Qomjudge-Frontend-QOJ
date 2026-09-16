"""Parse QOJ's server-rendered results without executing page scripts.

The caller must validate the results URL against the selected contest before
calling this parser: the public PC2 document itself need not contain its ID.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString

from qoj import QOJError


def _error():
    return QOJError("QOJ PC2 榜单结构或题目标签无法确认，请检查原生榜单页面。", 502)


def _text(node):
    return node.get_text(" ", strip=True)


def _identity(cell, username):
    """Only explicit account metadata or an exact displayed account name."""
    identities = set()
    for node in [cell, *cell.select("[data-username], [data-user-name]")]:
        for key in ("data-username", "data-user-name"):
            value = node.get(key)
            if isinstance(value, str) and value:
                identities.add(value)
    for link in cell.select("a[href]"):
        target = urlsplit(urljoin("https://qoj.ac/", link["href"]))
        if target.scheme != "https" or target.netloc != "qoj.ac":
            continue
        match = re.fullmatch(r"/user/profile/([^/]+)/?", target.path)
        if match:
            identities.add(unquote(match[1]))
    if len(identities) > 1:
        return "", False
    if identities:
        actual = next(iter(identities))
        return actual, bool(username) and actual == username
    name = _text(cell)
    return name, bool(username) and name == username


def _stat(cell):
    display = _text(cell)
    classes = set(cell.get("class", []))
    pending = "pending" in classes
    numeric = re.fullmatch(r"([0-9]+)/([0-9]+|--|\?+)", display)
    solved = (not pending and "yes" in classes and "no" not in classes
              and numeric is not None and numeric[2].isdigit())
    return {"text": display, "display": display, "solved": bool(solved),
            "pending": pending, "failed": "no" in classes and not pending and not solved,
            "attempts": int(numeric[1]) if numeric else None,
            "time": numeric[2] if solved else ""}


def parse_results_table(source, contest_id, problems, username):
    """Translate the native PC2 table, preserving its text and column order.

    Only a trusted problem-label mapping can identify the otherwise anonymous
    PC2 problem columns. No scoreboard totals or frozen results are recomputed.
    """
    if (not isinstance(source, str) or len(source) > 16 * 1024 * 1024
            or not isinstance(contest_id, int) or isinstance(contest_id, bool) or contest_id <= 0
            or not isinstance(problems, list) or not all(isinstance(problem, dict) for problem in problems)):
        raise _error()
    labels, identifiers = {}, set()
    for problem in problems:
        label, identifier = problem.get("label"), problem.get("id")
        if (not isinstance(label, str) or not label.strip() or label != label.strip()
                or label in labels or not isinstance(identifier, int)
                or isinstance(identifier, bool) or identifier <= 0 or identifier in identifiers):
            raise _error()
        labels[label] = identifier
        identifiers.add(identifier)
    if not labels:
        raise _error()
    soup = BeautifulSoup(source, "html.parser")
    for node in soup.select("script, style, template, noscript, [hidden], [aria-hidden='true'], .d-none"):
        if node.name and node.parent is not None:
            node.decompose()
    for node in soup.select("[style]"):
        if not node.name or node.parent is None:
            continue
        if re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b", node["style"], re.I):
            node.decompose()

    candidates = []
    for table in soup.select("table"):
        for row in table.select("tr"):
            if row.find_parent("table") is not table:
                continue
            cells = row.find_all("th", recursive=False)
            headings = [_text(cell) for cell in cells]
            if headings[:4] == ["Rank", "Name", "Solved", "Time"] and headings[-1:] == ["Total att/solv"]:
                candidates.append((table, row, cells, headings))
                break
    if len(candidates) != 1:
        raise _error()
    table, heading_row, headings, names = candidates[0]
    columns = names[4:-1]
    if len(columns) != len(labels) or set(columns) != set(labels):
        raise _error()
    for heading, label in zip(headings[4:-1], columns):
        for link in heading.select("a[href]"):
            target = urlsplit(urljoin("https://qoj.ac/", link["href"]))
            match = re.fullmatch(r"(?:/contest/([0-9]+))?/problem/([0-9]+)/?", target.path)
            if (match is None or target.scheme != "https" or target.netloc != "qoj.ac"
                    or int(match[2]) != labels[label]
                    or (match[1] is not None and int(match[1]) != contest_id)):
                raise _error()

    result = []
    problem_totals = {}
    for row in table.select("tr"):
        if row is heading_row or row.find_parent("table") is not table:
            continue
        cells = row.find_all("td", recursive=False)
        if (len(cells) == len(names) and not _text(cells[0])
                and _text(cells[1]) == "Submitted/1st Yes/Total Yes"
                and all(cell.get("colspan", "1") == "1" and cell.get("rowspan", "1") == "1" for cell in cells)):
            # The published footer counts attempts, not distinct submitting teams.
            for label, cell in zip(columns, cells[4:-1]):
                match = re.fullmatch(r"([0-9]+|\?+|--)/(?:[0-9]+|\?+|--)/([0-9]+|\?+|--)", _text(cell))
                problem_totals[str(labels[label])] = {
                    "solved": int(match[2]) if match and match[2].isdigit() else None,
                    "submissions": int(match[1]) if match and match[1].isdigit() else None,
                }
            continue
        if not cells or not _text(cells[0]):
            continue  # Native totals/footer rows have no rank.
        if (len(cells) != len(names) or not _text(cells[1])
                or any(cell.get("colspan", "1") != "1" or cell.get("rowspan", "1") != "1" for cell in cells)
                or any(len(_text(cell)) > 4096 for cell in cells)):
            raise _error()
        actual, is_self = _identity(cells[1], username)
        penalty = _text(cells[3])
        stats = {str(labels[label]): _stat(cell) for label, cell in zip(columns, cells[4:-1])}
        result.append({"rank": _text(cells[0]), "team_name": _text(cells[1]), "username": actual,
                       "solved": _text(cells[2]), "score": _text(cells[2]),
                       "penalty": penalty, "penalty_seconds": int(penalty) * 60 if re.fullmatch(r"[0-9]+", penalty) else None,
                       "is_self": is_self, "problem_stats": stats})
        if len(result) > 20000:
            raise _error()
    return {"rows": result, "ruleset": "ICPC", "finalized": None, "source": "qoj-pc2",
            "problem_totals": problem_totals}


def _external_error():
    return QOJError("QOJ External Standings 结构或题目标签无法确认，请检查上游榜单页面。", 502)


def _external_text(cell):
    """Preserve visible line breaks, without treating nested tags as breaks."""
    parts = []
    for node in cell.descendants:
        if isinstance(node, NavigableString) and not isinstance(node, Comment):
            parts.append(re.sub(r"\s+", " ", str(node)))
        elif getattr(node, "name", None) == "br":
            parts.append("\n")
    return "\n".join(line.strip() for line in "".join(parts).split("\n") if line.strip())


def _external_stat(cell):
    display = _external_text(cell)
    classes = set(cell.get("class", []))
    # The official external stylesheet names unpublished cells `frost`.
    # Keep the visible ? marker authoritative even with unfamiliar styling.
    pending = bool(classes & {"frost", "pending"}) or display.startswith("?")
    solved = not pending and "bad" not in classes and bool(classes & {"success", "opener"})
    lines = display.splitlines()
    marker = re.fullmatch(r"([+?\-])([0-9]*)", lines[0]) if lines else None
    attempts = None
    if marker:
        sign, count = marker.groups()
        if sign == "+":
            attempts = int(count or "0") + 1
        elif count:
            attempts = int(count)
        elif sign == "-":
            attempts = 0
    clock = lines[1] if len(lines) == 2 and re.fullmatch(r"[0-9]+:[0-5][0-9]", lines[1]) else ""
    return {"text": display, "display": display, "solved": solved,
            "pending": pending, "failed": "bad" in classes and not pending and not solved,
            "first_blood": solved and "opener" in classes,
            "attempts": attempts, "time": clock if not pending else ""}


def parse_external_results_table(source, contest_id, problems, username):
    """Read the default External Standings table and its explicit opener class.

    The caller validates the contest-specific results URL. The table has no
    reliable contest ID of its own; its ordered labels must match that contest.
    First blood is never inferred from rounded times or another scoreboard.
    """
    if (not isinstance(source, str) or len(source) > 16 * 1024 * 1024
            or not isinstance(contest_id, int) or isinstance(contest_id, bool) or contest_id <= 0
            or not isinstance(problems, list) or not problems):
        raise _external_error()
    labels, identifiers = [], set()
    for problem in problems:
        if not isinstance(problem, dict):
            raise _external_error()
        label, identifier = problem.get("label"), problem.get("id")
        if (not isinstance(label, str) or not label.strip() or label != label.strip()
                or label in labels or not isinstance(identifier, int) or isinstance(identifier, bool)
                or identifier <= 0 or identifier in identifiers):
            raise _external_error()
        labels.append(label)
        identifiers.add(identifier)

    soup = BeautifulSoup(source, "html.parser")
    for node in soup.select("script, style, template, noscript, [hidden], [aria-hidden='true'], .d-none"):
        if node.name and node.parent is not None:
            node.decompose()
    for node in soup.select("[style]"):
        if not node.name or node.parent is None:
            continue
        if re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b", node["style"], re.I):
            node.decompose()

    candidates = []
    for table in soup.select("table.standings"):
        for row in table.select("tr"):
            if row.find_parent("table") is not table:
                continue
            cells = row.find_all("th", recursive=False)
            names = [_text(cell) for cell in cells]
            if names[:2] == ["Rank.", "Username"] and names[-3:] == ["Solved", "Penalty", "Dirt"]:
                candidates.append((table, row, cells))
                break
    if len(candidates) != 1:
        raise _external_error()
    table, heading_row, headings = candidates[0]
    if (len(headings) != len(problems) + 5
            or any(cell.get("colspan", "1") != "1" or cell.get("rowspan", "1") != "1" for cell in headings)):
        raise _external_error()
    problem_totals = {}
    for heading, problem in zip(headings[2:-3], problems):
        summary = re.fullmatch(re.escape(problem["label"]) + r"(?:\s+([0-9]+|\?+|--)/([0-9]+|\?+|--))?", _text(heading))
        if summary is None:
            raise _external_error()
        if summary[1] is not None:
            problem_totals[str(problem["id"])] = {
                "solved": int(summary[1]) if summary[1].isdigit() else None,
                "submissions": int(summary[2]) if summary[2].isdigit() else None,
            }
        for link in heading.select("a[href]"):
            target = urlsplit(urljoin("https://qoj.ac/", link["href"]))
            match = re.fullmatch(r"(?:/contest/([0-9]+))?/problem/([0-9]+)/?", target.path)
            if (match is None or target.scheme != "https" or target.netloc != "qoj.ac"
                    or target.query or target.fragment or int(match[2]) != problem["id"]
                    or (match[1] is not None and int(match[1]) != contest_id)):
                raise _external_error()

    result = []
    for row in table.select("tr"):
        if row is heading_row or row.find_parent("table") is not table:
            continue
        cells = row.find_all("td", recursive=False)
        if not cells or not _text(cells[0]):
            continue  # Native summary rows have an empty rank and fewer cells.
        if (len(cells) != len(headings) or not _text(cells[1])
                or any(cell.get("colspan", "1") != "1" or cell.get("rowspan", "1") != "1" for cell in cells)
                or any(len(_text(cell)) > 4096 or cell.find("table") is not None for cell in cells)):
            raise _external_error()
        actual, is_self = _identity(cells[1], username)
        solved, penalty = _text(cells[-3]), _text(cells[-2])
        stats = {str(problem["id"]): _external_stat(cell) for problem, cell in zip(problems, cells[2:-3])}
        result.append({"rank": _text(cells[0]), "team_name": _text(cells[1]), "username": actual,
                       "solved": solved, "score": solved, "penalty": penalty,
                       "penalty_seconds": int(penalty) * 60 if re.fullmatch(r"[0-9]+", penalty) else None,
                       "is_self": is_self, "problem_stats": stats})
        if len(result) > 20000:
            raise _external_error()
    return {"rows": result, "ruleset": "ICPC", "finalized": None, "source": "qoj-external",
            "problem_totals": problem_totals}
