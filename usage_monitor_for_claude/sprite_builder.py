"""
Sprite Builder
===============

Describe a critter; your own Claude subscription draws it as a pixel grid;
it joins the HUD's visitor rotation.

Uses the same OAuth inference channel Claude Code itself uses (bearer token
from the CLI login or the app login, ``anthropic-beta: oauth-2025-04-20``),
so generation consumes a sliver of the user's existing quota - no API key.
The model must answer in a strict JSON grid format (palette + rows) which
is validated hard before anything is saved; failures degrade to an error
message, never to a broken visitor.

Saved sprites live as JSON next to the user's visitor PNGs
(``%APPDATA%/UsageMonitorForClaude/visitors/<name>.json``) and render with
the HUD's existing box-shadow grid renderer.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
from pathlib import Path
from typing import Any

import requests

from .api import read_access_token

__all__ = ['generate_sprite', 'save_sprite', 'validate_grid', 'VISITORS_DIR']

API_URL = 'https://api.anthropic.com/v1/messages'
# Best-first: pixel grids in text need strong spatial reasoning. Falls back
# down the list when a model is unavailable on the account.
MODELS = ('claude-fable-5', 'claude-sonnet-5')

VISITORS_DIR = Path(os.environ.get('APPDATA') or (Path.home() / 'AppData' / 'Roaming')) / 'UsageMonitorForClaude' / 'visitors'

# OAuth inference requires Claude Code's identity as the first system block.
_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

_INSTRUCTIONS = """You are a master pixel artist designing a small character sprite.
Reply with ONLY a JSON object, no prose, in exactly this shape:

{"name": "short-kebab-name", "notes": "brief design plan", "palette": {"o": "#1A1512", "a": "#RRGGBB"}, "rows": ["....ooo....", "..."]}

Canvas and format:
- rows: 18 to 26 strings, ALL exactly the same length, 18 to 28 characters wide.
- Each character is '.' (transparent) or a single-letter palette key.
- 5 to 10 palette colors.

Craft rules (this is what separates good pixel art from noise):
- notes: one sentence planning silhouette + landmark details BEFORE drawing.
- Strong, readable SILHOUETTE first - the shape must be recognizable filled with one color.
- Outline the entire exterior in a very dark color (near-black, slightly warm).
- Every material gets 2-3 shades: base, shadow, highlight. Light from top-left.
- Details (eyes, tools, props) get enough pixels to read - at this canvas a
  prop like a hat or mallet should be 5-10 pixels wide, not 2.
