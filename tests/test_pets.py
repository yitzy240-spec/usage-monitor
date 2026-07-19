"""Tests for the petdex pet installer and loader (pets.py)."""

from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests
from PIL import Image

from usage_monitor_for_claude import pets

# Test sheets use 48x52 cells - the exact official 192:208 aspect at 1/4
# scale, so the geometry validator accepts them.
CELL_W, CELL_H = 48, 52


def make_sheet(row_frames: dict[int, int] | None = None, mode: str = 'RGBA', fmt: str = 'WEBP') -> bytes:
    """A synthetic 8x9 sheet with opaque pixels in the requested cells."""
    im = Image.new(mode, (CELL_W * pets.SHEET_COLS, CELL_H * pets.SHEET_ROWS), (0, 0, 0, 0) if mode == 'RGBA' else (10, 10, 10))
    if row_frames:
        for row, count in row_frames.items():
            for col in range(count):
                box = (col * CELL_W + 5, row * CELL_H + 5, col * CELL_W + 20, row * CELL_H + 20)
                im.paste((200, 90, 60, 255) if mode == 'RGBA' else (200, 90, 60), box)
    out = io.BytesIO()
    im.save(out, format=fmt)
    return out.getvalue()


class FakeResponse:
    def __init__(self, *, status: int = 200, body: bytes = b'', payload: dict | None = None):
        self.status_code = status
        self.ok = status < 400
        self._body = body
        self._payload = payload
        self.raw = io.BytesIO(body)
        # requests exposes raw.read(amt, decode_content=...)
        _read = self.raw.read
        self.raw.read = lambda amt=-1, decode_content=True: _read(amt)

    def json(self):
        if self._payload is None:
            raise ValueError('no json')
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f'HTTP {self.status_code}')


def api_payload(slug: str = 'boba', host: str = 'assets.petdex.dev') -> dict:
    return {'ok': True, 'pet': {
        'slug': slug,
        'displayName': slug.title(),
        'petJsonUrl': f'https://{host}/pets/{slug}-abc/petjson.json',
        'spritesheetUrl': f'https://{host}/pets/{slug}-abc/sprite.webp',
        'spriteExt': 'webp',
    }}


class PetsDirsMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        base = Path(self._tmp.name)
        self._patches = [
            patch.object(pets, 'PETS_DIR', base / 'pets'),
            patch.object(pets, 'CODEX_PETS_DIR', base / 'codex-pets'),
            patch.object(pets, '_payload_cache', None),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._tmp.cleanup)
        for p in self._patches:
            self.addCleanup(p.stop)


class TestInstallPet(PetsDirsMixin):
    def _install(self, *, api=None, sheet=None, meta=None, slug='boba'):
        sheet = sheet if sheet is not None else make_sheet({0: 3, 1: 8})
        meta = meta if meta is not None else json.dumps({'displayName': 'Boba', 'description': 'a pet'}).encode()
        api = api or api_payload(slug)

        def fake_get(url, **kwargs):
            if url.startswith(pets.PETDEX_API):
                return FakeResponse(payload=api)
            if url.endswith('.json'):
                return FakeResponse(body=meta)
            return FakeResponse(body=sheet)

        with patch.object(pets.requests, 'get', side_effect=fake_get):
            return pets.install_pet(slug)

    def test_happy_path_writes_all_files(self):
        rev_before = pets.pets_rev()
        result = self._install()
        self.assertEqual(result['name'], 'Boba')
        dest = pets.PETS_DIR / 'boba'
        self.assertTrue((dest / 'pet.json').is_file())
        self.assertTrue((dest / 'spritesheet.webp').is_file())
        self.assertTrue((dest / 'sheet-small.webp').is_file())
        self.assertGreater(pets.pets_rev(), rev_before)

    def test_bad_slug_rejected(self):
        for slug in ('', '../evil', 'UPPER', 'a' * 80, 'sp ce!'):
            with self.assertRaises(pets.PetError):
                pets.install_pet(slug)

    def test_off_allowlist_asset_host_refused(self):
        with self.assertRaises(pets.PetError) as ctx:
            self._install(api=api_payload('boba', host='evil.example.com'))
        self.assertIn('unexpected', str(ctx.exception))

    def test_unknown_pet_is_a_friendly_404(self):
        def fake_get(url, **kwargs):
            return FakeResponse(status=404)
        with patch.object(pets.requests, 'get', side_effect=fake_get):
            with self.assertRaises(pets.PetError) as ctx:
                pets.install_pet('nope')
        self.assertIn('No pet named', str(ctx.exception))

    def test_oversize_sheet_rejected(self):
        with patch.object(pets, '_MAX_SHEET_BYTES', 100):
            with self.assertRaises(pets.PetError) as ctx:
                self._install()
        self.assertIn('too large', str(ctx.exception))

    def test_unreadable_sheet_rejected(self):
        with self.assertRaises(pets.PetError):
            self._install(sheet=b'not an image at all')

    def test_wrong_geometry_rejected(self):
        im = Image.new('RGBA', (300, 300))
        out = io.BytesIO()
        im.save(out, format='WEBP')
        with self.assertRaises(pets.PetError) as ctx:
            self._install(sheet=out.getvalue())
        self.assertIn('8x9', str(ctx.exception))

    def test_malformed_pet_json_rejected(self):
        with self.assertRaises(pets.PetError):
            self._install(meta=b'{broken')


