"""Template extraction step 2: the rendered overview the design is read from."""
import json
from pathlib import Path
import re
import tempfile
import unittest

from PIL import Image
from pptx import Presentation
from pptx.util import Pt

from agent_service import template_spec


def sample(path: Path) -> Path:
    prs = Presentation()
    for index in range(3):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.shapes.add_textbox(Pt(60), Pt(60), Pt(800), Pt(60)).text_frame.text = f'第 {index + 1} 页'
    prs.save(path)
    return path


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_montage_labels_every_page_and_scales_to_one_width(self):
        thumbs = [Image.new('RGB', (960, 540), '#ffffff') for _ in range(5)]
        sheet = template_spec._montage(thumbs, 4)
        self.assertEqual(sheet.width, 4 * template_spec.THUMB_WIDTH + 5 * template_spec.MARGIN)
        rows = 2
        self.assertEqual(sheet.height,
                         rows * (round(540 * template_spec.THUMB_WIDTH / 960) + template_spec.CAPTION)
                         + (rows + 1) * template_spec.MARGIN)

    def test_rendered_overview_lists_pages_and_the_sheet(self):
        source = sample(self.root / 'deck.pptx')
        try:
            import asyncio
            result = asyncio.run(template_spec.overview(source, self.root / 'overview', columns=3))
        except template_spec.OverviewUnavailable as error:
            self.skipTest(f'渲染器不可用：{error}')
        self.assertEqual(result['pageCount'], 3)
        self.assertEqual(result['pages'], ['page-01.png', 'page-02.png', 'page-03.png'])
        sheet = self.root / 'overview' / result['overview']
        self.assertTrue(sheet.is_file())
        self.assertGreater(sheet.stat().st_size, 1000)

    def test_missing_source_is_reported_not_crashed(self):
        import asyncio
        with self.assertRaises(template_spec.OverviewUnavailable):
            asyncio.run(template_spec.overview(self.root / 'nope.pptx', self.root / 'out'))

    def test_model_attachments_put_the_sheet_first(self):
        folder = self.root / 'overview'
        folder.mkdir()
        (folder / 'overview.png').write_bytes(b'x')
        for index in range(1, 9):
            (folder / f'page-{index:02d}.png').write_bytes(b'x')
        attachments = template_spec.read_overview(folder)
        self.assertEqual(attachments[0]['role'], 'template-overview')
        self.assertEqual(len(attachments), 7)  # sheet + six key pages

    def test_tokens_read_the_design_the_pages_paint(self):
        def text(identity, value, size, color, y, x=40):
            return {'elementId': identity, 'elementType': 'text', 'bounds': [x, y, 600, 30],
                    'content': {'text': f'<p><span style="font-family:微软雅黑;font-size:{size}px;color:{color}">{value}</span></p>'}}

        pages = [
            {'elements': [text('cover', '申购汇报', 66, '#FFFFFF', 200)]},
            {'elements': [text('t2', '一、必要性', 30, '#E50011', 20),
                          text('b2', '正文内容', 13, '#000000', 120),
                          text('f2', '公司名', 12, '#333333', 500)]},
            {'elements': [text('t3', '二、技术路线', 30, '#E50011', 20),
                          text('b3', '正文内容', 13, '#000000', 120),
                          text('f3', '公司名', 12, '#333333', 500)]},
            {'elements': [text('t4', '三、预算', 30, '#E50011', 20),
                          text('b4', '正文内容', 13, '#000000', 120),
                          text('f4', '公司名', 12, '#333333', 500)]},
            {'elements': [text('closing', '请领导讲话', 100, '#FFFFFF', 200)]},
        ]
        result = template_spec.tokens(pages)
        self.assertEqual(result['textStyles']['page-title']['fontSize'], 30.0)
        self.assertEqual(result['textStyles']['page-title']['color'], '#E50011')
        self.assertEqual(result['textStyles']['body']['fontSize'], 13.0)
        self.assertEqual(result['textStyles']['footer']['fontSize'], 12.0)
        self.assertEqual(result['colors']['primary'], '#E50011')
        self.assertEqual(result['metrics']['leftMargin'], 40.0)

    def test_components_keep_only_repeated_parts(self):
        def shape(identity, name, width, height):
            return {'elementId': identity, 'elementType': 'shape', 'shapeName': name,
                    'bounds': [10, 10, width, height]}
        pages = [{'elements': [shape(f'c{index}', 'roundRect', 163, 300)]} for index in range(3)]
        pages.append({'elements': [shape('once', 'star5', 80, 80)]})
        found = template_spec.components(pages)
        self.assertEqual([item['name'] for item in found], ['roundRect'])
        self.assertEqual(found[0]['uses'], 3)
        self.assertEqual(found[0]['size'], [163, 300])
        self.assertNotIn('star5', [item['name'] for item in found])

    def test_spec_schema_and_persistence(self):
        pass
        valid = {'summary': '一份企业模板', 'pageTypes': [
            {'id': 'cover', 'purpose': '开场', 'templatePages': [1], 'skeleton': '满版背景 + 大标题',
             'slots': [{'name': 'title', 'purpose': '标题', 'characters': 20, 'repeatable': False}]}],
            'components': [{'id': 'logo', 'kind': 'image', 'description': '右上角标识', 'evidence': [2, 3]}],
            'rules': ['标题 30px #E50011'], 'prohibitions': ['不使用蓝色']}
        spec = template_spec.TemplateSpec.model_validate(valid, strict=True)
        self.assertEqual(spec.pageTypes[0].slots[0].characters, 20)
        with self.assertRaises(ValueError):
            template_spec.TemplateSpec.model_validate({'summary': 'x', 'pageTypes': []}, strict=True)
        with self.assertRaises(ValueError):
            template_spec.TemplateSpec.model_validate(
                {**valid, 'pageTypes': [{**valid['pageTypes'][0], 'unknown': 1}]}, strict=True)
        folder = self.root / 'spec'
        folder.mkdir()
        (folder / 'spec.json').write_text(json.dumps(valid, ensure_ascii=False), encoding='utf-8')
        self.assertEqual(template_spec.load_spec(folder)['summary'], '一份企业模板')
        self.assertIsNone(template_spec.load_spec(self.root / 'missing'))

    def test_repeating_unit_grows_shrinks_and_renumbers(self):
        def row(index, label):
            top = 160 + index * 44
            return [
                {'elementId': f'badge{index}', 'elementType': 'shape', 'shapeName': 'roundRect',
                 'bounds': [288, top, 62, 34], 'fill': {'type': 'solid', 'color': '#E60013'}},
                {'elementId': f'badge{index}-text', 'elementType': 'text', 'bounds': [288, top, 62, 34],
                 'content': {'text': f'<p><span style="font-size:26.0px">{index + 1:02d}</span></p>'}},
                {'elementId': f'label{index}', 'elementType': 'text', 'bounds': [354, top + 2, 104, 29],
                 'content': {'text': f'<p><span style="font-size:13.0px">条目{index + 1}</span></p>'}},
            ]
        page = {'elements': [item for index in range(4) for item in row(index, '')]
                + [{'elementId': 'title', 'elementType': 'text', 'bounds': [428, 88, 123, 56],
                    'content': {'text': '<p><span style="font-size:40.0px">目 录</span></p>'}}]}

        def numbers(value):
            return [re.sub(r'<[^>]+>', '', element['content']['text']) for element in value['elements']
                    if element.get('elementType') == 'text' and re.fullmatch(r'\d{2}', re.sub(r'<[^>]+>', '', element['content']['text']))]

        self.assertEqual(len(template_spec.repeat_units(page)['units']), 4)
        grown = template_spec.expand_repeats(page, 6)
        self.assertEqual(numbers(grown), ['01', '02', '03', '04', '05', '06'])
        self.assertEqual(len(grown['elements']), len(page['elements']) + 6)
        self.assertEqual([element for element in grown['elements'] if element['elementId'] == 'title'],
                         [element for element in page['elements'] if element['elementId'] == 'title'])
        shrunk = template_spec.expand_repeats(page, 2)
        self.assertEqual(numbers(shrunk), ['01', '02'])
        filled = template_spec.fill_slots(grown, {'repeat': ['一', '二', '三', '四', '五', '六']})
        self.assertIn('六', ' '.join(re.sub(r'<[^>]+>', '', element['content']['text'])
                                    for element in filled['elements'] if element.get('elementType') == 'text'))


if __name__ == '__main__':
    unittest.main()
