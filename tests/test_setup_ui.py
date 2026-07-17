"""
Setup UI Tests
===============

Unit tests for the onboarding marker, save whitelist/validation, and
the settings write path helper.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from usage_monitor_for_claude.setup_ui import _clean_values, mark_onboarded, should_show_onboarding


class TestOnboardMarker(unittest.TestCase):
    """Tests for the first-run marker."""

    def test_marker_lifecycle(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage-monitor-settings.json'
            with patch('usage_monitor_for_claude.setup_ui.settings_write_path', return_value=path):
                self.assertTrue(should_show_onboarding())
                mark_onboarded()
                self.assertFalse(should_show_onboarding())


class TestCleanValues(unittest.TestCase):
    """Tests for the save whitelist/validation."""

    def test_valid_values_pass(self):
        cleaned = _clean_values({
            'hud_hotkey': ' Ctrl+Alt+U ',
            'hud_linger': 7.9,
            'hud_sessions': False,
            'codex_enabled': True,
            'hud_thresholds': [90.4, 70],
            'poll_interval': 200,
        })
        self.assertEqual(cleaned['hud_hotkey'], 'ctrl+alt+u')
        self.assertEqual(cleaned['hud_linger'], 7)
        self.assertFalse(cleaned['hud_sessions'])
        self.assertTrue(cleaned['codex_enabled'])
        self.assertEqual(cleaned['hud_thresholds'], [70, 90])
        self.assertEqual(cleaned['poll_interval'], 200)

    def test_invalid_and_unknown_dropped(self):
        cleaned = _clean_values({
            'hud_hotkey': 'not a hotkey',
            'hud_linger': 'soon',
            'hud_thresholds': [5],
            'poll_interval': True,
            'bar_fg': '#ff0000',
            'on_startup_command': 'evil.exe',
        })
        self.assertEqual(cleaned, {})

    def test_minimums_clamped(self):
        cleaned = _clean_values({'hud_linger': -3, 'poll_interval': 1})
        self.assertEqual(cleaned['hud_linger'], 0)
        self.assertEqual(cleaned['poll_interval'], 30)


class TestSettingsWritePath(unittest.TestCase):
    """settings_write_path falls back to the app dir when no file exists."""

    def test_prefers_existing_file(self):
        # Uses the custom-config-dir slot (first in the search order) so the
        # test stays hermetic regardless of real settings files on disk.
        from usage_monitor_for_claude.settings import SETTINGS_FILENAME, settings_write_path
        with TemporaryDirectory() as tmp:
            custom_file = Path(tmp) / SETTINGS_FILENAME
            custom_file.write_text('{}', encoding='utf-8')
            with patch('usage_monitor_for_claude.settings.is_default_config_dir', return_value=False):
                with patch('usage_monitor_for_claude.settings.effective_config_dir', return_value=Path(tmp)):
                    self.assertEqual(settings_write_path(), custom_file)


if __name__ == '__main__':
    unittest.main()