class TestSheetGeometry(unittest.TestCase):
    @staticmethod
    def _sheet(w, h):
        im = Image.new('RGBA', (w, h))
        out = io.BytesIO()
        im.save(out, format='WEBP')
        return out.getvalue()

    def test_v1_and_v2_layouts_accepted(self):
        for rows in (9, 11):
            sheet = pets._validate_sheet(self._sheet(192 * 8, 208 * rows))
            self.assertEqual(pets._sheet_rows(sheet), rows)

    def test_wrong_aspect_rejected(self):
        with self.assertRaises(pets.PetError):
            pets._validate_sheet(self._sheet(192 * 8, 300 * 9))  # cells not 192:208


class TestRowFrames(unittest.TestCase):
    def test_counts_real_frames_per_row(self):
        sheet = Image.open(io.BytesIO(make_sheet({0: 3, 1: 8, 3: 2})))
        frames = pets._row_frames(sheet)
        self.assertEqual(frames[0], 3)
        self.assertEqual(frames[1], 8)
        self.assertEqual(frames[3], 2)
        self.assertEqual(frames[2], 1)  # empty rows clamp to 1, never 0

    def test_no_alpha_falls_back_to_official_counts(self):
        sheet = Image.open(io.BytesIO(make_sheet({0: 2}, mode='RGB', fmt='PNG')))
        self.assertEqual(pets._row_frames(sheet), pets._DEFAULT_ROW_FRAMES)


class TestRemovePet(PetsDirsMixin):
    def test_removes_installed_pet_only(self):
        target = pets.PETS_DIR / 'boba'
        target.mkdir(parents=True)
        (target / 'pet.json').write_text('{}')
        self.assertTrue(pets.remove_pet('boba'))
        self.assertFalse(target.exists())

    def test_ignores_traversal_and_unknown(self):
        self.assertFalse(pets.remove_pet('..'))
        self.assertFalse(pets.remove_pet('missing'))
        self.assertFalse(pets.remove_pet('a/../../b'))


class TestScanAndPayload(PetsDirsMixin):
    def _plant(self, root: Path, slug: str, *, name: str | None = None, sheet: bytes | None = None):
        d = root / slug
        d.mkdir(parents=True)
        (d / 'spritesheet.webp').write_bytes(sheet if sheet is not None else make_sheet({0: 4}))
        if name:
            (d / 'pet.json').write_text(json.dumps({'displayName': name}))

    def test_merges_petdex_and_codex_sources(self):
        self._plant(pets.PETS_DIR, 'boba', name='Boba')
        self._plant(pets.CODEX_PETS_DIR, 'hatchling')
        (pets.CODEX_PETS_DIR / 'not-a-pet').mkdir(parents=True)  # no sheet: skipped
        found = pets.installed_pets()
        self.assertEqual([(p['slug'], p['source']) for p in found], [('boba', 'petdex'), ('hatchling', 'codex')])
        self.assertEqual(found[0]['name'], 'Boba')

    def test_payload_has_data_uri_and_frames_and_caches(self):
        self._plant(pets.PETS_DIR, 'boba', name='Boba')
        first = pets.pets_payload()
        self.assertEqual(len(first), 1)
        self.assertTrue(first[0]['sheet'].startswith('data:image/webp;base64,'))
        self.assertEqual(first[0]['rowFrames'][0], 4)
        self.assertIs(pets.pets_payload(), first)  # mtime-keyed cache hit

    def test_corrupt_sheet_skipped_not_fatal(self):
        self._plant(pets.PETS_DIR, 'bad', sheet=b'garbage')
        self._plant(pets.PETS_DIR, 'good')
        payload = pets.pets_payload()
        self.assertEqual([p['slug'] for p in payload], ['good'])


