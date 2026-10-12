"""PPTD authority, immutable snapshots, concurrent edits and recovery contracts."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile
from unittest.mock import patch

from agent_service.pptd_store import DocumentStore, RevisionConflict


def fixture():
    doc = {'version': 'v2', 'title': '原始标题', 'size': [960, 540], 'pages': ['pages/stable-a.page'], 'customUnknown': {'keep': ['on', '2026-01-01']}}
    page = {'pageType': 'content', 'elements': [{'elementId': 'title', 'elementType': 'text', 'bounds': [40, 40, 880, 70], 'content': {'text': '保留文字', 'style': {'fontFamily': 'Noto Sans CJK SC', 'fontSize': 28}}, 'unknown': {'nested': [1, 2]}}], 'animations': [{'effect': 'fade-in', 'target': 'title'}]}
    return doc, [{'path': doc['pages'][0], 'data': page}]


class DocumentStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = DocumentStore(self.root)
        self.doc, self.pages = fixture()
    def tearDown(self):
        self.tmp.cleanup()
    def create(self):
        return self.store.commit('project-a', self.doc, self.pages, '初稿')
    def test_commit_and_edit_keep_authority_fields_and_stable_paths(self):
        first = self.create()
        source = self.store.source_path('project-a')
        original_entry = source.read_bytes()
        result = self.store.edit('project-a', {'page': self.doc['pages'][0], 'elementId': 'title', 'text': '已修改'}, first['revision'])
        self.assertEqual(result['revision'], 2)
        self.assertEqual(source, self.store.source_path('project-a'))
        self.assertEqual(source.read_bytes(), original_entry)
        self.assertEqual(result['document'], self.doc)
        expected = deepcopy(self.pages)
        expected[0]['data']['elements'][0]['content']['text'] = '已修改'
        self.assertEqual(result['pages'], expected)
        self.assertEqual(result['versions'][0]['label'], '初稿')
        self.assertEqual(self.store.read('project-a'), result)
    def test_stale_browser_cannot_overwrite_new_edit(self):
        self.create()
        self.store.edit('project-a', {'pageIndex': 0, 'elementId': 'title', 'text': '新值'}, 1)
        with self.assertRaises(RevisionConflict):
            DocumentStore(self.root).edit('project-a', {'pageIndex': 0, 'elementId': 'title', 'text': '旧值'}, 1)
        self.assertEqual(self.store.read('project-a')['pages'][0]['data']['elements'][0]['content']['text'], '新值')
    def test_restore_creates_new_revision_and_keeps_all_versions(self):
        first = self.create()
        version = first['versions'][0]['id']
        self.store.edit('project-a', {'pageIndex': 0, 'elementId': 'title', 'bounds': [20, 30, 400, 100]}, 1)
        restored = self.store.restore('project-a', version, 2)
        self.assertEqual(restored['revision'], 3)
        self.assertEqual(restored['pages'], self.pages)
        self.assertEqual(len(restored['versions']), 3)
    def test_snapshot_and_bundle_are_complete_and_immutable(self):
        self.store.commit('project-a', self.doc, self.pages, '含资源', assets={'media/chart.bin': b'media bytes'})
        snapshot = self.root / 'snapshot'
        state = self.store.snapshot('project-a', snapshot)
        self.store.edit('project-a', {'pageIndex': 0, 'elementId': 'title', 'text': '另一个版本'}, 1)
        self.assertEqual(state['revision'], 1)
        self.assertIn('保留文字', (snapshot / 'pages/stable-a.page').read_text())
        self.assertEqual((snapshot / 'media/chart.bin').read_bytes(), b'media bytes')
        output = self.root / 'source.zip'
        self.store.bundle('project-a', output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), {'document.pptd', 'pages/stable-a.page', 'media/chart.bin'})
            self.assertNotIn('revision', archive.read('document.pptd').decode())
    def test_failed_publication_keeps_previous_complete_version(self):
        self.create()
        with patch('agent_service.pptd_store.os.replace', side_effect=OSError('simulated full disk')):
            with self.assertRaises(OSError):
                self.store.edit('project-a', {'pageIndex': 0, 'elementId': 'title', 'text': '失败修改'}, 1)
        self.assertEqual(self.store.read('project-a')['revision'], 1)
        self.assertEqual(self.store.read('project-a')['pages'], self.pages)
    def test_paths_symlinks_and_duplicate_elements_are_rejected(self):
        for project in ['../other', '/tmp/other', 'a/b']:
            with self.assertRaises(ValueError): self.store.exists(project)
        bad_doc = {**self.doc, 'pages': ['../outside.page']}
        with self.assertRaises(ValueError): self.store.commit('project-a', bad_doc, [{'path': '../outside.page', 'data': self.pages[0]['data']}], 'bad')
        bad_pages = deepcopy(self.pages)
        bad_pages[0]['data']['elements'].append(deepcopy(bad_pages[0]['data']['elements'][0]))
        with self.assertRaises(ValueError): self.store.commit('project-a', self.doc, bad_pages, 'bad')
        with self.assertRaises(ValueError): self.store.commit('project-a', self.doc, self.pages, 'bad', assets={'../escape': b'bad'})
        self.assertFalse(self.store.exists('project-a'))
    def test_legacy_migration_is_explicit_idempotent_and_preserves_html_as_text(self):
        legacy = {'title': '旧项目', 'slides': [{'id': 'stable-a', 'title': '<script>alert(1)</script>', 'points': ['资料'], 'notes': '讲稿'}]}
        migrated = self.store.migrate_legacy('project-a', legacy)
        self.assertEqual(migrated['document']['version'], 'v2')
        self.assertEqual(migrated['pages'][0]['path'], 'pages/stable-a.page')
        self.assertIn('&lt;script&gt;', migrated['pages'][0]['data']['elements'][0]['content']['text'])
        self.store.edit('project-a', {'pageIndex': 0, 'elementId': 'stable-a-title', 'text': '新内容'}, 1)
        unchanged = self.store.migrate_legacy('project-a', legacy)
        self.assertEqual(unchanged['revision'], 2)
        self.assertEqual(unchanged['pages'][0]['data']['elements'][0]['content']['text'], '新内容')
    def test_legacy_migration_copies_cached_image_and_attribution(self):
        image_id = 'a' * 64
        (self.root / 'images').mkdir()
        from PIL import Image
        Image.new('RGB', (80, 40), 'red').save(self.root / 'images' / (image_id + '.jpg'))
        state = self.store.migrate_legacy('project-a', {'title': '含图', 'slides': [{'id': 'one', 'title': '标题', 'points': ['正文'], 'image': {'assetId': image_id, 'sourceUrl': 'https://example.org/photo', 'title': '参考配图', 'author': '作者', 'license': 'CC BY'}}]})
        image = next(e for e in state['pages'][0]['data']['elements'] if e['elementType'] == 'image')
        self.assertEqual(image['src'], 'media/' + image_id + '.jpg')
        self.assertTrue(self.store.resource_path('project-a', image['src']).is_file())
        self.assertIn('https://example.org/photo', state['pages'][0]['data']['notes'])
    def test_missing_legacy_image_is_explicit_failure(self):
        with self.assertRaisesRegex(ValueError, '图片'):
            self.store.migrate_legacy('project-a', {'slides': [{'id': 'one', 'title': '标题', 'image': {'assetId': 'b' * 64}}]})
        self.assertFalse(self.store.exists('project-a'))

if __name__ == '__main__': unittest.main()
