"""
Sprite Builder Tests
=====================

Validation, response parsing, and save behavior for Claude-drawn visitors.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from usage_monitor_for_claude.sprite_builder import generate_sprite, save_sprite, validate_grid

GOOD = {
    'name': 'Grumpy Toaster!',
    'palette': {'a': '#C0C0C8', 'b': '#16130e'},
    'rows': ['..aaaa..', '.abbbba.', '.aaaaaa.', '.a.aa.a.'],
}


class TestValidateGrid(unittest.TestCase):
    def test_good_grid_cleans_name(self):
        grid = validate_grid(GOOD)
        self.assertEqual(grid['name'], 'grumpy-toaster')
        self.assertEqual(grid['rows'], GOOD['rows'])

    def test_rejects_bad_shapes(self):
        self.assertIsNone(validate_grid(None))
        self.assertIsNone(validate_grid({**GOOD, 'rows': ['..aa', '......']}))  # ragged
        self.assertIsNone(validate_grid({**GOOD, 'rows': ['x' * 8] * 4}))  # unknown char
        self.assertIsNone(validate_grid({**GOOD, 'palette': {'aa': '#ffffff'}}))  # multi-char key
        self.assertIsNone(validate_grid({**GOOD, 'palette': {'a': 'red'}}))  # non-hex
        self.assertIsNone(validate_grid({**GOOD, 'rows': ['.' * 8] * 4}))  # fully transparent

    def test_rejects_dot_palette_key(self):
        self.assertIsNone(validate_grid({**GOOD, 'palette': {'.': '#ffffff', 'a': '#000000'}}))


class TestGenerateSprite(unittest.TestCase):
    def _response(self, text, status=200):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = {'content': [{'type': 'text', 'text': text}]}
        return resp

    def test_happy_path_with_prose_wrapper(self):
        text = 'Here you go!\n' + json.dumps(GOOD) + '\nEnjoy.'
        with patch('usage_monitor_for_claude.sprite_builder.read_access_token', return_value='tok'):
            with patch('usage_monitor_for_claude.sprite_builder.requests.post', return_value=self._response(text)) as post:
                result = generate_sprite('a toaster')
        self.assertTrue(result['ok'])
        self.assertEqual(result['grid']['name'], 'grumpy-toaster')
        headers = post.call_args.kwargs['headers']
        self.assertEqual(headers['anthropic-beta'], 'oauth-2025-04-20')

    def test_no_login(self):
        with patch('usage_monitor_for_claude.sprite_builder.read_access_token', return_value=None):
            result = generate_sprite('a toaster')
        self.assertFalse(result['ok'])

    def test_unusable_reply(self):
        with patch('usage_monitor_for_claude.sprite_builder.read_access_token', return_value='tok'):
            with patch('usage_monitor_for_claude.sprite_builder.requests.post', return_value=self._response('sorry, no')):
                result = generate_sprite('a toaster')
        self.assertFalse(result['ok'])


class TestSaveSprite(unittest.TestCase):
    def test_saves_and_dedupes_names(self):
        with TemporaryDirectory() as tmp:
            with patch('usage_monitor_for_claude.sprite_builder.VISITORS_DIR', Path(tmp)):
                first = save_sprite(GOOD)
                second = save_sprite(GOOD)
        self.assertTrue(first['ok'] and second['ok'])
        self.assertTrue(first['path'].endswith('grumpy-toaster.json'))
        self.assertTrue(second['path'].endswith('grumpy-toaster-2.json'))

    def test_rejects_invalid(self):
        self.assertFalse(save_sprite({'nope': 1})['ok'])


if __name__ == '__main__':
    unittest.main()
