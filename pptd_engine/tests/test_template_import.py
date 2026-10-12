"""Company-template import: standard Office objects must survive the round trip.

These cover the objects a real corporate deck uses that the bounded importer
originally rejected or silently dropped: group shapes, freeform artwork,
doughnut charts with per-slice colors, table cell borders and gradient fills.
"""
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE
from pptx.util import Pt

from pptd_engine.pptx import Exporter, Importer, custgeom_to_svg, svg_path_to_custgeom
from pptd_engine.document import load_yaml

P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"


def xml(tag, namespace=A, **attributes):
    return etree.fromstring(
        f'<a:{tag} xmlns:a="{namespace[1:-1]}"'
        + "".join(f' {name}="{value}"' for name, value in attributes.items())
        + "/>"
    )


def build_group(slide, child, *, offset, extent, child_extent):
    """Wrap an existing shape element in an Office group with an explicit map."""
    group = etree.SubElement(slide.shapes._spTree, P + "grpSp")
    non_visual = etree.SubElement(group, P + "nvGrpSpPr")
    etree.SubElement(non_visual, P + "cNvPr", id="900", name="Group 1")
    for tag in ("cNvGrpSpPr", "nvPr"):
        etree.SubElement(non_visual, P + tag)
    xfrm = etree.SubElement(etree.SubElement(group, P + "grpSpPr"), A + "xfrm")
    etree.SubElement(xfrm, A + "off", x=str(offset[0]), y=str(offset[1]))
    etree.SubElement(xfrm, A + "ext", cx=str(extent[0]), cy=str(extent[1]))
    etree.SubElement(xfrm, A + "chOff", x="0", y="0")
    etree.SubElement(xfrm, A + "chExt", cx=str(child_extent[0]), cy=str(child_extent[1]))
    group.append(child)


class TemplateImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.prs = Presentation()
        self.prs.slide_width, self.prs.slide_height = Pt(960), Pt(540)

    def tearDown(self):
        self.tmp.cleanup()

    def slide(self):
        return self.prs.slides.add_slide(self.prs.slide_layouts[6])

    def import_document(self):
        path = self.root / "source.pptx"
        self.prs.save(path)
        report = Importer(path, self.root / "pptd").run()
        document = load_yaml(self.root / "pptd" / "doc.pptd")
        pages = [load_yaml(self.root / "pptd" / page) for page in document["pages"]]
        self.assertEqual([item for item in report["objects"] if item["status"] == "unsupported"], [])
        return report, pages

    def export(self):
        report = Exporter(self.root / "pptd" / "doc.pptd", self.root / "out", allow_vector=True).run()
        self.assertEqual(report["unsupported"], [])
        return ZipFile(self.root / "out" / "export.pptx")

    def elements(self, pages, **match):
        for page in pages:
            for element in page["elements"]:
                if all(element.get(key) == value for key, value in match.items()):
                    return element
        raise AssertionError(f"no element matching {match}")

    def test_group_shapes_are_flattened_into_slide_coordinates(self):
        slide = self.slide()
        shape = slide.shapes.add_shape(MSO_AUTO_SHAPE_TYPE.RECTANGLE, Pt(10), Pt(20), Pt(50), Pt(30))
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor.from_string("112233")
        element = shape._element
        slide.shapes._spTree.remove(element)
        # A 2x group: child (10,20)+50x30 pt maps to (120,240)+100x60 pt.
        build_group(slide, element, offset=(Pt(100), Pt(200)), extent=(Pt(200), Pt(100)),
                    child_extent=(Pt(100), Pt(50)))

        report, pages = self.import_document()
        artwork = self.elements(pages, elementType="shape")
        self.assertEqual([round(value, 3) for value in artwork["bounds"]], [120.0, 240.0, 100.0, 60.0])
        self.assertEqual(artwork["fill"], {"type": "solid", "color": "#112233"})
        self.assertIn("flattened", " ".join(report["notes"]))

    def test_freeform_artwork_becomes_an_editable_custom_shape(self):
        slide = self.slide()
        builder = slide.shapes.build_freeform(Pt(0), Pt(0), scale=Pt(1))
        builder.add_line_segments([(Pt(40), Pt(0)), (Pt(40), Pt(30)), (Pt(0), Pt(30))], close=True)
        shape = builder.convert_to_shape()
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor.from_string("E7201A")

        _, pages = self.import_document()
        artwork = self.elements(pages, shapeName="custom")
        self.assertRegex(artwork["path"], r"^M [\d.]+ [\d.]+ L")
        self.assertEqual(artwork["path"].count("Z"), 1)
        self.assertEqual(artwork["fill"], {"type": "solid", "color": "#E7201A"})

        archive = self.export()
        geometry = etree.fromstring(archive.read("ppt/slides/slide1.xml")).find(".//" + A + "custGeom")
        self.assertIsNotNone(geometry)
        self.assertTrue(geometry.findall(".//" + A + "moveTo"))
        self.assertTrue(geometry.findall(".//" + A + "close"))

    def test_svg_path_parser_keeps_every_subpath_and_curve(self):
        # An outer contour plus a counter-clockwise hole, with smooth and arc
        # commands, is the documented multi-subpath custom shape form.
        geometry = svg_path_to_custgeom("M0,0 A10,10 0 1 1 20,0 Z M5,5 C6,6 7,7 8,8 S9,9 10,10 Z", [20, 20])

        class Recorder:
            def issue(self, *arguments):
                raise AssertionError("unexpected report issue")

        view_box, restored = custgeom_to_svg(geometry, "test", Recorder())
        self.assertEqual(view_box, [20.0, 20.0])
        self.assertEqual(restored.count("Z"), 2)
        self.assertIn("C", restored)

    def test_doughnut_chart_keeps_hole_slice_colors_and_value_labels(self):
        slide = self.slide()
        data = CategoryChartData()
        data.categories = ["Oracle", "达梦", "MySQL"]
        data.add_series("系统数量", (2, 2, 1))
        chart = slide.shapes.add_chart(XL_CHART_TYPE.DOUGHNUT_EXPLODED, Pt(580), Pt(128), Pt(330), Pt(250), data).chart
        plot = chart._chartSpace.find(".//" + C + "doughnutChart")
        # python-pptx already writes the plot's own hole size; edit it in place.
        plot.find(".//" + C + "holeSize").set("val", "55")
        series = chart.plots[0].series[0]
        for index, value in enumerate(("E7211A", "C3050F", "8B98A8")):
            point = xml("dPt", C)
            point.append(xml("idx", C, val=str(index)))
            point.append(etree.fromstring(
                f'<c:spPr xmlns:c="{C[1:-1]}" xmlns:a="{A[1:-1]}"><a:solidFill>'
                f'<a:srgbClr val="{value}"/></a:solidFill></c:spPr>'))
            series._element.append(point)
        labels = xml("dLbls", C)
        labels.append(xml("showVal", C, val="1"))
        labels.append(xml("showPercent", C, val="0"))
        series._element.append(labels)

        _, pages = self.import_document()
        chart_element = self.elements(pages, elementType="chart")["series"][0]
        self.assertEqual(chart_element["type"], "pie")
        self.assertEqual(chart_element["innerRadius"], 0.55)
        self.assertEqual(chart_element["fill"], ["#E7211A", "#C3050F", "#8B98A8"])
        self.assertEqual(chart_element["dataLabels"], {"show": True, "content": "value"})

        archive = self.export()
        part = next(name for name in archive.namelist() if name.startswith("ppt/charts/chart"))
        exported = etree.fromstring(archive.read(part))
        self.assertEqual(exported.find(".//" + C + "holeSize").get("val"), "55")
        self.assertEqual(len(exported.findall(".//" + C + "dPt")), 3)
        self.assertEqual(exported.find(".//" + C + "showVal").get("val"), "1")

    def test_table_cell_borders_survive_as_explicit_drawingml(self):
        slide = self.slide()
        cell = slide.shapes.add_table(1, 1, Pt(50), Pt(50), Pt(200), Pt(60)).table.cell(0, 0)
        cell.text = "表头"
        properties = cell._tc.get_or_add_tcPr()
        for side in ("lnL", "lnR", "lnT", "lnB"):
            properties.insert(0, etree.fromstring(
                f'<a:{side} xmlns:a="{A[1:-1]}" w="25400"><a:solidFill>'
                f'<a:srgbClr val="000000"/></a:solidFill></a:{side}>'))

        _, pages = self.import_document()
        border = self.elements(pages, elementType="table")["rows"][0][0]["border"]
        self.assertEqual([item["width"] for item in border], [2.0, 2.0, 2.0, 2.0])
        self.assertEqual([item["color"] for item in border], ["#000000"] * 4)

        archive = self.export()
        exported = etree.fromstring(archive.read("ppt/slides/slide1.xml"))
        properties = exported.findall(".//" + A + "tcPr")
        self.assertTrue(properties)
        names = [etree.QName(child).localname for child in properties[0]]
        self.assertEqual([name for name in names if name.startswith("ln")], ["lnL", "lnR", "lnT", "lnB"])

    def test_gradient_fill_round_trips_as_native_drawingml(self):
        slide = self.slide()
        shape = slide.shapes.add_shape(MSO_AUTO_SHAPE_TYPE.RECTANGLE, Pt(40), Pt(330), Pt(880), Pt(66))
        shape.fill.gradient()
        stops = shape.fill.gradient_stops
        stops[0].color.rgb = RGBColor.from_string("FE4444")
        stops[0].position = 0.0
        stops[1].color.rgb = RGBColor.from_string("83A0FF")
        stops[1].position = 1.0

        _, pages = self.import_document()
        fill = self.elements(pages, elementType="shape")["fill"]
        self.assertEqual(fill["type"], "gradient")
        self.assertEqual([stop["color"] for stop in fill["stops"]], ["#FE4444", "#83A0FF"])

        archive = self.export()
        gradient = etree.fromstring(archive.read("ppt/slides/slide1.xml")).find(".//" + A + "gradFill")
        self.assertIsNotNone(gradient)
        self.assertEqual([stop.get("pos") for stop in gradient.findall(".//" + A + "gs")], ["0", "100000"])


if __name__ == "__main__":
    unittest.main()
