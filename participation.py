"""Read QOJ participation and standings, without executing upstream JavaScript.

QOJ owns registration, contest clocks and frozen results. These parsers only
translate native forms, scoreboard JSON literals, and PC2 results tables.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from qoj import QOJError, _duration, _page_title, _upstream_url, parse_contest


QOJ_TIMEZONE = timezone(timedelta(hours=8))
DATE_PATTERN = r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?"


def _ensure_account(client, expected):
    if expected and client.username != expected:
        raise QOJError("QOJ 账号在读取参赛状态时发生变化，请刷新后重试。", 409)


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value > 0 else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or QOJ_TIMEZONE).timestamp()
    except ValueError:
        return None


def response_now(response):
    """Use QOJ's HTTP time when present; never invent a participation start."""
    try:
        return parsedate_to_datetime(response.headers["Date"]).timestamp()
    except (KeyError, ValueError, TypeError, OverflowError):
        soup = BeautifulSoup(response.text, "html.parser")
        footer = soup.select_one(".uoj-footer")
        if footer:
            match = re.search(r"(?:Server Time|服务器时间)\s*[:：]\s*(" + DATE_PATTERN + r")", footer.get_text(" ", strip=True), re.I)
            if match and (value := _timestamp(match[1])) is not None:
                return value
        return time.time()


def _contest_link(soup, contest_id, suffix):
    for node in soup.select("a[href]"):
        try:
            target = _upstream_url(node["href"])
        except QOJError:
            continue
        if urlsplit(target).path == f"/contest/{contest_id}/{suffix}":
            return target
    return None


def results_url(value, contest_id, base_url="https://qoj.ac/"):
    """Keep External Standings and explicit PC2 views distinct for this contest."""
    if not isinstance(value, str):
        return None
    try:
        url = _upstream_url(value, base_url)
    except QOJError:
        return None
    parts = urlsplit(url)
    if not re.fullmatch(rf"/(?:contest/)?results/QOJ{int(contest_id)}/?", parts.path):
        return None
    query = parse_qsl(parts.query, keep_blank_values=True)
    if query not in ([], [("mode", "pc2")]):
        return None
    return "https://qoj.ac" + parts.path.rstrip("/") + ("?mode=pc2" if query else "")


def _results_link(source, contest_id, page_url, *, embedded_only=False):
    soup = BeautifulSoup(source, "html.parser")
    candidates = []
    for node in soup.select("iframe[src]" if embedded_only else "a[href], iframe[src]"):
        # Author-written statement/announcement links cannot select a different
        # scoreboard view. Only the surrounding contest interface is eligible.
        if node.find_parent(["article", "form"]) or node.find_parent(class_="uoj-article"):
            continue
        target = results_url(node.get("href") or node.get("src"), contest_id, page_url)
        if target:
            # The currently embedded Training view wins over an alternative
            # External Standings navigation link on the same page.
            candidates.append((0 if node.name == "iframe" else 1,
                               0 if urlsplit(target).query else 1, target))
    for node in soup.select("meta[http-equiv]"):
        if node.get("http-equiv", "").lower() == "refresh":
            match = re.fullmatch(r"\s*\d+\s*;\s*url\s*=\s*['\"]?([^'\"]+)['\"]?\s*", node.get("content", ""), re.I)
            if match and (target := results_url(match[1].strip(), contest_id, page_url)):
                candidates.append((0, 0 if urlsplit(target).query else 1, target))
    return min(candidates, key=lambda item: item[:2])[2] if candidates else None


def _standings_url(value, contest_id, base_url="https://qoj.ac/"):
    try:
        target = _upstream_url(value, base_url)
    except QOJError:
        return None
    parts = urlsplit(target)
    if parts.path == f"/contest/{contest_id}/standings" and not parts.query:
        return target
    return None


def _standings_link(source, contest_id, page_url):
    soup = BeautifulSoup(source, "html.parser")
    for node in soup.select("a[href]"):
        if node.find_parent(["article", "form"]) or node.find_parent(class_="uoj-article"):
            continue
        if target := _standings_url(node["href"], contest_id, page_url):
            return target
    return None


