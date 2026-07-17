"""
Setup & Settings Window
========================

First-run onboarding wizard and the tray "Setup & settings" window.

Both are the same pywebview page (``setup/setup.html``) in two modes:
onboarding walks through account checks (Claude Code / Codex CLI logins),
hotkey choice, and autostart; settings mode is a single form over the
same fields.  Neither implements its own OAuth - the CLIs own sign-in,
this window only detects their credentials and opens a terminal to fix
a missing login.

Saving writes ``usage-monitor-settings.json`` (via
:func:`settings.settings_write_path`) and offers an app restart, which
is how new settings take effect (settings are module constants read at
import).
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import webview  # type: ignore[import-untyped]  # no type stubs available

from .api import read_access_token
from .autostart import is_autostart_enabled, set_autostart
from .codex_api import read_codex_tokens
from .hud import parse_hotkey
from .settings import (
    CODEX_ENABLED, HUD_ENABLED, HUD_HOTKEY, HUD_LINGER, HUD_SESSIONS, HUD_THRESHOLDS,
    POLL_INTERVAL, settings_write_path,
)

_SETUP_DIR = Path(__file__).parent / 'setup'
_ONBOARD_MARKER = '.usage-monitor-onboarded'

# Keys the UI may write, with validators (None-return = drop invalid).
_SAVABLE_KEYS = frozenset({
    'hud_enabled', 'hud_hotkey', 'hud_linger', 'hud_thresholds', 'hud_sessions',
    'codex_enabled', 'poll_interval',
})

__all__ = ['SetupWindow', 'should_show_onboarding', 'mark_onboarded']

if TYPE_CHECKING:
    from .app import UsageMonitorForClaude


def _marker_path() -> Path:
    return settings_write_path().parent / _ONBOARD_MARKER


def should_show_onboarding() -> bool:
    """True until the wizard has been completed (or dismissed) once."""
    try:
        return not _marker_path().exists()
    except OSError:
        return False


def mark_onboarded() -> None:
    try:
        _marker_path().write_text('', encoding='utf-8')
    except OSError:
        pass


class _SetupApi:
    """Methods exposed to the setup page via pywebview's JS bridge."""

    def __init__(self, window: SetupWindow) -> None:
        self._win = window

    def get_state(self) -> dict[str, Any]:
        return {
            'mode': self._win.mode,
            'accounts': self.recheck(),
            'frozen': getattr(sys, 'frozen', False),
            'autostart': is_autostart_enabled(),
            'settings': {
                'hud_enabled': HUD_ENABLED,
                'hud_hotkey': HUD_HOTKEY,
                'hud_linger': HUD_LINGER,
                'hud_thresholds': list(HUD_THRESHOLDS[:2]),
                'hud_sessions': HUD_SESSIONS,
                'codex_enabled': CODEX_ENABLED,
                'poll_interval': POLL_INTERVAL,
            },
            'settings_path': str(settings_write_path()),
        }

    def recheck(self) -> dict[str, bool]:
        """Live login status for both providers."""
        return {
            'claude': bool(read_access_token()),
            'codex': read_codex_tokens() is not None,
        }

    def check_hotkey(self, spec: str) -> bool:
        return parse_hotkey(str(spec)) is not None

    def open_login(self, which: str) -> None:
        """Open a terminal running the provider's sign-in command."""
        command = 'claude' if which == 'claude' else 'codex login'
        try:
            subprocess.Popen(
                ['cmd', '/k', command],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        except OSError:
            pass

    def set_autostart(self, enabled: bool) -> bool:
        try:
            set_autostart(bool(enabled))
        except Exception:
            pass
        return is_autostart_enabled()

    def save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        """Merge the whitelisted values into the settings file."""
        cleaned = _clean_values(values if isinstance(values, dict) else {})
        path = settings_write_path()

        try:
            existing = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, ValueError):
            existing = {}

        existing.update(cleaned)
        try:
            path.write_text(json.dumps(existing, indent=4) + '\n', encoding='utf-8')
        except OSError as exc:
            return {'ok': False, 'error': str(exc), 'path': str(path)}

        mark_onboarded()
        return {'ok': True, 'path': str(path)}

    def finish(self) -> None:
        """Close without saving (still counts as having seen onboarding)."""
        mark_onboarded()
        self._win.close()

    def restart_app(self) -> None:
        mark_onboarded()
        self._win.close()
        self._win.app.on_restart()

    def close(self) -> None:
        self._win.close()


def _clean_values(values: dict[str, Any]) -> dict[str, Any]:
    """Keep only known keys with sane types/ranges (mirrors settings validation)."""
    cleaned: dict[str, Any] = {}
    for key, value in values.items():
        if key not in _SAVABLE_KEYS:
            continue
        if key in ('hud_enabled', 'hud_sessions', 'codex_enabled'):
            if isinstance(value, bool):
                cleaned[key] = value
        elif key == 'hud_hotkey':
            if isinstance(value, str) and parse_hotkey(value) is not None:
                cleaned[key] = value.strip().lower()
        elif key in ('hud_linger', 'poll_interval'):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                minimum = 0 if key == 'hud_linger' else 30
                cleaned[key] = max(minimum, int(value))
        elif key == 'hud_thresholds':
            if (
                isinstance(value, list) and len(value) == 2
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= 100 for v in value)
            ):
                cleaned[key] = sorted(round(v) for v in value)
    return cleaned


class SetupWindow:
    """Modal-ish setup window; blocks the calling thread until closed."""

    WIDTH = 460
    HEIGHT = 560

    def __init__(self, app: UsageMonitorForClaude, mode: str = 'settings') -> None:
        self.app = app
        self.mode = mode
        self._closed = threading.Event()

        self._window = webview.create_window(
            'Usage Monitor — Setup',
            url=str(_SETUP_DIR / 'setup.html'),
            width=self.WIDTH, height=self.HEIGHT,
            resizable=False,
            background_color='#1A1915',
            js_api=_SetupApi(self),
        )
        self._window.events.closed += self._closed.set
        self._closed.wait()

    def close(self) -> None:
        try:
            self._window.destroy()
        except Exception:
            pass
        self._closed.set()