SITEMAP = b'''<?xml version="1.0"?><urlset>
<url><loc>https://petdex.dev/about</loc></url>
<url><loc>https://petdex.dev/pets/boba</loc></url>
<url><loc>https://petdex.dev/es/pets/boba</loc></url>
<url><loc>https://petdex.dev/pets/broom-witch</loc></url>
<url><loc>https://petdex.dev/collections/dog-squad</loc></url>
</urlset>'''


class TestBrowseIndex(PetsDirsMixin):
    def test_parses_and_dedupes_locales(self):
        with patch.object(pets.requests, 'get', return_value=FakeResponse(body=SITEMAP)):
            index = pets.browse_pets(force=True)
        self.assertEqual(index, [
            {'slug': 'boba', 'name': 'Boba'},
            {'slug': 'broom-witch', 'name': 'Broom Witch'},
        ])

    def test_disk_cache_served_within_ttl(self):
        with patch.object(pets.requests, 'get', return_value=FakeResponse(body=SITEMAP)) as get:
            pets.browse_pets(force=True)
            self.assertEqual(get.call_count, 1)
            pets.browse_pets()
            self.assertEqual(get.call_count, 1)  # second call hits the disk cache

    def test_network_failure_is_a_pet_error(self):
        with patch.object(pets.requests, 'get', side_effect=requests.ConnectionError()):
            with self.assertRaises(pets.PetError):
                pets.browse_pets(force=True)


class TestPetPreview(PetsDirsMixin):
    def setUp(self):
        super().setUp()
        pets._preview_cache.clear()
        self.addCleanup(pets._preview_cache.clear)

    @staticmethod
    def _strip(frames: int) -> bytes:
        im = Image.new('RGBA', (192 * frames, 208), (10, 10, 10, 255))
        out = io.BytesIO()
        im.save(out, format='WEBP')
        return out.getvalue()

    def test_frames_derived_from_aspect_and_cached(self):
        with patch.object(pets.requests, 'get', return_value=FakeResponse(body=self._strip(6))) as get:
            preview = pets.pet_preview('boba')
            self.assertEqual(preview['frames'], 6)
            self.assertTrue(preview['sheet'].startswith('data:image/webp;base64,'))
            self.assertIs(pets.pet_preview('boba'), preview)
            self.assertEqual(get.call_count, 1)

    def test_missing_preview_is_a_pet_error(self):
        with patch.object(pets.requests, 'get', return_value=FakeResponse(status=404)):
            with self.assertRaises(pets.PetError):
                pets.pet_preview('nope')


class TestPackPets(unittest.TestCase):
    PAGE = (b'<a href="/pets/milo">x</a> stuff '
            b'self.__next_f.push("\\"slug\\":\\"rio\\" \\"slug\\":\\"milo\\"")')

    def test_extracts_from_hrefs_and_rsc_payload(self):
        with patch.object(pets.requests, 'get', return_value=FakeResponse(body=self.PAGE)):
            self.assertEqual(pets.pack_pets('dog-squad'), ['milo', 'rio'])

    def test_empty_pack_is_a_pet_error(self):
        with patch.object(pets.requests, 'get', return_value=FakeResponse(body=b'<html>nothing</html>')):
            with self.assertRaises(pets.PetError):
                pets.pack_pets('empty-pack')

    def test_bad_pack_slug_rejected(self):
        with self.assertRaises(pets.PetError):
            pets.pack_pets('NOT A SLUG')


class TestSmallSheet(unittest.TestCase):
    def test_large_cells_downscaled(self):
        big = Image.new('RGBA', (192 * 8, 208 * 9))
        small = Image.open(io.BytesIO(pets._small_sheet_bytes(big)))
        self.assertEqual(small.width, pets._SMALL_CELL_W * 8)

    def test_small_cells_left_alone(self):
        tiny = Image.new('RGBA', (40 * 8, 40 * 9))
        small = Image.open(io.BytesIO(pets._small_sheet_bytes(tiny)))
        self.assertEqual(small.size, tiny.size)


if __name__ == '__main__':
    unittest.main()
