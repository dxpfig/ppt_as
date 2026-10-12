"""Template extraction step 2: the rendered overview the design is read from."""
from pathlib import Path
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


if __name__ == '__main__':
    unittest.main()
