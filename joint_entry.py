"""Read-only VP preparation and one authorized native start for a joint VP.

Only public form descriptions and fingerprints leave this module. Fresh CSRF
tokens remain in the QOJ client and are fetched again immediately before POST.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import hmac
import json
import re

from bs4 import BeautifulSoup

from participation import (
    _ensure_account, _read_vp_registration, _remember_vp_registration,
    _vp_registration_link, _vp_registration_url, enter, form_payload,
    native_forms, participation,
)
from qoj import QOJError


def _lock(client):
    return getattr(client, "lock", None) or nullcontext()


def _confirmation_text(page, form):
    """Bind visible consent text, never hidden values, scripts or page content."""
    soup = BeautifulSoup(page.text, "html.parser")
    matches = []
    for node in soup.select("form"):
        buttons = node.select("button[name], input[type='submit'][name]")
        if any(button.get("name") == form["submit_name"] for button in buttons):
            matches.append(node)
    if len(matches) != 1:
        raise QOJError("QOJ 报名确认表单不唯一，请重新检查参赛状态。", 409)
    fragment = matches[0]
    for node in fragment.select("script, style, [hidden], [aria-hidden='true'], .d-none, .hidden, input[type='hidden'], "
                                ".alert-danger, .alert-success, .error-message, .invalid-feedback, .uoj-error"):
        if node.parent is not None:
            node.decompose()
    text = " ".join(fragment.get_text(" ", strip=True).split())
    for value in form["hidden"].values():
        if value:
            text = text.replace(value, "[hidden]")
    if len(text) > 16000:
        raise QOJError("QOJ 报名确认文字过长，请在原站检查后单独开始 VP。", 409)
    return text


def _plan(page, form, username):
    text = _confirmation_text(page, form)
    binding = {"account": username, "action": form["public"], "url": form["url"],
               "page_url": page.url, "submit_name": form["submit_name"],
               "submit_value": form["submit_value"], "hidden_names": sorted(form["hidden"]),
               "hidden_options": {name: value for name, value in form["hidden"].items()
                                  if not re.search(r"token|csrf|nonce", name, re.I)},
               "confirmation": text}
    fingerprint = hashlib.sha256(json.dumps(binding, sort_keys=True, ensure_ascii=False,
                                            separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"action": form["public"], "fingerprint": fingerprint,
            "message": text or "准备即授权组织者为本队提交此 QOJ VP 开赛表单。"}


def _one_form(page, contest_id):
    forms = native_forms(page.text, contest_id, page.url, "vp")
    if len(forms) != 1:
        raise QOJError("QOJ VP 报名表单无法唯一确认，请刷新后检查或单独开始 VP。", 409)
    return forms[0]


def prepare_entry(client, contest_id):
    """GET the final available VP form without starting upstream participation."""
    contest_id = int(contest_id)
    with _lock(client):
        expected_username = client.username
        current = participation(client, contest_id, "vp")
        _ensure_account(client, expected_username)
        if not client.username or not client.authenticated:
            raise QOJError("请先连接 QOJ 账号，再准备联合 VP。", 401)
        expected_username = client.username
        if current["status"] in ("scheduled", "running", "finished"):
            raise QOJError("当前账号已有本场 QOJ 参赛记录，不能准备新的联合 VP。", 409)
        if current["status"] != "unregistered" or not current.get("actions"):
            raise QOJError(current.get("message") or "尚未确认可以开始 QOJ VP。", 409)

        if current.get("entry_stage") == "confirmation":
            page = _read_vp_registration(client, contest_id, current["upstream_url"], expected_username)
            if not _vp_registration_url(page.url, contest_id):
                raise QOJError("QOJ 报名确认页面已失效，请重新准备。", 409)
        else:
            page = client._page(f"/contest/{contest_id}")
            _ensure_account(client, expected_username)
            form = _one_form(page, contest_id)
            if form["public"]["fields"]:
                # Do not bypass first-stage choices by jumping straight to a
                # final page. If another stage appears, it needs fresh consent.
                return _plan(page, form, expected_username)
            soup = BeautifulSoup(page.text, "html.parser")
            target = _vp_registration_link(soup, contest_id, page.url)
            # Modern QOJ's dashboard POST merely redirects here. A GET lets
            # the captain review this final form before authorizing any POST.
            target = target or f"https://qoj.ac/contest/{contest_id}/virtual/register"
            try:
                registration = _read_vp_registration(client, contest_id, target, expected_username)
            except QOJError as exc:
                if exc.status != 404:
                    raise
            else:
                if _vp_registration_url(registration.url, contest_id):
                    _one_form(registration, contest_id)
                    _remember_vp_registration(client, contest_id, registration.url)
                    page = registration
                else:
                    # Legacy one-step QOJ may redirect this path to dashboard.
                    # Use its newly issued token and public form, not old HTML.
                    page = registration
        _ensure_account(client, expected_username)
        return _plan(page, _one_form(page, contest_id), expected_username)


def _validate_values(form, values):
    if not isinstance(values, dict):
        raise QOJError("QOJ 报名选项必须是字段对象。", 400)
    for field in form["public"]["fields"]:
        if field["type"] == "checkbox" and (field["name"] not in values or
                                               not isinstance(values[field["name"]], bool)):
            raise QOJError("请明确确认报名选项：" + field["label"], 400)
    return form_payload(form, values)


def validate_preparation(plan, action, values):
    """Validate the captain's public choices without requesting or posting."""
    action_id = action.get("id") if isinstance(action, dict) else action
    if action_id != plan["action"]["id"]:
        raise QOJError("QOJ 报名表单已变化，请重新准备。", 409)
    form = {"public": plan["action"], "hidden": {}, "submit_name": "", "submit_value": ""}
    _validate_values(form, values)


