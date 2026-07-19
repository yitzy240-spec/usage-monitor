"""
HUD Tests
==========

Unit tests for hotkey parsing, mood mapping, and the provider payload.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from usage_monitor_for_claude.hud import _provider_payload, clamp_position, parse_hotkey, pick_mood


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

    def test_pace_ahead_sweats_at_low_usage(self):
        """Usage running ahead of the clock is a sweat even at low percent."""
        self.assertEqual(pick_mood(10, [70, 90], pace_ahead=True), 'sweat')
        self.assertEqual(pick_mood(95, [70, 90], pace_ahead=True), 'panic')


class TestClampPosition(unittest.TestCase):
    """Tests for clamp_position() keeping dragged spots on-screen."""

    WORK = (0, 0, 2880, 1704)

    def test_inside_unchanged(self):
        self.assertEqual(clamp_position((100, 200), (760, 470), self.WORK), (100, 200))

    def test_offscreen_clamped(self):
        self.assertEqual(clamp_position((-500, -50), (760, 470), self.WORK), (0, 0))
        self.assertEqual(clamp_position((99999, 99999), (760, 470), self.WORK), (2880 - 760, 1704 - 470))

    def test_negative_work_origin(self):
        # Secondary monitor left of primary: work area can start negative.
        self.assertEqual(clamp_position((5, 5), (100, 100), (-1920, 0, 0, 1080)), (-100, 5))


class TestVisitorDataUris(unittest.TestCase):
    """Tests for the user visitors folder loader."""

    def test_loads_small_pngs_as_data_uris(self):
        import usage_monitor_for_claude.hud as hud_mod
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from unittest.mock import patch
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'UsageMonitorForClaude' / 'visitors'
            folder.mkdir(parents=True)
            (folder / 'pet.png').write_bytes(b'\x89PNG\r\n\x1a\nfakedata')
            (folder / 'huge.png').write_bytes(b'x' * 400_000)  # over the cap
            (folder / 'notes.txt').write_text('ignored')
            hud_mod._visitor_cache = None
            with patch.dict('os.environ', {'APPDATA': tmp}):
                uris = hud_mod._visitor_data_uris()
            hud_mod._visitor_cache = None
        self.assertEqual(len(uris), 1)
        self.assertTrue(uris[0].startswith('data:image/png;base64,'))

    def test_missing_folder_is_empty(self):
        import usage_monitor_for_claude.hud as hud_mod
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        with TemporaryDirectory() as tmp:
            hud_mod._visitor_cache = None
            with patch.dict('os.environ', {'APPDATA': tmp}):
                self.assertEqual(hud_mod._visitor_data_uris(), [])
            hud_mod._visitor_cache = None


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
        self.assertEqual(payload['mood'], 'happy')

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
