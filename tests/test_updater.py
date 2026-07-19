"""
Updater Tests
==============

Unit tests for release discovery, version comparison, and checksum
verification. The actual EXE swap is exercised only for its guard paths
(frozen-only) - the rename dance needs a real packaged process.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from usage_monitor_for_claude.updater import (
    FORK_VERSION, _expected_hash, _is_newer, _parse_tag, check_for_update, download_and_apply,
)


class TestVersioning(unittest.TestCase):
    def test_parse_tag(self):
        self.assertEqual(_parse_tag('fork-v1.2.0'), (1, 2, 0))
        self.assertEqual(_parse_tag('fork-v2.0'), (2, 0))
        self.assertIsNone(_parse_tag('v1.2.0'))
        self.assertIsNone(_parse_tag('fork-v1.2.0-rc1'))
        self.assertIsNone(_parse_tag(''))

    def test_is_newer(self):
        self.assertTrue(_is_newer('fork-v1.3.0', '1.2.0'))
        self.assertTrue(_is_newer('fork-v2.0', '1.9.9'))
        self.assertFalse(_is_newer('fork-v1.2.0', '1.2.0'))
        self.assertFalse(_is_newer('fork-v1.1.9', '1.2.0'))
        self.assertFalse(_is_newer('garbage', '1.2.0'))

    def test_fork_version_is_release_shaped(self):
        self.assertIsNotNone(_parse_tag(f'fork-v{FORK_VERSION}'))


class TestCheckForUpdate(unittest.TestCase):
    def _release(self, tag, assets=True):
        body = {'tag_name': tag, 'assets': []}
        if assets:
            body['assets'] = [
                {'name': 'UsageMonitorForClaude.exe', 'browser_download_url': 'https://x/exe'},
                {'name': 'SHA256SUMS.txt', 'browser_download_url': 'https://x/sums'},
            ]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = body
        return resp

    def test_newer_release_found(self):
        with patch('usage_monitor_for_claude.updater.requests.get', return_value=self._release('fork-v99.0.0')):
            release = check_for_update()
        self.assertEqual(release['version'], '99.0.0')
        self.assertEqual(release['exe_url'], 'https://x/exe')

    def test_current_release_ignored(self):
        with patch('usage_monitor_for_claude.updater.requests.get', return_value=self._release(f'fork-v{FORK_VERSION}')):
            self.assertIsNone(check_for_update())

    def test_missing_assets_ignored(self):
        with patch('usage_monitor_for_claude.updater.requests.get', return_value=self._release('fork-v99.0.0', assets=False)):
            self.assertIsNone(check_for_update())

    def test_network_failure_is_quiet(self):
        with patch('usage_monitor_for_claude.updater.requests.get', side_effect=OSError('down')):
            self.assertIsNone(check_for_update())


class TestChecksums(unittest.TestCase):
    def test_expected_hash_parses_sums(self):
        sums = 'abc123  UsageMonitorForClaude.exe\n'
        self.assertEqual(_expected_hash(sums), 'abc123')
        self.assertEqual(_expected_hash('def456 *UsageMonitorForClaude.exe'), 'def456')
        self.assertIsNone(_expected_hash('abc123  other.exe'))
        self.assertIsNone(_expected_hash(''))


class TestDownloadAndApply(unittest.TestCase):
    def test_refuses_outside_frozen_builds(self):
        error = download_and_apply({'exe_url': 'https://x/exe', 'sums_url': 'https://x/sums'})
        self.assertIn('packaged EXE only', error)


if __name__ == '__main__':
    unittest.main()