def read_scoreboard(client, page, contest_id, problems):
    """Follow the current authorized page's scoreboard; no public-board fallback."""
    expected_username = client.username
    show_unofficial = getattr(client, "show_unofficial", None)
    native_url = _standings_link(page.text, contest_id, page.url)
    source_url = (_results_link(page.text, contest_id, page.url)
                  or native_url)
    if getattr(client, "scoreboard_mode", "auto") == "pc2":
        source_url = f"https://qoj.ac/results/QOJ{contest_id}?mode=pc2"
    # Only the native standings honor QOJ's Show unofficial preference. An
    # explicit startup choice takes priority over alternate results views.
    if show_unofficial is not None and native_url:
        source_url = native_url
    if not source_url:
        return {"scoreboard": [], "scoreboard_available": False, "scoreboard_status": "unavailable",
                "scoreboard_message": "QOJ 当前未提供榜单入口。", "scoreboard_url": None}
    def source_kind(url):
        target = results_url(url, contest_id)
        return ("qoj-pc2" if urlsplit(target).query else "qoj-external") if target else "qoj"

    result = {"scoreboard": [], "scoreboard_available": False, "scoreboard_status": "error",
              "scoreboard_url": source_url, "scoreboard_source": source_kind(source_url)}
    try:
        board = client._page(source_url)
        _ensure_account(client, expected_username)
        requested_results = results_url(source_url, contest_id)
        target = results_url(board.url, contest_id)
        if requested_results and target != requested_results:
            raise QOJError("QOJ 榜单重定向到了不同的比赛或显示模式。", 502)
        if target is None:
            if not _standings_url(board.url, contest_id):
                raise QOJError("QOJ 未返回当前比赛的榜单页面。", 502)
            # Keep native data despite an alternative External Standings link.
            # An embedded/redirected results view is still authoritative (e.g.
            # Training Mode); it must not be bypassed to read hidden scores.
            target = _results_link(board.text, contest_id, board.url,
                                   embedded_only=show_unofficial is not None)
            if target:
                result.update(scoreboard_url=target, scoreboard_source=source_kind(target))
                board = client._page(target)
                _ensure_account(client, expected_username)
                if results_url(board.url, contest_id) != target:
                    raise QOJError("QOJ 榜单重定向到了不同的比赛或显示模式。", 502)
        if target:
            if source_kind(target) == "qoj-external":
                embedded = _results_link(board.text, contest_id, board.url, embedded_only=True)
                if embedded and embedded != target:
                    target = embedded
                    result.update(scoreboard_url=target, scoreboard_source=source_kind(target))
                    board = client._page(target)
                    _ensure_account(client, expected_username)
                    if results_url(board.url, contest_id) != target:
                        raise QOJError("QOJ 榜单重定向到了不同的比赛或显示模式。", 502)
            kind = source_kind(target)
            result.update(scoreboard_url=target, scoreboard_source=kind)
            from results import parse_external_results_table, parse_results_table
            parser = parse_results_table if kind == "qoj-pc2" else parse_external_results_table
            parsed = parser(board.text, contest_id, problems, expected_username)
        else:
            parsed = parse_scoreboard(board.text, contest_id, problems, expected_username)
        messages = {"qoj-pc2": "QOJ PC2 / Training Mode 榜单",
                    "qoj-external": "QOJ External Standings", "qoj": "QOJ 原生榜单"}
        message = messages[result["scoreboard_source"]]
        if show_unofficial is not None:
            if result["scoreboard_source"] == "qoj":
                message += " · " + ("显示非正式参赛者" if show_unofficial else "隐藏非正式参赛者")
            else:
                message += "；此上游榜单不支持 Show unofficial 筛选，仍显示原榜。"
        result.update(scoreboard=parsed["rows"], scoreboard_available=True, scoreboard_status="available",
                      scoreboard_problem_totals=parsed.get("problem_totals", {}),
                      scoreboard_message=message,
                      ruleset=parsed["ruleset"], finalized=parsed.get("finalized"))
    except QOJError as exc:
        if exc.status in (401, 409):
            raise
        result["scoreboard_message"] = str(exc)
    return result


def _label(soup, element):
    identifier = element.get("id")
    node = soup.find("label", attrs={"for": identifier}) if identifier else None
    node = node or element.find_parent("label")
    return (node.get_text(" ", strip=True) if node else element.get("aria-label") or element.get("name") or "选项")[:1000]


def _vp_registration_url(value, contest_id, base_url="https://qoj.ac/"):
    try:
        target = _upstream_url(value, base_url)
    except QOJError:
        return None
    if urlsplit(target).path.rstrip("/") == f"/contest/{int(contest_id)}/virtual/register":
        return target
    return None


def _entry_url_allowed(value, contest_id, mode):
    """Do not follow entry redirects to another contest, mode or operation."""
    try:
        path = urlsplit(_upstream_url(value)).path.rstrip("/")
    except QOJError:
        return False
    allowed = {f"/contest/{contest_id}", f"/contest/{contest_id}/standings",
               "/login", "/login/2fa", "/login-2fa", "/two-factor-auth"}
    if mode == "vp":
        allowed.add(f"/contest/{contest_id}/virtual/register")
    else:
        allowed.update({f"/contest/{contest_id}/register", f"/contest/{contest_id}/registration"})
    return path in allowed


def _remember_vp_registration(client, contest_id, url):
    """Retain only an observed page URL, never a token or a confirmation value."""
    target = _vp_registration_url(url, contest_id)
    if not target or not client.username:
        return
    pages = getattr(client, "_vp_entry_pages", None)
    if pages is None:
        pages = client._vp_entry_pages = {}
    key = (int(contest_id), client.username)
    pages.pop(key, None)
    while len(pages) >= 32:
        pages.pop(next(iter(pages)))
    pages[key] = target


def _vp_registration_link(soup, contest_id, page_url):
    for link in soup.select("a[href]"):
        if link.find_parent(["article", "form"]) or link.find_parent(class_="uoj-article"):
            continue
        if target := _vp_registration_url(link["href"], contest_id, page_url):
            return target
    return None


def _read_vp_registration(client, contest_id, url, expected_username):
    page = client._page(url, url_guard=lambda target: _entry_url_allowed(target, contest_id, "vp"))
    _ensure_account(client, expected_username)
    if urlsplit(page.url).path.rstrip("/") == f"/contest/{int(contest_id)}":
        # QOJ can expire this step or send an already registered user home.
        # Drop the old URL so future refreshes can resume from that page.
        getattr(client, "_vp_entry_pages", {}).pop((int(contest_id), expected_username), None)
        return page
    if not _vp_registration_url(page.url, contest_id):
        raise QOJError("QOJ VP 报名页面已变化，请刷新参赛状态。", 409)
    return page


