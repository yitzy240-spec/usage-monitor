"""
Codex API Client Tests
=======================

Unit tests for the Codex (OpenAI) usage provider: token reading, usage
fetching/mapping, the 403 retry ladder, and token refresh with atomic
write-back to auth.json.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from usage_monitor_for_claude.codex_api import (
    _classify_window, _map_usage, fetch_codex_usage, read_codex_tokens,
)

VALID_AUTH = {
    'auth_mode': 'chatgpt',
    'OPENAI_API_KEY': None,
    'tokens': {
        'id_token': 'id-jwt',
        'access_token': 'at-1',
        'refresh_token': 'rt-1',
        'account_id': 'acct-uuid',
    },
    'last_refresh': '2026-07-01T00:00:00Z',
}

USAGE_RESPONSE = {
    'plan_type': 'plus',
    'rate_limit': {
        'primary_window': {'used_percent': 12.5, 'reset_at': 1789000000, 'limit_window_seconds': 18000},
        'secondary_window': {'used_percent': 34.0, 'reset_at': 1789600000, 'limit_window_seconds': 604800},
    },
    'additional_rate_limits': [
        {'limit_name': 'Spark', 'rate_limit': {'used_percent': 5.0, 'reset_at': 1789000000, 'limit_window_seconds': 18000}},
    ],
}


def _auth_file(tmp: str, data: dict | str = VALID_AUTH) -> Path:
    path = Path(tmp) / 'auth.json'
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding='utf-8')
    return path


def _response(status: int = 200, body: dict | None = None, text: str = '', headers: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.text = text or (json.dumps(body) if body is not None else '')
    if body is not None:
        resp.json.return_value = body
    else:
        resp.json.side_effect = ValueError('not json')
    return resp


# ---------------------------------------------------------------------------
# read_codex_tokens
# ---------------------------------------------------------------------------

class TestReadCodexTokens(unittest.TestCase):
    """Tests for read_codex_tokens()."""

    def test_file_missing(self):
        """Missing auth file returns None."""
        with TemporaryDirectory() as tmp:
            with patch('usage_monitor_for_claude.codex_api.CODEX_AUTH', Path(tmp) / 'nope.json'):
                self.assertIsNone(read_codex_tokens())

    def test_valid_tokens(self):
        """Extracts the tokens dict from a well-formed auth file."""
        with TemporaryDirectory() as tmp:
            with patch('usage_monitor_for_claude.codex_api.CODEX_AUTH', _auth_file(tmp)):
                tokens = read_codex_tokens()
                self.assertEqual(tokens['access_token'], 'at-1')
                self.assertEqual(tokens['account_id'], 'acct-uuid')

    def test_malformed_json(self):
        """Malformed JSON returns None."""
        with TemporaryDirectory() as tmp:
            with patch('usage_monitor_for_claude.codex_api.CODEX_AUTH', _auth_file(tmp, 'not json')):
                self.assertIsNone(read_codex_tokens())

    def test_missing_access_token(self):
        """Auth file without an access token returns None."""
        with TemporaryDirectory() as tmp:
            broken = {**VALID_AUTH, 'tokens': {'refresh_token': 'rt-1'}}
            with patch('usage_monitor_for_claude.codex_api.CODEX_AUTH', _auth_file(tmp, broken)):
                self.assertIsNone(read_codex_tokens())


# ---------------------------------------------------------------------------
# _classify_window / _map_usage
# ---------------------------------------------------------------------------

class TestMapping(unittest.TestCase):
    """Tests for window classification and quota-model mapping."""

    def test_classify_known_windows(self):
        """Known window durations map to upstream field names, with ±10% tolerance."""
        self.assertEqual(_classify_window(18000), 'five_hour')
        self.assertEqual(_classify_window(19000), 'five_hour')
        self.assertEqual(_classify_window(604800), 'seven_day')
        self.assertEqual(_classify_window(86400), 'one_day')
        self.assertEqual(_classify_window(2592000), 'thirty_day')

    def test_classify_unknown_window(self):
        """Unrecognized durations return None."""
        self.assertIsNone(_classify_window(12345))
        self.assertIsNone(_classify_window(0))
        self.assertIsNone(_classify_window(None))

    def test_map_usage_quota_shape(self):
        """Windows map onto the upstream quota-dict shape with ISO reset times."""
        mapped = _map_usage(USAGE_RESPONSE)
        self.assertEqual(mapped['five_hour']['utilization'], 12.5)
        self.assertEqual(mapped['seven_day']['utilization'], 34.0)
        expected_iso = datetime.fromtimestamp(1789000000, tz=timezone.utc).isoformat()
        self.assertEqual(mapped['five_hour']['resets_at'], expected_iso)
        self.assertEqual(mapped['plan_type'], 'plus')

    def test_map_usage_additional_limits(self):
        """additional_rate_limits become variant fields (five_hour_spark)."""
        mapped = _map_usage(USAGE_RESPONSE)
        self.assertEqual(mapped['five_hour_spark']['utilization'], 5.0)

    def test_map_usage_missing_windows(self):
        """A response without rate_limit yields no quota fields but no crash."""
        mapped = _map_usage({'plan_type': 'plus'})
        self.assertNotIn('five_hour', mapped)
        self.assertEqual(mapped['plan_type'], 'plus')

    def test_map_usage_null_reset(self):
        """A window without reset_at keeps utilization and a None resets_at."""
        data = {'rate_limit': {'primary_window': {'used_percent': 1.0, 'limit_window_seconds': 18000}}}
        mapped = _map_usage(data)
        self.assertEqual(mapped['five_hour']['utilization'], 1.0)
        self.assertIsNone(mapped['five_hour']['resets_at'])


# ---------------------------------------------------------------------------
# fetch_codex_usage
# ---------------------------------------------------------------------------

class TestFetchCodexUsage(unittest.TestCase):
    """Tests for fetch_codex_usage() request/error handling."""

    def _fetch(self, tmp: str, get_side_effect, auth: dict | str = VALID_AUTH, **kwargs):
        with patch('usage_monitor_for_claude.codex_api.CODEX_AUTH', _auth_file(tmp, auth)):
            with patch('usage_monitor_for_claude.codex_api.requests.get', side_effect=get_side_effect) as get:
                with patch('usage_monitor_for_claude.codex_api.time.sleep'):
                    result = fetch_codex_usage(**kwargs)
        return result, get

    def test_no_auth_file(self):
        """No auth file yields the no_token error with auth_error set."""
        with TemporaryDirectory() as tmp:
            with patch('usage_monitor_for_claude.codex_api.CODEX_AUTH', Path(tmp) / 'nope.json'):
                result = fetch_codex_usage()
        self.assertTrue(result['auth_error'])
        self.assertIn('error', result)

    def test_success(self):
        """A 200 response is mapped into quota fields."""
        with TemporaryDirectory() as tmp:
            result, get = self._fetch(tmp, [_response(200, USAGE_RESPONSE)])
        self.assertEqual(result['five_hour']['utilization'], 12.5)
        self.assertNotIn('error', result)
        # Auth headers present on the request
        _, call_kwargs = get.call_args
        self.assertEqual(call_kwargs['headers']['Authorization'], 'Bearer at-1')
        self.assertEqual(call_kwargs['headers']['chatgpt-account-id'], 'acct-uuid')

    def test_transient_403_retries_then_succeeds(self):
        """HTML 403s are retried on the ladder before succeeding."""
        with TemporaryDirectory() as tmp:
            responses = [_response(403, text='<html>cf</html>'), _response(403, text='<html>cf</html>'), _response(200, USAGE_RESPONSE)]
            result, get = self._fetch(tmp, responses)
        self.assertEqual(result['five_hour']['utilization'], 12.5)
        self.assertEqual(get.call_count, 3)

    def test_persistent_403_gives_error(self):
        """403 on every rung of the ladder yields an http error."""
        with TemporaryDirectory() as tmp:
            result, get = self._fetch(tmp, [_response(403, text='<html>cf</html>')] * 10)
        self.assertIn('error', result)
        self.assertNotIn('five_hour', result)

    def test_rate_limited_429(self):
        """429 sets rate_limited and parses Retry-After."""
        with TemporaryDirectory() as tmp:
            result, _ = self._fetch(tmp, [_response(429, headers={'Retry-After': '120'})])
        self.assertTrue(result['rate_limited'])
        self.assertEqual(result['retry_after'], 120)

    def test_connection_error(self):
        """Connection failures yield the connection_error message."""
        import requests as requests_mod
        with TemporaryDirectory() as tmp:
            result, _ = self._fetch(tmp, requests_mod.ConnectionError('down'))
        self.assertIn('error', result)
        self.assertNotIn('auth_error', result)

    def test_404_falls_back_to_wham(self):
        """A 404 on the primary endpoint retries the fallback URL."""
        with TemporaryDirectory() as tmp:
            responses = [_response(404), _response(200, USAGE_RESPONSE)]
            result, get = self._fetch(tmp, responses)
        self.assertEqual(result['five_hour']['utilization'], 12.5)
        urls = [call.args[0] for call in get.call_args_list]
        self.assertIn('wham', urls[1])


# ---------------------------------------------------------------------------
# Token refresh + write-back
# ---------------------------------------------------------------------------

class TestTokenRefresh(unittest.TestCase):
    """Tests for the 401 → re-read → refresh → write-back flow."""

    def test_reread_picks_up_rotated_token(self):
        """On 401, a token rotated externally (by the Codex CLI) is used without refreshing."""
        with TemporaryDirectory() as tmp:
            auth_path = _auth_file(tmp)

            rotated = {**VALID_AUTH, 'tokens': {**VALID_AUTH['tokens'], 'access_token': 'at-2'}}

            def rotate_then_serve(url, **kwargs):
                if kwargs['headers']['Authorization'] == 'Bearer at-1':
                    auth_path.write_text(json.dumps(rotated), encoding='utf-8')
                    return _response(401)
                return _response(200, USAGE_RESPONSE)

            with patch('usage_monitor_for_claude.codex_api.CODEX_AUTH', auth_path):
                with patch('usage_monitor_for_claude.codex_api.requests.get', side_effect=rotate_then_serve):
                    with patch('usage_monitor_for_claude.codex_api.requests.post') as post:
                        with patch('usage_monitor_for_claude.codex_api.time.sleep'):
                            result = fetch_codex_usage()

        self.assertEqual(result['five_hour']['utilization'], 12.5)
        post.assert_not_called()

    def test_refresh_and_atomic_write_back(self):
        """On 401 with an unchanged file, tokens are refreshed and written back preserving schema."""
        with TemporaryDirectory() as tmp:
            auth_path = _auth_file(tmp)

            def serve(url, **kwargs):
                if kwargs['headers']['Authorization'] == 'Bearer at-1':
                    return _response(401)
                self.assertEqual(kwargs['headers']['Authorization'], 'Bearer at-new')
                return _response(200, USAGE_RESPONSE)

            refresh_resp = _response(200, {'access_token': 'at-new', 'refresh_token': 'rt-new', 'id_token': 'id-new'})

            with patch('usage_monitor_for_claude.codex_api.CODEX_AUTH', auth_path):
                with patch('usage_monitor_for_claude.codex_api.requests.get', side_effect=serve):
                    with patch('usage_monitor_for_claude.codex_api.requests.post', return_value=refresh_resp) as post:
                        with patch('usage_monitor_for_claude.codex_api.time.sleep'):
                            result = fetch_codex_usage()
            written = json.loads(auth_path.read_text(encoding='utf-8'))

        self.assertEqual(result['five_hour']['utilization'], 12.5)
        # Refresh call shape
        _, post_kwargs = post.call_args
        self.assertEqual(post_kwargs['json']['grant_type'], 'refresh_token')
        self.assertEqual(post_kwargs['json']['refresh_token'], 'rt-1')
        # Write-back preserved schema and rotated tokens
        self.assertEqual(written['auth_mode'], 'chatgpt')
        self.assertIn('OPENAI_API_KEY', written)
        self.assertEqual(written['tokens']['access_token'], 'at-new')
        self.assertEqual(written['tokens']['refresh_token'], 'rt-new')
        self.assertEqual(written['tokens']['account_id'], 'acct-uuid')
        self.assertIn('last_refresh', written)

    def test_refresh_failure_is_auth_error(self):
        """A failed refresh yields auth_error and does not rewrite auth.json."""
        with TemporaryDirectory() as tmp:
            auth_path = _auth_file(tmp)
            original = auth_path.read_text(encoding='utf-8')

            refresh_resp = _response(400, {'error': 'refresh_token_expired'})

            with patch('usage_monitor_for_claude.codex_api.CODEX_AUTH', auth_path):
                with patch('usage_monitor_for_claude.codex_api.requests.get', return_value=_response(401)):
                    with patch('usage_monitor_for_claude.codex_api.requests.post', return_value=refresh_resp):
                        with patch('usage_monitor_for_claude.codex_api.time.sleep'):
                            result = fetch_codex_usage()
            after = auth_path.read_text(encoding='utf-8')

        self.assertTrue(result['auth_error'])
        self.assertEqual(after, original)


if __name__ == '__main__':
    unittest.main()
