"""Page-scoped action contracts."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from agent_service import page_actions
from agent_service.pptd_store import DocumentStore


def source_state():
    pages = []
    for index, text in enumerate(('第一页', '第二页'), 1):
        pages.append({
            'path': f'pages/{index}.page',
            'data': {
                'pageType': 'content',
                'elements': [{
                    'elementId': f'title-{index}',
                    'elementType': 'text',
                    'bounds': [40, 40, 880, 90],
                    'content': {'text': text, 'fontSize': 28},
                }],
            },
        })
    return {
        'document': {
            'version': 'v2',
            'title': '页面操作测试',
            'size': [960, 540],
            'pages': [page['path'] for page in pages],
        },
        'pages': pages,
    }


def request():
    return SimpleNamespace(
        prompt='改成左右双栏',
        scenario='management-report',
        usage='presentation',
        templateId=None,
        outputMode='auto',
        project=SimpleNamespace(
            id='pages-demo',
            templateId=None,
            generationMeta={'scenario': 'management-report'},
        ),
    )


class PageActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = DocumentStore(self.root)
        state = source_state()
        self.committed = self.store.commit(
            'pages-demo', state['document'], state['pages'], '测试初始化'
        )
        rendered_state = self.store.read('pages-demo')
        self.rendered = page_actions.RenderedPage(
            state=rendered_state,
            source_root=self.root,
            image=b'png',
            issues=[],
            layout={},
        )

    def tearDown(self):
        self.tmp.cleanup()

    async def test_review_is_read_only_and_reports_page_index(self):
        with patch.object(page_actions, '_render_page', AsyncMock(return_value=self.rendered)), patch.object(
            page_actions,
            '_review_rendered',
            AsyncMock(return_value={
                'action': 'review',
                'pagePath': 'pages/2.page',
                'revision': self.committed['revision'],
                'status': 'passed',
                'issues': [],
                'observations': [{'pagePath': 'pages/2.page', 'evidence': '页面清晰'}],
                'model': 'fake-vision',
            }),
        ):
            report = await page_actions.review_page(self.store, 'pages-demo', 'pages/2.page', request())
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(self.store.read('pages-demo')['revision'], self.committed['revision'])

    async def test_repair_commits_only_after_candidate_render_and_keeps_other_pages(self):
        candidate = deepcopy(self.rendered.state)
        candidate['pages'][0]['data']['elements'][0]['bounds'] = [40, 60, 880, 110]
        checked = page_actions.RenderedPage(
            state=candidate,
            source_root=self.root,
            image=b'fixed',
            issues=[],
            layout={},
        )
        with patch.object(
            page_actions,
            '_render_page',
            AsyncMock(side_effect=[self.rendered, checked]),
        ), patch.object(
            page_actions,
            '_review_rendered',
            AsyncMock(side_effect=[
                {
                    'issues': [{
                        'pagePath': 'pages/1.page',
                        'elementIds': ['title-1'],
                        'code': 'text-overflow',
                        'message': '标题溢出',
                    }],
                    'observations': [],
                    'model': 'fake-vision',
                },
                {
                    'action': 'repair',
                    'pagePath': 'pages/1.page',
                    'revision': self.committed['revision'] + 1,
                    'status': 'passed',
                    'issues': [],
                    'observations': [],
                    'model': 'fake-vision',
                },
            ]),
        ), patch.object(
            page_actions.repair_coordinator,
            'repair_deck',
            AsyncMock(return_value=candidate),
        ):
            report = await page_actions.repair_page(
                self.store, 'pages-demo', 'pages/1.page', request()
            )
        current = self.store.read('pages-demo')
        self.assertEqual(current['revision'], self.committed['revision'] + 1)
        self.assertEqual(current['pages'][0]['data']['elements'][0]['bounds'], [40, 60, 880, 110])
        self.assertEqual(current['pages'][1], self.committed['pages'][1])
        self.assertEqual(report['status'], 'passed')

    async def test_design_creates_candidate_then_apply_commits_a_new_revision(self):
        candidate = deepcopy(self.rendered.state)
        candidate['pages'][0]['data']['elements'][0]['bounds'] = [80, 70, 800, 120]
        checked = page_actions.RenderedPage(
            state=candidate,
            source_root=self.store._current('pages-demo'),
            image=b'design-preview',
            issues=[],
            layout={},
        )
        self.rendered.source_root = self.store._current('pages-demo')
        review = {
            'action': 'design',
            'pagePath': 'pages/1.page',
            'revision': self.committed['revision'],
            'status': 'passed',
            'issues': [],
            'observations': [{'pagePath': 'pages/1.page', 'evidence': '布局清晰'}],
            'model': 'fake-vision',
        }
        with patch.object(
            page_actions,
            '_render_page',
            AsyncMock(side_effect=[self.rendered, checked]),
        ), patch.object(
            page_actions,
            '_design_candidate',
            AsyncMock(return_value=candidate),
        ), patch.object(
            page_actions,
            '_review_rendered',
            AsyncMock(return_value=review),
        ):
            report = await page_actions.design_page(
                self.store, 'pages-demo', 'pages/1.page', request(), self.root
            )
        self.assertIn('candidate', report)
        self.assertEqual(self.store.read('pages-demo')['revision'], self.committed['revision'])
        folder, metadata = page_actions.read_design_candidate(
            self.root, 'pages-demo', report['candidate']['id']
        )
        self.assertEqual(metadata['status'], 'pending')
        self.assertTrue((folder / 'preview.png').is_file())
        applied = page_actions.apply_design_candidate(
            self.store,
            'pages-demo',
            report['candidate']['id'],
            self.committed['revision'],
        )
        self.assertEqual(applied['revision'], self.committed['revision'] + 1)
        self.assertEqual(
            self.store.read('pages-demo')['pages'][0]['data']['elements'][0]['bounds'],
            [80, 70, 800, 120],
        )

    def test_pending_design_candidate_can_be_discarded(self):
        folder = page_actions.design_candidate_dir(self.root, 'pages-demo', 'candidate1')
        folder.mkdir(parents=True)
        (folder / 'metadata.json').write_text(
            '{"id":"candidate1","projectId":"pages-demo","status":"pending"}',
            encoding='utf-8',
        )
        page_actions.discard_design_candidate(self.store, 'pages-demo', 'candidate1')
        self.assertFalse(folder.exists())

    def test_page_issue_filter_normalizes_existing_renderer_shape(self):
        issues = page_actions._page_issues([
            {'page': 'pages/1.page', 'elementId': 'title-1', 'code': 'text-overflow', 'message': '溢出'},
            {'page': 'pages/2.page', 'elementId': 'title-2', 'code': 'text-overlap', 'message': '重叠'},
        ], 'pages/1.page')
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['pagePath'], 'pages/1.page')
        self.assertEqual(issues[0]['elementIds'], ['title-1'])