def native_forms(source, contest_id, page_url, mode):
    """Return private form descriptors. Hidden fields never leave this module."""
    soup = BeautifulSoup(source, "html.parser")
    result = []
    accepted = {"regvir", "register-virtual", "register_virtual", "virtual-register"} if mode == "vp" else {"register", "registration", "reg", "register-contest"}
    confirmation = mode == "vp" and bool(_vp_registration_url(page_url, contest_id))
    if confirmation:
        accepted |= {"register_vp", "register-vp", "register", "registration", "confirm"}
    for form in soup.select("form"):
        form_name = re.sub(r"^form-", "", form.get("id", ""))
        buttons = form.select('button[name], input[type="submit"][name]')
        button = next((node for node in buttons if re.sub(r"^submit-", "", node.get("name", "")) in accepted), None)
        if form_name not in accepted and button is None:
            continue
        if button is None:
            continue
        if form.get("method", "get").lower() != "post" or button.has_attr("disabled"):
            continue
        try:
            action_url = _upstream_url(form.get("action") or page_url, page_url)
        except QOJError:
            continue
        paths = {f"/contest/{contest_id}", f"/contest/{contest_id}/standings"}
        if confirmation:
            paths = {f"/contest/{contest_id}/virtual/register"}
        elif mode == "official":
            paths.update({f"/contest/{contest_id}/register", f"/contest/{contest_id}/registration"})
        if urlsplit(action_url).path.rstrip("/") not in paths:
            continue
        hidden = {}
        valid = True
        for field in form.select('input[type="hidden"][name]'):
            if field["name"] in hidden:
                valid = False
            hidden[field["name"]] = field.get("value", "")
        if not hidden.get("_token") or not valid:
            continue
        fields, grouped = [], {}
        for field in form.select("input[name], select[name], textarea[name]"):
            name = field["name"]
            kind = "select" if field.name == "select" else "textarea" if field.name == "textarea" else field.get("type", "text").lower()
            if kind in ("hidden", "submit", "button") or field.has_attr("disabled"):
                continue
            if kind not in ("select", "radio", "checkbox", "text", "number", "datetime-local") or name in hidden:
                valid = False
                break
            if field.has_attr("multiple"):
                valid = False
                break
            required = field.has_attr("required")
            # Native radio groups normally rely on UOJForm's JS validation.
            required = required or kind in ("select", "radio")
            item = {"name": name, "label": _label(soup, field), "type": kind, "required": required}
            if kind == "select":
                item["options"] = [{"value": option.get("value", option.get_text()), "label": option.get_text(" ", strip=True)}
                                   for option in field.select("option") if not option.has_attr("disabled") and option.get("value", option.get_text()) != ""]
                if not item["options"]:
                    valid = False
                    break
                selected = next((option for option in field.select("option[selected]") if not option.has_attr("disabled")), None)
                if selected:
                    item["default"] = selected.get("value", selected.get_text())
            elif kind == "radio":
                if name not in grouped:
                    item["options"] = []
                    grouped[name] = item
                    fields.append(item)
                grouped[name]["options"].append({"value": field.get("value", "on"), "label": _label(soup, field)})
                if field.has_attr("checked"):
                    grouped[name]["default"] = field.get("value", "on")
                continue
            elif kind == "checkbox":
                item["value"] = field.get("value", "on")
                item["default"] = field.has_attr("checked")
            else:
                item["default"] = field.get("value", "")
                for attribute in ("min", "max", "step", "maxlength", "pattern"):
                    if field.has_attr(attribute):
                        item[attribute] = str(field[attribute])
            if name in grouped:
                valid = False
                break
            grouped[name] = item
            fields.append(item)
        if not valid:
            continue
        identifier = form_name or re.sub(r"^submit-", "", button["name"])
        if confirmation:
            identifier = "virtual-register:" + identifier
        descriptor = {"id": identifier, "label": button.get_text(" ", strip=True) or button.get("value") or ("开始 QOJ VP" if mode == "vp" else "报名 QOJ 比赛"),
                      "kind": "start" if mode == "vp" and not confirmation else "register", "fields": fields}
        result.append({"public": descriptor, "url": action_url, "hidden": hidden,
                       "submit_name": button["name"], "submit_value": button.get("value", ""), "page_url": page_url})
    return result


def form_payload(form, values):
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise QOJError("原生报名选项必须是字段对象。", 400)
    fields = {field["name"]: field for field in form["public"]["fields"]}
    if set(values) - set(fields):
        raise QOJError("报名请求包含 QOJ 表单未声明的字段。", 400)
    payload = dict(form["hidden"])
    payload[form["submit_name"]] = form["submit_value"]
    for name, field in fields.items():
        value = values.get(name, field.get("default", ""))
        if field["type"] == "checkbox":
            if value not in (True, False, "", field["value"]):
                raise QOJError("报名确认选项无效。", 400)
            checked = value is True or value == field["value"]
            if field["required"] and not checked:
                raise QOJError("请确认：" + field["label"], 400)
            if checked:
                payload[name] = field["value"]
            continue
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise QOJError("报名选项格式无效。", 400)
        if field["required"] and not value:
            raise QOJError("请选择或填写：" + field["label"], 400)
        if field["type"] in ("select", "radio") and value not in {option["value"] for option in field["options"]}:
            raise QOJError("请选择 QOJ 表单提供的参赛时间窗口。", 400)
        if field.get("maxlength", "").isdigit() and len(value) > int(field["maxlength"]):
            raise QOJError("报名字段超过 QOJ 表单允许的长度。", 400)
        payload[name] = value
    return payload


def _registrant_observation(source, username):
    """Read one page without treating absence from a page as non-VP status.

    Current QOJ's first table has #,Username,Rating,Start Time,Duration; the
    virtual table has #,Username,Start Time. This is observed public markup,
    not an inference from whether someone submitted after the official round.
    """
    if not username:
        return None, False, False
    soup = BeautifulSoup(source, "html.parser")
    record, virtual = None, False
    virtual_table_seen = False
    for table in soup.select("table"):
        headers = [cell.get_text(" ", strip=True).lower() for cell in table.select("thead th")]
        start_index = next((i for i, label in enumerate(headers) if label in ("start time", "start", "开始时间")), None)
        duration_index = next((i for i, label in enumerate(headers) if label in ("duration", "length", "时长", "比赛时长")), None)
        user_index = next((i for i, label in enumerate(headers) if label in ("username", "user", "用户名")), None)
        if start_index is None or user_index is None:
            continue
        is_virtual_table = duration_index is None and len(headers) == 3
        virtual_table_seen |= is_virtual_table
        for row in table.select("tbody tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) <= max(start_index, user_index):
                continue
            user = cells[user_index].select_one(".uoj-username")
            actual = user.get_text(" ", strip=True) if user else cells[user_index].get_text(" ", strip=True)
            link = cells[user_index].select_one('a[href*="/user/profile/"]')
            if link:
                match = re.search(r"/user/profile/([^/?#]+)", link["href"])
                if match:
                    actual = unquote(match[1])
            if actual != username:
                continue
            started = _timestamp(cells[start_index].get_text(" ", strip=True))
            if is_virtual_table:
                virtual = True
            if duration_index is not None and len(cells) > duration_index and started is not None:
                duration_text = cells[duration_index].get_text(" ", strip=True)
                match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|分钟)?", duration_text, re.I)
                if match and float(match[1]) > 0:
                    duration = float(match[1]) * 60
                    record = {"started_at": started, "ends_at": started + duration, "duration_seconds": duration}
    return record, virtual, virtual_table_seen


