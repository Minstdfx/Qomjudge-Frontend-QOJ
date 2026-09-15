"""Read submission identities and explicit source blocks from native QOJ HTML."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup


ORIGIN = "https://qoj.ac"
QOJ_TIMEZONE = timezone(timedelta(hours=8))
HEADERS = {
    "id": {"id", "#", "编号", "提交编号"},
    "problem_id": {"problem", "题目", "问题"},
    "username": {"submitter", "提交者", "提交人", "username", "用户"},
    "language": {"language", "语言", "编程语言"},
    "submitted_at": {"submit time", "submission time", "提交时间"},
}


def _path(value):
    try:
        parts = urlsplit(urljoin(ORIGIN + "/", value))
        if (parts.scheme != "https" or parts.hostname != "qoj.ac"
                or parts.port not in (None, 443) or parts.username is not None
                or parts.password is not None or "\\" in value):
            return None
        return parts.path
    except (TypeError, ValueError):
        return None


def _text(node):
    return node.get_text(" ", strip=True)


def _time(value):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}(?::[0-9]{2})?(?:Z|[+-][0-9]{2}:[0-9]{2})?", value):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=result.tzinfo or QOJ_TIMEZONE).timestamp()
    except (ValueError, OverflowError):
        return None


def records(source, contest_id=None):
    """Map explicit submission IDs to account/problem metadata.

    Current QOJ omits the language column. Missing language and time stay None;
    the caller must corroborate candidates with their explicit source blocks.
    """
    soup = BeautifulSoup(source, "html.parser")
    result, rejected = {}, set()
    for table in soup.select("table"):
        if (table.find_parent(["pre", "code", "textarea", "script", "template"])
                or table.find_parent(class_="uoj-submission-code")):
            continue
        rows = [row for row in table.select("tr") if row.find_parent("table") is table]
        heading = next((row for row in rows if row.find_all("th", recursive=False)), None)
        positions = {}
        if heading:
            labels = [_text(cell).casefold() for cell in heading.find_all("th", recursive=False)]
            ambiguous = False
            for name, aliases in HEADERS.items():
                matches = [index for index, label in enumerate(labels) if label in aliases]
                if len(matches) > 1:
                    ambiguous = True
                elif matches:
                    positions[name] = matches[0]
            if ambiguous or not {"id", "problem_id", "username"}.issubset(positions):
                continue
        else:
            # Legacy detail fixtures/pages omit headings. Still require exact
            # links in separate ID/problem cells and an explicit account node.
            positions = {"id": 0, "problem_id": 1, "username": 2}
        for row in rows:
            cells = row.find_all("td", recursive=False)
            if not cells or len(cells) <= max(positions.values()):
                continue
            sid_values = set()
            for link in cells[positions["id"]].select("a[href]"):
                path = _path(link["href"])
                match = re.fullmatch(r"(?:/contest/([0-9]+))?/submission/([0-9]+)/?", path or "")
                if match:
                    sid_values.add(int(match[2]))
            if len(sid_values) != 1:
                rejected.update(sid_values)
                continue
            sid = next(iter(sid_values))
            contest_values = set()
            for link in row.select("a[href]"):
                match = re.match(r"/contest/([0-9]+)(?:/|$)", _path(link["href"]) or "")
                if match:
                    contest_values.add(int(match[1]))
            pids = set()
            for link in cells[positions["problem_id"]].select("a[href]"):
                match = re.fullmatch(r"(?:/contest/([0-9]+))?/problem/([0-9]+)/?", _path(link["href"]) or "")
                if match:
                    pids.add(int(match[2]))
            user_cell = cells[positions["username"]]
            usernames = {_text(node) for node in user_cell.select("span.uoj-username") if _text(node)}
            for link in user_cell.select("a[href]"):
                match = re.fullmatch(r"/user/profile/([^/]+)/?", _path(link["href"]) or "")
                if match:
                    usernames.add(unquote(match[1]))
            if (sid <= 0 or len(pids) != 1 or next(iter(pids), 0) <= 0 or len(usernames) != 1 or len(contest_values) > 1
                    or (contest_id is not None and contest_values and contest_values != {contest_id})):
                rejected.add(sid)
                continue
            submitted_text = _text(cells[positions["submitted_at"]]) if "submitted_at" in positions else None
            item = {"username": next(iter(usernames)), "problem_id": next(iter(pids)),
                    "language": (_text(cells[positions["language"]]) or None) if "language" in positions else None,
                    "submitted_at": _time(submitted_text) if submitted_text else None,
                    "submitted_text": submitted_text,
                    "contest_id": next(iter(contest_values)) if contest_values else None}
            if sid in result and result[sid] != item:
                rejected.add(sid)
            else:
                result[sid] = item
    return {sid: item for sid, item in result.items() if sid not in rejected}


def _language(block, container, soup):
    scopes = []
    for node in [block, *block.parents]:
        if node is container or set(node.get("class", [])) & {"card", "panel", "uoj-submission-code", "submission-source"}:
            scopes.append(node)
        if node is container:
            break
    if container not in scopes:
        scopes.append(container)
    if container.name in ("pre", "textarea", "code"):
        panel = container.find_parent(class_=lambda value: value in ("card", "panel", "uoj-submission-code", "submission-source"))
        if panel:
            scopes.append(panel)
    for scope in scopes:
        texts = [_text(node) for node in scope.select(".card-footer, .panel-footer, .card-header, .panel-heading, label")]
        for candidate in (scope, block):
            identifier = candidate.get("id")
            if identifier:
                texts.extend(_text(node) for node in soup.find_all("label", attrs={"for": identifier}))
        values = set()
        for value in texts:
            match = re.search(r"(?:\bLanguage|编程语言|语言)\s*[:：]\s*([^\n\r,;，；]+)", value, re.I)
            if match and match[1].strip():
                values.add(match[1].strip())
        if len(values) == 1:
            return next(iter(values))
        if len(values) > 1:
            return None
    return None


def extract_sources(html):
    """Read source containers only; test input/output pre blocks are excluded."""
    soup = BeautifulSoup(html, "html.parser")
    containers = soup.select(".uoj-submission-code, .submission-source, #submission-source, #source-code, #submission-code, #submission_source, #source_code")
    found, visited = [], set()
    for container in containers:
        blocks = [container] if container.name in ("pre", "textarea", "code") else container.select("pre, textarea")
        for block in blocks:
            source_node = block.select_one("code") if block.name == "pre" else None
            source_node = source_node or block
            if id(source_node) in visited:
                continue
            visited.add(id(source_node))
            found.append({"source": source_node.get_text(), "language": _language(block, container, soup)})
    return found


def fingerprint(source):
    """Ignore CRLF differences and renderer-added final LF, preserving spaces."""
    normalized = source.replace("\r\n", "\n").rstrip("\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
