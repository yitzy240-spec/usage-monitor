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

__all__ = [
    'install_pet', 'remove_pet', 'installed_pets', 'pets_payload', 'pets_rev',
    'browse_pets', 'browse_packs', 'pet_preview', 'pack_pets', 'FEATURED_PACKS', 'PetError',
]

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
SHEET_ROWS = 9          # rows every sheet must have (v1); v2 sheets add more
_MAX_SHEET_ROWS = 16    # sanity ceiling for extended layouts
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


def _rate_limited(resp: Any) -> bool:
    """petdex signals throttling as HTTP 429 or an {'error': 'rate_limited'} body."""
    if resp.status_code == 429:
        return True
    try:
        return resp.json().get('error') == 'rate_limited'
    except ValueError:
        return False


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


def _sheet_rows(sheet: 'Any') -> int:
    """Animation rows in a sheet: cells are 192:208, columns always 8."""
    cell_h = (sheet.width / SHEET_COLS) * 208 / 192
    return round(sheet.height / cell_h) if cell_h else 0


def _row_frames(sheet: 'Any') -> list[int]:
    """Count real frames per animation row via the alpha channel.

    Pet authors don't always fill all 8 cells of a row; playing into empty
    cells makes the pet flash invisible. Sheets without alpha fall back to
    the official row lengths. Only the first 9 (official) rows matter to the
    HUD - v2 sheets' extra rows are ignored.
    """
    if 'A' not in sheet.getbands():
        return list(_DEFAULT_ROW_FRAMES)
    rows = max(_sheet_rows(sheet), 1)
    cell_w = sheet.width // SHEET_COLS
    cell_h = sheet.height // rows
    alpha = sheet.getchannel('A')
    counts = []
    for row in range(min(rows, SHEET_ROWS)):
        count = 0
        for col in range(SHEET_COLS):
            box = (col * cell_w, row * cell_h, (col + 1) * cell_w, (row + 1) * cell_h)
            if alpha.crop(box).getbbox() is not None:
                count = col + 1
        counts.append(max(count, 1))
    while len(counts) < SHEET_ROWS:
        counts.append(1)
    return counts


def _validate_sheet(data: bytes) -> 'Any':
    from PIL import Image
    try:
        sheet = Image.open(io.BytesIO(data))
        sheet.load()
    except Exception as exc:
        raise PetError('The spritesheet is not a readable image.') from exc
    rows = _sheet_rows(sheet)
    cell_h = sheet.height / rows if rows else 0
    ok = (
        sheet.width % SHEET_COLS == 0
        and sheet.width // SHEET_COLS >= 32
        and SHEET_ROWS <= rows <= _MAX_SHEET_ROWS
        and cell_h and sheet.height % rows == 0
        and abs(cell_h - (sheet.width / SHEET_COLS) * 208 / 192) < 1
    )
    if not ok:
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

    import time
    resp = None
    for attempt in range(2):
        try:
            resp = requests.get(PETDEX_API + slug, headers=_HEADERS, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            raise PetError('Could not reach petdex.dev - check your connection.') from exc
        if _rate_limited(resp) and attempt == 0:
            time.sleep(4)  # one polite retry - pack installs can trip the limiter
            continue
        break
    if resp.status_code == 404:
        raise PetError(f'No pet named "{slug}" on petdex.dev.')
    if _rate_limited(resp):
        raise PetError('petdex.dev is rate-limiting installs - wait a minute and try again.')
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
    pets = installed_pets()[:24]
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
            'sheetRows': _sheet_rows(sheet),
        })
    _payload_cache = (key, out)
    return out


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# Gallery browsing - a searchable index + per-pet animated previews
# ---------------------------------------------------------------------------

# petdex has no public list API; its sitemap enumerates every approved pet
# (~3.7k) and collection (~200) as of 2026-07. One fetch a day is plenty
# for a browse index; pets and packs come from the same download.
_SITEMAP_URL = 'https://petdex.dev/sitemap.xml'
_MAX_SITEMAP_BYTES = 40_000_000
_INDEX_TTL_SECONDS = 24 * 3600
_INDEX_FILE = 'gallery-index.json'
_SITEMAP_SLUG_RE = re.compile(r'petdex\.dev/pets/([a-z0-9][a-z0-9-]{0,62})<')
_SITEMAP_PACK_RE = re.compile(r'petdex\.dev/collections/([a-z0-9][a-z0-9-]{0,62})<')

# Every pet exposes a lightweight idle-row strip (cells 192x208) at a
# guessable URL - browse cards animate from this without the full sheet.
_PREVIEW_URL = 'https://assets.petdex.dev/pets/{slug}/preview.webp'
_MAX_PREVIEW_BYTES = 500_000
_PREVIEW_CACHE_MAX = 200

_preview_cache: dict[str, dict[str, Any]] = {}


def _named(slugs: set[str]) -> list[dict[str, Any]]:
    return [{'slug': s, 'name': s.replace('-', ' ').title()} for s in sorted(slugs)]


