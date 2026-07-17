"""
Claude Code Session Context
============================

Reads the context-window fill of currently active Claude Code sessions.

Claude Code appends every turn to ``<config>/projects/<project>/<uuid>.jsonl``;
each assistant entry carries a ``message.usage`` block whose input tokens
(fresh + cache-created + cache-read) plus the turn's output are the context
occupancy after that turn.  A session whose transcript was written to in the
last few minutes is considered active.

Only the file tail is read (bounded), so polling stays cheap even with large
transcripts.
"""
from __future__ import annotations

import json
import time
from pathlib import PurePath
from typing import Any

from . import api

__all__ = ['active_sessions']

# A transcript untouched for this long is no longer "current".  Sessions
# only write on turns, so a quiet-but-open window keeps its context alive
# far longer than the last write; the HUD shows staleness via age instead
# of dropping the session early.
ACTIVE_WINDOW_SECONDS = 3600
# Beyond this quiet time the session is rendered as idle (dimmed).
IDLE_AFTER_SECONDS = 600
# Context limit is not recorded in the transcript; assume the standard
# window unless the observed context already exceeds it (long-context model).
DEFAULT_CONTEXT_LIMIT = 200_000
LARGE_CONTEXT_LIMIT = 1_000_000
_TAIL_BYTES = 256 * 1024


def active_sessions(max_sessions: int = 3, now: float | None = None) -> list[dict[str, Any]]:
    """Return context info for recently active sessions, newest first."""
    projects = api.CLAUDE_CONFIG_DIR / 'projects'
    if not projects.is_dir():
        return []

    now = now if now is not None else time.time()
    candidates = []
    try:
        for path in projects.glob('*/*.jsonl'):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if now - mtime <= ACTIVE_WINDOW_SECONDS:
                candidates.append((mtime, path))
    except OSError:
        return []

    sessions = []
    for mtime, path in sorted(candidates, reverse=True):
        info = _read_session_tail(path)
        if info is not None:
            info['age_seconds'] = max(0, int(now - mtime))
            info['idle'] = info['age_seconds'] >= IDLE_AFTER_SECONDS
            sessions.append(info)
        if len(sessions) >= max_sessions:
            break

    return sessions


def _read_session_tail(path: Any) -> dict[str, Any] | None:
    """Extract the latest context usage from a transcript's tail, or None."""
    try:
        with open(path, 'rb') as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            tail = fh.read().decode('utf-8', errors='ignore')
    except OSError:
        return None

    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line or '"usage"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue

        usage = ((entry.get('message') or {}).get('usage')) if isinstance(entry, dict) else None
        if not isinstance(usage, dict) or usage.get('input_tokens') is None:
            continue

        tokens = sum(
            usage.get(key) or 0
            for key in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')
        )
        if tokens <= 0:
            continue

        limit = DEFAULT_CONTEXT_LIMIT if tokens <= DEFAULT_CONTEXT_LIMIT else LARGE_CONTEXT_LIMIT
        cwd = entry.get('cwd') or ''
        name = PurePath(cwd.replace('\\', '/')).name if cwd else path.parent.name
        return {
            'name': name,
            'tokens': tokens,
            'limit': limit,
            'pct': round(tokens / limit * 100),
        }

    return None
