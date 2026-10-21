"""Content-addressed delivery export cache."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from agent_service import export_service
from agent_service.pptd_store import DocumentStore


def state():
    return {
        'document': {'version': 'v2', 'size': [960, 540], 'pages': ['pages/a.page']},
        'pages': [{'path': 'pages/a.page', 'data': {'elements': []}}],
    }


class ExportCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_document_reuses_cached_delivery(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = DocumentStore(root)
            value = state()
            store.commit('project', value['document'], value['pages'], 'initial')

            async def export(_store, _project_id, folder, _progress, **_kwargs):
                folder.mkdir(parents=True)
                (folder / 'presentation.pptx').write_bytes(b'pptx')
                (folder / 'presentation.pdf').write_bytes(b'pdf')
                return {
                    'pptx': 'presentation.pptx',
                    'pdf': 'presentation.pdf',
                    'source': 'source.zip',
                    'report': 'export-report.json',
                    'previews': ['slide-1.png'],
                    'revision': 1,
                    'pageCount': 1,
                    'warnings': [],
                }

            with patch.object(export_service.pptd_exports, 'export_document', AsyncMock(side_effect=export)) as runner:
                first = await export_service.export_document(store, 'project', root / 'out-1')
                second = await export_service.export_document(store, 'project', root / 'out-2')
            self.assertEqual(first['revision'], second['revision'])
            runner.assert_awaited_once()
            self.assertTrue((root / 'out-2' / 'presentation.pptx').is_file())
            self.assertEqual(
                json.loads((root / 'export-cache' / next((root / 'export-cache').iterdir()).name / 'metadata.json').read_text())['revision'],
                1,
            )

    async def test_disabled_cache_exports_every_time(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = DocumentStore(root)
            value = state()
            store.commit('project', value['document'], value['pages'], 'initial')

            async def export(_store, _project_id, folder, _progress, **_kwargs):
                folder.mkdir(parents=True)
                return {'revision': 1, 'pageCount': 1, 'warnings': []}

            with patch.dict('os.environ', {'PPT_EXPORT_CACHE': '0'}), patch.object(
                export_service.pptd_exports,
                'export_document',
                AsyncMock(side_effect=export),
            ) as runner:
                await export_service.export_document(store, 'project', root / 'one')
                await export_service.export_document(store, 'project', root / 'two')
            self.assertEqual(runner.await_count, 2)

    async def test_concurrent_same_content_publishes_one_complete_cache(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = DocumentStore(root)
            value = state()
            store.commit('project', value['document'], value['pages'], 'initial')

            async def export(_store, _project_id, folder, _progress, **_kwargs):
                await asyncio.sleep(0.05)
                folder.mkdir(parents=True)
                (folder / 'presentation.pptx').write_bytes(b'pptx')
                return {'revision': 1, 'pageCount': 1, 'warnings': []}

            with patch.object(
                export_service.pptd_exports,
                'export_document',
                AsyncMock(side_effect=export),
            ) as runner:
                results = await asyncio.gather(
                    export_service.export_document(store, 'project', root / 'one'),
                    export_service.export_document(store, 'project', root / 'two'),
                )

            self.assertEqual([result['revision'] for result in results], [1, 1])
            self.assertEqual(runner.await_count, 1)
            self.assertTrue((root / 'one' / 'presentation.pptx').is_file())
            self.assertTrue((root / 'two' / 'presentation.pptx').is_file())
            entries = list((root / 'export-cache').iterdir())
            self.assertEqual(len(entries), 1)
            self.assertTrue((entries[0] / 'artifact' / 'presentation.pptx').is_file())
            self.assertTrue((entries[0] / 'metadata.json').is_file())
