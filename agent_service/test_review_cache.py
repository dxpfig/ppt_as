"""Persistent visual review cache contracts."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agent_service import review_service


def state():
    return {
        'document': {'version': 'v2', 'size': [960, 540], 'pages': ['pages/a.page']},
        'pages': [{'path': 'pages/a.page', 'data': {'elements': []}}],
    }


class ReviewCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_incremental_deck_review_only_calls_vision_for_changed_pages(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / 'slide-1.png').write_bytes(b'one')
            (folder / 'slide-2.png').write_bytes(b'two')
            deck = {
                'document': {
                    'version': 'v2',
                    'size': [960, 540],
                    'pages': ['pages/a.page', 'pages/b.page'],
                },
                'pages': [
                    {'path': 'pages/a.page', 'data': {'elements': []}},
                    {'path': 'pages/b.page', 'data': {'elements': []}},
                ],
            }
            unchanged = {
                'status': 'passed',
                'coveredPages': ['pages/a.page'],
                'observations': [
                    {'pagePath': 'pages/a.page', 'evidence': '页面 A 已有审稿证据'}
                ],
                'issues': [],
                'model': 'vision',
            }
            previous = {
                'status': 'issues',
                'coveredPages': ['pages/a.page', 'pages/b.page'],
                'pageReviews': [
                    unchanged,
                    {
                        **unchanged,
                        'status': 'issues',
                        'coveredPages': ['pages/b.page'],
                        'observations': [
                            {'pagePath': 'pages/b.page', 'evidence': '旧页面 B 证据'}
                        ],
                        'issues': [{'code': 'old'}],
                    },
                ],
            }
            changed = {
                **unchanged,
                'coveredPages': ['pages/b.page'],
                'observations': [
                    {'pagePath': 'pages/b.page', 'evidence': '修改后的页面 B 证据'}
                ],
            }
            config = SimpleNamespace(
                provider='openai',
                model='vision',
                base_url='http://vision',
            )
            with patch.object(
                review_service.visual_review,
                'vision_configuration',
                return_value=config,
            ), patch.object(
                review_service.visual_review,
                'review_page',
                AsyncMock(return_value=changed),
            ) as review:
                report = await review_service.review_deck(
                    deck,
                    folder,
                    previous_review=previous,
                    only_pages=['pages/b.page'],
                    concurrency=2,
                )
            review.assert_awaited_once()
            self.assertEqual(
                review.await_args.kwargs['image_name'],
                'slide-2.png',
            )
            self.assertEqual(
                report['coveredPages'],
                ['pages/a.page', 'pages/b.page'],
            )
            self.assertEqual(report['incrementalPages'], ['pages/b.page'])
            self.assertEqual(report['status'], 'passed')

    async def test_unchanged_page_review_reuses_cached_report(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / 'slide-1.png').write_bytes(b'image')
            report = {
                'status': 'passed',
                'coveredPages': ['pages/a.page'],
                'observations': [{'pagePath': 'pages/a.page', 'evidence': '页面清晰'}],
                'issues': [],
                'model': 'vision',
            }
            config = SimpleNamespace(provider='openai', model='vision', base_url='http://vision')
            with patch.object(review_service.visual_review, 'vision_configuration', return_value=config), patch.object(
                review_service.visual_review,
                'review_page',
                AsyncMock(return_value=report),
            ) as review:
                first = await review_service.review_page(
                    state(), folder, 'pages/a.page', cache_dir=folder / 'cache'
                )
                second = await review_service.review_page(
                    state(), folder, 'pages/a.page', cache_dir=folder / 'cache'
                )
            self.assertEqual(first['status'], 'passed')
            self.assertTrue(second['cached'])
            review.assert_awaited_once()
            cached = list((folder / 'cache').glob('*.json'))
            self.assertEqual(len(cached), 1)
            self.assertEqual(json.loads(cached[0].read_text())['status'], 'passed')

    async def test_changed_page_image_invalidates_page_cache(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            image = folder / 'slide-1.png'
            image.write_bytes(b'one')
            config = SimpleNamespace(provider='openai', model='vision', base_url='http://vision')
            report = {'status': 'passed', 'coveredPages': ['pages/a.page'], 'observations': [], 'issues': []}
            with patch.object(review_service.visual_review, 'vision_configuration', return_value=config), patch.object(
                review_service.visual_review,
                'review_page',
                AsyncMock(return_value=report),
            ) as review:
                await review_service.review_page(state(), folder, 'pages/a.page', cache_dir=folder / 'cache')
                image.write_bytes(b'two')
                await review_service.review_page(state(), folder, 'pages/a.page', cache_dir=folder / 'cache')
            self.assertEqual(review.await_count, 2)
