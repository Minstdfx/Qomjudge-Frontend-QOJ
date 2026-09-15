"""Exercise native entry using real requests serialization and a strict fake QOJ.

Every request is handled by an in-process transport adapter: no socket, account,
QOJ cookie or real contest action is used. The fake only starts after validating
the POST actually sent by QOJClient, rather than accepting every fetch call.
"""
from email.utils import formatdate
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import BaseAdapter

from app import create_app
from qoj import QOJClient
import test_participation as fixtures


class NativeEntryTransport(BaseAdapter):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.entered = False
        self.issued_token = None
        self.form_generation = 0
        self.rejection = None
        self.ignore_post = False

    @staticmethod
    def response(request, text, status=200, headers=None):
        response = requests.Response()
        response.status_code = status
        response.url = request.url
        response.request = request
        response.headers.update({'Content-Type': 'text/html; charset=utf-8',
                                 'Date': formatdate(fixtures.NOW, usegmt=True), **(headers or {})})
        response._content = text.encode('utf-8')
        response.encoding = 'utf-8'
        return response

    @staticmethod
    def document(body, title='Offline contest'):
        # Generic UOJ becomeMsgPage() also renders inside .uoj-content, with a
        # Message/消息 page title, and can return HTTP 200 without an alert class.
        return (f'<!doctype html><html><head><title>{escape(title)} - QOJ.ac</title></head><body>'
                '<nav><a href="/user/profile/fixture-team">fixture-team</a><a href="/logout">Logout</a></nav>'
                f'<div class="uoj-content">{body}</div></body></html>')

    def send(self, request, **kwargs):
        self.requests.append(request)
        parsed = urlsplit(request.url)
        if parsed.scheme != 'https' or parsed.netloc != 'qoj.ac':
            raise AssertionError('The test transport must never leave the fixture QOJ origin')
        if 'UOJSESSID=offline-captain-session' not in request.headers.get('Cookie', ''):
            return self.response(request, '<form id="form-login"></form>', headers={'Location': '/login'})
        if request.method == 'POST':
            if parsed.path != '/contest/9':
                raise AssertionError('Unexpected native action URL: ' + request.url)
            if (request.headers.get('Content-Type') != 'application/x-www-form-urlencoded'
                    or request.headers.get('Origin') != 'https://qoj.ac'
                    or request.headers.get('Referer') != 'https://qoj.ac/contest/9'):
                return self.response(request, self.document('Invalid request headers.', 'Message'))
            raw = request.body.decode('utf-8') if isinstance(request.body, bytes) else request.body
            values = parse_qs(raw, keep_blank_values=True)
            if (values != {'_token': [self.issued_token], 'submit-regvir': ['regvir']}
                    or int(request.headers.get('Content-Length', '-1')) != len(raw.encode('utf-8'))):
                return self.response(request, self.document('The form is incomplete.', 'Message'))
            if self.rejection:
                return self.response(request, self.document(self.rejection, 'Message'))
            if self.ignore_post:
                return self.dashboard(request)
            self.entered = True
            return self.response(request, '', 303, {'Location': '/contest/9'})
        if request.method != 'GET':
            raise AssertionError('Unexpected HTTP method')
        if parsed.path == '/contest/9':
            return self.dashboard(request)
        if parsed.path == '/contest/9/registrants':
            body = fixtures.registrants() if self.entered else fixtures.registrants(username='other-fixture-team')
            return self.response(request, self.document(body))
        if parsed.path == '/contest/9/standings':
            if not self.entered:
                raise AssertionError('The scoreboard must stay closed before a confirmed start')
            return self.response(request, self.document(fixtures.scoreboard()))
        raise AssertionError('Unexpected fixture path: ' + request.url)

    def dashboard(self, request):
        if self.entered:
            body = '<div class="card"><h4 class="card-header">Virtual Contest</h4><div class="countdown" data-rest="17940"></div></div>'
        else:
            self.form_generation += 1
            self.issued_token = f'offline-fresh-token-{self.form_generation}/+&='
            body = fixtures.VP_FORM.replace('fresh-token', escape(self.issued_token, quote=True))
        return self.response(request, self.document('<h1>Offline contest</h1>' + body + fixtures.LINKS + fixtures.PROBLEM_TABLE))

    def close(self):
        pass


class NativeEntryTransportTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.transport = NativeEntryTransport()
        self.upstream = QOJClient()
        self.addCleanup(self.upstream.session.close)
        self.upstream.session.adapters.clear()  # No fallback network transport.
        self.upstream.session.mount('https://qoj.ac/', self.transport)
        self.upstream.import_cookies('UOJSESSID=offline-captain-session')
        self.app = create_app(contest_id=9, data_dir=Path(directory.name), client_factory=lambda: self.upstream)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        state = self.client.get('/api/state').get_json()
        self.csrf = state['csrf_token']
        created = self.client.post('/api/team/create', json={'name': 'Strict transport fixture'},
                                   headers={'X-CSRF-Token': self.csrf})
        self.assertEqual(created.status_code, 200)

    def start(self):
        return self.client.post('/api/start', json={'mode': 'vp', 'action': 'regvir', 'selection': {}},
                                headers={'X-CSRF-Token': self.csrf})

    def post_count(self):
        return sum(request.method == 'POST' for request in self.transport.requests)

    def test_real_form_encoding_fresh_token_cookie_and_redirect_start_once(self):
        response = self.start()
        self.assertEqual(response.status_code, 200, response.get_json())
        state = response.get_json()
        self.assertTrue(self.transport.entered)
        self.assertEqual(state['participation']['status'], 'running')
        self.assertEqual(state['run']['started_at'], fixtures.NOW - 60)
        self.assertEqual(self.post_count(), 1)
        again = self.start().get_json()
        self.assertEqual(again['run']['id'], state['run']['id'])
        self.assertEqual(self.post_count(), 1)

    def test_http_200_csrf_rejection_is_visible_and_never_creates_a_run(self):
        self.transport.rejection = 'This page has expired.'
        response = self.start()
        self.assertEqual(response.status_code, 422, response.get_json())
        self.assertIn('This page has expired.', response.get_json()['error'])
        self.assertFalse(self.transport.entered)
        self.assertEqual(self.post_count(), 1)
        state = self.client.get('/api/state').get_json()
        self.assertIsNone(state['run'])
        self.assertFalse(state['permissions']['view_content'])
        self.assertEqual(self.post_count(), 1)

    def test_http_200_unchanged_start_form_is_not_success(self):
        self.transport.ignore_post = True
        response = self.start()
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertTrue(response.get_json()['error'])
        self.assertFalse(self.transport.entered)
        self.assertEqual(self.post_count(), 1)
        state = self.client.get('/api/state').get_json()
        self.assertIsNone(state['run'])
        self.assertFalse(state['permissions']['submit'])
        self.assertEqual(self.post_count(), 1)


if __name__ == '__main__':
    unittest.main()