def registrant_record(source, username):
    """Parse a complete registration listing already supplied by the caller."""
    record, virtual, virtual_table_seen = _registrant_observation(source, username)
    if record:
        record["is_virtual"] = virtual if virtual_table_seen else None
    return record


MAX_REGISTRANT_PAGES = 64


def _registrant_page_url(value, contest_id, base_url):
    """Only QOJ's advertised pagination for this exact registration listing."""
    try:
        target = _upstream_url(value, base_url)
    except QOJError:
        return None
    parts = urlsplit(target)
    if parts.path != f"/contest/{contest_id}/registrants" or parts.fragment:
        return None
    query = parse_qsl(parts.query, keep_blank_values=True)
    base = "https://qoj.ac" + parts.path
    if not query:
        return base
    if (len(query) != 1 or query[0][0] != "page" or not re.fullmatch(r"[0-9]{1,6}", query[0][1])
            or int(query[0][1]) < 1):
        return None
    number = int(query[0][1])
    return base if number == 1 else f"{base}?page={number}"


def read_registrant_record(client, contest_id, listing_url, username):
    """Join the current account's timed row and VP identity across QOJ pages.

    QOJ 1802's two tables share ``?page=N`` while ordering their rows
    differently: a VP user's identity can be on page 1 and their Start Time /
    Duration on page 3. Fetch only advertised same-contest pages, stop on
    positive evidence, and retain only matching page URLs as future hints.
    Identity, times and membership are freshly read on every call.
    """
    if not username:
        return None
    base = _registrant_page_url(listing_url, contest_id, listing_url)
    if not base:
        raise QOJError("QOJ 未提供当前比赛的有效参赛名单地址。", 502)
    hints = getattr(client, "_registrant_page_hints", {})
    if not isinstance(hints, dict):
        hints = {}
    key = (contest_id, username)
    queue = []
    for value in (*hints.get(key, ()), base):
        target = _registrant_page_url(value, contest_id, base)
        if target and target not in queue:
            queue.append(target)
    seen, matched_pages = set(), []
    record, virtual, virtual_table_seen = None, False, False

    def resolved():
        if record is None:
            return None
        # Cache only URLs that supplied this account's own rows. A later
        # refresh verifies those rows again and follows links if they moved.
        if key not in hints and len(hints) >= 32:
            hints.pop(next(iter(hints)))
        hints[key] = tuple(matched_pages)
        client._registrant_page_hints = hints
        return {**record, "is_virtual": virtual if virtual_table_seen else None}

    while queue:
        if len(seen) >= MAX_REGISTRANT_PAGES:
            raise QOJError("QOJ 参赛名单分页较多，尚未完整确认当前账号的参赛时间与类型；请在 QOJ 核查。", 502)
        target = queue.pop(0)
        if target in seen:
            continue
        seen.add(target)
        page = client._page(target)
        _ensure_account(client, username)
        actual = _registrant_page_url(page.url, contest_id, base)
        if not actual:
            raise QOJError("QOJ 参赛名单重定向到了不同的比赛或页面。", 502)
        observed, is_virtual, has_virtual_table = _registrant_observation(page.text, username)
        if observed:
            if record is not None and observed != record:
                raise QOJError("QOJ 在读取分页名单时返回了不同的个人参赛时间，请刷新后重试。", 409)
            record = observed
        virtual |= is_virtual
        virtual_table_seen |= has_virtual_table
        if (observed or is_virtual) and actual not in matched_pages:
            matched_pages.append(actual)
        if record and virtual:
            return resolved()
        soup = BeautifulSoup(page.text, "html.parser")
        discovered = set()
        for link in soup.select(".pagination a[href]"):
            if link.find_parent("form") or link.find_parent(class_="disabled"):
                continue
            next_page = _registrant_page_url(link["href"], contest_id, page.url)
            if next_page and next_page not in seen and next_page not in queue:
                discovered.add(next_page)
        # Recent VP users are commonly near the end of the main table, while
        # the VP table puts recent users first. Try the advertised end first.
        queue.extend(sorted(discovered, key=lambda url: int(dict(parse_qsl(urlsplit(url).query)).get("page", 1)), reverse=True))
    # A negative VP-membership result is valid only after the complete linked
    # listing has been read. Finding a main-table row alone is insufficient.
    return resolved()


def _json_variables(source, names):
    """Decode JSON literals from script assignments; never eval or run scripts."""
    result = {}
    decoder = json.JSONDecoder()
    soup = BeautifulSoup(source, "html.parser")
    pattern = re.compile(r"(?<![\w.$])(?:var\s+|let\s+|const\s+)?(" + "|".join(re.escape(name) for name in names) + r")\s*=(?!=)")
    for script in soup.select("script:not([src])"):
        text = script.string or script.get_text()
        for match in pattern.finditer(text):
            remainder = text[match.end():].lstrip()
            try:
                value, consumed = decoder.raw_decode(remainder)
            except (ValueError, RecursionError):
                continue
            if remainder[consumed:].lstrip()[:1] not in ("", ";", ","):
                continue
            if match[1] in result and result[match[1]] != value:
                raise QOJError("QOJ 榜单包含互相冲突的数据，暂不显示。", 502)
            result[match[1]] = value
    return result


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _penalty(seconds):
    if not _number(seconds) or seconds < 0:
        raise QOJError("QOJ 榜单时间格式无法识别。", 502)
    # Match the live getPenaltyTimeStr(): hours + ':' + two-digit minutes.
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}"


