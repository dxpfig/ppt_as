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

            def render(entry):
                output = entry.parent.parent / '.rendered'
                (output / 'pages').mkdir(parents=True)
                Image.new('RGB', (960, 540), 'white').save(
                    output / 'pages' / 'page-1.png'
                )
                return output

            async def run():
                publisher = kimi_preview.DraftPreviewPublisher(
                    workspace,
                    deck / 'deck.pptd',
                    'job',
                    on_event=lambda event: events.append(event),
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
            self.assertTrue(
                (workspace / 'draft-previews' / 'page-1.png').is_file()
            )
            self.assertEqual(events[0]['pagePath'], 'pages/1.page')
            self.assertIn('实际预览', events[0]['message'])

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
            self.assertEqual(job['draftPreview']['latestIndex'], 0)
            self.assertIn(
                '/api/studio/jobs/job/draft-previews/page-1.png',
                job['draftPreview']['pages'][0]['previewUrl'],
            )


if __name__ == '__main__':
    unittest.main()
