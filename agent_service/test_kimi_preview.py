from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from agent_service import kimi_preview
from agent_service.job_store import JobStore


class KimiDraftPreviewTests(unittest.TestCase):
    def test_strict_page_check_enables_browser_layout_measurement(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            deck = workspace / 'deck'
            (deck / 'pages').mkdir(parents=True)
            entry = deck / 'deck.pptd'
            entry.write_text(
                'version: v2\nsize: [960, 540]\n'
                'pages: [pages/1.page]\n',
                encoding='utf-8',
            )
            (deck / 'pages' / '1.page').write_text(
                'elements:\n'
                '- elementId: title\n'
                '  elementType: text\n'
                '  bounds: [40, 40, 400, 60]\n'
                '  content: {text: 页面}\n',
                encoding='utf-8',
            )
            calls = []

            def render(*_args, browser_check=False):
                calls.append(browser_check)
                return {
                    'ok': False,
                    'error': '测试停止',
                    'issues': [],
                }

            async def run():
                publisher = kimi_preview.DraftPreviewPublisher(
                    workspace,
                    entry,
                    'job',
                )
                return await publisher.check_page(0, 'pages/1.page')

            with patch.object(
                kimi_preview,
                '_render_page',
                side_effect=render,
            ):
                checked = asyncio.run(run())

        self.assertFalse(checked['ok'])
        self.assertEqual(calls, [True])

    def test_metadata_only_stub_is_not_published_as_ready_preview(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / 'workspace'
            deck = workspace / 'deck'
            (deck / 'pages').mkdir(parents=True)
            (deck / 'deck.pptd').write_text(
                'version: v2\nsize: [960, 540]\npages: [pages/1.page]\n',
                encoding='utf-8',
            )
            (deck / 'pages' / '1.page').write_text(
                'title: 空壳页\nelements: []\n',
                encoding='utf-8',
            )

            async def run():
                publisher = kimi_preview.DraftPreviewPublisher(
                    workspace,
                    deck / 'deck.pptd',
                    'job',
                )
                await publisher.start()
                await publisher.stop()

            with patch.object(kimi_preview, '_render_pages') as render:
                asyncio.run(run())
            render.assert_not_called()
            manifest = json.loads(
                (workspace / '.draft-preview.json').read_text(encoding='utf-8')
            )
            self.assertEqual(manifest['pages'][0]['status'], 'pending')
            self.assertEqual(manifest['completedPages'], 0)

    def test_generated_page_publishes_browser_preview_and_job_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / 'kimi-workspace' / 'project' / 'job'
            deck = workspace / 'deck'
            (deck / 'pages').mkdir(parents=True)
            (deck / 'deck.pptd').write_text(
                'version: v2\nsize: [960, 540]\npages: [pages/1.page]\n',
                encoding='utf-8',
            )
            (deck / 'pages' / '1.page').write_text(
                'elements:\n'
                '- elementId: title\n'
                '  elementType: text\n'
                '  bounds: [40, 40, 400, 60]\n'
                '  content:\n'
                '    text: 实时页面\n',
                encoding='utf-8',
            )
            (deck / 'plan.json').write_text(
                json.dumps({
                    'deckTitle': '预览测试',
                    'pages': [{
                        'path': 'pages/1.page',
                        'title': '第一页',
                        'readerTask': '说明',
                        'content': '内容',
                        'visualPlan': '文本',
                        'sourceSections': [],
                    }],
                }, ensure_ascii=False),
                encoding='utf-8',
            )
            events = []
            inspections = []

            def render(entry):
                output = entry.parent.parent / '.rendered'
                (output / 'pages').mkdir(parents=True)
                Image.new('RGB', (960, 540), 'white').save(
                    output / 'pages' / 'page-1.png'
                )
                return output

            async def inspect(path, image, state, structural_issues):
                inspections.append((path, image, state, structural_issues))
                return {
                    'status': 'passed',
                    'issues': [],
                    'model': 'vision-test',
                }

            async def run():
                publisher = kimi_preview.DraftPreviewPublisher(
                    workspace,
                    deck / 'deck.pptd',
                    'job',
                    on_event=lambda event: events.append(event),
                    inspect_page=inspect,
                )
                await publisher.start()
                await publisher.stop()

            with patch.object(kimi_preview, '_render_pages', side_effect=render):
                asyncio.run(run())

            manifest = json.loads(
                (workspace / '.draft-preview.json').read_text(encoding='utf-8')
            )
            self.assertEqual(manifest['completedPages'], 1)
            self.assertEqual(manifest['pages'][0]['status'], 'ready')
            self.assertEqual(manifest['pages'][0]['title'], '第一页')
            self.assertEqual(manifest['pages'][0]['reviewStatus'], 'passed')
            self.assertEqual(manifest['pages'][0]['text'], '实时页面')
            self.assertEqual(manifest['reviewedPages'], 1)
            self.assertTrue(
                (workspace / 'draft-previews' / 'page-1.png').is_file()
            )
            self.assertEqual(inspections[0][0], 'pages/1.page')
            self.assertEqual(events[0]['pagePath'], 'pages/1.page')
            self.assertIn('通过单页检视', events[0]['message'])

            store = JobStore(root)
            store.save(
                'job',
                {
                    'id': 'job',
                    'projectId': 'project',
                    'status': 'running',
                    'stage': 25,
                    'message': '正在生成',
                },
            )
            job = store.read('job')
            self.assertEqual(job['draftPreview']['completedPages'], 1)
            self.assertEqual(job['draftPreview']['reviewedPages'], 1)
            self.assertEqual(job['draftPreview']['latestIndex'], 0)
            self.assertEqual(
                job['draftPreview']['pages'][0]['reviewStatus'],
                'passed',
            )
            self.assertEqual(job['draftPreview']['pages'][0]['text'], '实时页面')
            self.assertIn(
                '/api/studio/jobs/job/draft-previews/page-1.png',
                job['draftPreview']['pages'][0]['previewUrl'],
            )

    def test_page_issues_are_visible_without_blocking_other_previews(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / 'kimi-workspace' / 'project' / 'job'
            deck = workspace / 'deck'
            (deck / 'pages').mkdir(parents=True)
            (deck / 'deck.pptd').write_text(
                'version: v2\nsize: [960, 540]\npages: [pages/1.page]\n',
                encoding='utf-8',
            )
            (deck / 'pages' / '1.page').write_text(
                'elements:\n'
                '- elementId: title\n'
                '  elementType: text\n'
                '  bounds: [40, 40, 400, 60]\n'
                '  content:\n'
                '    text: 需要调整的页面\n',
                encoding='utf-8',
            )
            (deck / 'plan.json').write_text(
                json.dumps({
                    'deckTitle': '预览测试',
                    'pages': [{'path': 'pages/1.page', 'title': '第一页'}],
                }, ensure_ascii=False),
                encoding='utf-8',
            )
            events = []

            def render(entry):
                output = entry.parent.parent / '.rendered'
                (output / 'pages').mkdir(parents=True)
                Image.new('RGB', (960, 540), 'white').save(
                    output / 'pages' / 'page-1.png'
                )
                return output

            async def inspect(*_args):
                return {
                    'status': 'issues',
                    'model': 'vision-test',
                    'issues': [{
                        'code': 'text-overflow',
                        'severity': 'error',
                        'location': 'pages/1.page#title',
                        'message': '标题存在溢出',
                    }],
                }

            async def run():
                publisher = kimi_preview.DraftPreviewPublisher(
                    workspace,
                    deck / 'deck.pptd',
                    'job',
                    on_event=lambda event: events.append(event),
                    inspect_page=inspect,
                )
                await publisher.start()
                await publisher.stop()

            with patch.object(kimi_preview, '_render_pages', side_effect=render):
                asyncio.run(run())

            manifest = json.loads(
                (workspace / '.draft-preview.json').read_text(encoding='utf-8')
            )
            self.assertEqual(manifest['pages'][0]['reviewStatus'], 'issues')
            self.assertEqual(manifest['issuePages'], 1)
            self.assertEqual(manifest['pages'][0]['issues'][0]['code'], 'text-overflow')
            self.assertEqual(events[0]['status'], 'waiting')
            self.assertIn('1 项问题', events[0]['message'])


if __name__ == '__main__':
    unittest.main()
