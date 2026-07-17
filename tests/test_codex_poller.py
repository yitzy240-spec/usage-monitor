"""
Codex Poller Tests
===================

Unit tests for CodexPoller cadence, backoff, and the auth-error latch.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from usage_monitor_for_claude.codex_poller import CodexPoller
from usage_monitor_for_claude.settings import CODEX_POLL_INTERVAL, MAX_BACKOFF, POLL_ERROR

OK = {'five_hour': {'utilization': 10.0, 'resets_at': None}}
TOKENS = {'access_token': 'at-1'}


class TestPollOnce(unittest.TestCase):
    """Tests for CodexPoller._poll_once()."""

    def test_success_uses_normal_interval(self):
        """A successful fetch stores the snapshot and returns the normal cadence."""
        poller = CodexPoller()
        with patch('usage_monitor_for_claude.codex_poller.read_codex_tokens', return_value=TOKENS):
            with patch('usage_monitor_for_claude.codex_poller.fetch_codex_usage', return_value=OK):
                interval = poller._poll_once()
        self.assertEqual(interval, CODEX_POLL_INTERVAL)
        self.assertEqual(poller.usage, OK)
        self.assertIsNotNone(poller.last_success_time)

    def test_error_backoff_doubles_and_caps(self):
        """Consecutive errors double the backoff, capped at MAX_BACKOFF."""
        poller = CodexPoller()
        with patch('usage_monitor_for_claude.codex_poller.read_codex_tokens', return_value=TOKENS):
            with patch('usage_monitor_for_claude.codex_poller.fetch_codex_usage', return_value={'error': 'x'}):
                intervals = [poller._poll_once() for _ in range(10)]
        self.assertEqual(intervals[0], POLL_ERROR)
        self.assertEqual(intervals[1], POLL_ERROR * 2)
        self.assertEqual(intervals[-1], MAX_BACKOFF)

    def test_success_resets_backoff(self):
        """A success after errors returns to the normal cadence."""
        poller = CodexPoller()
        with patch('usage_monitor_for_claude.codex_poller.read_codex_tokens', return_value=TOKENS):
            with patch('usage_monitor_for_claude.codex_poller.fetch_codex_usage', return_value={'error': 'x'}):
                poller._poll_once()
                poller._poll_once()
            with patch('usage_monitor_for_claude.codex_poller.fetch_codex_usage', return_value=OK):
                self.assertEqual(poller._poll_once(), CODEX_POLL_INTERVAL)
            with patch('usage_monitor_for_claude.codex_poller.fetch_codex_usage', return_value={'error': 'x'}):
                self.assertEqual(poller._poll_once(), POLL_ERROR)

    def test_rate_limited_honors_retry_after(self):
        """A 429 with Retry-After waits at least that long (capped)."""
        poller = CodexPoller()
        result = {'error': 'x', 'rate_limited': True, 'retry_after': 300}
        with patch('usage_monitor_for_claude.codex_poller.read_codex_tokens', return_value=TOKENS):
            with patch('usage_monitor_for_claude.codex_poller.fetch_codex_usage', return_value=result):
                self.assertEqual(poller._poll_once(), 300)

    def test_auth_error_latches_until_token_changes(self):
        """After an auth error, fetches are skipped while the token is unchanged."""
        poller = CodexPoller()
        with patch('usage_monitor_for_claude.codex_poller.read_codex_tokens', return_value=TOKENS):
            with patch('usage_monitor_for_claude.codex_poller.fetch_codex_usage', return_value={'error': 'x', 'auth_error': True}) as fetch:
                poller._poll_once()
                poller._poll_once()
                poller._poll_once()
        self.assertEqual(fetch.call_count, 1)

        # A new login (different token) clears the latch.
        with patch('usage_monitor_for_claude.codex_poller.read_codex_tokens', return_value={'access_token': 'at-2'}):
            with patch('usage_monitor_for_claude.codex_poller.fetch_codex_usage', return_value=OK) as fetch:
                self.assertEqual(poller._poll_once(), CODEX_POLL_INTERVAL)
        self.assertEqual(fetch.call_count, 1)

    def test_on_update_callback_receives_snapshot(self):
        """The on_update callback gets a copy of each fetched snapshot."""
        seen = []
        poller = CodexPoller(on_update=seen.append)
        with patch('usage_monitor_for_claude.codex_poller.read_codex_tokens', return_value=TOKENS):
            with patch('usage_monitor_for_claude.codex_poller.fetch_codex_usage', return_value=OK):
                poller._poll_once()
        self.assertEqual(seen, [OK])

    def test_on_update_exception_is_swallowed(self):
        """A crashing callback does not break polling."""
        def boom(_):
            raise RuntimeError('boom')

        poller = CodexPoller(on_update=boom)
        with patch('usage_monitor_for_claude.codex_poller.read_codex_tokens', return_value=TOKENS):
            with patch('usage_monitor_for_claude.codex_poller.fetch_codex_usage', return_value=OK):
                self.assertEqual(poller._poll_once(), CODEX_POLL_INTERVAL)


if __name__ == '__main__':
    unittest.main()
