"""
Claude App Login Tests
=======================

Unit tests for the OAuth login flow: PKCE URL shape, code exchange,
refresh, DPAPI-encrypted storage, and the api.py token fallback order.
"""
from __future__ import annotations

import time
import unittest
import urllib.parse
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from usage_monitor_for_claude import claude_oauth
from usage_monitor_for_claude.claude_oauth import (
    CLIENT_ID, exchange_code, generate_login, get_access_token, sign_out,
)


def _store(tmp: str):
    """Patch the token store into a temp dir and reset the cache."""
    claude_oauth._cache_mtime = claude_oauth._cache_tokens = None
    return patch.multiple(
        claude_oauth,
        STORE_DIR=Path(tmp),
        STORE_PATH=Path(tmp) / 'claude-oauth.dat',
    )


def _response(status=200, body=None):
    resp = MagicMock()
    resp.status_code = status
    if body is not None:
        resp.json.return_value = body
    else:
        resp.json.side_effect = ValueError('not json')
    return resp


class TestGenerateLogin(unittest.TestCase):
    def test_url_shape(self):
        url, verifier = generate_login()
        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        self.assertEqual(parsed.hostname, 'claude.ai')
        self.assertEqual(params['client_id'], CLIENT_ID)
        self.assertEqual(params['code_challenge_method'], 'S256')
        self.assertEqual(params['state'], verifier)
        self.assertNotIn(verifier, params['code_challenge'])  # challenge is hashed
        self.assertGreaterEqual(len(verifier), 43)

    def test_verifier_unique(self):
        self.assertNotEqual(generate_login()[1], generate_login()[1])


class TestExchangeAndStorage(unittest.TestCase):
    def test_exchange_saves_dpapi_roundtrip(self):
        with TemporaryDirectory() as tmp:
            with _store(tmp):
                body = {'access_token': 'at-app-1', 'refresh_token': 'rt-app-1', 'expires_in': 3600}
                with patch('usage_monitor_for_claude.claude_oauth.requests.post', return_value=_response(200, body)) as post:
                    self.assertIsNone(exchange_code('thecode#thestate', 'verif'))
                sent = post.call_args.kwargs['json']
                self.assertEqual(sent['code'], 'thecode')
                self.assertEqual(sent['state'], 'thestate')
                self.assertEqual(sent['code_verifier'], 'verif')

                # Stored encrypted: raw file must not contain the token bytes.
                raw = (Path(tmp) / 'claude-oauth.dat').read_bytes()
                self.assertNotIn(b'at-app-1', raw)
                # But DPAPI decrypts it back for use.
                self.assertEqual(get_access_token(), 'at-app-1')

    def test_exchange_rejection(self):
        with TemporaryDirectory() as tmp:
            with _store(tmp):
                with patch('usage_monitor_for_claude.claude_oauth.requests.post', return_value=_response(403, {})):
                    error = exchange_code('bad#state', 'verif')
                self.assertIn('403', error)
                self.assertIsNone(get_access_token())

    def test_sign_out_removes_store(self):
        with TemporaryDirectory() as tmp:
            with _store(tmp):
                body = {'access_token': 'at', 'refresh_token': 'rt', 'expires_in': 3600}
                with patch('usage_monitor_for_claude.claude_oauth.requests.post', return_value=_response(200, body)):
                    exchange_code('c#s', 'v')
                sign_out()
                self.assertIsNone(get_access_token())


class TestRefresh(unittest.TestCase):
    def test_expired_token_refreshes(self):
        with TemporaryDirectory() as tmp:
            with _store(tmp):
                claude_oauth._save({'access_token': 'at-old', 'refresh_token': 'rt-1', 'expires_at': time.time() - 10})
                body = {'access_token': 'at-new', 'refresh_token': 'rt-2', 'expires_in': 3600}
                with patch('usage_monitor_for_claude.claude_oauth.requests.post', return_value=_response(200, body)) as post:
                    self.assertEqual(get_access_token(), 'at-new')
                self.assertEqual(post.call_args.kwargs['json']['grant_type'], 'refresh_token')
                # Rotated refresh token persisted: next load uses rt-2.
                claude_oauth._cache_mtime = claude_oauth._cache_tokens = None
                self.assertEqual(claude_oauth._load()['refresh_token'], 'rt-2')

    def test_failed_refresh_returns_none(self):
        with TemporaryDirectory() as tmp:
            with _store(tmp):
                claude_oauth._save({'access_token': 'at-old', 'refresh_token': 'rt-1', 'expires_at': time.time() - 10})
                with patch('usage_monitor_for_claude.claude_oauth.requests.post', return_value=_response(400, {})):
                    self.assertIsNone(get_access_token())


class TestApiFallback(unittest.TestCase):
    def test_cli_token_wins(self):
        from usage_monitor_for_claude import api
        with TemporaryDirectory() as tmp:
            creds = Path(tmp) / '.credentials.json'
            creds.write_text('{"claudeAiOauth": {"accessToken": "cli-token"}}')
            with patch('usage_monitor_for_claude.api.CLAUDE_CREDENTIALS', creds):
                with patch('usage_monitor_for_claude.claude_oauth.get_access_token', return_value='app-token') as app:
                    self.assertEqual(api.read_access_token(), 'cli-token')
                    app.assert_not_called()

    def test_app_login_fallback(self):
        from usage_monitor_for_claude import api
        with TemporaryDirectory() as tmp:
            with patch('usage_monitor_for_claude.api.CLAUDE_CREDENTIALS', Path(tmp) / 'missing.json'):
                with patch('usage_monitor_for_claude.claude_oauth.get_access_token', return_value='app-token'):
                    self.assertEqual(api.read_access_token(), 'app-token')


if __name__ == '__main__':
    unittest.main()