- No stray isolated pixels; no checkerboard dithering.
- Side or 3/4 view facing right; feet/base touch the bottom row.
- The sprite lives on a DARK background (#1A1512): main body colors must be
  mid-to-bright so the silhouette pops - never near-black except the outline.
- All body parts connect - no floating fragments.
"""

_HEX = re.compile(r'^#[0-9a-fA-F]{6}$')


def validate_grid(data: Any) -> dict[str, Any] | None:
    """Return a cleaned {name, palette, rows} grid, or None if invalid."""
    if not isinstance(data, dict):
        return None
    palette = data.get('palette')
    rows = data.get('rows')
    name = data.get('name')
    if not isinstance(palette, dict) or not isinstance(rows, list) or not isinstance(name, str):
        return None

    clean_palette = {}
    for key, value in palette.items():
        if not (isinstance(key, str) and len(key) == 1 and key != '.'):
            return None
        if not (isinstance(value, str) and _HEX.match(value.strip())):
            return None
        clean_palette[key] = value.strip()
    if not 1 <= len(clean_palette) <= 12:
        return None

    if not 4 <= len(rows) <= 56:
        return None
    width = len(rows[0]) if isinstance(rows[0], str) else 0
    if not 6 <= width <= 56:
        return None
    for row in rows:
        if not isinstance(row, str) or len(row) != width:
            return None
        if any(ch != '.' and ch not in clean_palette for ch in row):
            return None
    if not any(ch != '.' for row in rows for ch in row):
        return None

    slug = re.sub(r'[^a-z0-9-]+', '-', name.lower()).strip('-')[:40] or 'sprite'
    clean = {'name': slug, 'palette': clean_palette, 'rows': list(rows)}

    # Optional animation frames: same canvas, same palette, minimal diffs.
    frames = data.get('frames')
    if isinstance(frames, dict):
        clean_frames = {}
        for frame_name, frame_rows in frames.items():
            if (
                isinstance(frame_name, str) and frame_name in ('blink', 'wave')
                and isinstance(frame_rows, list) and len(frame_rows) == len(rows)
                and all(
                    isinstance(r, str) and len(r) == width
                    and all(ch == '.' or ch in clean_palette for ch in r)
                    for r in frame_rows
                )
            ):
                clean_frames[frame_name] = list(frame_rows)
        if clean_frames:
            clean['frames'] = clean_frames
    return clean


def _normalize(data: Any) -> Any:
    """Repair near-miss model output (ragged rows, oversize, stray chars)."""
    if not isinstance(data, dict) or not isinstance(data.get('rows'), list) or not isinstance(data.get('palette'), dict):
        return data
    rows = [str(row)[:56] for row in data['rows'][:56] if str(row).strip('.')]
    if not rows:
        return data
    width = min(max(len(row) for row in rows), 56)
    keys = set(k for k in data['palette'] if isinstance(k, str) and len(k) == 1)
    rows = [
        ''.join(ch if ch == '.' or ch in keys else '.' for ch in row.ljust(width, '.')[:width])
        for row in rows
    ]
    return {**data, 'rows': rows}


_NEXT_MODEL = '_next_model_'


def _scale2x(rows: list[str]) -> list[str]:
    """EPX/Scale2x: edge-preserving 2x upscale of a character grid.

    Doubles pixel density while keeping diagonals smooth - the model then
    REFINES a dense grid instead of having to compose one from scratch
    (composing 40+ coherent rows is where text pixel art falls apart).
    """
    h, w = len(rows), len(rows[0])

    def at(x: int, y: int) -> str:
        return rows[y][x] if 0 <= x < w and 0 <= y < h else '.'

    out = []
    for y in range(h):
        top, bottom = [], []
        for x in range(w):
            p = at(x, y)
            a, b, c, d = at(x, y - 1), at(x + 1, y), at(x - 1, y), at(x, y + 1)
            e1 = a if (c == a and c != d and a != b) else p
            e2 = b if (a == b and a != c and b != d) else p
            e3 = c if (d == c and d != a and c != b) else p
            e4 = d if (b == d and b != a and d != c) else p
            top += [e1, e2]
            bottom += [e3, e4]
        out.append(''.join(top))
        out.append(''.join(bottom))
    return out


def _render_png(grid: dict[str, Any], scale: int = 8) -> bytes:
    """Rasterize a grid so the model can SEE its own sprite (vision refine)."""
    from PIL import Image, ImageDraw

    rows, palette = grid['rows'], grid['palette']
    img = Image.new('RGBA', (len(rows[0]) * scale, len(rows) * scale), (26, 21, 18, 255))
    draw = ImageDraw.Draw(img)
    # Checkered background: makes silhouette gaps and dark-on-dark colors
    # visible to the critiquing model (a plain dark bg hid both).
    for y in range(0, len(rows), 4):
        for x in range(0, len(rows[0]), 4):
            if (x // 4 + y // 4) % 2:
                draw.rectangle(
                    [x * scale, y * scale, (x + 4) * scale - 1, (y + 4) * scale - 1],
                    fill=(52, 46, 40, 255),
                )
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch in palette:
                draw.rectangle(
                    [x * scale, y * scale, (x + 1) * scale - 1, (y + 1) * scale - 1],
                    fill=palette[ch],
                )
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def _draw(token: str, model: str, messages: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    """One drawing request. Returns (grid, None), (None, error) or (None, _NEXT_MODEL)."""
    try:
        resp = requests.post(
            API_URL,
            headers={
                'Authorization': f'Bearer {token}',
                'anthropic-beta': 'oauth-2025-04-20',
                'anthropic-version': '2023-06-01',
                'Content-Type': 'application/json',
            },
            json={
                'model': model,
                'max_tokens': 6000,
                # Without this the model may spend the whole budget thinking
                # and return no text at all (observed with default settings).
                'thinking': {'type': 'disabled'},
                'system': [
                    {'type': 'text', 'text': _IDENTITY},
                    {'type': 'text', 'text': _INSTRUCTIONS},
                ],
                'messages': messages,
            },
            timeout=120,
        )
    except requests.RequestException as exc:
        return None, f'network error: {exc.__class__.__name__}'

    if resp.status_code in (400, 403, 404):
        # Model not available on this account/tier: try the next one.
        return None, _NEXT_MODEL
    if resp.status_code != 200:
        detail = ''
        try:
            detail = (resp.json().get('error') or {}).get('message', '')[:120]
        except Exception:
            pass
        return None, f'Claude said no (HTTP {resp.status_code}) {detail}'.strip()

    try:
        text = ''.join(
            block.get('text', '') for block in resp.json().get('content', [])
            if isinstance(block, dict) and block.get('type') == 'text'
        )
        start, end = text.find('{'), text.rfind('}')
        parsed = json.loads(text[start:end + 1]) if start >= 0 <= end else None
        return validate_grid(parsed) or validate_grid(_normalize(parsed)), None
    except (ValueError, KeyError):
        return None, None


def generate_sprite(prompt: str) -> dict[str, Any]:
    """Ask Claude for a sprite. Returns {'ok': True, 'grid': ...} or {'ok': False, 'error': ...}."""
    prompt = (prompt or '').strip()
    if not prompt:
        return {'ok': False, 'error': 'describe your critter first'}

    token = read_access_token()
    if not token:
        return {'ok': False, 'error': 'no Claude login - sign in first'}

    ask = {'role': 'user', 'content': f'Sprite request: {prompt}'}
    for model in MODELS:
        draft, error = _draw(token, model, [ask])
        if error == _NEXT_MODEL:
            continue
        if error:
            return {'ok': False, 'error': error}
        if draft is None:
            draft, error = _draw(token, model, [ask])  # one retry on unusable output
            if error == _NEXT_MODEL:
                continue
            if draft is None:
                continue

        # Densify: EPX-upscale the coherent small draft to 2x pixel density
        # (Codex-sprite territory), then let the vision rounds refine detail
        # into the dense grid.
        current = draft
        if len(draft['rows']) <= 28 and len(draft['rows'][0]) <= 28:
            dense = validate_grid({**draft, 'rows': _scale2x(draft['rows'])})
            if dense is not None:
                current = dense

        # Vision refine: the model SEES its own sprite rendered and fixes
        # what actually reads poorly - far stronger than text-only critique.
        for _round in range(2):
            try:
                png_b64 = base64.b64encode(_render_png(current)).decode('ascii')
            except Exception:
                break
            refined, _refine_error = _draw(token, model, [
                ask,
                {'role': 'assistant', 'content': json.dumps({k: current[k] for k in ('name', 'palette', 'rows')})},
                {'role': 'user', 'content': [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': png_b64}},
                    {'type': 'text', 'text': (
                        'This is your sprite rendered (it was upscaled 2x, so you have a dense '
                        'canvas to work with). Look at it as an art director: does the subject '
                        'read instantly? Are silhouette, proportions and details (faces, props) '
                        'recognizable, or do parts collapse into noise? Use the extra density: '
                        'smooth jagged curves, refine shading transitions, sharpen the small '
                        'details. Output the corrected sprite - JSON only, same format, same '
                        'canvas size. If it already reads well, output it unchanged.'
                    )},
                ]},
            ])
            if refined is None or refined['rows'] == current['rows']:
                break
            current = refined

        # Animation frames: minimal-pixel-diff variants of the final grid.
        frames, _frames_error = _draw(token, model, [
            ask,
            {'role': 'assistant', 'content': json.dumps({k: current[k] for k in ('name', 'palette', 'rows')})},
            {'role': 'user', 'content': (
                'Now give this sprite life with two animation frames, each a MINIMAL edit '
                'of the exact rows above (same canvas size, same palette, change as few '
                'pixels as possible): "blink" - eyes closed/looking away; "wave" - one '
                'arm/appendage/feature raised in greeting. Reply with ONLY: '
                '{"name": "...", "palette": {...the same palette...}, "rows": [...the same rows...], '
                '"frames": {"blink": [...], "wave": [...]}}'
            )},
        ])
        if frames is not None and frames.get('frames'):
            # Frames must fit the FINAL grid (dims + palette), however the
            # model may have re-echoed the rows.
            merged = validate_grid({**current, 'frames': frames['frames']})
            if merged is not None and merged.get('frames'):
                current = merged
        return {'ok': True, 'grid': current}

    return {'ok': False, 'error': 'the sketch came back unusable - try rephrasing'}


def save_sprite(grid: Any) -> dict[str, Any]:
    """Persist a validated grid into the visitors folder."""
    clean = validate_grid(grid)
    if clean is None:
        return {'ok': False, 'error': 'invalid sprite'}
    try:
        VISITORS_DIR.mkdir(parents=True, exist_ok=True)
        path = VISITORS_DIR / f"{clean['name']}.json"
        counter = 2
        while path.exists():
            path = VISITORS_DIR / f"{clean['name']}-{counter}.json"
            counter += 1
        path.write_text(json.dumps(clean, indent=2), encoding='utf-8')
    except OSError as exc:
        return {'ok': False, 'error': str(exc)}

    # New sprite joins the rotation without a restart.
    from . import hud
    hud._visitor_grid_cache = None
    return {'ok': True, 'path': str(path)}
