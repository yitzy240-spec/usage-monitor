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

from .claude_sessions import active_sessions
from .i18n import T
from .settings import (
    HUD_HOTKEY, HUD_LINGER, HUD_POSITION, HUD_SESSIONS, HUD_SIZE, HUD_THRESHOLDS, HUD_VISITORS,
    settings_write_path,
)

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
_WM_POWERBROADCAST = 0x0218
_PBT_APMRESUMESUSPEND = 0x0007
_PBT_APMRESUMEAUTOMATIC = 0x0012

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


__all__ = ['UsageHud', 'clamp_position', 'parse_hotkey', 'pick_mood']

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


def pick_mood(worst_pct: float, thresholds: list[float] | None = None, pace_ahead: bool = False) -> str:
    """Map a provider's worst utilization (and burn pace) onto a mascot mood.

    ``pace_ahead`` marks usage running ahead of elapsed time in some window
    (draining faster than the period refills) - that alone is worth a sweat
    even at a low absolute percentage.
    """
    lo, hi = (thresholds or HUD_THRESHOLDS)[:2]
    if worst_pct >= hi:
        return 'panic'
    if worst_pct >= lo or pace_ahead:
        return 'sweat'
    return 'happy'


def clamp_position(pos: tuple[int, int], size: tuple[int, int], work: tuple[int, int, int, int]) -> tuple[int, int]:
    """Keep a remembered window position fully inside the work area.

    Guards against a dragged spot that no longer exists (monitor unplugged,
    resolution change) resurrecting the HUD off-screen.
    """
    left, top, right, bottom = work
    width, height = size
    x = max(left, min(pos[0], right - width))
    y = max(top, min(pos[1], bottom - height))
    return x, y


_visitor_cache: list[str] | None = None
_visitor_grid_cache: list[dict[str, Any]] | None = None


def _visitor_grids() -> list[dict[str, Any]]:
    """User-built sprite grids (Sprite Builder JSON files) for the roamer."""
    global _visitor_grid_cache
    if _visitor_grid_cache is not None:
        return _visitor_grid_cache

    from .sprite_builder import VISITORS_DIR, validate_grid
    grids: list[dict[str, Any]] = []
    try:
        for path in sorted(VISITORS_DIR.glob('*.json'))[:12]:
            try:
                grid = validate_grid(json.loads(path.read_text(encoding='utf-8')))
            except (OSError, ValueError):
                continue
            if grid is not None:
                grids.append(grid)  # px stays density-derived in JS
    except OSError:
        pass
    _visitor_grid_cache = grids
    return grids


def _visitor_data_uris() -> list[str]:
    """User-supplied visitor sprites as data URIs (loaded once per run).

    Drop small PNGs into ``%APPDATA%/UsageMonitorForClaude/visitors/`` and
    they join the built-in critters wandering across the HUD. Data URIs keep
    the page's strict CSP intact (img-src data:). We deliberately bundle no
    third-party characters - what users drop in locally is their business.
    """
    global _visitor_cache
    if _visitor_cache is not None:
        return _visitor_cache

    import base64
    import os as _os
    folder = Path(_os.environ.get('APPDATA') or (Path.home() / 'AppData' / 'Roaming')) / 'UsageMonitorForClaude' / 'visitors'
    uris: list[str] = []
    try:
        for path in sorted(folder.glob('*.png'))[:8]:
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if 0 < len(data) <= 300_000:
                uris.append('data:image/png;base64,' + base64.b64encode(data).decode('ascii'))
    except OSError:
        pass
    _visitor_cache = uris
    return uris


def _pets_rev() -> int:
    """Petdex-pet revision stamp; the sheets themselves travel over the JS
    bridge (get_pets) only when this changes - they are far too big to ride
    along in every refresh payload."""
    try:
        from .pets import pets_rev
        return pets_rev()
    except Exception:
        return 0


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

    peak = round(max((bar['fill_pct'] * 100 for bar in bars), default=0)) if bars else None
    pace_ahead = any(bar['warn'] for bar in bars)
    return {
        'usage': bars,
        'error': error,
        'plan': str(usage.get('plan_type', '')).title(),
        'peak': peak,
        'mood': pick_mood(peak or 0, pace_ahead=pace_ahead),
    }


