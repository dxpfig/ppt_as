"""Native editable text keeps PPTD character spacing, including theme inheritance."""
import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from lxml import etree
from pptd_engine.pptx import Exporter, NS


class TextSpacingTests(unittest.TestCase):
    def test_theme_spacing_and_direct_override_survive_native_export(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'document.pptd'
            source.write_text(json.dumps({'version':'v2','size':[960,540],'pages':[],
                'theme':{'textStyles':{'kicker':{'fontSize':14,'letterSpacing':1.75}}}}))
            exporter = Exporter(source,root/'out')
            slide = exporter.prs.slides.add_slide(exporter.prs.slide_layouts[6])
            for index, (text, override) in enumerate([('Inherited',{}),('Tight',{'letterSpacing':-.5}),('Reset',{'letterSpacing':0})]):
                exporter.element(slide,{'elementId':str(index),'elementType':'text','bounds':[10,20+index*40,300,35],
                    'content':{'style':'$kicker','text':text,**override}},f'page#{index}')
            output = root/'spacing.pptx'
            exporter.prs.save(output)
            with ZipFile(output) as archive:
                xml = etree.fromstring(archive.read('ppt/slides/slide1.xml'))
            self.assertEqual(xml.xpath('//a:r/a:rPr/@spc',namespaces=NS),['175','-50','0'])
            self.assertEqual(xml.xpath('//a:r/a:t/text()',namespaces=NS),['Inherited','Tight','Reset'])
            self.assertEqual(exporter.report.data['unsupported'],[])

    def test_font_substitution_is_reported_as_a_user_readable_export_note(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'pages').mkdir()
            (root / 'pages/a.page').write_text(json.dumps({
                'pageType': 'content',
                'elements': [{
                    'elementId': 'title',
                    'elementType': 'text',
                    'bounds': [40, 40, 500, 60],
                    'content': {'text': 'Arial sample', 'fontFamily': 'Arial', 'fontSize': 24},
                }],
            }), encoding='utf-8')
            source = root / 'document.pptd'
            source.write_text(json.dumps({
                'version': 'v2',
                'title': 'Font note',
                'size': [960, 540],
                'pages': ['pages/a.page'],
            }), encoding='utf-8')

            report = Exporter(source, root / 'out').run()

            self.assertEqual(report['fontSubstitutions'], [
                '字体替换：Arial 未安装，已改用开源等价字体 Liberation Sans 并随文件内嵌。',
            ])


if __name__ == '__main__': unittest.main()
