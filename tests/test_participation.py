"""Native QOJ participation fixtures; no real account, requests or submissions."""
import json
import unittest
from unittest.mock import Mock, patch
from datetime import datetime
from email.utils import formatdate
from urllib.parse import urlsplit

import requests

from participation import (QOJ_TIMEZONE, enter, form_payload, native_forms,
                           parse_scoreboard, participation, read_registrant_record, registrant_record)
from qoj import QOJClient, QOJError, parse_contest


NOW = datetime(2026, 9, 15, 13, 0, tzinfo=QOJ_TIMEZONE).timestamp()
PROBLEMS = [{"id": 10, "label": "A", "title": "Hidden A"}, {"id": 11, "label": "B", "title": "Hidden B"}]
LINKS = '<a href="/contest/9/registrants">Registrants</a><a href="/contest/9/standings">Standings</a>'
PROBLEM_TABLE = '<table><tr><td>A</td><td><a href="/contest/9/problem/10">Hidden A</a></td></tr><tr><td>B</td><td><a href="/contest/9/problem/11">Hidden B</a></td></tr></table>'
ATTACHMENT = '<div class="card"><h4 class="card-header">Attachments</h4><a href="/download.php?type=attachments&id=9">Hidden statements</a></div>'
VP_FORM = '<form id="form-regvir" method="post" action="/contest/9"><input type="hidden" name="_token" value="fresh-token"><button name="submit-regvir" value="regvir">Start!</button></form>'
REGISTER_FORM = '''<form id="form-register" method="post" action="/contest/9/register">
  <input type="hidden" name="_token" value="window-token">
  <label for="window">Participation window</label><select name="time-window" id="window" required>
    <option value="">Choose</option><option value="17">Window 17</option><option value="21">Window 21</option>
  </select><label><input type="checkbox" name="agree" value="yes" required>I agree</label>
  <button name="submit-register" value="register">Register</button></form>'''

# Stripped public QOJ markup observed 2026-09-15 at
# https://training.qoj.ac/contest/1042 and /contest/1042/registrants.
# Token/account/rating are fixture values; field names and table layout match
# the native pages. The actual registrants page contained 52 + 7 rows without
# pagination links or a search form.
OBSERVED_VP_FORM = '''<form action="/contest/1042" class="form-horizontal" id="form-regvir" method="post">
<input name="_token" type="hidden" value="fixture-token"/><div class="text-center">
<button class="btn btn-secondary" id="button-submit-regvir" name="submit-regvir" type="submit" value="regvir">Start!</button>
</div></form>'''
OBSERVED_REGISTRANTS = '''<table><thead><tr><th>#</th><th>Username</th><th>Rating</th><th>Start Time</th><th>Duration</th></tr></thead>
<tbody><tr><td>47</td><td><span class="uoj-username" data-nickname="Fixture nickname" data-rated="1" data-rating="1800" data-user-color="rgb(159, 221, 187)">fixture-team</span></td>
<td>1800</td><td>2024-05-24 09:06:36</td><td>210</td></tr></tbody></table>
<table><thead><tr><th>#</th><th>Username</th><th>Start Time</th></tr></thead>
<tbody><tr><td>6</td><td><span class="uoj-username" data-nickname="Fixture nickname" data-rated="1" data-rating="1800" data-user-color="rgb(159, 221, 187)">fixture-team</span></td>
<td>2024-05-24 09:06:36</td></tr></tbody></table>'''


def response(body, path="/contest/9"):
    result = requests.Response()
    result.status_code = 200
    result.url = "https://qoj.ac" + path
    result.headers["Date"] = formatdate(NOW, usegmt=True)
    result._content = body.encode()
    result.encoding = "utf-8"
    return result


