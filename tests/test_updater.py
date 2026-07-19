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
    FORK_VERSION, _expected_hash, _free_old_slot, _is_newer, _parse_tag, check_for_update,
    download_and_apply,
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


class TestFreeOldSlot(unittest.TestCase):
    """A locked .old must never abort an update (2026-07-19 field failure)."""

    def setUp(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.exe = Path(self._tmp.name) / 'App.exe'
        self.exe.write_bytes(b'x')

    def test_prefers_plain_old(self):
        slot = _free_old_slot(self.exe)
        self.assertEqual(slot.name, 'App.exe.old')

    def test_deletes_stale_unlocked_old(self):
        stale = self.exe.with_name('App.exe.old')
        stale.write_bytes(b'stale')
        slot = _free_old_slot(self.exe)
        self.assertEqual(slot.name, 'App.exe.old')
        self.assertFalse(stale.exists())

    def test_falls_through_locked_slots(self):
        real_unlink = type(self.exe).unlink

        def unlink(path, missing_ok=False):
            if path.name.endswith('.old') or path.name.endswith('.old2'):
                raise PermissionError(5, 'Access is denied')
            return real_unlink(path, missing_ok=missing_ok)

        with patch('pathlib.Path.unlink', unlink):
            slot = _free_old_slot(self.exe)
        self.assertEqual(slot.name, 'App.exe.old3')

    def test_gives_up_when_all_slots_locked(self):
        def unlink(path, missing_ok=False):
            raise PermissionError(5, 'Access is denied')

        with patch('pathlib.Path.unlink', unlink):
            self.assertIsNone(_free_old_slot(self.exe))


class TestCleanupSweep(unittest.TestCase):
    def test_sweeps_every_old_variant(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        import sys as real_sys
        from usage_monitor_for_claude import updater
        with TemporaryDirectory() as tmp:
            exe = Path(tmp) / 'App.exe'
            exe.write_bytes(b'x')
            for suffix in ('.old', '.old2', '.old5'):
                exe.with_name(exe.name + suffix).write_bytes(b'stale')
            with patch.object(real_sys, 'frozen', True, create=True), \
                 patch.object(real_sys, 'executable', str(exe)):
                updater.cleanup_old_exe()
            leftovers = list(Path(tmp).glob('App.exe.old*'))
            self.assertEqual(leftovers, [])
            self.assertTrue(exe.exists())


if __name__ == '__main__':
    unittest.main()