def _first_solve_times(score, problem_count):
    """Match ICPC showStandings() in https://qoj.ac/js/uoj.js.

    Use the complete supplied score set and seconds, before formatting times.
    QOJ includes pending and hacked (97) records in this minimum; a hacked
    record cannot itself receive the first-solve highlight. Unreadable records
    prevent us from claiming a first solve based on an incomplete comparison.
    """
    first = {}
    for cells in score.values():
        if not isinstance(cells, (list, dict)):
            return {}
        for index in range(problem_count):
            cell = cells[index] if isinstance(cells, list) and index < len(cells) else cells.get(str(index)) if isinstance(cells, dict) else None
            if cell is None:
                continue
            if not isinstance(cell, list) or len(cell) < 5 or not all(_number(cell[i]) for i in (0, 1, 4)):
                return {}
            if cell[0] == cell[4] or cell[0] == 97:
                seconds = int(cell[1])
                first[index] = min(first.get(index, seconds), seconds)
    return first


def parse_scoreboard(source, contest_id, problems, username):
    names = ("standings", "score", "problems", "problems_id", "contest_id", "contest_type", "standings_version", "my_name", "finalized")
    data = _json_variables(source, names)
    if data.get("standings") == [] and data.get("score") == []:
        data["score"] = {}
    if data.get("contest_id") != contest_id or not isinstance(data.get("standings"), list) or not isinstance(data.get("score"), dict):
        raise QOJError("QOJ 尚未提供可识别的原生榜单数据。", 502)
    ids = data.get("problems")
    allowed = {problem["id"] for problem in problems}
    if (not isinstance(ids, list) or not all(isinstance(pid, int) and not isinstance(pid, bool) for pid in ids)
            or len(set(ids)) != len(ids) or set(ids) != allowed):
        raise QOJError("QOJ 榜单题目与当前比赛不一致。", 502)
    rules = data.get("contest_type")
    if rules not in ("ICPC", "OI", "IOI", "UOJ"):
        raise QOJError("暂不能识别此 QOJ 榜单赛制。", 502)
    version = data.get("standings_version", 1)
    if not _number(version):
        raise QOJError("QOJ 榜单版本无法识别。", 502)
    rows = []
    if len(data["standings"]) > 20000:
        raise QOJError("QOJ 榜单规模超过当前解析限制。", 502)
    first_solves = _first_solve_times(data["score"], len(ids)) if rules == "ICPC" else {}
    for row in data["standings"]:
        if (not isinstance(row, list) or len(row) < 4 or not isinstance(row[2], list) or len(row[2]) < 4
                or not isinstance(row[2][0], str) or not _number(row[0]) or not _number(row[1])
                or not isinstance(row[3], (str, int, float))):
            raise QOJError("QOJ 榜单行结构发生变化，暂不显示。", 502)
        user = row[2][0]
        cells = data["score"].get(user, {})
        if not isinstance(cells, (list, dict)):
            raise QOJError("QOJ 榜单题目结果结构发生变化。", 502)
        stats, accepted = {}, 0
        for index, pid in enumerate(ids):
            cell = cells[index] if isinstance(cells, list) and index < len(cells) else cells.get(str(index)) if isinstance(cells, dict) else None
            if cell is None:
                stats[str(pid)] = {"text": "", "display": "", "solved": False, "pending": False, "attempts": 0, "time": "", "first_blood": False}
                continue
            if not isinstance(cell, list) or len(cell) < 5 or not all(_number(cell[i]) for i in (0, 1, 3, 4)):
                raise QOJError("QOJ 榜单题目结果结构发生变化。", 502)
            solved = cell[0] == cell[4] or (rules == "ICPC" and cell[0] == 97)
            accepted += int(solved)
            pending = len(cell) > 5 and cell[5] == 1
            value = cell[0] if solved or rules != "ICPC" else 0
            if rules == "ICPC":
                if pending:
                    display = "?" + str(int(cell[3]) + 1)
                elif value == 0:
                    display = "-" + str(int(cell[3]) + 1)
                else:
                    display = "+" + (str(int(cell[3])) if cell[3] else "")
            else:
                display = str(int(value)) if value == int(value) else f"{value:.2f}"
            clock = ""
            if rules in ("ICPC", "UOJ") and cell[1] >= 0 and (version < 2 or value > 0):
                clock = _penalty(cell[1])
                display += "\n" + clock
            stats[str(pid)] = {"text": display, "display": display, "solved": solved, "pending": pending,
                               "attempts": int(cell[3]) + 1, "time": clock, "qoj_id": cell[2], "hacked": rules == "ICPC" and cell[0] == 97,
                               "first_blood": rules == "ICPC" and cell[0] == cell[4] and cell[0] != 97 and cell[1] == first_solves.get(index)}
        name = row[2][3] if isinstance(row[2][3], str) and row[2][3] else user
        if row[2][2] in (1, 2):
            name = "* " + name
        rows.append({"rank": row[3], "team_name": name, "username": user, "solved": accepted if rules == "ICPC" else row[0],
                     "score": row[0], "penalty": _penalty(row[1]) if rules in ("ICPC", "UOJ") else "—", "penalty_seconds": row[1],
                     "is_self": user == username, "problem_stats": stats})
    return {"rows": rows, "ruleset": rules, "finalized": data.get("finalized"), "source": "qoj"}


