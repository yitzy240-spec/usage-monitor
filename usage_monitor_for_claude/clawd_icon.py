"""
Clawd Tray Icon
================

Branded tray icon: the canonical Clawd pixel grid, body-tinted by the
worst quota severity (orange / amber / red), over two mini fill bars -
Claude session in orange, Codex worst window in violet.  Selected via
the ``tray_style`` setting ('clawd', default) with upstream's plain
bars still available ('bars').
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from .settings import HUD_THRESHOLDS

__all__ = ['create_clawd_icon']

_SIZE = 64

# The recovered Clawd grid (see hud/hud.js; MIT ClawdMoji recreation).
_ART = [
    '..########..',
    '..#O####O#..',
    '############',
    '############',
    '..########..',
    '..########..',
    '..#.#..#.#..',
    '..#.#..#.#..',
]

_BODY_OK = (218, 119, 88, 255)      # Claude orange
_BODY_WARN = (229, 181, 103, 255)   # amber
_BODY_CRIT = (240, 62, 62, 255)     # true red
_EYE = (22, 19, 14, 255)
_CLAUDE_BAR = (218, 119, 88, 255)
_CODEX_BAR = (167, 139, 250, 255)   # violet


def _body_color(worst_pct: float) -> tuple[int, int, int, int]:
    lo, hi = HUD_THRESHOLDS[:2]
    if worst_pct >= hi:
        return _BODY_CRIT
    if worst_pct >= lo:
        return _BODY_WARN
    return _BODY_OK


def create_clawd_icon(
    worst_pct: float, claude_pct: float, codex_pct: float | None, light_taskbar: bool = False,
) -> Image.Image:
    """Render the Clawd tray icon with severity tint and mini fill bars."""
    img = Image.new('RGBA', (_SIZE, _SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    body = _body_color(worst_pct)
    cell = 5
    ox = (_SIZE - len(_ART[0]) * cell) // 2
    oy = 1
    for j, row in enumerate(_ART):
        for i, ch in enumerate(row):
            color = body if ch == '#' else _EYE if ch == 'O' else None
            if color:
                x, y = ox + i * cell, oy + j * cell
                draw.rectangle([x, y, x + cell - 1, y + cell - 1], fill=color)

    track = (0, 0, 0, 70) if light_taskbar else (255, 255, 255, 60)
    bars: list[tuple[float | None, tuple[int, int, int, int]]] = [(claude_pct, _CLAUDE_BAR)]
    if codex_pct is not None:
        bars.append((codex_pct, _CODEX_BAR))

    bar_h = 6
    y = _SIZE - bar_h * len(bars) - 3 * (len(bars) - 1) - 1
    for pct, color in bars:
        draw.rounded_rectangle([0, y, _SIZE - 1, y + bar_h - 1], radius=2, fill=track)
        fill_w = max(0, min(_SIZE, round(_SIZE * (pct or 0) / 100)))
        if fill_w >= 4:
            draw.rounded_rectangle([0, y, fill_w - 1, y + bar_h - 1], radius=2, fill=color)
        y += bar_h + 3

    return img