def _load_index(force: bool = False) -> dict[str, Any]:
    """{'pets': [...], 'packs': [...]} from the sitemap, disk-cached a day."""
    import time
    index_path = PETS_DIR / _INDEX_FILE
    if not force:
        try:
            if time.time() - index_path.stat().st_mtime < _INDEX_TTL_SECONDS:
                cached = json.loads(index_path.read_text(encoding='utf-8'))
                # Pre-1.10 caches were a bare pet list - treat as stale.
                if isinstance(cached, dict) and cached.get('pets'):
                    return cached
        except (OSError, ValueError):
            pass

    try:
        xml = _fetch(_SITEMAP_URL, _MAX_SITEMAP_BYTES).decode('utf-8', 'replace')
    except (requests.RequestException, PetError) as exc:
        raise PetError('Could not load the petdex gallery - check your connection.') from exc
    # 'category-*' collections are auto-generated tag intersections
    # (category-calm-cat, ...) - noise next to the curated packs.
    packs = {s for s in _SITEMAP_PACK_RE.findall(xml) if not s.startswith('category-')}
    index = {
        'pets': _named(set(_SITEMAP_SLUG_RE.findall(xml))),
        'packs': _named(packs),
    }
    if index['pets']:
        try:
            PETS_DIR.mkdir(parents=True, exist_ok=True)
            tmp = index_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(index), encoding='utf-8')
            os.replace(tmp, index_path)
        except OSError:
            pass
    return index


def browse_pets(force: bool = False) -> list[dict[str, Any]]:
    """The full pet gallery as [{slug, name}], cached on disk for a day."""
    return _load_index(force)['pets']


def browse_packs(force: bool = False) -> list[dict[str, Any]]:
    """All curated collections ("packs") as [{slug, name}], same cache."""
    return _load_index(force)['packs']


def pet_preview(slug: str) -> dict[str, Any]:
    """A pet's idle-strip preview as a data URI + frame count (cached)."""
    slug = (slug or '').strip().lower()
    if not _SLUG_RE.match(slug):
        raise PetError('Bad pet name.')
    hit = _preview_cache.get(slug)
    if hit is not None:
        return hit

    from PIL import Image
    try:
        data = _fetch(_PREVIEW_URL.format(slug=slug), _MAX_PREVIEW_BYTES)
        im = Image.open(io.BytesIO(data))
        im.load()
    except (requests.RequestException, PetError, OSError) as exc:
        raise PetError('No preview available.') from exc
    # Idle strips are N cells of 192x208 side by side; derive N from the
    # aspect ratio rather than trusting exact pixel sizes.
    cell_w = im.height * 192 / 208
    frames = max(1, min(8, round(im.width / cell_w))) if cell_w else 1
    preview = {
        'slug': slug,
        'frames': frames,
        'sheet': 'data:image/webp;base64,' + base64.b64encode(data).decode('ascii'),
    }
    if len(_preview_cache) >= _PREVIEW_CACHE_MAX:
        _preview_cache.pop(next(iter(_preview_cache)))
    _preview_cache[slug] = preview
    return preview


# Community collections double as installable "packs". These four are the
# hand-picked featured set (crafter-originals = petdex's own original
# mascots, the default starter pack).
FEATURED_PACKS = [
    {'slug': 'crafter-originals', 'name': 'Crafter Originals', 'blurb': 'petdex’s own 20 original mascots'},
    {'slug': 'dog-squad', 'name': 'Dog Squad', 'blurb': 'a pile of very good dogs'},
    {'slug': 'cats-universe', 'name': 'Cats Universe', 'blurb': 'every kind of cat'},
    {'slug': 'coders-club', 'name': 'Coders Club', 'blurb': 'developer-themed companions'},
]

_COLLECTION_URL = 'https://petdex.dev/collections/{slug}'
_MAX_PAGE_BYTES = 8_000_000
# Collection pages are server-rendered Next.js; pet slugs appear both as
# plain hrefs and inside the RSC flight payload (escaped JSON).
_PAGE_HREF_RE = re.compile(r'href="/pets/([a-z0-9][a-z0-9-]{0,62})"')
_PAGE_RSC_RE = re.compile(r'\\"slug\\":\\"([a-z0-9][a-z0-9-]{0,62})\\"')


def pack_pets(slug: str) -> list[str]:
    """Pet slugs inside a petdex collection ("pack")."""
    slug = (slug or '').strip().lower()
    if not _SLUG_RE.match(slug):
        raise PetError('Pack names are lowercase letters, digits and dashes.')
    try:
        html = _fetch(_COLLECTION_URL.format(slug=slug), _MAX_PAGE_BYTES).decode('utf-8', 'replace')
    except (requests.RequestException, PetError) as exc:
        raise PetError('Could not load that pack from petdex.dev.') from exc
    slugs = sorted(set(_PAGE_HREF_RE.findall(html)) | set(_PAGE_RSC_RE.findall(html)))
    if not slugs:
        raise PetError(f'No pets found in a pack named "{slug}".')
    _augment_index(slugs)
    return slugs


def _augment_index(slugs: list[str]) -> None:
    """Merge newly-seen pet slugs into the cached gallery index.

    The sitemap is not complete (franchise pets in particular can be
    missing), so every pack fetch teaches the local index new pets.
    """
    index_path = PETS_DIR / _INDEX_FILE
    try:
        cached = json.loads(index_path.read_text(encoding='utf-8'))
        if not isinstance(cached, dict) or 'pets' not in cached:
            return
        known = {p['slug'] for p in cached['pets']}
        fresh = [s for s in slugs if s not in known]
        if not fresh:
            return
        cached['pets'] = sorted(
            cached['pets'] + _named(set(fresh)), key=lambda p: p['slug'])
        tmp = index_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(cached), encoding='utf-8')
        os.replace(tmp, index_path)
    except (OSError, ValueError, KeyError, TypeError):
        pass  # augmentation is best-effort; the sitemap refresh still wins
