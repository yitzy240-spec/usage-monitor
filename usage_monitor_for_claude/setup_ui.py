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

import ctypes
import ctypes.wintypes
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import webview  # type: ignore[import-untyped]  # no type stubs available

import webbrowser

from . import claude_oauth
from .api import read_access_token
from .autostart import is_autostart_enabled, set_autostart
from .codex_api import read_codex_tokens
from .hud import parse_hotkey
from .settings import (
    CODEX_ENABLED, HUD_ENABLED, HUD_HOTKEY, HUD_LINGER, HUD_SESSIONS, HUD_THRESHOLDS,
    HUD_VISITORS, POLL_INTERVAL, settings_write_path,
)

_SETUP_DIR = Path(__file__).parent / 'setup'
_ONBOARD_MARKER = '.usage-monitor-onboarded'

# Keys the UI may write, with validators (None-return = drop invalid).
_SAVABLE_KEYS = frozenset({
    'hud_enabled', 'hud_hotkey', 'hud_linger', 'hud_thresholds', 'hud_sessions',
    'hud_visitors', 'codex_enabled', 'poll_interval',
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
                'hud_visitors': HUD_VISITORS,
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
            'claude_app_login': claude_oauth.has_app_login(),
        }

    # Claude app login (OAuth) - for chat/Desktop users without the CLI

    def claude_login_start(self) -> bool:
        """Open the browser on Anthropic's login page; remember the verifier."""
        url, verifier = claude_oauth.generate_login()
        self._claude_verifier = verifier
        try:
            webbrowser.open(url)
        except Exception:
            return False
        return True

    def claude_login_finish(self, pasted: str) -> dict[str, Any]:
        verifier = getattr(self, '_claude_verifier', None)
        if not verifier:
            return {'ok': False, 'error': 'start the sign-in first'}
        error = claude_oauth.exchange_code(str(pasted), verifier)
        return {'ok': error is None, 'error': error}

    def claude_sign_out(self) -> dict[str, bool]:
        claude_oauth.sign_out()
        return self.recheck()

    # Sprite Builder - your Claude subscription draws a visitor

    def build_sprite(self, prompt: str) -> dict[str, Any]:
        from .sprite_builder import generate_sprite
        return generate_sprite(str(prompt))

    def save_sprite(self, grid: dict[str, Any]) -> dict[str, Any]:
        from .sprite_builder import save_sprite
        return save_sprite(grid)

    # Petdex pets - adopt gallery companions as HUD visitors

    def install_pet(self, slug: str) -> dict[str, Any]:
        from .pets import PetError, install_pet
        try:
            meta = install_pet(str(slug))
        except PetError as exc:
            return {'error': str(exc)}
        except Exception:
            return {'error': 'Installing the pet failed unexpectedly.'}
        return {'pet': meta, 'pets': self.list_pets()}

    def remove_pet(self, slug: str) -> list[dict[str, Any]]:
        from .pets import remove_pet
        remove_pet(str(slug))
        return self.list_pets()

    def list_pets(self) -> list[dict[str, Any]]:
        from .pets import pets_payload
        try:
            return pets_payload()
        except Exception:
            return []

    def open_petdex(self) -> None:
        webbrowser.open('https://petdex.dev')

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
        if key in ('hud_enabled', 'hud_sessions', 'hud_visitors', 'codex_enabled'):
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
        self._window.events.loaded += self._position_near_tray
        self._window.events.closed += self._closed.set
        self._closed.wait()

    def _position_near_tray(self) -> None:
        """Dock bottom-right above the tray, where the detail panel lives."""
        try:
            hwnd = self._window.native.Handle.ToInt32()
            tray = ctypes.windll.user32.FindWindowW('Shell_TrayWnd', None)
            hmon = ctypes.windll.user32.MonitorFromWindow(tray, 2)  # MONITOR_DEFAULTTONEAREST

            class _MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ('cbSize', ctypes.wintypes.DWORD),
                    ('rcMonitor', ctypes.wintypes.RECT),
                    ('rcWork', ctypes.wintypes.RECT),
                    ('dwFlags', ctypes.wintypes.DWORD),
                ]

            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info))
            work = info.rcWork

            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            width = rect.right - rect.left
            height = rect.bottom - rect.top

            dpi = ctypes.windll.user32.GetDpiForWindow(hwnd) or ctypes.windll.user32.GetDpiForSystem()
            margin = int(12 * dpi / 96)
            x = work.right - width - margin
            y = work.bottom - height - margin
            ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010)  # NOSIZE|NOZORDER|NOACTIVATE
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._window.destroy()
        except Exception:
            pass
        self._closed.set()