class UsageHud:
    """Hold-to-peek HUD window driven by a global hotkey."""

    WIDTH = 380      # card width, logical px
    HEIGHT = 236     # initial card height

    def __init__(self, app: UsageMonitorForClaude) -> None:
        self.app = app
        self._window: Any = None
        self._hwnd = 0
        self._height = self.HEIGHT  # logical px, grows with content
        self._visible = False
        # Sticky mode set by the pin button: summons stay on screen instead
        # of hiding on hotkey release.  Survives hide/show cycles.
        self._pin_mode = False
        self._lock = threading.Lock()
        self._loaded = threading.Event()
        self._pump_tid = 0
        self._refresh_stop = threading.Event()
        # Custom position (physical px, window top-left) from dragging; None
        # means the default bottom-right-above-tray placement.
        self._custom_pos: tuple[int, int] | None = None
        if isinstance(HUD_POSITION, list) and len(HUD_POSITION) == 2:
            self._custom_pos = (int(HUD_POSITION[0]), int(HUD_POSITION[1]))
        # Manual size (logical px) from a user edge-resize; None = auto
        # (fixed width, content-driven height). Once set, the user's size is
        # the authority and content scrolls instead of growing the window.
        self._manual_size: tuple[int, int] | None = None
        if isinstance(HUD_SIZE, list) and len(HUD_SIZE) == 2:
            self._manual_size = (max(300, int(HUD_SIZE[0])), max(160, int(HUD_SIZE[1])))
        self._applying_size = False
        self._dragging = False
        self._drag_offset = (0, 0)
        # Set when the system resumes from sleep: WebView2 composition often
        # comes back wedged (double-image flicker), so the next summon gets
        # a freshly created window instead.
        self._stale = False

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

        if HUD_SESSIONS:
            try:
                claude['sessions'] = active_sessions()
            except Exception:
                claude['sessions'] = []

        return {
            'claude': claude,
            'codex': codex,
            'thresholds': HUD_THRESHOLDS,
            'pin_mode': self._pin_mode,
            'visitors': _visitor_data_uris(),
            'visitor_grids': _visitor_grids(),
            'visitors_enabled': HUD_VISITORS,
            'pets_rev': _pets_rev(),
        }

    # Window

    def _create_window(self) -> None:
        if self._window is not None:
            return

        width, height = self._intended_size()
        self._window = webview.create_window(
            '', url=str(_HUD_DIR / 'hud.html'),
            width=width, height=height,
            resizable=False, frameless=True, shadow=False,
            easy_drag=False, on_top=True, hidden=True,
            background_color='#1A1915',
            js_api=_HudApi(self),
        )
        self._window.events.loaded += self._on_loaded
        self._window.events.closed += self._on_window_closed

    def _intended_size(self) -> tuple[int, int]:
        """Current target size in logical px (manual override or auto).

        The window IS the card. A transparent apron for over-the-rim critter
        entrances was tried and reverted: WebView2 renders nothing through
        either pywebview transparent=True or LWA_COLORKEY layered windows -
        real per-pixel transparency there needs DirectComposition work.
        """
        if self._manual_size is not None:
            return self._manual_size
        return self.WIDTH, self._height

    def _on_window_closed(self) -> None:
        """Forget a destroyed window so the next summon recreates it.

        The window can die outside our control (Alt+F4 on a focused HUD,
        a WebView2 crash); treating it as permanent would leave the
        hotkey summoning a dead handle forever.
        """
        self._window = None
        self._hwnd = 0
        self._visible = False
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

    def _monitor_metrics(self, hmon: int) -> tuple[Any, int]:
        """Return (work rect, effective DPI) for a monitor handle.

        Sizing must follow the monitor the HUD actually occupies - mixed-DPI
        setups (200% laptop + 100% external) otherwise render the window at
        double or half its intended size after a drag.
        """
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info))

        dpi_x = ctypes.c_uint(0)
        dpi_y = ctypes.c_uint(0)
        try:
            ctypes.windll.shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))  # MDT_EFFECTIVE_DPI
            dpi = dpi_x.value
        except Exception:
            dpi = 0
        if not dpi:
            dpi = ctypes.windll.user32.GetDpiForWindow(self._hwnd) or ctypes.windll.user32.GetDpiForSystem() or _BASELINE_DPI
        return info.rcWork, dpi

    def _apply_size(self) -> None:
        """The ONE place that sets window size, in logical px via pywebview.

        pywebview/WinForms scale logical sizes by the window's current
        monitor DPI and reapply them on WM_DPICHANGED - setting physical
        sizes ourselves raced that machinery and produced flicker storms
        when crossing mixed-DPI monitors.
        """
        width, height = self._intended_size()
        self._applying_size = True
        try:
            self._window.resize(width, height)
        except Exception:
            pass
        finally:
            self._applying_size = False

    # Grip-driven resize (bridge-called): the card's own corner grip drives
    # the same single logical-size authority - no OS frame, no non-client
    # hacks, no guessing which resized events were the user.

    def _begin_resize(self) -> bool:
        if not self._hwnd:
            return False
        cursor = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(cursor))
        self._resize_anchor = (cursor.x, cursor.y)
        # Anchor CARD dimensions (manual size is card-space; the window adds
        # the apron on top - mixing the two inflates the card every resize).
        self._resize_anchor_size = self._manual_size or (self.WIDTH, self._height)
        dpi = ctypes.windll.user32.GetDpiForWindow(self._hwnd) or ctypes.windll.user32.GetDpiForSystem()
        self._resize_scale = dpi / _BASELINE_DPI
        self._resizing = True
        return True

    def _resize_drag(self) -> bool:
        if not getattr(self, '_resizing', False) or not self._hwnd:
            return False
        cursor = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(cursor))
        dx = int((cursor.x - self._resize_anchor[0]) / self._resize_scale)
        dy = int((cursor.y - self._resize_anchor[1]) / self._resize_scale)
        width = max(300, min(900, self._resize_anchor_size[0] + dx))
        height = max(160, min(1200, self._resize_anchor_size[1] + dy))
        if (width, height) != self._manual_size:
            self._manual_size = (width, height)
            self._apply_size()
        return True

    def _end_resize(self) -> None:
        if not getattr(self, '_resizing', False):
            return
        self._resizing = False
        if self._manual_size is not None:
            self._persist_setting('hud_size', list(self._manual_size))

    def _position(self) -> None:
        """Move (never size) the HUD: dragged spot, else bottom-right above the tray.

        The physical target comes from the destination monitor's work area
        and DPI; the conversion to pywebview's logical coordinates uses the
        window's own current DPI, which is the factor WinForms scales by.
        """
        if self._custom_pos is not None:
            point = ctypes.wintypes.POINT(self._custom_pos[0] + 10, self._custom_pos[1] + 10)
            hmon = ctypes.windll.user32.MonitorFromPoint(point, 2)  # MONITOR_DEFAULTTONEAREST
        else:
            tray = ctypes.windll.user32.FindWindowW('Shell_TrayWnd', None)
            hmon = ctypes.windll.user32.MonitorFromWindow(tray, 2)
        work, dpi = self._monitor_metrics(hmon)

        scale = dpi / _BASELINE_DPI
        logical_w, logical_h = self._intended_size()
        width = int(logical_w * scale)
        height = int(logical_h * scale)
        margin = int(16 * scale)

        if self._custom_pos is not None:
            x, y = clamp_position(self._custom_pos, (width, height), (work.left, work.top, work.right, work.bottom))
        else:
            x = work.right - width - margin
            y = work.bottom - height - margin

        window_dpi = ctypes.windll.user32.GetDpiForWindow(self._hwnd) or dpi
        window_scale = window_dpi / _BASELINE_DPI
        try:
            self._window.move(int(x / window_scale), int(y / window_scale))
        except Exception:
            pass
        _logger.debug('hud: position dpi=%s window_dpi=%s -> (%s,%s)', dpi, window_dpi, x, y)

    def show(self) -> None:
        """Show the HUD without activating it and start live refresh.

        A window marked stale (system resume) or failing a health ping is
        torn down and rebuilt first - WebView2 composition does not reliably
        survive sleep and renders a doubled, flickering image afterwards.
        """
        with self._lock:
            if self._stale and self._window is not None:
                self._destroy_window()
            if self._window is None:
                self._create_window()
            if self._window is None or not self._loaded.wait(timeout=5):
                return
            try:
                if self._window.evaluate_js('1') != 1:
                    raise RuntimeError('hud page unresponsive')
            except Exception:
                self._destroy_window()
                self._create_window()
                if self._window is None or not self._loaded.wait(timeout=5):
                    return
            self._push_data()
            self._apply_size()
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

    def hide(self, fade: bool = True) -> None:
        with self._lock:
            if not self._visible:
                return
            self._visible = False
            self._refresh_stop.set()

        if fade:
            try:
                self._window.evaluate_js('hudFadeOut && hudFadeOut()')
                time.sleep(0.24)
            except Exception:
                pass
            # A re-summon may have raced the fade; never hide the new peek.
            if self._visible:
                return

        ctypes.windll.user32.ShowWindow(self._hwnd, _SW_HIDE)

    def set_pin_mode(self, enabled: bool) -> bool:
        self._pin_mode = bool(enabled)
        return self._pin_mode

    def _destroy_window(self) -> None:
        """Tear the window down; the next summon builds a fresh one."""
        window, self._window = self._window, None
        self._hwnd = 0
        self._visible = False
        self._loaded.clear()
        self._refresh_stop.set()
        self._stale = False
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass

    def _mark_stale(self) -> None:
        """Called on system resume: retire the current window."""
        self._stale = True
        if self._visible:
            self.hide(fade=False)

    # Dragging (bridge-called; same physical-pixel math as the popup)

    def _begin_drag(self) -> bool:
        if not self._hwnd:
            return False
        cursor = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(cursor))
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(self._hwnd, ctypes.byref(rect))
        self._drag_offset = (cursor.x - rect.left, cursor.y - rect.top)
        self._dragging = True
        return True

    def _drag(self) -> bool:
        if not self._dragging or not self._hwnd:
            return False
        cursor = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(cursor))
        x = cursor.x - self._drag_offset[0]
        y = cursor.y - self._drag_offset[1]
        ctypes.windll.user32.SetWindowPos(self._hwnd, 0, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010)  # NOSIZE|NOZORDER|NOACTIVATE
        return True

    def _end_drag(self) -> None:
        if not self._dragging:
            return
        self._dragging = False
        if not self._hwnd:
            return
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(self._hwnd, ctypes.byref(rect))
        self._custom_pos = (rect.left, rect.top)
        # After a cross-DPI drag WinForms has rescaled the window itself;
        # re-asserting the LOGICAL size settles any drift without fighting
        # it (popup.py's proven end-drag remedy).
        self._apply_size()
        self._persist_setting('hud_position', list(self._custom_pos))

    def _persist_setting(self, key: str, value: Any) -> None:
        """Merge one runtime-remembered value into the settings file."""
        try:
            path = settings_write_path()
            try:
                data = json.loads(path.read_text(encoding='utf-8-sig'))
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            data[key] = value
            path.write_text(json.dumps(data, indent=4) + '\n', encoding='utf-8')
        except OSError:
            pass

    def _set_height(self, height: int) -> None:
        """Grow/shrink the window to the CARD height reported by JS.

        Only in auto-size mode - once the user has resized manually, their
        size is the authority and overflowing content scrolls instead.
        Gates on actual Win32 visibility, not ``_visible``: reports arrive
        on bridge threads and can land mid-``show()`` before the flag flips.
        """
        if self._manual_size is not None:
            return
        height = max(int(height), 160)
        if height == self._height:
            return
        self._height = height
        if self._hwnd and ctypes.windll.user32.IsWindowVisible(self._hwnd):
            self._apply_size()
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

        self._create_power_window()

        try:
            while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == _WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                    self._on_hotkey(vk)
                else:
                    ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
                    ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            ctypes.windll.user32.UnregisterHotKey(None, _HOTKEY_ID)
            self._pump_tid = 0

    def _create_power_window(self) -> None:
        """Hidden top-level window receiving WM_POWERBROADCAST on this thread.

        Sleep/resume wedges WebView2's composition (doubled flicker); this is
        the reliable signal to retire the window.  Message-only windows do
        not receive power broadcasts, hence a real (hidden) one.
        """
        wintypes = ctypes.wintypes
        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM,
        )

        # Private, fully-typed bindings: handles are 64-bit, and ctypes'
        # default c_int conversion overflows on them.
        user32 = ctypes.WinDLL('user32')
        kernel32 = ctypes.WinDLL('kernel32')
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND

        def wndproc(hwnd: int, message: int, wparam: int, lparam: int) -> int:
            if message == _WM_POWERBROADCAST and wparam in (_PBT_APMRESUMESUSPEND, _PBT_APMRESUMEAUTOMATIC):
                threading.Thread(target=self._mark_stale, daemon=True).start()
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._power_wndproc = WNDPROC(wndproc)  # keep referenced (GC)

        class _WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ('style', ctypes.c_uint), ('lpfnWndProc', WNDPROC),
                ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                ('hInstance', wintypes.HINSTANCE), ('hIcon', wintypes.HANDLE),
                ('hCursor', wintypes.HANDLE), ('hbrBackground', wintypes.HANDLE),
                ('lpszMenuName', wintypes.LPCWSTR), ('lpszClassName', wintypes.LPCWSTR),
            ]

        wc = _WNDCLASSW()
        wc.lpfnWndProc = self._power_wndproc
        wc.lpszClassName = 'UsageMonitorHudPower'
        wc.hInstance = kernel32.GetModuleHandleW(None)
        if ctypes.windll.user32.RegisterClassW(ctypes.byref(wc)):
            user32.CreateWindowExW(
                0, wc.lpszClassName, None, 0, 0, 0, 0, 0, None, None, wc.hInstance, None,
            )

    def _on_hotkey(self, vk: int) -> None:
        """Handle one hotkey press.

        Pin mode off: hold-to-peek - the HUD lives exactly as long as the
        keys are held.  Pin mode on: a trigger leaves the HUD on screen
        until the next trigger, Escape, or the close button.
        """
        # Re-sync with reality: the window may have died or been hidden
        # behind our back, and a stale True would turn a summon into a no-op.
        if self._visible and not (self._hwnd and ctypes.windll.user32.IsWindowVisible(self._hwnd)):
            self._visible = False

        _logger.info('hud: hotkey (visible=%s)', self._visible)
        if self._visible:
            self.hide()
            return

        self.show()
        _logger.info('hud: shown=%s hwnd=%s', self._visible, self._hwnd)
        while ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000:
            time.sleep(0.03)
            if not self._visible:  # closed from the HUD while still holding
                return

        if self._pin_mode:
            return
        if HUD_LINGER > 0:
            # Let JS own the countdown - it can see hovering, so reaching
            # for the pin button never races a hide.
            try:
                self._window.evaluate_js(f'beginLinger && beginLinger({int(HUD_LINGER)})')
                return
            except Exception:
                pass
        self.hide()


class _HudApi:
    """Methods exposed to the HUD JavaScript via pywebview's JS bridge."""

    def __init__(self, hud: UsageHud) -> None:
        self._hud = hud

    def set_pin_mode(self, enabled: bool) -> bool:
        return self._hud.set_pin_mode(enabled)

    def report_height(self, height: int) -> None:
        if height:
            self._hud._set_height(height)

    def begin_drag(self) -> bool:
        return self._hud._begin_drag()

    def drag(self) -> bool:
        return self._hud._drag()

    def end_drag(self) -> None:
        self._hud._end_drag()

    def begin_resize(self) -> bool:
        return self._hud._begin_resize()

    def resize_drag(self) -> bool:
        return self._hud._resize_drag()

    def end_resize(self) -> None:
        self._hud._end_resize()

    def open_settings(self) -> None:
        self._hud.app.on_open_setup()

    def get_pets(self) -> list[dict[str, Any]]:
        """Installed petdex/Codex pets (small-sheet data URIs) for visitors."""
        try:
            from .pets import pets_payload
            return pets_payload()
        except Exception:
            return []

    def close(self) -> None:
        self._hud.hide()
