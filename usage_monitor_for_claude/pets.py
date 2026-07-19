"""Petdex pets - install and load animated companions for the HUD.

petdex.dev is the public MIT-licensed gallery of pets in the official Codex
spritesheet format: 8 columns x 9 rows of 192x208 frames, one animation
state per row (idle, run-right, run-left, wave, jump, ...). We install pets
the way its own CLI does: resolve the slug through the typed JSON contract
``GET https://petdex.dev/api/install-pet/{slug}``, then download pet.json
plus the spritesheet from the allowlisted asset host. Network access happens
ONLY when the user clicks Install in Settings - never in the background.

Installed pets live in ``%APPDATA%/UsageMonitorForClaude/pets/<slug>/``.
Pets hatched locally by the Codex CLI (``~/.codex/pets/<name>/``) use the
same on-disk format and are picked up automatically as visitors too.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import requests

__all__ = ['install_pet', 'remove_pet', 'installed_pets', 'pets_payload', 'pets_rev', 'PetError']

PETDEX_API = 'https://petdex.dev/api/install-pet/'
# The petdex server normalizes stored asset URLs to its canonical R2 host;
# never download from anywhere else even if the API said so.
_ALLOWED_ASSET_HOSTS = frozenset({'assets.petdex.dev'})
# The asset CDN rejects Python's default urllib/requests User-Agent.
_HEADERS = {'User-Agent': 'UsageMonitorForClaude-petdex-install'}
_SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,62}$')
_TIMEOUT = 20
_MAX_JSON_BYTES = 64_000
_MAX_SHEET_BYTES = 10_000_000
SHEET_COLS = 8
SHEET_ROWS = 9
# Frame counts per row on the official Codex sheet - the fallback when a
# sheet has no alpha channel to measure real per-row content from.
_DEFAULT_ROW_FRAMES = [6, 8, 8, 4, 5, 8, 8, 8, 6]
# HUD copies are downscaled: display is ~26 css px wide, so a 64px-wide
# cell survives 200% DPI while keeping data URIs small.
_SMALL_CELL_W = 64

PETS_DIR = Path(os.environ.get('APPDATA') or (Path.home() / 'AppData' / 'Roaming')) / 'UsageMonitorForClaude' / 'pets'
CODEX_PETS_DIR = Path.home() / '.codex' / 'pets'

_rev = 1
_payload_cache: tuple[tuple[Any, ...], list[dict[str, Any]]] | None = None


class PetError(Exception):
    """User-presentable failure while installing a pet."""


def pets_rev() -> int:
    """Monotonic counter the HUD polls to know when to re-fetch pets."""
    return _rev


def _bump() -> None:
    global _rev, _payload_cache
    _rev += 1
    _payload_cache = None


def _asset_url_ok(url: str) -> bool:
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == 'https' and parts.hostname in _ALLOWED_ASSET_HOSTS


def _fetch(url: str, cap: int) -> bytes:
    resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, stream=True)
    resp.raise_for_status()
    data = resp.raw.read(cap + 1, decode_content=True)
    if len(data) > cap:
        raise PetError('Pet file is too large.')
    return data


def _row_frames(sheet: 'Any') -> list[int]:
    """Count real frames per animation row via the alpha channel.

    Pet authors don't always fill all 8 cells of a row; playing into empty
    cells makes the pet flash invisible. Sheets without alpha fall back to
    the official row lengths.
    """
    if 'A' not in sheet.getbands():
        return list(_DEFAULT_ROW_FRAMES)
    cell_w = sheet.width // SHEET_COLS
    cell_h = sheet.height // SHEET_ROWS
    alpha = sheet.getchannel('A')
    counts = []
    for row in range(SHEET_ROWS):
        count = 0
        for col in range(SHEET_COLS):
            box = (col * cell_w, row * cell_h, (col + 1) * cell_w, (row + 1) * cell_h)
            if alpha.crop(box).getbbox() is not None:
                count = col + 1
        counts.append(max(count, 1))
    return counts


def _validate_sheet(data: bytes) -> 'Any':
    from PIL import Image
    try:
        sheet = Image.open(io.BytesIO(data))
        sheet.load()
    except Exception as exc:
        raise PetError('The spritesheet is not a readable image.') from exc
    if sheet.width % SHEET_COLS or sheet.height % SHEET_ROWS or sheet.width // SHEET_COLS < 32:
        raise PetError('The spritesheet is not in the 8x9 Codex pet format.')
    return sheet


def _small_sheet_bytes(sheet: 'Any') -> bytes:
    """Downscaled RGBA webp used for the HUD data URI."""
    from PIL import Image
    small = sheet.convert('RGBA')
    cell_w = sheet.width // SHEET_COLS
    if cell_w > _SMALL_CELL_W:
        scale = _SMALL_CELL_W / cell_w
        small = small.resize((round(sheet.width * scale), round(sheet.height * scale)), Image.LANCZOS)
    out = io.BytesIO()
    small.save(out, format='WEBP', quality=85)
    return out.getvalue()


def install_pet(slug: str) -> dict[str, Any]:
    """Download a pet from petdex.dev into the local pets folder."""
    slug = (slug or '').strip().lower().replace(' ', '-')
    if not _SLUG_RE.match(slug):
        raise PetError('Pet names are lowercase letters, digits and dashes.')

    try:
        resp = requests.get(PETDEX_API + slug, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise PetError('Could not reach petdex.dev - check your connection.') from exc
    if resp.status_code == 404:
        raise PetError(f'No pet named "{slug}" on petdex.dev.')
    if not resp.ok:
        raise PetError(f'petdex.dev answered {resp.status_code} - try again later.')
    try:
        pet = resp.json()['pet']
        json_url, sheet_url = pet['petJsonUrl'], pet['spritesheetUrl']
    except (ValueError, KeyError, TypeError) as exc:
        raise PetError('Unexpected reply from petdex.dev.') from exc
    if not (_asset_url_ok(json_url) and _asset_url_ok(sheet_url)):
        raise PetError('Pet assets are hosted somewhere unexpected - refusing to download.')

    try:
        meta_raw = _fetch(json_url, _MAX_JSON_BYTES)
        sheet_raw = _fetch(sheet_url, _MAX_SHEET_BYTES)
    except requests.RequestException as exc:
        raise PetError('Downloading the pet failed - try again.') from exc
    try:
        meta = json.loads(meta_raw.decode('utf-8'))
        if not isinstance(meta, dict):
            raise ValueError('not an object')
    except (ValueError, UnicodeDecodeError) as exc:
        raise PetError('The pet.json file is malformed.') from exc
    sheet = _validate_sheet(sheet_raw)

    dest = PETS_DIR / slug
    dest.mkdir(parents=True, exist_ok=True)
    ext = 'png' if (sheet.format or '').upper() == 'PNG' else 'webp'
    for name, blob in (
        ('pet.json', meta_raw),
        (f'spritesheet.{ext}', sheet_raw),
        ('sheet-small.webp', _small_sheet_bytes(sheet)),
    ):
        tmp = dest / (name + '.tmp')
        tmp.write_bytes(blob)
        os.replace(tmp, dest / name)
    _bump()
    return {
        'slug': slug,
        'name': str(meta.get('displayName') or pet.get('displayName') or slug),
        'description': str(meta.get('description') or ''),
    }


def remove_pet(slug: str) -> bool:
    """Delete an installed petdex pet (never touches ~/.codex/pets)."""
    if not _SLUG_RE.match(slug or ''):
        return False
    target = (PETS_DIR / slug).resolve()
    if target.parent != PETS_DIR.resolve() or not target.is_dir():
        return False
    shutil.rmtree(target, ignore_errors=True)
    _bump()
    return True


def _scan(folder: Path, source: str) -> list[dict[str, Any]]:
    pets: list[dict[str, Any]] = []
    try:
        entries = sorted(p for p in folder.iterdir() if p.is_dir())
    except OSError:
        return pets
    for entry in entries:
        sheets = [p for p in (entry / 'spritesheet.webp', entry / 'spritesheet.png') if p.is_file()]
        if not sheets:
            continue
        name = entry.name
        try:
            meta = json.loads((entry / 'pet.json').read_text(encoding='utf-8'))
            name = str(meta.get('displayName') or name)
        except (OSError, ValueError):
            pass
        pets.append({'slug': entry.name, 'name': name, 'dir': entry, 'sheet': sheets[0], 'source': source})
    return pets


def installed_pets() -> list[dict[str, Any]]:
    """All pets available as visitors: installed from petdex + Codex-hatched."""
    return _scan(PETS_DIR, 'petdex') + _scan(CODEX_PETS_DIR, 'codex')


def pets_payload() -> list[dict[str, Any]]:
    """Pets ready for the HUD: small-sheet data URIs + per-row frame counts.

    Cached against (path, mtime) so the bridge call after a rev bump is the
    only time image work happens. Codex-hatched pets have no pre-built small
    sheet; one is derived in-memory here.
    """
    global _payload_cache
    pets = installed_pets()[:12]
    key = tuple((str(p['sheet']), _mtime(p['sheet'])) for p in pets)
    if _payload_cache is not None and _payload_cache[0] == key:
        return _payload_cache[1]

    out: list[dict[str, Any]] = []
    for pet in pets:
        try:
            sheet = _validate_sheet(pet['sheet'].read_bytes())
            small = pet['dir'] / 'sheet-small.webp'
            blob = small.read_bytes() if small.is_file() else _small_sheet_bytes(sheet)
        except (OSError, PetError):
            continue
        out.append({
            'slug': pet['slug'],
            'name': pet['name'],
            'source': pet['source'],
            'sheet': 'data:image/webp;base64,' + base64.b64encode(blob).decode('ascii'),
            'rowFrames': _row_frames(sheet),
        })
    _payload_cache = (key, out)
    return out


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
