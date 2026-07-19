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
MODEL = 'claude-sonnet-5'

VISITORS_DIR = Path(os.environ.get('APPDATA') or (Path.home() / 'AppData' / 'Roaming')) / 'UsageMonitorForClaude' / 'visitors'

# OAuth inference requires Claude Code's identity as the first system block.
_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

_INSTRUCTIONS = """You design tiny pixel-art sprites for a desktop widget.
Reply with ONLY a JSON object, no prose, in exactly this shape:

{"name": "short-kebab-name", "palette": {"a": "#RRGGBB", "b": "#RRGGBB"}, "rows": ["..aab...", "..."]}

Rules:
- rows: 8 to 14 strings, all the same length, 8 to 16 characters wide.
- Each character is '.' (transparent) or a palette key (single letter).
- 2 to 6 palette colors. Warm, readable colors; dark outline color helps.
- The creature should read clearly at ~24px: strong silhouette, 1-2px details.
- Side view facing right, standing on the bottom row (feet touch it).
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
    if not 1 <= len(clean_palette) <= 8:
        return None

    if not 4 <= len(rows) <= 16:
        return None
    width = len(rows[0]) if isinstance(rows[0], str) else 0
    if not 6 <= width <= 20:
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
    rows = [str(row)[:20] for row in data['rows'][:16] if str(row).strip('.')]
    if not rows:
        return data
    width = min(max(len(row) for row in rows), 20)
    keys = set(k for k in data['palette'] if isinstance(k, str) and len(k) == 1)
    rows = [
        ''.join(ch if ch == '.' or ch in keys else '.' for ch in row.ljust(width, '.')[:width])
        for row in rows
    ]
    return {**data, 'rows': rows}


def generate_sprite(prompt: str) -> dict[str, Any]:
    """Ask Claude for a sprite. Returns {'ok': True, 'grid': ...} or {'ok': False, 'error': ...}."""
    prompt = (prompt or '').strip()
    if not prompt:
        return {'ok': False, 'error': 'describe your critter first'}

    token = read_access_token()
    if not token:
        return {'ok': False, 'error': 'no Claude login - sign in first'}

    last_error = 'the sketch came back unusable - try rephrasing'
    for _attempt in range(2):
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
                    'model': MODEL,
                    'max_tokens': 4000,
                    # Without this the model may spend the whole budget
                    # thinking and return no text at all.
                    'thinking': {'type': 'disabled'},
                    'system': [
                        {'type': 'text', 'text': _IDENTITY},
                        {'type': 'text', 'text': _INSTRUCTIONS},
                    ],
                    'messages': [{'role': 'user', 'content': f'Sprite request: {prompt}'}],
                },
                timeout=90,
            )
        except requests.RequestException as exc:
            return {'ok': False, 'error': f'network error: {exc.__class__.__name__}'}

        if resp.status_code != 200:
            detail = ''
            try:
                detail = (resp.json().get('error') or {}).get('message', '')[:120]
            except Exception:
                pass
            return {'ok': False, 'error': f'Claude said no (HTTP {resp.status_code}) {detail}'.strip()}

        try:
            text = ''.join(
                block.get('text', '') for block in resp.json().get('content', [])
                if isinstance(block, dict) and block.get('type') == 'text'
            )
            start, end = text.find('{'), text.rfind('}')
            parsed = json.loads(text[start:end + 1]) if start >= 0 <= end else None
            grid = validate_grid(parsed) or validate_grid(_normalize(parsed))
        except (ValueError, KeyError):
            grid = None

        if grid is not None:
            return {'ok': True, 'grid': grid}

    return {'ok': False, 'error': last_error}


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
