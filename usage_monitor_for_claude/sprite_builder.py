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

    if not 4 <= len(rows) <= 32:
        return None
    width = len(rows[0]) if isinstance(rows[0], str) else 0
    if not 6 <= width <= 32:
        return None
    for row in rows:
        if not isinstance(row, str) or len(row) != width:
            return None
        if any(ch != '.' and ch not in clean_palette for ch in row):
            return None
    if not any(ch != '.' for row in rows for ch in row):
        return None

    slug = re.sub(r'[^a-z0-9-]+', '-', name.lower()).strip('-')[:40] or 'sprite'
    return {'name': slug, 'palette': clean_palette, 'rows': list(rows)}


def _normalize(data: Any) -> Any:
    """Repair near-miss model output (ragged rows, oversize, stray chars)."""
    if not isinstance(data, dict) or not isinstance(data.get('rows'), list) or not isinstance(data.get('palette'), dict):
        return data
    rows = [str(row)[:32] for row in data['rows'][:32] if str(row).strip('.')]
    if not rows:
        return data
    width = min(max(len(row) for row in rows), 32)
    keys = set(k for k in data['palette'] if isinstance(k, str) and len(k) == 1)
    rows = [
        ''.join(ch if ch == '.' or ch in keys else '.' for ch in row.ljust(width, '.')[:width])
        for row in rows
    ]
    return {**data, 'rows': rows}


_NEXT_MODEL = '_next_model_'


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

        # Self-critique refine pass: real fidelity gains, best-effort.
        refined, _refine_error = _draw(token, model, [
            ask,
            {'role': 'assistant', 'content': json.dumps({k: draft[k] for k in ('name', 'palette', 'rows')})},
            {'role': 'user', 'content': (
                'Critique this draft against the craft rules - silhouette clarity, exterior '
                'outline, per-material shading, prop readability - then output the IMPROVED '
                'sprite. JSON only, same format, same or larger canvas.'
            )},
        ])
        return {'ok': True, 'grid': refined or draft}

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
