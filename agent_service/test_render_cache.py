"""Page render cache keys and persisted cache reads."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agent_service import render_service


def state(text='same'):
    return {
        'revision': 1,
        'document': {'version': 'v2', 'size': [960, 540], 'theme': {'colors': {'accent': '#123456'}}, 'pages': ['pages/a.page']},
        'pages': [{
            'path': 'pages/a.page',
            'data': {'elements': [{'elementId': 'title', 'elementType': 'text', 'content': {'text': text}}]},
        }],
    }


class RenderCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_render_uses_cached_image_without_browser(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / 'source'
            (source_dir / 'media').mkdir(parents=True)
            (source_dir / 'media' / 'asset.png').write_bytes(b'asset')

            def snapshot(_project_id, folder):
                (folder / 'media').mkdir(parents=True)
                (folder / 'media' / 'asset.png').write_bytes(b'asset')
                return state()

            source = SimpleNamespace(root=root, snapshot=snapshot)
            render_service.render_cache_key(state(), source_dir, 'pages/a.page')
            key = render_service.render_cache_key(state(), source_dir, 'pages/a.page')
            cache = root / 'render-cache' / key
            cache.mkdir(parents=True)
            (cache / 'page.png').write_bytes(b'cached')
            (cache / 'metadata.json').write_text(
                json.dumps({'issues': [], 'layout': {'cached': True}}),
                encoding='utf-8',
            )
            rendered = await render_service.render_page(source, 'project', root / 'out', 'pages/a.page')
        self.assertEqual(rendered.image, b'cached')
        self.assertTrue(rendered.layout['cached'])

    def test_cache_key_changes_when_page_content_or_media_changes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'media').mkdir()
            first = render_service.render_cache_key(state('first'), root, 'pages/a.page')
            second = render_service.render_cache_key(state('second'), root, 'pages/a.page')
            self.assertNotEqual(first, second)
            (root / 'media' / 'asset.png').write_bytes(b'one')
            third = render_service.render_cache_key(state('first'), root, 'pages/a.page')
            (root / 'media' / 'asset.png').write_bytes(b'two')
            fourth = render_service.render_cache_key(state('first'), root, 'pages/a.page')
        self.assertNotEqual(first, third)
        self.assertNotEqual(third, fourth)
