"""
Hold-to-peek HUD
=================

A frameless, always-on-top usage widget summoned by a global hotkey.

Hold the hotkey → the HUD appears (without stealing focus); release →
it hides.  A quick tap (or a click inside the HUD while peeking) pins
it until the next tap, Escape, or its close button.  Rendering is HTML
(``hud/``) in a pywebview window created once and shown/hidden, so a
summon is instant after the first use.

Win32 specifics: the hotkey lives on its own daemon thread's message
queue (``RegisterHotKey`` with a NULL hwnd); visibility is driven with
``ShowWindow(SW_SHOWNA)``/``SW_HIDE`` directly so peeking never
activates the window; Windows 11 DWM rounds the corners.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import webview  # type: ignore[import-untyped]  # no type stubs available

from .i18n import T
from .settings import HUD_HOTKEY, HUD_THRESHOLDS

_logger = logging.getLogger(__name__)

_HUD_DIR = Path(__file__).parent / 'hud'
_BASELINE_DPI = 96

_GWL_EXSTYLE = -20
_WS_EX_APPWINDOW = 0x00040000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_SW_HIDE = 0
_SW_SHOWNA = 8
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_MOD_NOREPEAT = 0x4000
_HOTKEY_ID = 0xB00
# Held shorter than this counts as a tap and pins the HUD open, so a
# quick press never produces a useless flash.
_TAP_SECONDS = 0.35

_MODIFIERS = {'alt': 0x1, 'ctrl': 0x2, 'control': 0x2, 'shift': 0x4, 'win': 0x8}
_NAMED_VKS = {
    'space': 0x20, 'tab': 0x09, 'enter': 0x0D, 'backspace': 0x08,
    'home': 0x24, 'end': 0x23, 'pageup': 0x21, 'pagedown': 0x22,
    'insert': 0x2D, 'delete': 0x2E, 'pause': 0x13,
    'up': 0x26, 'down': 0x28, 'left': 0x25, 'right': 0x27,
    '`': 0xC0, 'backquote': 0xC0, '-': 0xBD, '=': 0xBB,
    '[': 0xDB, ']': 0xDD, ';': 0xBA, "'": 0xDE, ',': 0xBC, '.': 0xBE, '/': 0xBF, '\\': 0xDC,
}


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ('cbSize', ctypes.wintypes.DWORD),
        ('rcMonitor', ctypes.wintypes.RECT),
        ('rcWork', ctypes.wintypes.RECT),
        ('dwFlags', ctypes.wintypes.DWORD),
    ]


__all__ = ['UsageHud', 'parse_hotkey', 'pick_mood']

if TYPE_CHECKING:
    from .app import UsageMonitorForClaude


def parse_hotkey(spec: str) -> tuple[int, int] | None:
    """Parse a hotkey spec like ``'ctrl+alt+space'`` into (modifiers, vk).

    Accepts any number of modifiers (ctrl/alt/shift/win) plus exactly one
    main key: a letter, a digit, ``f1``–``f24``, or a named key.  Returns
    None when the spec is invalid.
    """
    mods = 0
    vk = None
    for raw in spec.lower().split('+'):
        part = raw.strip()
        if not part:
            return None
        if part in _MODIFIERS:
            mods |= _MODIFIERS[part]
            continue
        if vk is not None:
            return None
        if len(part) == 1 and (part.isalpha() or part.isdigit()):
            vk = ord(part.upper())
        elif part in _NAMED_VKS:
            vk = _NAMED_VKS[part]
        elif part.startswith('f') and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x70 + int(part[1:]) - 1
        else:
            return None

    return (mods, vk) if vk is not None else None


def pick_mood(worst_pct: float, thresholds: list[float] | None = None) -> str:
    """Map the worst utilization onto a mascot mood."""
    lo, hi = (thresholds or HUD_THRESHOLDS)[:2]
    if worst_pct >= hi:
        return 'panic'
    if worst_pct >= lo:
        return 'sweat'
    return 'happy'


def _provider_payload(usage: dict[str, Any] | None, login_hint: str) -> dict[str, Any]:
    """Build one provider block (bars + error text) for the HUD JS."""
    from .popup import _usage_bar_list

    usage = usage or {}
    bars = _usage_bar_list(usage)
    error = None
    if usage.get('auth_error'):
        error = login_hint
    elif usage.get('error'):
        error = str(usage['error'])[:120]
    elif not bars:
        error = T['loading'] if not usage else None

    return {'usage': bars, 'error': error, 'plan': str(usage.get('plan_type', '')).title()}


class UsageHud:
    """Hold-to-peek HUD window driven by a global hotkey."""

    WIDTH = 380
    HEIGHT = 236

    def __init__(self, app: UsageMonitorForClaude) -> None:
        self.app = app
        self._window: Any = None
        self._hwnd = 0
        self._height = self.HEIGHT  # logical px, grows with content
        self._visible = False
        self._pinned = False
        self._lock = threading.Lock()
        self._loaded = threading.Event()
        self._pump_tid = 0
        self._refresh_stop = threading.Event()

    # Lifecycle

    def start(self) -> None:
        """Create the (hidden) window and start the hotkey thread."""
        self._create_window()
        threading.Thread(target=self._hotkey_pump, daemon=True).start()

    def stop(self) -> None:
        """Stop the hotkey pump; the window dies with webview shutdown."""
        self._refresh_stop.set()
        if self._pump_tid:
            ctypes.windll.user32.PostThreadMessageW(self._pump_tid, _WM_QUIT, 0, 0)

    # Data

    def payload(self) -> dict[str, Any]:
        """Build the full HUD data payload (both providers + mood)."""
        snap = self.app.cache.snapshot
        claude = _provider_payload(snap.usage, f"{T['warn_no_token']} {T['warn_login']}")
        codex = _provider_payload(self.app._codex_response, T['codex_login_hint']) if self.app.codex is not None else None

        worst = 0.0
        for provider in (claude, codex):
            for bar in (provider or {}).get('usage', []):
                worst = max(worst, bar['fill_pct'] * 100)

        return {
            'claude': claude,
            'codex': codex,
            'mood': pick_mood(worst),
            'worst_pct': round(worst),
            'thresholds': HUD_THRESHOLDS,
            'pinned': self._pinned,
        }

    # Window

    def _create_window(self) -> None:
        if self._window is not None:
            return

        self._window = webview.create_window(
            '', url=str(_HUD_DIR / 'hud.html'),
            width=self.WIDTH, height=self.HEIGHT,
            resizable=False, frameless=True, shadow=False,
            easy_drag=False, on_top=True, hidden=True,
            background_color='#1A1915',
            js_api=_HudApi(self),
        )
        self._window.events.loaded += self._on_loaded
        self._window.events.closed += self._on_window_closed

    def _on_window_closed(self) -> None:
        """Forget a destroyed window so the next summon recreates it.

        The window can die outside our control (Alt+F4 on a focused HUD,
        a WebView2 crash); treating it as permanent would leave the
        hotkey summoning a dead handle forever.
        """
        self._window = None
        self._hwnd = 0
        self._visible = False
        self._pinned = False
        self._loaded.clear()
        self._refresh_stop.set()

    def _on_loaded(self) -> None:
        try:
            self._hwnd = self._window.native.Handle.ToInt32()

            # No taskbar button, never activated by the shell.
            ex_style = ctypes.windll.user32.GetWindowLongW(self._hwnd, _GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                self._hwnd, _GWL_EXSTYLE,
                (ex_style | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE) & ~_WS_EX_APPWINDOW,
            )

            # Windows 11 rounded corners; ignored (E_INVALIDARG) on older builds.
            corner = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(self._hwnd, 33, ctypes.byref(corner), 4)

            self._window.evaluate_js(f'init({json.dumps(self._init_config())})')
            self._loaded.set()
        except Exception as exc:
            _logger.warning('hud: window setup failed: %s', exc)

    def _init_config(self) -> dict[str, Any]:
        # Brand names are not translated.
        return {
            't': {'claude': 'Claude', 'codex': T['codex']},
            'data': self.payload(),
        }

    def _position(self) -> None:
        """Place the HUD bottom-right above the tray on the taskbar monitor."""
        tray = ctypes.windll.user32.FindWindowW('Shell_TrayWnd', None)
        hmon = ctypes.windll.user32.MonitorFromWindow(tray, 2)  # MONITOR_DEFAULTTONEAREST
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        work = info.rcWork

        dpi = ctypes.windll.user32.GetDpiForWindow(self._hwnd) or ctypes.windll.user32.GetDpiForSystem()
        scale = dpi / _BASELINE_DPI
        width = int(self.WIDTH * scale)
        height = int(self._height * scale)
        margin = int(16 * scale)

        x = work.right - width - margin
        y = work.bottom - height - margin
        # insertAfter 0 + SWP_NOZORDER: the window is already TopMost via
        # pywebview, and passing HWND_TOPMOST (-1) through ctypes' default
        # c_int conversion corrupts the 64-bit HWND parameter.
        ok = ctypes.windll.user32.SetWindowPos(self._hwnd, 0, x, y, width, height, 0x0010 | 0x0004)  # SWP_NOACTIVATE | SWP_NOZORDER
        _logger.info('hud: position dpi=%s -> (%s,%s %sx%s) ok=%s', dpi, x, y, width, height, ok)

    def show(self, pinned: bool = False) -> None:
        """Show the HUD without activating it and start live refresh."""
        with self._lock:
            if self._window is None:
                self._create_window()
            if self._window is None or not self._loaded.wait(timeout=5):
                return
            self._pinned = pinned
            self._push_data()
            self._position()
            ctypes.windll.user32.ShowWindow(self._hwnd, _SW_SHOWNA)
            # Height reports can land between the pre-show positioning and
            # ShowWindow; re-assert so the first frame is already right.
            self._position()
            try:
                self._window.evaluate_js('hudShown && hudShown()')
            except Exception:
                pass
            if not self._visible:
                self._visible = True
                self._refresh_stop.clear()
                threading.Thread(target=self._refresh_loop, daemon=True).start()

    def hide(self) -> None:
        with self._lock:
            if not self._visible:
                return
            self._visible = False
            self._pinned = False
            self._refresh_stop.set()
            ctypes.windll.user32.ShowWindow(self._hwnd, _SW_HIDE)

    def _pin(self) -> None:
        self._pinned = True
        self._push_data()

    def _set_height(self, height: int) -> None:
        """Grow/shrink the window to the content height reported by JS.

        Gates on actual Win32 visibility, not ``_visible``: reports arrive
        on bridge threads and can land mid-``show()`` before the flag flips.
        """
        height = max(int(height), 160)
        if height == self._height:
            return
        self._height = height
        if self._hwnd and ctypes.windll.user32.IsWindowVisible(self._hwnd):
            self._position()

    def _push_data(self) -> None:
        try:
            self._window.evaluate_js(f'updateData({json.dumps(self.payload())})')
        except Exception:
            pass

    def _refresh_loop(self) -> None:
        """Push fresh data every 2s while the HUD is visible."""
        while not self._refresh_stop.wait(2):
            if not self._visible:
                break
            self._push_data()

    # Hotkey

    def _hotkey_pump(self) -> None:
        parsed = parse_hotkey(HUD_HOTKEY)
        if parsed is None:
            _logger.warning('hud: invalid hud_hotkey %r; HUD disabled', HUD_HOTKEY)
            return
        mods, vk = parsed

        # Force queue creation before publishing the thread id (see popup.py).
        msg = ctypes.wintypes.MSG()
        ctypes.windll.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self._pump_tid = ctypes.windll.kernel32.GetCurrentThreadId()

        if not ctypes.windll.user32.RegisterHotKey(None, _HOTKEY_ID, mods | _MOD_NOREPEAT, vk):
            _logger.warning('hud: RegisterHotKey failed for %r (in use by another app?)', HUD_HOTKEY)
            return

        try:
            while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == _WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                    self._on_hotkey(vk)
        finally:
            ctypes.windll.user32.UnregisterHotKey(None, _HOTKEY_ID)
            self._pump_tid = 0

    def _on_hotkey(self, vk: int) -> None:
        """Handle one hotkey press: peek while held, pin on tap, toggle off."""
        # Re-sync with reality: the window may have died or been hidden
        # behind our back, and a stale True would turn a summon into a no-op.
        if self._visible and not (self._hwnd and ctypes.windll.user32.IsWindowVisible(self._hwnd)):
            self._visible = False
            self._pinned = False

        if self._visible:
            self.hide()
            return

        self.show()
        pressed_at = time.time()
        while ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000:
            time.sleep(0.03)
            if not self._visible:  # closed from the HUD while still holding
                return

        if self._pinned:
            return
        if time.time() - pressed_at < _TAP_SECONDS:
            self._pin()  # a quick tap pins instead of flashing
        else:
            self.hide()


class _HudApi:
    """Methods exposed to the HUD JavaScript via pywebview's JS bridge."""

    def __init__(self, hud: UsageHud) -> None:
        self._hud = hud

    def pin(self) -> bool:
        self._hud._pin()
        return True

    def report_height(self, height: int) -> None:
        if height:
            self._hud._set_height(height)

    def close(self) -> None:
        self._hud.hide()

    def open_popup(self) -> None:
        self._hud.hide()
        self._hud.app.on_show_popup()