def registrants(start=NOW - 60, username="fixture-team", virtual=True, virtual_table=True):
    stamp = datetime.fromtimestamp(start, QOJ_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    result = f'''<table><thead><tr><th>#</th><th>Username</th><th>Rating</th><th>Start Time</th><th>Duration</th></tr></thead>
      <tbody><tr><td>1</td><td><span class="uoj-username" data-nickname="Other display name">{username}</span></td>
      <td>1800</td><td>{stamp}</td><td>300</td></tr></tbody></table>'''
    if virtual_table:
        result += '<table><thead><tr><th>#</th><th>Username</th><th>Start Time</th></tr></thead><tbody>'
        if virtual:
            result += f'<tr><td>1</td><td><span class="uoj-username">{username}</span></td><td>{stamp}</td></tr>'
        result += '</tbody></table>'
    return result


def scoreboard(score=None, standings=None, rules="ICPC"):
    values = {"contest_id": 9, "contest_type": rules, "standings_version": 2,
              "standings": standings if standings is not None else [[100, 7200, ["fixture-team", 1800, 0, "Fixture Team"], 7]],
              "score": score if score is not None else {"fixture-team": [[100, 3600, 42, 2, 100, 0], [0, -1, -1, 3, 100, 1]]},
              "problems": [10, 11], "my_name": "fixture-team", "finalized": 0}
    return '<script>' + ''.join(name + '=' + json.dumps(value) + ';' for name, value in values.items()) + '</script>'


class FixtureClient:
    def __init__(self, mode="vp", entered=False, start=NOW - 60, virtual_table=True):
        self.username = "fixture-team"
        self.authenticated = True
        self.mode = mode
        self.entered = entered
        self.start = start
        self.virtual_table = virtual_table
        self.gets, self.posts = [], []
        self.board = scoreboard()

    def _page(self, path):
        path = urlsplit(path).path
        self.gets.append(path)
        if path == "/contest/9":
            if not self.entered:
                form = VP_FORM if self.mode == "vp" else '<a href="/contest/9/register">Register</a>'
                body = form
            else:
                heading = "Virtual Contest" if self.mode == "vp" else "Contest starts in" if self.start > NOW else "Contest ends in"
                body = f'<div class="card"><h4 class="card-header">{heading}</h4><div class="countdown" data-rest="100"></div></div>'
            return response('<h1>QOJ.ac</h1><h1>Fixture Contest</h1>' + body + LINKS + PROBLEM_TABLE + ATTACHMENT)
        if path == "/contest/9/register":
            return response(REGISTER_FORM, path)
        if path == "/contest/9/registrants":
            return response(registrants(self.start, virtual=self.mode == "vp", virtual_table=self.virtual_table), path)
        if path == "/contest/9/standings":
            return response(self.board, path)
        raise AssertionError("Unexpected request: " + path)

    def fetch(self, method, url, **kwargs):
        self.posts.append((method, url, kwargs))
        self.entered = True
        return response("Registered")


class FormTests(unittest.TestCase):
    def test_stripped_public_qoj_fixture_retains_native_form_fields(self):
        form = native_forms(OBSERVED_VP_FORM, 1042, "https://qoj.ac/contest/1042", "vp")[0]
        self.assertEqual(form["url"], "https://qoj.ac/contest/1042")
        self.assertEqual(form_payload(form, {}), {"_token": "fixture-token", "submit-regvir": "regvir"})

    def test_live_vp_form_descriptor_hides_token_and_preserves_action(self):
        form = native_forms(VP_FORM, 9, "https://qoj.ac/contest/9", "vp")[0]
        self.assertEqual(form["url"], "https://qoj.ac/contest/9")
        self.assertEqual(form["public"], {"id": "regvir", "label": "Start!", "kind": "start", "fields": []})
        self.assertNotIn("token", json.dumps(form["public"]))
        self.assertEqual(form_payload(form, {}), {"_token": "fresh-token", "submit-regvir": "regvir"})

    def test_form_options_are_advertised_and_untrusted_hidden_fields_rejected(self):
        form = native_forms(REGISTER_FORM, 9, "https://qoj.ac/contest/9/register", "official")[0]
        self.assertEqual(form["public"]["fields"][0]["options"], [{"value": "17", "label": "Window 17"}, {"value": "21", "label": "Window 21"}])
        payload = form_payload(form, {"time-window": "21", "agree": True})
        self.assertEqual(payload, {"_token": "window-token", "submit-register": "register", "time-window": "21", "agree": "yes"})
        for values in ({"time-window": "wrong", "agree": True}, {"time-window": "17", "agree": False},
                       {"time-window": "17", "agree": True, "_token": "attacker"},
                       {"time-window": "17", "agree": True, "submit-regvir": "regvir"}, {}):
            with self.subTest(values=values), self.assertRaises(QOJError):
                form_payload(form, values)

    def test_foreign_actions_wrong_contest_and_destructive_forms_are_not_accepted(self):
        for action in ("https://evil.test/contest/9", "/contest/10", "/logout", "/contest/9/manage"):
            self.assertEqual(native_forms(VP_FORM.replace('action="/contest/9"', f'action="{action}"'), 9, "https://qoj.ac/contest/9", "vp"), [])
        self.assertEqual(native_forms(VP_FORM.replace("regvir", "unregister"), 9, "https://qoj.ac/contest/9", "vp"), [])
        self.assertEqual(native_forms(VP_FORM.replace("regvir", "delete"), 9, "https://qoj.ac/contest/9", "official"), [])

    def test_unknown_required_file_and_missing_token_fail_closed(self):
        for source in (VP_FORM.replace('<button', '<input type="file" name="proof"><button'),
                       VP_FORM.replace('name="_token"', 'name="different"'),
                       VP_FORM.replace('method="post"', 'method="get"')):
            self.assertEqual(native_forms(source, 9, "https://qoj.ac/contest/9", "vp"), [])


class TimingTests(unittest.TestCase):
    def test_stripped_public_qoj_fixture_retains_native_virtual_membership(self):
        record = registrant_record(OBSERVED_REGISTRANTS, "fixture-team")
        start = datetime(2024, 5, 24, 9, 6, 36, tzinfo=QOJ_TIMEZONE).timestamp()
        self.assertEqual(record, {"started_at": start, "ends_at": start + 12600,
                                  "duration_seconds": 12600, "is_virtual": True})
        self.assertIsNone(registrant_record(OBSERVED_REGISTRANTS, "Fixture nickname"))

    def test_exact_account_registration_time_and_virtual_membership(self):
        record = registrant_record(registrants(), "fixture-team")
        self.assertEqual(record["started_at"], NOW - 60)
        self.assertEqual(record["ends_at"], NOW - 60 + 18000)
        self.assertTrue(record["is_virtual"])
        self.assertIsNone(registrant_record(registrants(), "Other display name"))
        self.assertIsNone(registrant_record(registrants(), ""))
        self.assertFalse(registrant_record(registrants(virtual=False), "fixture-team")["is_virtual"])

    def test_unregistered_vp_hides_problem_metadata_attachments_and_board(self):
        client = FixtureClient()
        state = participation(client, 9)
        self.assertEqual(state["status"], "unregistered")
        self.assertEqual(state["contest"]["title"], "Fixture Contest")
        self.assertFalse(state["content_available"])
        self.assertEqual(state["contest"]["problems"], [])
        self.assertEqual(state["contest"]["attachments"], [])
        self.assertEqual(state["scoreboard"], [])
        self.assertEqual(client.gets, ["/contest/9"])

    def test_scheduled_official_window_without_virtual_table_stays_hidden(self):
        client = FixtureClient("official", entered=True, start=NOW + 3600, virtual_table=False)
        state = participation(client, 9, "official")
        self.assertEqual(state["status"], "scheduled")
        self.assertEqual(state["started_at"], NOW + 3600)
        self.assertFalse(state["content_available"])
        self.assertFalse(state["can_submit"])
        self.assertNotIn("/contest/9/standings", client.gets)

    def test_existing_official_and_vp_sessions_restore_upstream_time(self):
        for mode in ("vp", "official"):
            with self.subTest(mode=mode):
                client = FixtureClient(mode, entered=True, virtual_table=mode == "vp")
                state = participation(client, 9, mode)
                self.assertEqual(state["status"], "running")
                self.assertEqual(state["started_at"], NOW - 60)
                self.assertTrue(state["can_submit"])
                self.assertEqual(len(state["contest"]["problems"]), 2)
                self.assertTrue(state["scoreboard_available"])
                self.assertEqual(client.posts, [])

    def test_finished_run_cannot_submit_and_wrong_mode_is_not_reinterpreted(self):
        client = FixtureClient(entered=True, start=NOW - 20000)
        state = participation(client, 9)
        self.assertEqual(state["status"], "finished")
        self.assertTrue(state["content_available"])
        self.assertFalse(state["can_submit"])
        state = participation(client, 9, "official")
        self.assertEqual(state["status"], "unavailable")
        self.assertFalse(state["content_available"])
        self.assertEqual(state["contest"]["problems"], [])

    def test_unknown_account_or_unrecognized_record_never_invents_start(self):
        client = FixtureClient(entered=True)
        client.username = "someone-else"
        state = participation(client, 9)
        self.assertEqual(state["status"], "unavailable")
        self.assertIsNone(state["started_at"])
        self.assertIsNone(state["ends_at"])
        self.assertFalse(state["can_submit"])
        self.assertNotIn("/contest/9/standings", client.gets)

    def test_pending_contest_metadata_can_be_parsed_without_revealing_questions(self):
        data = parse_contest('<h1>Future Contest</h1>', 9, allow_empty=True)
        self.assertEqual(data["title"], "Future Contest")
        self.assertEqual(data["problems"], [])


class PaginatedRegistrantTests(unittest.TestCase):
    # The public QOJ 1802 registrants page was checked without credentials on
    # 2026-09-15: both tables had 100 first-page rows and no common usernames.
    # The first public VP user's Duration=300 row was on ?page=3. Only table
    # shapes / pagination are reproduced; names and timestamps are fixtures.
    base = "https://qoj.ac/contest/9/registrants"

    @staticmethod
    def main(username="fixture-team", start=NOW - 60):
        return registrants(username=username, start=start, virtual_table=False)

    @staticmethod
    def virtual(username="fixture-team", start=NOW - 60):
        return registrants(username=username, start=start).split("</table>", 1)[1]

    @classmethod
    def links(cls, *pages):
        return '<ul class="pagination">' + ''.join(
            f'<li><a href="/contest/9/registrants?page={number}">{number}</a></li>' for number in pages) + '</ul>'

    @classmethod
    def client(cls, pages):
        class Client:
            username = "fixture-team"

            def __init__(self):
                self.gets = []

            def _page(self, url):
                self.gets.append(url)
                if url not in pages:
                    raise AssertionError("Unexpected registration URL: " + url)
                value = pages[url]
                if isinstance(value, Exception):
                    raise value
                path = urlsplit(url).path + ("?" + urlsplit(url).query if urlsplit(url).query else "")
                return response(value, path)

        return Client()

    def read(self, client):
        return read_registrant_record(client, 9, self.base, client.username)

    def test_vp_identity_and_duration_are_joined_across_pages_then_stop(self):
        second = self.base + "?page=2"
        pages = {self.base: self.main("other") + self.virtual() + self.links(2),
                 second: self.main() + self.virtual("other") + self.links(1, 3)}
        client = self.client(pages)
        self.assertEqual(self.read(client), {"started_at": NOW - 60, "ends_at": NOW - 60 + 18000,
                                             "duration_seconds": 18000, "is_virtual": True})
        self.assertEqual(client.gets, [self.base, second])

    def test_main_row_alone_does_not_claim_official_before_later_vp_page(self):
        second = self.base + "?page=2"
        client = self.client({self.base: self.main() + self.virtual("other") + self.links(2),
                              second: self.main("other") + self.virtual()})
        self.assertTrue(self.read(client)["is_virtual"])
        self.assertEqual(client.gets, [self.base, second])

    def test_official_absence_requires_the_complete_advertised_listing(self):
        second = self.base + "?page=2"
        client = self.client({self.base: self.main() + self.virtual("other") + self.links(2),
                              second: self.main("another") + self.virtual("another")})
        self.assertFalse(self.read(client)["is_virtual"])
        self.assertEqual(client.gets, [self.base, second])

    def test_recent_user_uses_advertised_last_page_and_only_matching_url_hints(self):
        second, last = self.base + "?page=2", self.base + "?page=3"
        pages = {self.base: self.main("other") + self.virtual("other") + self.links(2, 3),
                 last: self.main() + self.virtual()}
        client = self.client(pages)
        self.assertTrue(self.read(client)["is_virtual"])
        self.assertEqual(client.gets, [self.base, last])
        self.assertEqual(client._registrant_page_hints, {(9, "fixture-team"): (last,)})
        client.gets.clear()
        pages[last] = self.main(start=NOW - 100) + self.virtual(start=NOW - 100)
        self.assertEqual(self.read(client)["started_at"], NOW - 100)
        self.assertEqual(client.gets, [last])
        client.gets.clear()
        pages[last] = self.main("other") + self.virtual("other") + self.links(1, 2)
        pages[second] = self.main(start=NOW - 120) + self.virtual(start=NOW - 120)
        self.assertEqual(self.read(client)["started_at"], NOW - 120)
        self.assertEqual(client.gets, [last, self.base, second])

    def test_page_hints_are_scoped_to_current_username(self):
        last = self.base + "?page=3"
        pages = {self.base: self.main("new-team") + self.virtual("new-team") + self.links(3),
                 last: self.main() + self.virtual()}
        client = self.client(pages)
        self.read(client)
        client.gets.clear()
        client.username = "new-team"
        self.assertTrue(self.read(client)["is_virtual"])
        self.assertEqual(client.gets, [self.base])

    def test_untrusted_pagination_paths_queries_and_global_search_are_ignored(self):
        second = self.base + "?page=2"
        rejected = ["https://elsewhere.test/contest/9/registrants?page=2",
                    "/contest/10/registrants?page=2", "/contest/9/standings?page=2",
                    "/contest/9/registrants?page=2&action=start", "/contest/9/registrants?page=0",
                    "/contest/9/registrants?page=2&page=3", "/contest/9/registrants?search=other"]
        unsafe = '<ul class="pagination">' + ''.join(f'<li><a href="{url}">More</a></li>' for url in rejected) + '</ul>'
        global_search = '<form id="form-search-problem" method="get"><input name="search" placeholder="Search the problem set"></form>'
        client = self.client({self.base: self.main() + self.virtual("other") + unsafe + global_search + self.links(2),
                              second: self.virtual()})
        self.assertTrue(self.read(client)["is_virtual"])
        self.assertEqual(client.gets, [self.base, second])

    def test_incomplete_listing_cannot_treat_missing_vp_row_as_official(self):
        second = self.base + "?page=2"
        client = self.client({self.base: self.main() + self.virtual("other") + self.links(2),
                              second: QOJError("No permission on the next page", 403)})
        with self.assertRaises(QOJError) as caught:
            self.read(client)
        self.assertEqual(caught.exception.status, 403)
        self.assertFalse(getattr(client, "_registrant_page_hints", {}))

    def test_bounded_scan_fails_closed_instead_of_inventing_membership(self):
        pages = {}
        for number in range(1, 5):
            path = self.base if number == 1 else self.base + f"?page={number}"
            pages[path] = self.main() + self.virtual("other") + self.links(number + 1)
        client = self.client(pages)
        with patch("participation.MAX_REGISTRANT_PAGES", 3), self.assertRaises(QOJError) as caught:
            self.read(client)
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(len(client.gets), 3)
        self.assertFalse(getattr(client, "_registrant_page_hints", {}))

    def test_vp_row_without_actual_duration_does_not_create_default_clock(self):
        client = self.client({self.base: self.main("other") + self.virtual()})
        self.assertIsNone(self.read(client))

    def test_account_change_or_different_contest_response_is_rejected(self):
        for switch in ("account", "contest"):
            with self.subTest(switch=switch):
                client = self.client({self.base: self.main() + self.virtual()})
                original = client._page

                def altered(url):
                    page = original(url)
                    if switch == "account":
                        client.username = "changed-team"
                    else:
                        page.url = "https://qoj.ac/contest/10/registrants"
                    return page

                client._page = altered
                with self.assertRaises(QOJError) as caught:
                    self.read(client)
                self.assertEqual(caught.exception.status, 409 if switch == "account" else 502)


class EntryTests(unittest.TestCase):
    def test_start_posts_native_form_once_then_confirms_upstream_registration(self):
        client = FixtureClient()
        state = enter(client, 9, "vp", {"action": "regvir", "values": {}})
        self.assertEqual(state["status"], "running")
        self.assertEqual(len(client.posts), 1)
        method, url, kwargs = client.posts[0]
        self.assertEqual((method, url), ("POST", "https://qoj.ac/contest/9"))
        self.assertEqual(kwargs["data"], {"_token": "fresh-token", "submit-regvir": "regvir"})

    def test_official_time_window_registration_does_not_use_virtual_form(self):
        client = FixtureClient("official", start=NOW + 3600, virtual_table=False)
        state = enter(client, 9, "official", {"action": "register", "values": {"time-window": "17", "agree": True}})
        self.assertEqual(state["status"], "scheduled")
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(client.posts[0][1], "https://qoj.ac/contest/9/register")
        self.assertEqual(client.posts[0][2]["data"]["time-window"], "17")
        self.assertNotIn("submit-regvir", client.posts[0][2]["data"])

    def test_existing_participation_is_restored_without_any_repeat_post(self):
        client = FixtureClient(entered=True)
        state = enter(client, 9, "vp", {"action": "regvir", "values": {}})
        self.assertEqual(state["status"], "running")
        self.assertEqual(client.posts, [])

    def test_invalid_action_and_unadvertised_value_do_not_post(self):
        client = FixtureClient()
        with self.assertRaises(QOJError):
            enter(client, 9, "vp", {"action": "delete", "values": {}})
        self.assertEqual(client.posts, [])
        with self.assertRaises(QOJError):
            enter(client, 9, "vp", {"action": "regvir", "values": {"_token": "new"}})
        self.assertEqual(client.posts, [])


class EntryResponseTests(unittest.TestCase):
    @staticmethod
    def responder(client, body, *, accepted=False, path="/contest/9"):
        def fetch(method, url, **kwargs):
            client.posts.append((method, url, kwargs))
            client.entered = accepted
            return response(body, path)
        client.fetch = fetch
        return client

    @staticmethod
    def message(body, title="Message"):
        # UOJ becomeMsgPage() uses the standard page header/footer and HTTP
        # 200. Its CSRF guard emits exactly "This page has expired." here.
        return f'<title>{title} - QOJ.ac</title><nav>PRIVATE NAVBAR</nav><div class="uoj-content">{body}</div><footer>PRIVATE FOOTER</footer>'

    def start(self, client):
        return enter(client, 9, "vp", {"action": "regvir", "values": {}})

    def test_http_200_expired_csrf_message_is_an_explicit_rejection(self):
        markup = self.message('This page has expired. _token=fresh-token'
                              '<form><input name="_token" value="fresh-token"></form>'
                              '<table><tr><td>PRIVATE PROBLEM TABLE</td></tr></table>'
                              '<pre>PRIVATE SOURCE</pre><article>PRIVATE ANNOUNCEMENT</article>')
        client = self.responder(FixtureClient(), markup)
        with self.assertRaises(QOJError) as caught:
            self.start(client)
        self.assertEqual(caught.exception.status, 422)
        self.assertIn("This page has expired.", str(caught.exception))
        self.assertNotIn("fresh-token", str(caught.exception))
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(len(client.posts), 1)
        self.assertFalse(client.entered)

    def test_json_rejections_and_server_validation_maps_are_explained(self):
        bodies = [{"error": "Native registration rejected"},
                  {"errors": {"extra": ["Native registration rejected"]}},
                  {"success": False, "message": "Native registration rejected", "token": "fresh-token"},
                  {"extra": "Native registration rejected"}]
        for body in bodies:
            with self.subTest(body=body):
                client = self.responder(FixtureClient(), json.dumps(body))
                with self.assertRaises(QOJError) as caught:
                    self.start(client)
                self.assertEqual(caught.exception.status, 422)
                self.assertIn("Native registration rejected", str(caught.exception))
                self.assertNotIn("fresh-token", str(caught.exception))
                self.assertEqual(len(client.posts), 1)

    def test_field_validation_error_and_oops_page_are_not_silenced(self):
        sources = ['<form><div class="has-error"><span class="help-block">This window is closed.</span></div></form>',
                   '<form><input class="is-invalid"><div class="invalid-feedback">This window is closed.</div></form>',
                   self.message('<p>This window is closed.</p>', title="Oops...")]
        for source in sources:
            with self.subTest(source=source):
                client = self.responder(FixtureClient(), source)
                with self.assertRaises(QOJError) as caught:
                    self.start(client)
                self.assertEqual(caught.exception.status, 422)
                self.assertIn("This window is closed.", str(caught.exception))
                self.assertEqual(len(client.posts), 1)

    def test_returned_start_form_is_reported_as_not_started(self):
        client = self.responder(FixtureClient(), VP_FORM + PROBLEM_TABLE)
        state = self.start(client)
        self.assertEqual(state["status"], "unregistered")
        self.assertTrue(state["entry_uncertain"])
        self.assertIn("仍返回开始参赛表单", state["message"])
        self.assertIn("未确认已开启 VP", state["message"])
        self.assertNotIn("Hidden A", state["message"])
        self.assertFalse(state["content_available"])
        self.assertEqual(len(client.posts), 1)

    def test_neutral_native_notice_does_not_claim_entry_succeeded(self):
        client = self.responder(FixtureClient(), self.message("Please contact the contest administrator."))
        state = self.start(client)
        self.assertTrue(state["entry_uncertain"])
        self.assertIn("Please contact the contest administrator.", state["message"])
        self.assertFalse(state["content_available"])
        self.assertEqual(len(client.posts), 1)

    def test_positive_message_does_not_override_fresh_success_confirmation(self):
        client = self.responder(FixtureClient(), self.message("Registration completed."), accepted=True)
        state = self.start(client)
        self.assertEqual(state["status"], "running")
        self.assertFalse(state.get("entry_uncertain", False))
        self.assertEqual(len(client.posts), 1)

    def test_post_response_account_identity_is_observed_before_further_reads(self):
        client = self.responder(FixtureClient(), '<nav><a href="/user/profile/other-team">Other</a></nav>')
        client._observe_page = lambda page: QOJClient._observe_page(client, page)
        with self.assertRaises(QOJError) as caught:
            self.start(client)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(client.username, "other-team")
        self.assertEqual(len(client.posts), 1)
        self.assertNotIn("/contest/9/registrants", client.gets)

    def test_post_login_form_at_same_url_invalidates_account(self):
        for observed in (False, True):
            with self.subTest(observer=observed):
                client = self.responder(FixtureClient(), '<form id="form-login"><input name="username"></form>')
                if observed:
                    client._observe_page = lambda page: QOJClient._observe_page(client, page)
                with self.assertRaises(QOJError) as caught:
                    self.start(client)
                self.assertEqual(caught.exception.status, 401)
                self.assertFalse(client.authenticated)
                self.assertEqual(client.username, "")
                self.assertEqual(len(client.posts), 1)

    def test_hidden_placeholders_and_author_content_are_not_form_errors(self):
        markup = ('<article><div class="alert-danger">Author example</div></article>'
                  '<div hidden><div class="error-message">Hidden placeholder</div></div>'
                  '<div class="d-none"><div class="alert-danger">Hidden placeholder</div></div>'
                  '<form><input><div class="invalid-feedback">Required placeholder</div></form>')
        client = self.responder(FixtureClient(), markup, accepted=True)
        self.assertEqual(self.start(client)["status"], "running")
        self.assertEqual(len(client.posts), 1)


class ScoreboardTests(unittest.TestCase):
    def test_native_icpc_rank_penalty_and_frozen_cells_match_renderer(self):
        parsed = parse_scoreboard(scoreboard(), 9, PROBLEMS, "fixture-team")
        row = parsed["rows"][0]
        self.assertEqual(row["rank"], 7)
        # QOJ row[0] is points; its own ICPC renderer counts accepted cells.
        self.assertEqual(row["solved"], 1)
        self.assertEqual(row["penalty"], "2:00")
        self.assertEqual(row["problem_stats"]["10"]["text"], "+2\n1:00")
        self.assertEqual(row["problem_stats"]["11"]["text"], "?4")
        self.assertFalse(row["problem_stats"]["11"]["solved"])
        self.assertTrue(row["is_self"])

    def test_upstream_empty_php_score_array_is_a_valid_empty_board(self):
        self.assertEqual(parse_scoreboard(scoreboard(score=[], standings=[]), 9, PROBLEMS, "fixture-team")["rows"], [])

    def test_unknown_script_code_and_conflicting_assignments_are_not_evaluated(self):
        for source in ('<script>contest_id=9;standings=evil();score={};problems=[10,11];contest_type="ICPC";</script>',
                       scoreboard() + '<script>contest_id=99;</script>', scoreboard().replace('contest_id=9', 'contest_id=10')):
            with self.subTest(source=source[:60]), self.assertRaises(QOJError):
                parse_scoreboard(source, 9, PROBLEMS, "fixture-team")

    def test_other_problem_ids_cannot_leak_through_board(self):
        with self.assertRaises(QOJError):
            parse_scoreboard(scoreboard(), 9, [PROBLEMS[0]], "fixture-team")

    def test_oi_keeps_upstream_points_instead_of_icpc_accept_count(self):
        rows = [[124, 0, ["fixture-team", 1800, 0, "Fixture Team"], 3]]
        data = {"fixture-team": [[100, 3600, 42, 0, 100, 0], [24, 4000, 43, 0, 100, 0]]}
        row = parse_scoreboard(scoreboard(data, rows, "OI"), 9, PROBLEMS, "fixture-team")["rows"][0]
        self.assertEqual(row["solved"], 124)
        self.assertEqual(row["problem_stats"]["11"]["text"], "24")
        self.assertEqual(row["penalty"], "—")


class AccountBoundaryTests(unittest.TestCase):
    def test_snapshot_does_not_mix_account_registration_or_board(self):
        for switch_path in ("/contest/9/registrants", "/contest/9/standings"):
            with self.subTest(path=switch_path):
                client = FixtureClient(entered=True)
                original_page = client._page
                def page(path):
                    result = original_page(path)
                    if urlsplit(path).path == switch_path:
                        client.username = "different-team"
                    return result
                client._page = page
                with self.assertRaises(QOJError) as caught:
                    participation(client, 9)
                self.assertEqual(caught.exception.status, 409)
                self.assertEqual(client.posts, [])

    def test_form_refresh_cannot_switch_registration_account(self):
        for mode in ("vp", "official"):
            with self.subTest(mode=mode):
                client = FixtureClient(mode)
                original_page = client._page
                path_counts = {}
                switch_path = "/contest/9" if mode == "vp" else "/contest/9/register"
                def page(path):
                    path = urlsplit(path).path
                    result = original_page(path)
                    path_counts[path] = path_counts.get(path, 0) + 1
                    if path == switch_path and path_counts[path] == 2:
                        client.username = "different-team"
                    return result
                client._page = page
                with self.assertRaises(QOJError) as caught:
                    enter(client, 9, mode)
                self.assertEqual(caught.exception.status, 409)
                self.assertEqual(client.posts, [])

    def test_explicit_logged_out_menu_clears_old_identity(self):
        client = QOJClient()
        client.username, client.authenticated = "old-team", True
        client.fetch = Mock(return_value=response('<ul class="nav nav-pills"><li><a href="//qoj.ac/login">Login</a></li></ul><h1>Public page</h1>'))
        with self.assertRaises(QOJError) as caught:
            client._page("/contest/9")
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(client.username, "")
        self.assertFalse(client.authenticated)

    def test_problem_page_cannot_switch_the_submission_account(self):
        client = QOJClient()
        client.username = "old-team"
        client.fetch = Mock()
        def page(_):
            client.username = "different-team"
            return response("Changed user")
        client._page = page
        with self.assertRaises(QOJError) as caught:
            client.submit(9, 10, "C++17", "fixture-source", "fixture.cpp")
        self.assertEqual(caught.exception.status, 409)
        client.fetch.assert_not_called()

    def test_submission_list_cannot_switch_the_submission_account(self):
        client = QOJClient()
        client.username = "old-team"
        client.fetch = Mock()
        form = '''<form id="form-answer" action="/contest/9/problem/10">
          <input type="hidden" name="_token" value="token"><select name="answer_answer_language">
          <option value="C++17">C++17</option></select><button name="submit-answer" value="answer">Submit</button></form>'''
        def page(path):
            if path.endswith("/submissions"):
                client.username = "different-team"
                return response("Changed user")
            return response(form, "/contest/9/problem/10")
        client._page = page
        with self.assertRaises(QOJError) as caught:
            client.submit(9, 10, "C++17", "fixture-source", "fixture.cpp")
        self.assertEqual(caught.exception.status, 409)
        client.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