def _explicit_times(source, mode):
    """Only account-specific timing labels; generic contest schedule is not proof."""
    soup = BeautifulSoup(source, "html.parser")
    if mode == "vp":
        containers = [node for node in soup.select(".card, .panel")
                      if re.search(r"Virtual Contest|虚拟比赛|虚拟参赛", (node.select_one(".card-header,.panel-heading") or node).get_text(" ", strip=True), re.I)]
    else:
        containers = [node for node in soup.select(".card, .panel")
                      if re.search(r"Your (?:Contest|Participation)|你的比赛|您的比赛|参赛时间", node.get_text(" ", strip=True), re.I)]
    for node in containers:
        text = node.get_text(" ", strip=True)
        values = []
        for label in (r"(?:Start(?:ing)?(?: Time)?|开始时间|开始)", r"(?:End(?:ing)?(?: Time)?|结束时间|结束)"):
            match = re.search(label + r"\s*[:：]?\s*(" + DATE_PATTERN + r")", text, re.I)
            values.append(_timestamp(match[1]) if match else None)
        if all(value is not None for value in values) and values[1] > values[0]:
            return {"started_at": values[0], "ends_at": values[1], "duration_seconds": values[1] - values[0], "is_virtual": mode == "vp"}
    return None


def _page_mode(soup):
    """Native countdown locations can distinguish a live official window.

    Before public virtual registrations exist, registrants may have only the
    main table. An account's timed row together with the official countdown is
    positive evidence; absence of a virtual table alone is not evidence.
    """
    for node in soup.select(".card, .panel"):
        heading = node.select_one(".card-header,.panel-heading")
        text = heading.get_text(" ", strip=True) if heading else ""
        if re.search(r"Virtual Contest|虚拟比赛|虚拟参赛", text, re.I) and node.select_one(".countdown[data-rest], #contest-countdown"):
            return "vp"
    for node in soup.select(".card, .panel"):
        heading = node.select_one(".card-header,.panel-heading")
        text = heading.get_text(" ", strip=True) if heading else ""
        if re.search(r"Contest (?:starts|begins|ends) in|距离比赛(?:开始|结束)|比赛(?:开始|结束)(?:倒计时|还有|剩余)", text, re.I) and node.select_one(".countdown[data-rest]"):
            return "official"
    if soup.select_one("#form-unregister, #form-deregister, [name='submit-unregister'], [name='submit-deregister']"):
        return "official"
    return None


def _scoreboard_freeze_after(source):
    """Read QOJ's explicit relative freeze rule from the system sidebar.

    Markup observed at https://contest.ucup.ac/contest/2567 on 2026-10-23
    (QOJ 4.5.47): ``Scoreboard will be frozen after 4:00:00``. This is
    elapsed contest time, so it can be anchored to the current participant's
    official/virtual start instead of the historical contest schedule.
    Unknown/localized wording remains unknown; neither pending cells nor
    ``finalized`` establish when the current participant enters the freeze.
    """
    soup = BeautifulSoup(source, "html.parser")
    durations = set()
    for node in soup.select(".uoj-content > .row > .col-sm-3 > ul.list-group > li.list-group-item"):
        if (node.find_parent(["article", "form", "table", "pre", "code"])
                or node.find_parent(class_="uoj-article")):
            continue
        text = " ".join(node.get_text(" ", strip=True).split())
        prefix = "Scoreboard will be frozen after"
        if not text.startswith(prefix):
            continue
        match = re.fullmatch(re.escape(prefix) + r"\s+(\d{1,5}):([0-5]\d):([0-5]\d)", text)
        if not match:
            return None
        hours, minutes, seconds = map(int, match.groups())
        durations.add(hours * 3600 + minutes * 60 + seconds)
    return durations.pop() if len(durations) == 1 else None


