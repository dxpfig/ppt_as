#!/usr/bin/env python3
"""Independent structural/data assertions for the bounded two-page PPTX probe.

Run after pptx_probe.py. Checks OOXML/XLSX and the independent existing deck,
not screenshots or a claim of visual fidelity. Accepts an edited fixture copy.
"""

import argparse
from collections import Counter
from html.parser import HTMLParser
from io import BytesIO
import json
import re
from pathlib import Path
from zipfile import ZipFile

from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
import yaml


NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
      "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class Plain(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.parts = []
        self.in_p = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag == "p":
            self.in_p = True

    def handle_data(self, text):
        if text.strip() or self.in_p:
            self.parts.append(text)

    def handle_endtag(self, tag):
        if tag == "p":
            self.parts.append("\n")
            self.in_p = False

    def value(self):
        return "".join(self.parts).rstrip("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("experiments/pptd-v2/fixture/probe.pptd"))
    parser.add_argument("--output", type=Path, default=Path("runtime/pptd-v2-spike/pptx"))
    parser.add_argument("--import-source", type=Path, default=Path("runtime/test-results/design-layouts/presentation.pptx"))
    args = parser.parse_args()
    doc = yaml.safe_load(args.input.read_text())
    pages = [yaml.safe_load((args.input.parent / ref).read_text()) for ref in doc["pages"]]
    checks = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    pptx_path = args.output / "export.pptx"
    check("source is a v2 two-page fixture", doc["version"] == "v2" and len(pages) == 2)
    with ZipFile(pptx_path) as z:
        slides = [etree.fromstring(z.read(f"ppt/slides/slide{i + 1}.xml")) for i in range(2)]
        check("native table (not page image)", len(slides[0].xpath("//a:tbl", namespaces=NS)) == 1)
        check("native chart relationship (not page image)", len(slides[1].xpath("//c:chart", namespaces=NS)) == 1)
        check("only the source picture is raster", sum(len(s.xpath("//p:pic", namespaces=NS)) for s in slides) == 1)
        for index, page in enumerate(pages):
            source_texts = [e for e in page["elements"] if e["elementType"] == "text"]
            native_texts = slides[index].xpath("//p:sp[p:txBody/a:p/a:r/a:t]", namespaces=NS)
            check(f"slide {index + 1} native text object count", len(native_texts) == len(source_texts))
            for e in source_texts:
                shapes = [s for s in native_texts if s.xpath("string(p:nvSpPr/p:cNvPr/@name)", namespaces=NS) == e["elementId"]]
                check(f"text identity {index + 1}/{e['elementId']}", len(shapes) == 1)
                native_text = "\n".join("".join(p.xpath("a:r/a:t/text()", namespaces=NS)) for p in shapes[0].xpath("p:txBody/a:p", namespaces=NS))
                check(f"saved text content {index + 1}/{e['elementId']}", native_text == re.sub(r"\\\(.*?\\\)", "", Plain(e["content"]["text"]).value(), flags=re.S))
                pos = shapes[0].xpath("p:spPr/a:xfrm/a:off", namespaces=NS)[0]
                check(f"saved coordinates {index + 1}/{e['elementId']}", int(pos.get("x")) == int(e["bounds"][0] * 12700) and int(pos.get("y")) == int(e["bounds"][1] * 12700))
        check("semantic bold preserved as run formatting", "中文富文本" in slides[0].xpath("//a:r[a:rPr/@b='1']/a:t/text()", namespaces=NS))
        check("inline theme color resolved", len(slides[0].xpath("//a:r[a:t='主题色']/a:rPr/a:solidFill/a:srgbClr[@val='166854']", namespaces=NS)) == 1)
        check("explicit shape effects prevent unintended theme shadows", all(s.xpath("p:spPr/a:effectLst", namespaces=NS) for slide in slides for s in slide.xpath("//p:sp | //p:pic | //p:cxnSp", namespaces=NS)))
        check("theme effect references explicitly disabled for LibreOffice", all(v == "0" for slide in slides for v in slide.xpath("//p:style/a:effectRef/@idx", namespaces=NS)))
        crop = slides[0].xpath("//p:pic/p:blipFill/a:srcRect", namespaces=NS)[0]
        check("native positive crop equals 12.5% on each side", crop.get("l") == crop.get("r") == "12500" and int(crop.get("t", "0")) == int(crop.get("b", "0")) == 0)
        table = slides[0].xpath("//a:tbl", namespaces=NS)[0]
        expected_table = next(e for e in pages[0]["elements"] if e["elementType"] == "table")
        check("native table dimensions and data", len(table.xpath("a:tr", namespaces=NS)) == 3 and len(table.xpath("a:tblGrid/a:gridCol", namespaces=NS)) == 2 and table.xpath("a:tr/a:tc/a:txBody/a:p/a:r/a:t/text()", namespaces=NS) == [c["text"] for row in expected_table["rows"] for c in row])
        check("table vertical anchor on actual cells", table.xpath("a:tr/a:tc/a:tcPr/@anchor", namespaces=NS) == ["ctr"] * 6)
        chart = etree.fromstring(z.read("ppt/charts/chart1.xml"))
        check("editable bar chart with exact category/value cache", chart.xpath("//c:cat//c:pt/c:v/text()", namespaces=NS) == ["Q1", "Q2", "Q3"] and [float(v) for v in chart.xpath("//c:val//c:pt/c:v/text()", namespaces=NS)] == [12, 18, 15])
        workbook = next(n for n in z.namelist() if n.startswith("ppt/embeddings/") and n.endswith(".xlsx"))
        with ZipFile(BytesIO(z.read(workbook))) as wb:
            sheet = etree.fromstring(wb.read("xl/worksheets/sheet1.xml"))
            nums = [float(c.xpath("string(s:v)", namespaces=NS)) for c in sheet.xpath("//s:c", namespaces=NS) if c.get("t") != "s"]
            check("embedded workbook has editable numeric source data", nums == [12, 18, 15])
        check("animation timing exported", any(s.xpath("//p:timing", namespaces=NS) for s in slides))
        check("native equations exported", any(s.xpath("//*[local-name()='oMath']") for s in slides))
        check("actual embedded fonts present", any(name.startswith("ppt/fonts/") for name in z.namelist()))
        check("font names and font embedding", "JetBrains Mono" in slides[0].xpath("//a:latin/@typeface", namespaces=NS) and any(n.startswith("ppt/fonts/") for n in z.namelist()))

    report = json.loads((args.output / "export-report.json").read_text())
    features = {e["feature"] for e in report["unsupported"]}
    check("advanced features implemented; table border gap still explicit", "border" in features and not ({"customFonts", "animations", "LaTeX", "element-export"} & features))
    check("all supported fixture objects exported natively", report["summary"] == {"native-object": 14})
    imported_dir = args.output / "imported"
    imported = yaml.safe_load((imported_dir / "doc.pptd").read_text())
    source = Presentation(args.import_source)
    check("independent existing PPTX import has six page references", len(source.slides) == len(imported["pages"]) == 6 and args.import_source.resolve() != pptx_path.resolve())
    counts = Counter()
    for index, ref in enumerate(imported["pages"]):
        page_path = (imported_dir / ref).resolve()
        check(f"import page {index + 1} is self-contained", page_path.is_relative_to(imported_dir.resolve()) and page_path.suffix == ".page")
        page = yaml.safe_load(page_path.read_text())
        ids = [e["elementId"] for e in page["elements"]]
        check(f"import page {index + 1} has unique elementIds", len(ids) == len(set(ids)))
        indexed = {e["elementId"]: e for e in page["elements"]}
        for shape in source.slides[index].shapes:
            e = indexed[f"s{shape.shape_id}"]
            check(f"import bounds {index + 1}/{shape.shape_id}", e["bounds"] == [shape.left.pt, shape.top.pt, shape.width.pt, shape.height.pt])
            if shape.has_text_frame:
                check(f"import retains all text and line boundaries {index + 1}/{shape.shape_id}", Plain(e["content"]["text"]).value() == shape.text.replace("\v", "\n"))
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                image_path = (imported_dir / e["src"]).resolve()
                check("import picture remains original media", image_path.is_relative_to(imported_dir.resolve()) and image_path.read_bytes() == shape.image.blob)
        counts.update(e["elementType"] for e in page["elements"])
    check("existing deck imports actual object inventory", counts == {"text": 63, "line": 29, "image": 1})
    result = {"passed": len(checks), "checks": checks, "limits": "Structural/content checks only; visual fidelity requires independent rendering. No official Kimi checker or renderer is used."}
    (args.output / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"PASS: {len(checks)} structural/content assertions; {pptx_path}")


if __name__ == "__main__":
    main()
