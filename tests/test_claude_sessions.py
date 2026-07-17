"""
Claude Session Context Tests
=============================

Unit tests for the active-session context scanner.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from usage_monitor_for_claude.claude_sessions import (
    DEFAULT_CONTEXT_LIMIT, LARGE_CONTEXT_LIMIT, _read_session_tail, active_sessions,
)


def _entry(input_tokens=100, cache_creation=1000, cache_read=50000, output=500, cwd='C:\\Users\\x\\git\\myproject'):
    return json.dumps({
        'type': 'assistant',
        'cwd': cwd,
        'message': {
            'model': 'claude-sonnet-5',
            'usage': {
                'input_tokens': input_tokens,
                'cache_creation_input_tokens': cache_creation,
                'cache_read_input_tokens': cache_read,
                'output_tokens': output,
            },
        },
    })


def _write_session(projects: Path, project: str, name: str, lines: list[str], age_seconds: float = 0) -> Path:
    session_dir = projects / project
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f'{name}.jsonl'
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    return path


class TestReadSessionTail(unittest.TestCase):
    """Tests for _read_session_tail()."""

    def test_latest_usage_wins(self):
        """The most recent usage-bearing line determines the context."""
        with TemporaryDirectory() as tmp:
            path = _write_session(Path(tmp), 'p', 's', [
                _entry(cache_read=10_000),
                json.dumps({'type': 'user', 'text': 'hi'}),
                _entry(cache_read=99_000),
            ])
            info = _read_session_tail(path)
        self.assertEqual(info['tokens'], 100 + 1000 + 99_000 + 500)
        self.assertEqual(info['name'], 'myproject')
        self.assertEqual(info['limit'], DEFAULT_CONTEXT_LIMIT)

    def test_large_context_assumes_1m_window(self):
        """Context beyond 200k implies a long-context model."""
        with TemporaryDirectory() as tmp:
            path = _write_session(Path(tmp), 'p', 's', [_entry(cache_read=430_000)])
            info = _read_session_tail(path)
        self.assertEqual(info['limit'], LARGE_CONTEXT_LIMIT)
        self.assertEqual(info['pct'], round(info['tokens'] / LARGE_CONTEXT_LIMIT * 100))

    def test_no_usage_lines(self):
        """A transcript without usage entries yields None."""
        with TemporaryDirectory() as tmp:
            path = _write_session(Path(tmp), 'p', 's', [json.dumps({'type': 'user'}), 'not json'])
            self.assertIsNone(_read_session_tail(path))


class TestActiveSessions(unittest.TestCase):
    """Tests for active_sessions() discovery and filtering."""

    def test_filters_stale_and_sorts_newest_first(self):
        with TemporaryDirectory() as tmp:
            projects = Path(tmp) / 'projects'
            _write_session(projects, 'proj-a', 'old', [_entry(cwd='C:\\x\\old')], age_seconds=3600)
            _write_session(projects, 'proj-a', 'older-active', [_entry(cwd='C:\\x\\beta')], age_seconds=120)
            _write_session(projects, 'proj-b', 'newest', [_entry(cwd='C:\\x\\alpha')], age_seconds=10)

            with patch('usage_monitor_for_claude.api.CLAUDE_CONFIG_DIR', Path(tmp)):
                sessions = active_sessions()

        self.assertEqual([s['name'] for s in sessions], ['alpha', 'beta'])

    def test_missing_projects_dir(self):
        with TemporaryDirectory() as tmp:
            with patch('usage_monitor_for_claude.api.CLAUDE_CONFIG_DIR', Path(tmp)):
                self.assertEqual(active_sessions(), [])

    def test_max_sessions_cap(self):
        with TemporaryDirectory() as tmp:
            projects = Path(tmp) / 'projects'
            for i in range(5):
                _write_session(projects, 'p', f's{i}', [_entry(cwd=f'C:\\x\\proj{i}')], age_seconds=i)
            with patch('usage_monitor_for_claude.api.CLAUDE_CONFIG_DIR', Path(tmp)):
                self.assertEqual(len(active_sessions(max_sessions=3)), 3)


if __name__ == '__main__':
    unittest.main()