def participation(client, contest_id, mode="vp"):
    if mode not in ("vp", "official"):
        raise QOJError("参赛模式必须是 vp 或 official。", 400)
    page = client._page(f"/contest/{int(contest_id)}")
    expected_username = client.username
    if (urlsplit(page.url).path not in (f"/contest/{contest_id}", f"/contest/{contest_id}/register", f"/contest/{contest_id}/registration")
            and not (mode == "vp" and _vp_registration_url(page.url, contest_id))):
        raise QOJError("QOJ 未返回当前比赛的参赛页面。", 422)
    soup = BeautifulSoup(page.text, "html.parser")
    forms = native_forms(page.text, contest_id, page.url, mode)
    registration_url = _contest_link(soup, contest_id, "register") or _contest_link(soup, contest_id, "registration")
    if mode == "official" and not forms and registration_url:
        registration = client._page(registration_url)
        _ensure_account(client, expected_username)
        forms = native_forms(registration.text, contest_id, registration.url, mode)
    confirmation = mode == "vp" and bool(_vp_registration_url(page.url, contest_id))
    if mode == "vp":
        if confirmation:
            _remember_vp_registration(client, contest_id, page.url)
        else:
            pending = getattr(client, "_vp_entry_pages", {}).get((int(contest_id), expected_username)) if forms else None
            pending = pending or (_vp_registration_link(soup, contest_id, page.url) if not forms else None)
            if pending:
                page = _read_vp_registration(client, contest_id, pending, expected_username)
                soup = BeautifulSoup(page.text, "html.parser")
                forms = native_forms(page.text, contest_id, page.url, mode)
                confirmation = bool(_vp_registration_url(page.url, contest_id))
                _remember_vp_registration(client, contest_id, page.url)
    now = response_now(page)
    metadata = {"id": contest_id, "title": _page_title(soup, f"QOJ Contest {contest_id}"),
                "duration_seconds": _duration(soup, page.text), "problems": [], "attachments": []}
    result = {"mode": mode, "status": "unavailable", "content_available": False, "can_submit": False,
              "started_at": None, "ends_at": None, "username": client.username, "server_time": now,
              "actions": [form["public"] for form in forms], "contest": metadata,
              "scoreboard": [], "scoreboard_available": False, "scoreboard_message": "参赛开始前不读取榜单。",
              "scoreboard_frozen": None, "scoreboard_freeze_at": None,
              "message": "尚未确认当前账号的 QOJ 原生参赛状态。", "upstream_url": f"https://qoj.ac/contest/{contest_id}"}
    if confirmation:
        result.update(entry_stage="confirmation", upstream_url=page.url,
                      message="请完成下方 QOJ VP 报名确认；仅进入此页面尚未开始参赛。请确保在 VP 过程中遵守比赛规则。")
        if not forms:
            result["message"] = "暂不能识别 QOJ VP 的第二步报名表单，请在 QOJ 报名页核查。"
            return result
    if forms:
        result["status"] = "unregistered"
        if not confirmation:
            result["message"] = "请通过 QOJ 原生表单开始 VP。" if mode == "vp" else "请选择 QOJ 参赛时间窗口并报名。"
        return result
    if not client.username:
        result["message"] = "无法确认 QOJ 当前账号，请重新连接账号后刷新。"
        return result
    record = _explicit_times(page.text, mode)
    registrants_url = _contest_link(soup, contest_id, "registrants")
    if registrants_url:
        try:
            native_record = read_registrant_record(client, contest_id, registrants_url, expected_username)
            if native_record:
                record = native_record
        except QOJError as exc:
            if exc.status in (401, 409):
                raise
            result["message"] = "QOJ 暂未提供可读取的账号参赛时间：" + str(exc)
    if not record:
        return result
    if record.get("is_virtual") is None:
        observed_mode = _page_mode(soup)
        if observed_mode is None:
            result["message"] = "QOJ 时间记录缺少参赛类型，无法确认是正式比赛还是 VP。"
            return result
        record["is_virtual"] = observed_mode == "vp"
    if record["is_virtual"] != (mode == "vp"):
        result["message"] = "当前账号已有另一种 QOJ 原生参赛记录，请切换参赛模式。"
        return result
    started, ends = record["started_at"], record["ends_at"]
    getattr(client, "_vp_entry_pages", {}).pop((int(contest_id), expected_username), None)
    result.update(started_at=started, ends_at=ends)
    metadata["duration_seconds"] = record["duration_seconds"]
    if now < started:
        result.update(status="scheduled", message="已在 QOJ 报名；参赛窗口开始前隐藏题目和附件。")
        return result
    result.update(status="running" if now < ends else "finished", content_available=True, can_submit=now < ends,
                  message="已接入 QOJ 原生参赛，时间与榜单均以上游为准。" if now < ends else "QOJ 原生参赛已结束。")
    contest = parse_contest(page.text, contest_id, allow_empty=True)
    contest["duration_seconds"] = record["duration_seconds"]
    result["contest"] = contest
    if not contest["problems"]:
        result.update(content_available=False, can_submit=False, message="QOJ 尚未开放当前参赛账号的比赛题目。")
        return result
    freeze_after = _scoreboard_freeze_after(page.text)
    if freeze_after is not None and 0 <= freeze_after < ends - started:
        result["scoreboard_freeze_at"] = started + freeze_after
        # An ended participation does not prove that QOJ has released its
        # scoreboard; leave the current frozen state unknown after the end.
        if now < ends:
            result["scoreboard_frozen"] = now >= result["scoreboard_freeze_at"]
    result.update(read_scoreboard(client, page, contest_id, contest["problems"]))
    return result


def _entry_message_text(fragment, form):
    """Extract a bounded error explanation, never the returned contest page."""
    message = BeautifulSoup(str(fragment), "html.parser")
    for node in message.select("script, style, nav, header, footer, form, input, textarea, select, button, "
                               "article, .uoj-article, table, pre, code, .uoj-username, "
                               ".navbar, .uoj-footer, a[href*='/user/profile/']"):
        node.decompose()
    text = " ".join(message.get_text(" ", strip=True).split())
    for name, value in form["hidden"].items():
        if value and (len(value) >= 6 or re.search(r"token|csrf|cookie|session|auth", name, re.I)):
            text = text.replace(value, "[redacted]")
    return text[:500]


def _entry_response_feedback(response, form):
    """Recognize explicit QOJ/UOJ form failures, including HTTP-200 messages.

    UOJ's becomeMsgPage() wraps its message in the regular .uoj-content area
    and usually leaves HTTP status at 200; crsf_defend() uses this for expired
    tokens. Generic Message pages can also be informational, so their neutral
    text is retained until the subsequent participation check confirms it.
    """
    source = response.text
    clean = lambda value: _entry_message_text(value, form)
    try:
        body = json.loads(source) if len(source) <= 65536 else None
    except (ValueError, TypeError):
        body = None
    if isinstance(body, dict):
        errors = body.get("errors") or body.get("error")
        failed = body.get("success") is False or body.get("ok") is False or body.get("status") in ("error", "failed")
        fields = {item["name"] for item in form["public"]["fields"]} | {"extra", "_token"}
        # UOJ's optional server validator returns {field: explanation}.
        if errors is None and body and set(body) <= fields:
            errors = body
        values = []
        if isinstance(errors, str):
            values = [errors]
        elif isinstance(errors, dict):
            for value in errors.values():
                values.extend(value if isinstance(value, list) else [value])
        elif isinstance(errors, list):
            values = errors
        text = clean(" ".join(value for value in values if isinstance(value, str)))
        if not text and failed and isinstance(body.get("message"), str):
            text = clean(body["message"])
        if text or failed or errors is True:
            return {"error": text or "QOJ 返回了参赛拒绝响应。"}
        return {}
    soup = BeautifulSoup(source, "html.parser")
    for node in soup.select(".alert-danger, .uoj-error, .error-message, .is-invalid ~ .invalid-feedback, .has-error .help-block"):
        if node.find_parent("article") or node.find_parent(class_="uoj-article"):
            continue
        if any(parent.has_attr("hidden") or parent.get("aria-hidden") == "true"
               or set(parent.get("class", ())) & {"hidden", "d-none"}
               or re.search(r"display\s*:\s*none", parent.get("style", ""), re.I)
               for parent in (node, *node.parents)):
            continue
        if text := clean(node):
            return {"error": text}
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    heading = soup.select_one(".uoj-content > h1, .uoj-content > h2, main > h1, main > h2")
    titles = [title, heading.get_text(" ", strip=True) if heading else ""]
    generic = re.compile(r"^(?:messages?|消息|提示)(?:\s*(?:[-|:：]|$))", re.I)
    error_page = re.compile(r"^(?:error|错误|出错|oops(?:\.{3}|!)?)(?:\s*(?:[-|:：]|$))", re.I)
    content = soup.select_one(".uoj-content, main")
    if content and any(generic.search(value) or error_page.search(value) for value in titles):
        text = clean(content)
        if text:
            definite = re.search(r"this page has expired|form is (?:incomplete|too large)|invalid.{0,20}(?:token|csrf)|"
                                 r"(?:token|csrf).{0,30}(?:invalid|expired)|not (?:allowed|permitted)|permission denied|"
                                 r"registration.{0,20}(?:failed|closed)|cannot|拒绝|失败|无权|过期|禁止", text, re.I)
            return {"error" if definite or any(error_page.search(value) for value in titles) else "notice": text}
    # UOJ can return the same CSRF/form error as plain text for AJAX requests.
    if not soup.find(True) and re.search(r"this page has expired|the form is incomplete|invalid csrf token", source, re.I):
        return {"error": clean(source)}
    return {}


