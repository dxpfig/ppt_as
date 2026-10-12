from pathlib import Path
import tempfile
import unittest
from pptd_engine.fonts import document_font_usage, FontResolver

class FontUsageTests(unittest.TestCase):
    def test_actual_inherited_runs_and_formula_text_are_separate(self):
        doc={'theme':{'textStyles':{'body':{'fontFamily':{'latin':'JetBrains Mono','ea':'Noto Sans CJK SC'},'fontSize':20}}}}
        pages=[{'elements':[{'elementType':'text','content':{'style':'$body','text':r'中文 ABC <strong>123</strong> \(\frac{x}{y}\)'}}]}]
        uses=document_font_usage(doc,pages)
        self.assertEqual(uses[('JetBrains Mono',400,False)],set(' ABC '))
        self.assertEqual(uses[('Noto Sans CJK SC',400,False)],set('中文'))
        self.assertEqual(uses[('JetBrains Mono',700,False)],set('123'))

    def test_table_theme_font_and_inline_override(self):
        doc={'theme':{'tableStyles':{'t':{'cellStyle':{'fontFamily':'JetBrains Mono'}}}}}
        pages=[{'elements':[{'elementType':'table','style':'$t','columnWidths':[1],'rowHeights':[1],'rows':[[{'text':'中文'}]]}]}]
        self.assertEqual(document_font_usage(doc,pages)[('JetBrains Mono',400,False)],set('中文'))

    def test_formula_color_preserves_alpha_at_export_boundary(self):
        from pptd_engine.pptx import Exporter,ROOT
        source=ROOT/'experiments/pptd-v2/advanced-fixture/advanced.pptd'
        with tempfile.TemporaryDirectory() as d:
            exporter=Exporter(source,Path(d));slide=exporter.prs.slides.add_slide(exporter.prs.slide_layouts[6])
            shape=exporter.element(slide,{'elementId':'math','elementType':'text','bounds':[0,0,200,60],'content':{'text':r'\(x\)','color':'#12345680'}},'p#math')
            self.assertEqual(shape._element.xpath('.//*[local-name()="oMath"]//a:alpha/@val'),['50196'])
            self.assertEqual(shape._element.xpath('.//a:defRPr/a:solidFill/a:srgbClr/a:alpha/@val'),['50196'])

if __name__=='__main__':unittest.main()