class _GuardedEntry:
    """Check the exact form used by enter(), after all of its fresh GETs."""
    def __init__(self, client, contest_id, fingerprint, action, values):
        object.__setattr__(self, "_joint_client", client)
        object.__setattr__(self, "_joint_contest", contest_id)
        object.__setattr__(self, "_joint_fingerprint", fingerprint)
        object.__setattr__(self, "_joint_action", action)
        object.__setattr__(self, "_joint_values", values)
        object.__setattr__(self, "_joint_username", client.username)
        object.__setattr__(self, "_joint_pages", {})
        object.__setattr__(self, "_joint_posted", False)

    def __getattr__(self, name):
        return getattr(self._joint_client, name)

    def __setattr__(self, name, value):
        if name.startswith("_joint_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._joint_client, name, value)

    def _page(self, *args, **kwargs):
        page = self._joint_client._page(*args, **kwargs)
        self._joint_pages[page.url] = page
        return page

    def fetch(self, method, url, **kwargs):
        if method.upper() != "POST":
            return self._joint_client.fetch(method, url, **kwargs)
        if self._joint_posted:
            raise QOJError("本次联合 VP 已发送过开赛请求，请先核查 QOJ 状态。", 409)
        _ensure_account(self._joint_client, self._joint_username)
        if not self._joint_client.authenticated or not self._joint_client.username:
            raise QOJError("QOJ 登录已失效，请重新连接后准备。", 401)
        referer = kwargs.get("headers", {}).get("Referer")
        page = self._joint_pages.get(referer)
        if page is None:
            raise QOJError("未能核实最新 QOJ 报名表单，请重新准备。", 409)
        form = _one_form(page, self._joint_contest)
        plan = _plan(page, form, self._joint_username)
        if (form["public"]["id"] != self._joint_action or form["url"] != url or
                not hmac.compare_digest(plan["fingerprint"], self._joint_fingerprint)):
            raise QOJError("QOJ 报名选项或确认文字已变化，请队长重新准备。", 409)
        payload = _validate_values(form, self._joint_values)
        if payload != kwargs.get("data"):
            raise QOJError("QOJ 报名请求与队长确认的选项不一致，请重新准备。", 409)
        self._joint_posted = True
        return self._joint_client.fetch(method, url, **kwargs)


def authorize_entry(client, contest_id, fingerprint, action, values):
    """Start once with the captain-approved form; never accept new follow-ups."""
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise QOJError("联合 VP 报名确认已失效，请重新准备。", 409)
    action_id = action.get("id") if isinstance(action, dict) else action
    if not isinstance(action_id, str) or not action_id:
        raise QOJError("请选择 QOJ VP 报名操作。", 400)
    if not isinstance(values, dict):
        raise QOJError("QOJ 报名选项必须是字段对象。", 400)
    with _lock(client):
        guarded = _GuardedEntry(client, int(contest_id), fingerprint, action_id, dict(values))
        return enter(guarded, int(contest_id), "vp", {"action": action_id, "values": dict(values)})
