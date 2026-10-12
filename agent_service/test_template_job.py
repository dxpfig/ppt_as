"""The template route keeps the company deck and changes only its words."""
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

from pptx import Presentation
from pptx.util import Pt

from agent_service import template_job as jobs


def sample(path: Path) -> Path:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Pt(960), Pt(540)
    cover = prs.slides.add_slide(prs.slide_layouts[6])
    cover.shapes.add_textbox(Pt(60), Pt(180), Pt(800), Pt(80)).text_frame.text = '关于AI服务器的申购汇报'
    cover.shapes.add_textbox(Pt(60), Pt(300), Pt(400), Pt(40)).text_frame.text = 'AI项目组软件团队'
    body = prs.slides.add_slide(prs.slide_layouts[6])
    body.shapes.add_textbox(Pt(60), Pt(60), Pt(800), Pt(60)).text_frame.text = '方向一：公司内部全面AI化建设'
    extra = prs.slides.add_slide(prs.slide_layouts[6])
    extra.shapes.add_textbox(Pt(60), Pt(60), Pt(800), Pt(60)).text_frame.text = '方向二：国产软硬件'
    closing = prs.slides.add_slide(prs.slide_layouts[6])
    closing.shapes.add_textbox(Pt(60), Pt(240), Pt(800), Pt(60)).text_frame.text = '请领导讲话！'
    prs.save(path)
    return path


def request(**kwargs):
    values = dict(prompt='做一份 AI 服务器申购汇报', audience='公司领导', pageCount=3, references='',
                  scenario='management-report', project=SimpleNamespace(id='template-job-test'))
    values.update(kwargs)
    return SimpleNamespace(**values)


class TemplateJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.template = sample(self.root / 'template.pptx')

    def tearDown(self):
        self.tmp.cleanup()

    async def run_generate(self, deck, **kwargs):
        async def ask(*arguments, **options):
            if isinstance(deck, Exception):
                raise deck
            return deck
        with patch.object(jobs, 'ask_deck', side_effect=ask):
            return await jobs.generate(request(**kwargs), lambda *a: None,
                                       template_path=self.template, model=object(), folder=self.root / 'out')

    def deck(self):
        return {'title': 'AI服务器申购汇报',
                'pages': [{'number': 1, 'title': '封面', 'paragraphs': [
                    {'id': 's1#p0', 'text': '2026 年度 AI 服务器申购汇报'},
                    {'id': 's1#p1', 'text': '信息化部 · AI 项目组'}]},
                    {'number': 2, 'title': '必要性', 'paragraphs': [
                        {'id': 's2#p0', 'text': '一、必要性：算力缺口已成业务瓶颈'}]},
                    {'number': 4, 'title': '请示', 'paragraphs': [
                        {'id': 's4#p0', 'text': '请领导批示'}]}]}

    async def test_deck_keeps_the_template_pages_and_replaces_only_named_text(self):
        result = await self.run_generate(self.deck())
        output = Path(result['report']['output'])
        self.assertTrue(output.is_file())
        self.assertEqual(result['report']['pages'], [1, 2, 4])
        info = jobs.inventory(output)
        # The published deck is renumbered in display order.
        self.assertEqual([page['number'] for page in info['pages']], [1, 2, 3])
        texts = [item['text'] for page in info['pages'] for item in page['paragraphs']]
        self.assertIn('2026 年度 AI 服务器申购汇报', texts)
        self.assertIn('请领导批示', texts)
        self.assertNotIn('关于AI服务器的申购汇报', texts)
        # The dropped page is gone and the kept pages keep their original parts.
        with ZipFile(self.template) as before, ZipFile(output) as after:
            self.assertNotIn('ppt/slides/slide3.xml', after.namelist())
            # Everything outside the named slides and the entry/relationships stays
            # byte-identical; only p:sldIdLst and its rels are rewritten for pruning.
            for name in ('ppt/slideMasters/slideMaster1.xml', 'ppt/slideLayouts/slideLayout1.xml',
                         'docProps/app.xml' if 'docProps/app.xml' in before.namelist() else 'ppt/theme/theme1.xml'):
                self.assertEqual(before.read(name), after.read(name), name)
            self.assertIn('请领导批示', after.read('ppt/slides/slide4.xml').decode('utf-8'))

    async def test_unknown_paragraph_is_repaired_before_any_write(self):
        broken = self.deck()
        broken['pages'][1]['paragraphs'][0]['id'] = 's9#p9'
        with patch.object(jobs, 'ask_deck', side_effect=[broken, self.deck()]):
            result = await jobs.generate(request(), lambda *a: None,
                                         template_path=self.template, model=object(), folder=self.root / 'out2')
        self.assertEqual(result['report']['pages'], [1, 2, 4])

    async def test_persistent_invalid_deck_stops_without_touching_the_template(self):
        broken = self.deck()
        broken['pages'][0]['number'] = 2
        with self.assertRaises(jobs.TemplateIncomplete) as caught:
            await self.run_generate(broken)
        self.assertTrue(any(issue['code'] == 'CoverRequired' for issue in caught.exception.issues))
        self.assertFalse((self.root / 'out' / 'presentation.pptx').exists())
        self.assertEqual(ZipFile(self.template).read('ppt/slides/slide1.xml'),
                         ZipFile(self.template).read('ppt/slides/slide1.xml'))

    async def test_model_failure_is_reported_not_faked(self):
        with self.assertRaises(jobs.TemplateIncomplete):
            await self.run_generate(ValueError('模型返回内容未通过 JSON 检查'))


if __name__ == '__main__':
    unittest.main()
