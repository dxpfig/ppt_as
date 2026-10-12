"""Native editable line geometry must match the browser's SVG path semantics."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from pptd_engine.pptx import Exporter


class NativeLines(unittest.TestCase):
    def test_polylines_quadratic_and_cubic_paths_keep_authored_control_points(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'document.pptd'
            source.write_text(json.dumps({'version':'v2','title':'Lines','size':[960,540],
                'theme':{},'pages':['one.page']}))
            (root / 'one.page').write_text('{"elements":[]}')
            exporter = Exporter(source, root / 'export')
            slide = exporter.prs.slides.add_slide(exporter.prs.slide_layouts[6])
            for curve, points, tag, expected in [
                ('sharp', '0,1 0.5,0.6 1,0', 'lnTo', [('500000','600000'),('1000000','0')]),
                ('smooth', '0,1 0.5,0.6 1,0', 'quadBezTo', [('500000','600000'),('1000000','0')]),
                ('smooth', '0,1 0.3,0.8 0.7,0.2 1,0', 'cubicBezTo', [('300000','800000'),('700000','200000'),('1000000','0')]),
            ]:
                with self.subTest(curve=curve, tag=tag):
                    shape = exporter.element(slide, {'elementType':'line','elementId':tag,
                        'bounds':[600,210,300,240], 'viewBox':[1,1], 'points':points,
                        'curve':curve,'border':{'width':2,'style':'solid','color':'#123B2F'}}, tag)
                    path = shape._element.xpath('.//a:custGeom/a:pathLst/a:path')[0]
                    self.assertEqual(path.get('fill'), 'none')
                    actual = [(p.get('x'),p.get('y')) for p in shape._element.xpath('.//a:' + tag + '/a:pt')]
                    self.assertEqual(actual, expected)
                    self.assertEqual((shape.left.pt,shape.top.pt,shape.width.pt,shape.height.pt), (600,210,300,240))
            self.assertEqual(exporter.report.data['unsupported'], [])
