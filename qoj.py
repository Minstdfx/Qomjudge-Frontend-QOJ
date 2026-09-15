"""QOJ HTTP adapter. Upstream cookies stay in this process, never in local cookies.

The live QOJ forms are the source of truth for CSRF, language and upload fields.
No submission POST is retried, including after an uncertain network result.
"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import math
import re
import threading
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie, CookieError
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Comment


ORIGIN = "https://qoj.ac"
COLORS = ["#e64b55", "#3f86ce", "#e9b949", "#63ab66", "#9361c8", "#e88738", "#45aeb0", "#de79aa", "#9196a3", "#8c7158", "#5374a6", "#b1b34d", "#be616c"]


class QOJError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _upstream_url(path: str, base: str = ORIGIN + "/") -> str:
    """Reject off-site requests and ambiguous URLs before requests sees cookies."""
    if not isinstance(path, str) or any(ord(c) < 32 for c in path) or "\\" in path:
        raise QOJError("无效的 QOJ 地址。", 400)
    parsed = urlsplit(urljoin(base, path))
    try:
        valid = (parsed.scheme == "https" and parsed.hostname == "qoj.ac"
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except ValueError:
        valid = False
    if not valid:
        raise QOJError("反代仅允许访问 https://qoj.ac。", 400)
    return urlunsplit(("https", "qoj.ac", parsed.path or "/", parsed.query, ""))


def _proxy_url(value: str, base: str) -> str:
    value = html.unescape(value.strip())
    if not value or value.startswith("#"):
        return value
    target = urlsplit(urljoin(base, value))
    if target.scheme in ("https", "http") and target.hostname == "qoj.ac":
        path = target.path or "/"
        if not path.startswith("/qoj/"):
            path = "/qoj" + path
        return urlunsplit(("", "", path, target.query, target.fragment))
    return value


def rewrite_proxy(text: str, content_type: str, upstream_url: str) -> str:
    """Rewrite QOJ HTML, JS AJAX paths, and CSS assets to the local /qoj mount.

    Native pages are trusted QOJ application pages. Problem statements are
    separately sanitized by statement_document() before entering the VP UI.
    """
    text = re.sub(r"(?:https?:)?//qoj\.ac(?=[:/?#\"'\s<]|$)(?::443)?", "/qoj", text, flags=re.I)
    # Vendor bundles calculate asset locations from their own script URL. Even
    # '/fonts' is often a suffix concatenated to that directory, not a root URL.
    # Only QOJ's own uoj*.js and inline HTML scripts contain application routes.
    if ("javascript" in content_type or "ecmascript" in content_type) and not urlsplit(upstream_url).path.rsplit("/", 1)[-1].startswith("uoj"):
        return text
    # Only known endpoint prefixes in JavaScript strings: rewriting arbitrary
    # slash strings corrupts MathJax's math delimiters and regular expressions.
    endpoints = r"(?:login|logout|register|reset-password|forgot-password|problem|problems|contest|contests|results|submission|submissions|submission-status-details|user|download\.php|js|css|fonts|images|pictures|cdn-cgi|blog|blogs|announcement|announcements|paste|pastes|api|vote)"
    text = re.sub(r"([\"'`])/(?=" + endpoints + r"(?:/|[?#\"'`]))", r"\1/qoj/", text)
    # QOJ login's successful redirect defaults to the site root. A bare '/'
    # elsewhere (e.g. MathJax or jquery internals) must remain untouched.
    text = re.sub(r"((?:(?:window\.)?location(?:\.href)?|prevUrl)\s*=\s*)(['\"])/\2", r"\1\2/qoj/\2", text)
    text = re.sub(r"((?:window\.)?location\.(?:assign|replace)\(\s*)(['\"])/\2", r"\1\2/qoj/\2", text)
    # QOJ emits unquoted PDF iframe src attributes.
    text = re.sub(r"(\b(?:src|href|action|poster)\s*=\s*)/(?!/|qoj/)", r"\1/qoj/", text, flags=re.I)
    text = re.sub(r"url\(\s*(['\"]?)/(?!/|qoj/)", r"url(\1/qoj/", text, flags=re.I)
    if "html" in content_type:
        # Relative attributes resolve against the original page (not a possibly
        # redirected local request URL). Leave scripts' source text untouched.
        def attr(match):
            prefix, quote, value = match.groups()
            if value.startswith(("/qoj/", "#", "data:", "javascript:", "mailto:")):
                return match.group(0)
            return prefix + quote + _proxy_url(value, upstream_url) + quote
        text = re.sub(r"(\b(?:src|href|action|poster)\s*=\s*)([\"'])(.*?)\2", attr, text, flags=re.I)
        # Once the native login succeeds it must stay inside the proxy.
        text = re.sub(r"(<form\b[^>]*)(>)", lambda m: m.group(0) if re.search(r"\baction\s*=", m.group(1), re.I)
                      else m.group(1) + ' action="' + html.escape(_proxy_url(upstream_url, upstream_url), quote=True) + '"' + m.group(2), text, flags=re.I)
    return text


def _page_title(soup: BeautifulSoup, fallback: str) -> str:
    for selector in ("h1.page-header", ".page-header h1.text-center", "h1.text-center", "h1", "h2.page-header", "title"):
        for node in soup.select(selector):
            value = node.get_text(" ", strip=True)
            if value and value not in ("QOJ", "QOJ.ac", "pjudge", "Public Judge"):
                return re.sub(r"\s+-\s+(?:Problem\s+-\s+)?QOJ\.ac\s*$", "", value).strip()
    return fallback


def _clean_title(title: str) -> str:
    return re.sub(r"^(?:#\d+|[A-Z]{1,3})\s*[.、:：]\s*", "", title).strip()


def statement_document(fragment: str, upstream_url: str) -> str:
    soup = BeautifulSoup(fragment, "html.parser")
    allowed = set("a abbr article b blockquote br caption center code col colgroup dd del details div dl dt em figure figcaption h1 h2 h3 h4 h5 h6 hr i iframe img kbd li mark ol p pre s samp section small span strong sub summary sup table tbody td th thead tr u ul var".split())
    attrs = {"class", "id", "title", "colspan", "rowspan", "width", "height", "align", "alt", "open"}
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()
    for node in list(soup.find_all(True)):
        if not node.name or node.parent is None:
            continue
        if node.name in ("script", "style", "form", "input", "button", "textarea", "select", "object", "embed", "link", "meta", "base", "svg", "math"):
            node.decompose()
            continue
        if node.name not in allowed:
            node.unwrap()
            continue
        for key in list(node.attrs):
            if key not in attrs | {"href", "src"}:
                del node[key]
        for key in ("href", "src"):
            if key not in node.attrs:
                continue
            value = str(node[key]).strip()
            parsed = urlsplit(urljoin(upstream_url, value))
            if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
                del node[key]
            else:
                node[key] = _proxy_url(value, upstream_url) if parsed.hostname == "qoj.ac" else parsed.geturl()
        if node.name == "a":
            node["target"] = "_blank"
            node["rel"] = "noopener noreferrer"
        if node.name == "iframe":
            parsed = urlsplit(str(node.get("src", "")))
            query = parse_qs(parsed.query)
            is_statement = parsed.path == "/qoj/download.php" and query.get("type") == ["statement"]
            if not is_statement and not (parsed.path.startswith("/qoj/") and parsed.path.lower().endswith(".pdf")):
                node.decompose()
                continue
            node["title"] = "QOJ problem statement PDF"
            node["width"] = "100%"
            node["height"] = "850"
            node["loading"] = "lazy"
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>body{font:16px/1.7 system-ui,sans-serif;color:#253349;margin:24px}img{max-width:100%;height:auto}pre{white-space:pre-wrap;background:#f4f6f8;padding:16px;border:1px solid #dfe4ea;overflow:auto}table{border-collapse:collapse;max-width:100%}td,th{border:1px solid #dfe4ea;padding:6px 12px}iframe{border:0;min-height:75vh}a{color:#2672b8}</style>'
            '<script src="/static/math-config.js"></script>'
            '<script defer src="/qoj/js/mathjax/MathJax.js?config=TeX-AMS_SVG"></script></head><body>'
            + str(soup) + '</body></html>')


def _duration(soup: BeautifulSoup, source: str) -> int | None:
    text = soup.get_text(" ", strip=True)
    match = re.search(r"(?:Duration|Length|比赛时长|比赛长度|时长)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|小时|minutes?|mins?|min|分钟)", text, re.I)
    if match:
        hours = match[2].lower() in ("h", "hour", "hours", "hr", "hrs", "小时")
        duration = int(float(match[1]) * (3600 if hours else 60))
        if hours:
            minutes = re.match(r"\s*(?:,|and|及)?\s*(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|min|分钟)", text[match.end():], re.I)
            if minutes:
                duration += int(float(minutes[1]) * 60)
        return duration
    match = re.search(r"(?:Duration|Length|比赛时长|比赛长度|时长)\s*[:：]?\s*(\d{1,3}):(\d{2})(?::(\d{2}))?", text, re.I)
    if match:
        return int(match[1]) * 3600 + int(match[2]) * 60 + int(match[3] or 0)
    match = re.search(r"[\"']?(?:last_min|duration_minutes)[\"']?\s*[:=]\s*(\d+)", source)
    if match:
        return int(match[1]) * 60
    times = []
    for label in (r"(?:Start\s*(?:time)?|开始时间)", r"(?:End\s*(?:time)?|结束时间)"):
        match = re.search(label + r"\s*[:：]?\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)", text, re.I)
        if match:
            times.append(datetime.fromisoformat(match[1]))
    if len(times) == 2 and times[1] > times[0]:
        return int((times[1] - times[0]).total_seconds())
    return None


def _attachment_url(value: str, base_url: str) -> str | None:
    """Normalize advertised links, never derive a download endpoint or trust <base>."""
    if not isinstance(value, str) or not value.strip() or value.strip().startswith("#"):
        return None
    # urlsplit strips some literal controls; reject them before normalization.
    decoded = unquote(value)
    if any(ord(char) < 32 or 127 <= ord(char) < 160 for char in decoded) or "\\" in decoded:
        return None
    try:
        parts = urlsplit(urljoin(base_url, value.strip()))
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None or parts.password is not None:
            return None
        port = parts.port
        hostname = parts.hostname.lower()
        if hostname == "qoj.ac":
            if port not in (None, 80 if parts.scheme == "http" else 443):
                return None
            return urlunsplit(("https", "qoj.ac", parts.path or "/", parts.query, ""))
        host = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None and port != (443 if parts.scheme == "https" else 80):
            host += f":{port}"
        return urlunsplit((parts.scheme, host, parts.path or "/", parts.query, parts.fragment))
    except ValueError:
        return None


def parse_attachments(source: str, contest_id: int, base_url: str | None = None) -> list[dict]:
    """Read the contest's Attachments block without requesting any linked files.

    QOJ publishes this as a card-header followed by list-group links. The panel
    and semantic-heading forms cover older UOJ markup. Links in contest prose,
    navigation and question/discussion tables are deliberately not collected.
    """
    soup = BeautifulSoup(source, "html.parser")
    base_url = base_url or f"{ORIGIN}/contest/{int(contest_id)}"
    headings = soup.select("h1, h2, h3, h4, h5, h6, .card-header, .panel-heading")
    candidates = []
    for heading in headings:
        label = heading.get_text(" ", strip=True).rstrip(" :：").casefold()
        if label not in {"attachments", "contest attachments", "附件", "比赛附件"}:
            continue
        container = heading.find_parent(class_=lambda value: value and ("card" in value.split() or "panel" in value.split()))
        if container is not None:
            candidates.extend(container.select(".list-group a[href], .card-body a[href], .panel-body a[href]"))
            continue
        # For plain heading + list/table markup, stop at the next section.
        for sibling in heading.next_siblings:
            if getattr(sibling, "name", None) in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                break
            if getattr(sibling, "name", None) == "div" and "list-group" not in sibling.get("class", []):
                break
            if getattr(sibling, "name", None) in {"ul", "ol", "table", "div", "p", "a"}:
                if sibling.name == "a" and sibling.has_attr("href"):
                    candidates.append(sibling)
                else:
                    candidates.extend(sibling.select("a[href]"))
    result, seen = [], set()
    for link in candidates:
        if link.find_parent(["nav", "form"]) or link.find_parent(class_=lambda value: value and any(name in value.split() for name in ("navbar", "nav", "breadcrumb"))):
            continue
        url = _attachment_url(link.get("href"), base_url)
        if not url or url in seen:
            continue
        parts = urlsplit(url)
        external = parts.hostname != "qoj.ac"
        # A QOJ download.php link identifies its owner explicitly. Problem
        # statements and other contests must not enter this contest's files.
        if not external and parts.path == "/download.php":
            query = parse_qs(parts.query, keep_blank_values=True)
            if query.get("type") != ["attachments"] or query.get("id") != [str(int(contest_id))]:
                continue
        elif not external:
            if re.match(r"^/(?:login|logout|register|user|submission|submissions|discussions?|issues?|problems?|contests?)(?:/|$)", parts.path):
                continue
        label = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).strip()
        fallback = link.get("download") or link.get("title") or unquote(parts.path.rsplit("/", 1)[-1])
        if not label:
            label = str(fallback) if fallback and fallback != "download.php" else "比赛附件"
        label = "".join(char for char in label if ord(char) >= 32 and not 127 <= ord(char) < 160)[:300]
        kind = "attachment"
        if re.search(r"tutorial|editorial|solution|题解|解説|解说", label, re.I):
            kind = "tutorial"
        elif re.search(r"statement|题面|题目描述|问题描述", label, re.I):
            kind = "statement"
        seen.add(url)
        result.append({"id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:24],
                       "name": label or "比赛附件", "url": url, "type": kind, "external": external})
    return result


def parse_contest(source: str, contest_id: int, allow_empty: bool = False) -> dict:
    soup = BeautifulSoup(source, "html.parser")
    problems, seen = [], set()
    # Problem rows avoid sidebar/history references, retaining the upstream order.
    for row in soup.select("table tr"):
        candidate = None
        for link in row.select("a[href]"):
            path = urlsplit(urljoin(ORIGIN, link["href"])).path
            match = re.fullmatch(rf"/contest/{contest_id}/problem/(\d+)/?", path)
            if not match:
                match = re.fullmatch(r"/problem/(\d+)/?", path)
            if match:
                candidate = (link, int(match[1]))
                break
        if not candidate or candidate[1] in seen:
            continue
        cells = row.find_all(["td", "th"], recursive=False)
        # Submission/history tables must never become contest problem lists.
        if row.select_one('a[href*="/submission/"]') or len(cells) > 7:
            continue
        link, pid = candidate
        label = cells[0].get_text(" ", strip=True) if cells else ""
        if not re.fullmatch(r"[A-Z]{1,3}", label):
            label = chr(65 + len(problems)) if len(problems) < 26 else str(len(problems) + 1)
        seen.add(pid)
        problems.append({"id": pid, "label": label, "title": _clean_title(link.get_text(" ", strip=True)), "color": COLORS[(len(problems)) % len(COLORS)]})
    if not problems and not allow_empty:
        raise QOJError("QOJ 页面没有可读取的比赛题目。请确认比赛 ID、账号访问权限以及比赛是否已公开。", 422)
    duration = _duration(soup, source)
    return {"id": contest_id, "title": _page_title(soup, f"QOJ Contest {contest_id}"), "duration_seconds": duration,
            "problems": problems, "attachments": parse_attachments(source, contest_id)}


def _submission_form(soup: BeautifulSoup) -> tuple:
    form = soup.select_one("form#form-answer")
    if not form:
        button = soup.select_one('[name="submit-answer"]')
        form = button.find_parent("form") if button else None
    if not form:
        return None, [], []
    groups = []
    for script in form.select("script"):
        text = script.string or script.get_text()
        pattern = r"\.source_code_form_group\(\s*(['\"])([^'\"]+)\1\s*,\s*(['\"])(?:\\.|(?!\3).)*\3\s*,\s*(\"(?:\\.|[^\"\\])*\")\s*(?:,\s*(true|false))?"
        for match in re.finditer(pattern, text, re.S):
            try:
                options = BeautifulSoup(json.loads(match[4]), "html.parser")
            except (ValueError, TypeError):
                continue
            langs = [{"value": opt.get("value", ""), "label": opt.get_text(" ", strip=True)} for opt in options.select("option[value]")]
            groups.append({"name": match[2], "languages": langs, "file_only": match[5] == "true"})
    if not groups:
        for select in form.select('select[name$="_language"]'):
            name = select["name"][:-9]
            groups.append({"name": name, "languages": [{"value": opt["value"], "label": opt.get_text(" ", strip=True)} for opt in select.select("option[value]")], "file_only": False})
    # Reject output-only and multi-file tasks instead of silently dropping files.
    unsupported = form.select('input[type="file"]') if not groups else []
    if re.search(r"\.text_file_form_group\(", str(form)):
        unsupported = [True]
    return form, groups, unsupported


def parse_problem(source: str, problem_id: int, upstream_url: str) -> dict:
    soup = BeautifulSoup(source, "html.parser")
    title = _page_title(soup, f"Problem {problem_id}")
    label_match = re.match(r"([A-Z]{1,3})\s*[.、:]", title)
    statement = soup.select_one("#tab-statement") or soup.select_one("article.uoj-article") or soup.select_one("article")
    if statement is None:
        raise QOJError("无法识别 QOJ 题面；请在 QOJ 页面检查题目权限。", 422)
    form, groups, unsupported = _submission_form(soup)
    available = bool(form and len(groups) == 1 and groups[0]["languages"] and not unsupported)
    result = {"id": problem_id, "title": _clean_title(title), "label": label_match[1] if label_match else "", "statement_html": statement_document(str(statement), upstream_url), "languages": groups[0]["languages"] if len(groups) == 1 else [], "submission_available": available}
    if not available:
        result["message"] = "此题未提供可识别的单文件代码提交表单；多文件、交互 grader 或提交答案题请使用 QOJ 原生页面。"
    text = soup.get_text(" ", strip=True)
    for key, pattern in (("time_limit", r"(?:Time Limit|时间限制)\s*[:：]\s*([\d.]+\s*(?:s|秒))"), ("memory_limit", r"(?:Memory Limit|空间限制|内存限制)\s*[:：]\s*([\d.]+\s*(?:MB|MiB|KB|GB))")):
        match = re.search(pattern, text, re.I)
        if match:
            result[key] = match[1]
    return result


VERDICTS = [
    ("CE", r"(?:compile(?:\s+time)?|compiler|compilation)\s+error|compilation\s+failed|编译错误|编译失败"),
    ("TLE", r"time\s+limit\s+exceeded|时间超限|超时"),
    ("MLE", r"memory\s+limit\s+exceeded|内存超限|空间超限"),
    ("OLE", r"output\s+limit\s+exceeded|输出超限"),
    ("RE", r"run[\s-]*time\s+error|run[- ]error|运行错误"),
    ("PE", r"presentation\s+error|格式错误"),
    ("JE", r"(?:judg(?:e|e?ment|ing)|system|internal)\s+(?:failed|error)|评测失败|系统错误|内部错误"),
    ("WA", r"wrong\s+answer|答案错误"),
    ("AC", r"accepted|答案正确|通过"),
    ("PENDING", r"waiting|judging|compiling|running|pending|in\s+queue|等待|评测中|编译中"),
]

# QOJ's ICPC/Training view uses TL and ML, while the local result codes
# follow the TLE/MLE convention. Preserve the upstream text as the message.
VERDICT_ALIASES = {"TL": "TLE", "ML": "MLE", "OL": "OLE", "RTE": "RE", "JF": "JE", "IE": "JE"}


def parse_verdict(source: str, qoj_id: int) -> dict:
    soup = BeautifulSoup(source, "html.parser")
    result_cell = None
    metrics = {"time_text": "", "memory_text": ""}
    referenced_ids = {int(match[1]) for a in soup.select('table a[href]')
                      if (match := re.fullmatch(r"/submission/(\d+)/?", urlsplit(urljoin(ORIGIN, a["href"])).path))}
    for table in soup.select("table"):
        headers = [h.get_text(" ", strip=True).lower() for h in table.select("thead th")]
        index = next((i for i, h in enumerate(headers) if h in ("result", "verdict", "status", "结果", "评测结果", "状态")), None)
        for row in table.select("tbody tr") or table.select("tr"):
            identity = any(re.fullmatch(rf"/submission/{qoj_id}/?", urlsplit(urljoin(ORIGIN, a["href"])).path) for a in row.select("a[href]"))
            if not identity:
                continue
            cells = row.find_all("td", recursive=False)
            for key, aliases in (("time_text", {"time", "运行时间", "用时", "时间"}),
                                 ("memory_text", {"memory", "内存", "空间", "内存使用"})):
                metric_index = next((i for i, header in enumerate(headers) if header in aliases), None)
                if metric_index is not None and metric_index < len(cells):
                    metrics[key] = cells[metric_index].get_text(" ", strip=True)
            if index is not None and len(cells) > index:
                result_cell = cells[index]
                break
            score = row.select_one(".uoj-score")
            if score:
                result_cell = score.find_parent("td") or score
                break
            if len(cells) >= 4:
                result_cell = cells[3]
                break
        if result_cell:
            break
    if result_cell is None and not referenced_ids:
        result_cell = soup.select_one(".uoj-score") or soup.select_one(".submission-verdict") or soup.select_one("[data-verdict]")
    if result_cell is None:
        return {"verdict": "UNKNOWN", "message": "QOJ 页面中未找到可确认的评测结果。", **metrics}
    text = result_cell.get_text(" ", strip=True)
    direct = re.sub(r"\s*[✓✔√✗✘×]\s*$", "", text.replace("\ufe0f", "")).upper().strip()
    direct = VERDICT_ALIASES.get(direct, direct)
    if direct in {code for code, _ in VERDICTS}:
        return {"verdict": direct, "message": text, **metrics}
    for code, pattern in VERDICTS:
        if re.search(pattern, text, re.I):
            return {"verdict": code, "message": text, **metrics}
    score_node = result_cell.select_one(".uoj-score") or (result_cell if "uoj-score" in result_cell.get("class", []) else None)
    score_text = score_node.get_text(" ", strip=True) if score_node else text
    score_match = re.fullmatch(r"(\d+(?:\.\d+)?)(?:\s*/\s*(\d+(?:\.\d+)?))?\s*[✓✔]?", score_text)
    if score_match:
        score = float(score_match[1])
        full = score_node.get("data-full") if score_node else None
        if full is not None and not re.fullmatch(r"\d+(?:\.\d+)?", full):
            return {"verdict": "UNKNOWN", "message": text, **metrics}
        maximum = float(score_match[2]) if score_match[2] else float(full) if full is not None else 100.0
        if (not math.isfinite(score) or not math.isfinite(maximum) or maximum <= 0
                or (full is not None and float(full) != maximum)):
            return {"verdict": "UNKNOWN", "message": text, **metrics}
        if score == maximum:
            return {"verdict": "AC", "message": f"{score:g} / {maximum:g}", **metrics}
        if 0 <= score < maximum:
            # Use detailed failure labels only after the aggregate score proves
            # the run is not accepted. Never match source-code text or a title.
            for node in soup.select(".panel-title, .card-title, .uoj-test-result, .test-result"):
                details = node.get_text(" ", strip=True)
                for code, pattern in VERDICTS:
                    if code not in ("AC", "PENDING") and re.search(pattern, details, re.I):
                        return {"verdict": code, "message": f"{details} ({score_text})", **metrics}
            return {"verdict": "WA", "message": f"QOJ score: {score_text}", **metrics}
    return {"verdict": "UNKNOWN", "message": text or "QOJ 尚未公布可识别结果。", **metrics}


class QOJCookieJar(requests.cookies.RequestsCookieJar):
    """Keep one QOJ cookie per name/path when the upstream renews a session.

    Cookie request headers do not carry their original Domain attribute. QOJ
    uses .qoj.ac, while imported and host-only cookies otherwise use qoj.ac.
    Treat these as one scope in this client, which only permits qoj.ac requests.
    This also makes expiry remove an imported value instead of reviving it.
    """
    @staticmethod
    def _domain(domain):
        return ".qoj.ac" if domain and domain.lstrip(".").lower() == "qoj.ac" else domain

    def set_cookie(self, cookie, *args, **kwargs):
        if self._domain(cookie.domain) == ".qoj.ac":
            cookie = copy.copy(cookie)
            cookie.domain = ".qoj.ac"
            cookie.domain_specified = True
            cookie.domain_initial_dot = True
        return super().set_cookie(cookie, *args, **kwargs)

    def clear(self, domain=None, path=None, name=None):
        return super().clear(self._domain(domain), path, name)


class QOJClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.cookies = QOJCookieJar()
        self.lock = threading.RLock()
        self.authenticated = False
        self.username = ""
        self._cooldown_until = 0.0
        self._vp_entry_pages = {}

    def clear(self):
        with self.lock:
            self.session.cookies.clear()
            self.authenticated = False
            self.username = ""
            self._vp_entry_pages.clear()

    def import_cookies(self, cookie_header: str):
        if not isinstance(cookie_header, str) or len(cookie_header) > 32768 or any(ord(c) < 32 for c in cookie_header):
            raise QOJError("Cookie 格式无效；请粘贴单行 Cookie 请求头。", 400)
        raw = re.sub(r"^\s*Cookie\s*:\s*", "", cookie_header, flags=re.I).strip()
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except CookieError:
            raise QOJError("Cookie 格式无效。", 400) from None
        if not cookie or not all("=" in part for part in raw.split("; ")):
            raise QOJError("Cookie 为空或格式无效。", 400)
        with self.lock:
            self.clear()
            for name, morsel in cookie.items():
                self.session.cookies.set(name, morsel.value, domain="qoj.ac", path="/", secure=True)

    def fetch(self, method: str, path: str, **kwargs) -> requests.Response:
        method = method.upper()
        if method not in {"GET", "HEAD", "POST"}:
            raise QOJError("不支持的请求方法。", 405)
        url = _upstream_url(path)
        url_guard = kwargs.pop("url_guard", None)
        if url_guard is not None and not url_guard(url):
            raise QOJError("此设备无权访问该 QOJ 资源。", 403)
        kwargs.pop("allow_redirects", None)
        kwargs.setdefault("timeout", (10, 35))
        streaming = bool(kwargs.get("stream"))
        headers = dict(kwargs.pop("headers", {}) or {})
        for name in list(headers):
            if name.lower() in ("host", "cookie", "authorization", "proxy-authorization"):
                del headers[name]
        headers.setdefault("Referer", ORIGIN + "/")
        if method == "POST":
            headers.setdefault("Origin", ORIGIN)
        history = []
        with self.lock:
            for _ in range(8):
                remaining = self._cooldown_until - time.monotonic()
                if remaining > 0:
                    limited = QOJError("QOJ 请求正在限流冷却，稍后会自动重试。", 429)
                    limited.retry_after = max(1, math.ceil(remaining))
                    raise limited
                try:
                    response = self.session.request(method, url, headers=headers, allow_redirects=False, **kwargs)
                except requests.RequestException:
                    detail = "QOJ 网络请求失败，请检查服务器网络连接。"
                    if method == "POST":
                        detail = "提交请求的网络结果不确定，请先在 QOJ 检查记录，勿立即重复提交。"
                    raise QOJError(detail, 502) from None
                if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("Location"):
                    try:
                        next_url = _upstream_url(response.headers["Location"], url)
                        if url_guard is not None and not url_guard(next_url):
                            raise QOJError("此设备无权访问资源重定向的目标。", 403)
                        history.append(response)
                        if method == "POST":
                            if response.status_code in (307, 308):
                                raise QOJError("QOJ 要求重复发送 POST；为避免重复提交已停止。请检查 QOJ 记录。", 502)
                            method = "GET"
                            for key in ("data", "json", "files"):
                                kwargs.pop(key, None)
                            headers.pop("Content-Type", None)
                            headers.pop("Content-Length", None)
                    finally:
                        if streaming:
                            response.close()
                    kwargs.pop("params", None)
                    url = next_url
                    continue
                response.history = history
                # The download route must retain Content-Range on an
                # unsatisfiable range, instead of returning a generic 502.
                if streaming and response.status_code == 416:
                    return response
                try:
                    if response.status_code == 403 and ("Just a moment" in response.text or "cf-chl" in response.text):
                        raise QOJError("QOJ 返回了 Cloudflare 验证页面。请在浏览器完成验证后导入同网络环境的 Cookie。", 502)
                    if response.status_code == 429:
                        limited = QOJError("QOJ 暂时限制了请求频率，请稍后重试。", 429)
                        retry = response.headers.get("Retry-After", "")
                        delay = 60
                        if re.fullmatch(r"\d+", retry):
                            delay = max(delay, int(retry))
                        elif retry:
                            try:
                                # Use the server's Date to avoid local clock skew.
                                baseline = parsedate_to_datetime(response.headers["Date"]).timestamp() if response.headers.get("Date") else time.time()
                                delay = max(delay, math.ceil(parsedate_to_datetime(retry).timestamp() - baseline))
                            except (ValueError, TypeError, OverflowError):
                                pass
                        limited.retry_after = delay
                        self._cooldown_until = time.monotonic() + delay
                        raise limited
                    if response.status_code >= 400:
                        if response.status_code == 401:
                            self.authenticated = False
                            self.username = ""
                        raise QOJError(f"QOJ 返回 HTTP {response.status_code}。请检查资源和账号权限。", response.status_code if response.status_code in (401, 403, 404) else 502)
                    if urlsplit(response.url).path.startswith("/login"):
                        if response.text.strip() == "ok":
                            self.authenticated = True
                        elif response.text.strip() in ("2fa", "pending_2fa") or 'id="form-login"' in response.text:
                            self.authenticated = False
                except Exception:
                    if streaming:
                        response.close()
                    raise
                return response
        raise QOJError("QOJ 页面重定向次数过多。", 502)

    def _page(self, path: str, **kwargs) -> requests.Response:
        response = self.fetch("GET", path, **kwargs)
        self._observe_page(response)
        return response

    def _observe_page(self, response):
        """Observe the authenticated navbar on both GET and POST responses."""
        soup = BeautifulSoup(response.text, "html.parser")
        if urlsplit(response.url).path.startswith("/login") or soup.select_one("form#form-login"):
            self.authenticated = False
            self.username = ""
            raise QOJError("此 QOJ 页面需要登录，请先连接 QOJ 账号。", 401)
        # The navbar, not author credits in the footer, identifies the account.
        menu_links = soup.select(".nav-pills a[href], nav a[href], .navbar a[href]")
        if any(urlsplit(urljoin(ORIGIN, node["href"])).path == "/login" for node in menu_links):
            self.authenticated = False
            self.username = ""
            raise QOJError("QOJ 当前页面显示账号未登录，请重新连接账号。", 401)
        for node in soup.select('nav a[href*="/user/profile/"], .navbar a[href*="/user/profile/"], .nav-pills a[href*="/user/profile/"]'):
            match = re.search(r"/user/profile/([^/?#]+)", node.get("href", ""))
            if match:
                self.username = unquote(match[1])
                self.authenticated = True
                break
        if any(urlsplit(urljoin(ORIGIN, node["href"])).path == "/logout" for node in soup.select(".nav a[href], nav a[href], .navbar a[href]")):
            self.authenticated = True

    def _same_account(self, expected):
        if not expected or self.username != expected:
            raise QOJError("QOJ 账号在确认提交时发生变化，请重新连接原账号后刷新。", 409)

    def contest(self, contest_id: int) -> dict:
        with self.lock:
            response = self._page(f"/contest/{int(contest_id)}")
            result = parse_contest(response.text, int(contest_id))
            # Current QOJ requires authentication to display contest pages.
            self.authenticated = True
            return result

    def participation(self, contest_id: int, mode: str = "vp") -> dict:
        from participation import participation
        with self.lock:
            return participation(self, int(contest_id), mode)

    def enter(self, contest_id: int, mode: str = "vp", selection=None) -> dict:
        from participation import enter
        with self.lock:
            return enter(self, int(contest_id), mode, selection)

    def problem(self, contest_id: int, problem_id: int) -> dict:
        with self.lock:
            response = self._page(f"/contest/{int(contest_id)}/problem/{int(problem_id)}")
            return parse_problem(response.text, int(problem_id), response.url)

    @staticmethod
    def _rows(source: str, problem_id: int, username: str, language: str) -> dict:
        from submission_data import records
        if not username:
            return {}
        normalize = lambda value: re.sub(r"\s+", "", value).casefold()
        return {sid: row for sid, row in records(source).items()
                if row["problem_id"] == problem_id and row["username"] == username
                and (not language or not row.get("language") or normalize(row["language"]) == normalize(language))}

    def submit(self, contest_id: int, pid: int, language: str, source: str, filename: str, on_prepared=None) -> dict:
        from submission_data import fingerprint, records
        with self.lock:
            expected_username = self.username
            response = self._page(f"/contest/{int(contest_id)}/problem/{int(pid)}")
            if expected_username and self.username != expected_username:
                raise QOJError("QOJ 账号已变化，请刷新参赛状态后重新提交。", 409)
            soup = BeautifulSoup(response.text, "html.parser")
            form, groups, unsupported = _submission_form(soup)
            if not form or len(groups) != 1 or unsupported:
                raise QOJError("此题不是受支持的单文件代码提交表单，请使用 QOJ 原生页面。", 422)
            group = groups[0]
            if language not in {item["value"] for item in group["languages"]}:
                raise QOJError("该语言不在此题当前允许的语言列表中。", 400)
            token = form.select_one('input[name="_token"]')
            if not token or not token.get("value"):
                raise QOJError("QOJ 提交表单缺少 CSRF token，请刷新后重试。", 502)
            action = _upstream_url(form.get("action") or response.url, response.url)
            if urlsplit(action).path not in (f"/problem/{int(pid)}", f"/contest/{int(contest_id)}/problem/{int(pid)}"):
                raise QOJError("QOJ 提交表单地址与所选题目不一致。", 422)
            list_path = f"/contest/{int(contest_id)}/submissions"
            before = self._page(list_path)
            if expected_username and self.username != expected_username:
                raise QOJError("QOJ 账号已变化，已停止向另一账号发送代码。", 409)
            expected_username = self.username
            self._same_account(expected_username)
            if urlsplit(before.url).path != list_path:
                raise QOJError("QOJ 未返回当前比赛的提交列表，已停止发送代码。", 502)
            previous = records(before.text, int(contest_id))
            try:
                observed_at = parsedate_to_datetime(before.headers["Date"]).timestamp()
            except (KeyError, ValueError, TypeError, OverflowError):
                observed_at = time.time()
            receipt = {"version": 1, "contest_id": int(contest_id), "problem_id": int(pid),
                       "username": expected_username, "language": language,
                       "language_labels": [item["label"] for item in group["languages"] if item["value"] == language],
                       "source_sha256": fingerprint(source), "old_ids": sorted(previous),
                       "min_id": max(previous, default=0), "submitted_after": observed_at - 2,
                       "submitted_before": observed_at + 120}
            fields = {field["name"]: field.get("value", "") for field in form.select('input[type="hidden"][name]')}
            button = form.select_one('[name="submit-answer"]')
            if not button:
                raise QOJError("无法识别 QOJ 提交按钮。", 422)
            fields[button["name"]] = button.get("value", "answer")
            fields[group["name"] + "_language"] = language
            fields[group["name"] + "_upload_type"] = "file"
            filename = re.split(r"[/\\]", filename or "main.cpp")[-1]
            filename = re.sub(r"[^\w. -]", "_", filename)[:128] or "main.cpp"
            # Persist the evidence before the one and only POST. A restart or
            # timeout can then resume GET-only confirmation without resending.
            if on_prepared is not None:
                on_prepared(receipt)
            posted = self.fetch("POST", action, data=fields,
                                files={group["name"] + "_file": (filename, source.encode("utf-8"), "text/plain")},
                                headers={"Referer": response.url})
            self._observe_page(posted)
            self._same_account(expected_username)
            match = re.fullmatch(r"/submission/(\d+)/?", urlsplit(posted.url).path)
            if match:
                sid = int(match[1])
                if sid <= receipt["min_id"] or sid in previous:
                    raise QOJError("QOJ 返回了已有提交编号，正在核对本次新提交。", 502)
                return {"qoj_id": sid, "qoj_url": f"{ORIGIN}/submission/{sid}"}
            result_soup = BeautifulSoup(posted.text, "html.parser")
            error = result_soup.select_one(".alert-danger, .uoj-error, #error, .error-message")
            if error:
                raise QOJError("QOJ 拒绝提交：" + error.get_text(" ", strip=True)[:400], 422)
            result = self._confirm_submission(receipt, initial=posted)
            if result.get("qoj_id"):
                return result
            raise QOJError(result["message"], 502)

    def reconcile_submission(self, contest_id, pid, language, receipt, exclude_ids=()):
        """Recover an uncertain POST using persisted evidence and GETs only."""
        with self.lock:
            if (not isinstance(receipt, dict) or receipt.get("version") != 1
                    or receipt.get("contest_id") != int(contest_id) or receipt.get("problem_id") != int(pid)
                    or receipt.get("language") != language
                    or not isinstance(receipt.get("source_sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", receipt["source_sha256"])):
                raise QOJError("此记录缺少可核对的提交凭据。", 422)
            self._same_account(receipt.get("username"))
            return self._confirm_submission(receipt, exclude_ids=exclude_ids)

    def _confirm_submission(self, receipt, initial=None, exclude_ids=()):
        from submission_data import extract_sources, fingerprint, records
        username, cid, pid = receipt["username"], receipt["contest_id"], receipt["problem_id"]
        self._same_account(username)
        excluded = set(receipt["old_ids"]) | set(exclude_ids)
        normalize = lambda value: re.sub(r"\s+", "", value).casefold()
        languages = {normalize(value) for value in [receipt["language"], *receipt.get("language_labels", [])]}
        list_path = f"/contest/{cid}/submissions"

        def candidates(page):
            if urlsplit(page.url).path != list_path:
                return {}
            return {sid: row for sid, row in records(page.text, cid).items()
                    if sid not in excluded and sid > receipt["min_id"]
                    and row["username"] == username and row["problem_id"] == pid
                    and (not row.get("language") or normalize(row["language"]) in languages)
                    and (row.get("submitted_at") is None
                         or receipt["submitted_after"] <= row["submitted_at"] <= receipt["submitted_before"])}

        found = candidates(initial) if initial is not None else {}
        page = initial
        if not found:
            # Even when the POST response contains old matching rows, fetch the
            # list again: an old row is not evidence that the new one is absent.
            page = self._page(list_path)
            self._same_account(username)
            if urlsplit(page.url).path != list_path:
                raise QOJError("QOJ 未返回当前比赛的提交列表。", 502)
            found = candidates(page)
        # Follow only native pagination links of this same submission list.
        # This recovers a delayed run pushed off the first page by other teams.
        if not found and page is not None:
            seen = {page.url}
            for link in BeautifulSoup(page.text, "html.parser").select(".pagination a[href]"):
                try:
                    target = _upstream_url(link["href"], page.url)
                except QOJError:
                    continue
                if target in seen or urlsplit(target).path != list_path:
                    continue
                seen.add(target)
                next_page = self._page(target)
                self._same_account(username)
                found.update(candidates(next_page))
                if found or len(seen) >= 3:
                    break
        if len(found) > 5:
            return {"qoj_id": None, "message": "发现多条候选提交，正在继续核对编号；不会自动重新提交。"}
        confirmed = []
        inaccessible = False
        for sid, row in found.items():
            try:
                detail = self._page(f"/submission/{sid}")
                self._same_account(username)
                if urlsplit(detail.url).path.rstrip("/") != f"/submission/{sid}":
                    inaccessible = True
                    continue
                actual_rows = records(detail.text, cid)
                if sid in actual_rows and (actual_rows[sid]["username"] != username or actual_rows[sid]["problem_id"] != pid):
                    continue
                source_blocks = extract_sources(detail.text)
                if not source_blocks:
                    inaccessible = True
                for item in source_blocks:
                    actual_language = item.get("language") or actual_rows.get(sid, {}).get("language") or row.get("language")
                    if actual_language and normalize(actual_language) not in languages:
                        continue
                    if fingerprint(item["source"]) == receipt["source_sha256"]:
                        confirmed.append(sid)
                        break
            except QOJError as exc:
                if exc.status in (401, 409, 429) or exc.status >= 500:
                    raise
                inaccessible = True
        if len(confirmed) == 1 and not inaccessible:
            sid = confirmed[0]
            return {"qoj_id": sid, "qoj_url": f"{ORIGIN}/submission/{sid}", "message": "已核对账号、题目及源码，确认 QOJ 提交编号。"}
        return {"qoj_id": None, "message": "提交请求已发送，正在核对 QOJ 编号；页面会自动重查，不会重复提交代码。"}

    def verdict(self, qoj_id: int) -> dict:
        with self.lock:
            expected = self.username
            response = self._page(f"/submission/{int(qoj_id)}")
            self._same_account(expected)
            if urlsplit(response.url).path.rstrip("/") != f"/submission/{int(qoj_id)}":
                raise QOJError("QOJ 未返回指定提交的评测页面。", 502)
            return parse_verdict(response.text, int(qoj_id))
