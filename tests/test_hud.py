"""
HUD Tests
==========

Unit tests for hotkey parsing, mood mapping, and the provider payload.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from usage_monitor_for_claude.hud import _provider_payload, parse_hotkey, pick_mood


class TestParseHotkey(unittest.TestCase):
    """Tests for parse_hotkey()."""

    def test_default_spec(self):
        """The default ctrl+alt+space parses to MOD_CONTROL|MOD_ALT + VK_SPACE."""
        self.assertEqual(parse_hotkey('ctrl+alt+space'), (0x2 | 0x1, 0x20))

    def test_letters_digits_and_function_keys(self):
        self.assertEqual(parse_hotkey('ctrl+shift+u'), (0x2 | 0x4, ord('U')))
        self.assertEqual(parse_hotkey('win+9'), (0x8, ord('9')))
        self.assertEqual(parse_hotkey('alt+f12'), (0x1, 0x7B))

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(parse_hotkey('Ctrl + Alt + Space'), (0x3, 0x20))

    def test_invalid_specs(self):
        """Garbage, missing main key, or two main keys are rejected."""
        self.assertIsNone(parse_hotkey(''))
        self.assertIsNone(parse_hotkey('ctrl+alt'))
        self.assertIsNone(parse_hotkey('ctrl+a+b'))
        self.assertIsNone(parse_hotkey('ctrl+banana'))
        self.assertIsNone(parse_hotkey('ctrl++a'))


class TestPickMood(unittest.TestCase):
    """Tests for pick_mood() threshold mapping."""

    def test_thresholds(self):
        self.assertEqual(pick_mood(0, [70, 90]), 'happy')
        self.assertEqual(pick_mood(69.9, [70, 90]), 'happy')
        self.assertEqual(pick_mood(70, [70, 90]), 'sweat')
        self.assertEqual(pick_mood(89.9, [70, 90]), 'sweat')
        self.assertEqual(pick_mood(90, [70, 90]), 'panic')
        self.assertEqual(pick_mood(100, [70, 90]), 'panic')


class TestProviderPayload(unittest.TestCase):
    """Tests for _provider_payload()."""

    def test_bars_from_quota_fields(self):
        usage = {
            'five_hour': {'utilization': 42.0, 'resets_at': None},
            'seven_day': {'utilization': 12.0, 'resets_at': None},
        }
        payload = _provider_payload(usage, 'hint')
        keys = {bar['key'] for bar in payload['usage']}
        self.assertEqual(keys, {'five_hour', 'seven_day'})
        self.assertIsNone(payload['error'])
        self.assertEqual(payload['peak'], 42)

    def test_auth_error_uses_login_hint(self):
        payload = _provider_payload({'error': 'x', 'auth_error': True}, 'run codex login')
        self.assertEqual(payload['error'], 'run codex login')
        self.assertEqual(payload['usage'], [])
        self.assertIsNone(payload['peak'])

    def test_plain_error_passthrough(self):
        payload = _provider_payload({'error': 'HTTP 500'}, 'hint')
        self.assertEqual(payload['error'], 'HTTP 500')

    def test_plan_type_title_cased(self):
        payload = _provider_payload({'plan_type': 'plus'}, 'hint')
        self.assertEqual(payload['plan'], 'Plus')


if __name__ == '__main__':
    unittest.main()