def enter(client, contest_id, mode="vp", selection=None):
    """Refetch and validate the native form, then make exactly one POST."""
    if mode not in ("vp", "official"):
        raise QOJError("参赛模式必须是 vp 或 official。", 400)
    selection = selection or {}
    if not isinstance(selection, dict) or set(selection) - {"action", "values"}:
        raise QOJError("原生参赛表单选项无效。", 400)
    expected_username = client.username
    current = participation(client, contest_id, mode)
    _ensure_account(client, expected_username)
    if not client.username:
        raise QOJError("无法确认当前 QOJ 账号，请重新连接后再报名。", 401)
    expected_username = client.username
    if current["status"] in ("scheduled", "running", "finished"):
        return current
    if not current["actions"]:
        raise QOJError(current["message"], 409)
    if mode == "vp" and current.get("entry_stage") == "confirmation":
        page = _read_vp_registration(client, contest_id, current["upstream_url"], expected_username)
        if not _vp_registration_url(page.url, contest_id):
            raise QOJError("QOJ 报名确认页面已失效或参赛状态已变化，请刷新后重新选择。", 409)
    else:
        page = client._page(f"/contest/{int(contest_id)}")
    _ensure_account(client, expected_username)
    forms = native_forms(page.text, contest_id, page.url, mode)
    if mode == "official" and not forms:
        soup = BeautifulSoup(page.text, "html.parser")
        registration_url = _contest_link(soup, contest_id, "register") or _contest_link(soup, contest_id, "registration")
        if registration_url:
            page = client._page(registration_url)
            _ensure_account(client, expected_username)
            forms = native_forms(page.text, contest_id, page.url, mode)
    action = selection.get("action")
    candidates = [form for form in forms if action is None or form["public"]["id"] == action]
    if len(candidates) != 1:
        raise QOJError("QOJ 参赛表单已变化，请刷新后重新选择。", 409)
    form = candidates[0]
    payload = form_payload(form, selection.get("values"))
    response = client.fetch("POST", form["url"], data=payload, headers={"Referer": form["page_url"]},
                            url_guard=lambda target: _entry_url_allowed(target, contest_id, mode))
    observer = getattr(client, "_observe_page", None)
    if callable(observer):
        observer(response)
    if urlsplit(response.url).path.startswith("/login") or BeautifulSoup(response.text, "html.parser").select_one("form#form-login"):
        client.authenticated = False
        client.username = ""
        raise QOJError("QOJ 登录已过期；请重新连接后检查原生参赛状态。", 401)
    _ensure_account(client, expected_username)
    feedback = _entry_response_feedback(response, form)
    if feedback.get("error"):
        raise QOJError("QOJ 拒绝参赛请求：" + feedback["error"], 422)
    next_confirmation = mode == "vp" and bool(_vp_registration_url(response.url, contest_id))
    if next_confirmation:
        _remember_vp_registration(client, contest_id, response.url)
    updated = participation(client, contest_id, mode)
    _ensure_account(client, expected_username)
    if (next_confirmation and not _vp_registration_url(form["page_url"], contest_id)
            and updated.get("entry_stage") == "confirmation" and updated.get("actions")):
        # The dashboard's Start button only opened the registration page.
        # Show that native form; never silently accept its next confirmation.
        updated["entry_pending"] = True
        return updated
    if updated["status"] in ("unregistered", "unavailable"):
        if feedback.get("notice"):
            updated["message"] = "QOJ 返回参赛提示：" + feedback["notice"] + " 当前尚未确认参赛状态，请在 QOJ 核查；请求不会自动重发。"
        elif updated.get("entry_stage") == "confirmation":
            updated["message"] = "QOJ 尚未接受 VP 报名确认，请检查报名选项或在 QOJ 核查；请求不会自动重发。"
        elif updated["status"] == "unregistered" and updated.get("actions"):
            updated["message"] = ("QOJ 仍返回开始参赛表单，未确认已开启 VP。" if mode == "vp" else
                                  "QOJ 仍返回报名表单，未确认已完成报名。") + "请在 QOJ 检查登录及参赛提示；请求不会自动重发。"
        else:
            updated["message"] = "已发送一次原生参赛请求，但 QOJ 尚未确认参赛状态。请刷新检查，不要重复点击开始。"
        updated["entry_uncertain"] = True
    return updated
