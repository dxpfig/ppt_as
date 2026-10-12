#!/usr/bin/env python3
"""PPTD v2 native/editable PPTX exporter and bounded independent importer.

Uses python-pptx's public writing APIs, plus documented DrawingML alpha and
effectRef=0 OOXML patches, and reads OOXML for evidence. Advanced modules provide native math, FA vectors, timing, charts and EOT fonts.
No whole-slide rasterization or source edits.
The shared YAML reader uses YAML 1.2 core scalar rules.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime
from html import escape
from html.parser import HTMLParser
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any
from zipfile import BadZipFile, ZipFile

from .math import append_math, split_math
from .icons import add_icon
from .fonts import FontResolver, ALIASES, embed_fonts, is_east_asian
from . import theme as theme_module

from lxml import etree
from PIL import Image
import pptx
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.dml import MSO_COLOR_TYPE, MSO_FILL_TYPE, MSO_LINE_DASH_STYLE
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE, MSO_CONNECTOR, MSO_SHAPE_TYPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Pt
from pptx.oxml.xmlchemy import OxmlElement
import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = "docs/kimi-ppt/original/reference/pptd.md"
NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
}
PALETTE = ["#1783FE", "#67B8A6", "#C9A869", "#8A84C7", "#8C98A8"]
SHAPES = {s.xml_value: s for s in MSO_AUTO_SHAPE_TYPE if s.xml_value}
TEXT_KEYS = {
    "color", "fontSize", "fontFamily", "bold", "italic", "lineHeight",
    "lineHeightPx", "letterSpacing", "marginTop", "text", "style", "textStyle", "align", "wrap",
}
ALIGN = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT,
         "justify": PP_ALIGN.JUSTIFY, "distributed": PP_ALIGN.DISTRIBUTE}
VALIGN = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE, "bottom": MSO_ANCHOR.BOTTOM}
DASH = {"solid": MSO_LINE_DASH_STYLE.SOLID, "dash": MSO_LINE_DASH_STYLE.DASH,
        "dot": MSO_LINE_DASH_STYLE.ROUND_DOT}
SVG_PATH_TOKEN = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])|([-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?)")
# Office chart type -> (PPTD series type, stack mode, category orientation).
# Horizontal bars keep the same series type but swap the value/category channels,
# which is how PPTD expresses a bar chart whose categories run down the axis.
CHART_IMPORT_TYPES = {
    XL_CHART_TYPE.COLUMN_CLUSTERED: ("bar", None, "columns"),
    XL_CHART_TYPE.COLUMN_STACKED: ("bar", "value", "columns"),
    XL_CHART_TYPE.COLUMN_STACKED_100: ("bar", "percent", "columns"),
    XL_CHART_TYPE.BAR_CLUSTERED: ("bar", None, "bars"),
    XL_CHART_TYPE.BAR_STACKED: ("bar", "value", "bars"),
    XL_CHART_TYPE.BAR_STACKED_100: ("bar", "percent", "bars"),
    XL_CHART_TYPE.LINE: ("line", None, "columns"),
    XL_CHART_TYPE.LINE_MARKERS: ("line", None, "columns"),
    XL_CHART_TYPE.AREA: ("area", None, "columns"),
    XL_CHART_TYPE.PIE: ("pie", None, "columns"),
    XL_CHART_TYPE.PIE_EXPLODED: ("pie", None, "columns"),
    XL_CHART_TYPE.DOUGHNUT: ("pie", None, "columns"),
    XL_CHART_TYPE.DOUGHNUT_EXPLODED: ("pie", None, "columns"),
}


def compact_number(value: float) -> str:
    """Render a path coordinate without trailing zeros or exponent noise."""
    text = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-", "-0"} else text


def custgeom_arc_points(node: Any, current: tuple[float, float], scale_x: float, scale_y: float,
                        loc: str, report: "Report") -> list[tuple[float, float]]:
    """Sample an Office <a:arcTo> into points; PPTD/SVG has no equivalent command."""
    radius_x = abs(float(node.get("wR") or 0)) * scale_x
    radius_y = abs(float(node.get("hR") or 0)) * scale_y
    start = math.radians(float(node.get("stAng") or 0) / 60000)
    swing = math.radians(float(node.get("swAng") or 0) / 60000)
    if radius_x <= 0 or radius_y <= 0 or swing == 0:
        return []
    report.issue(loc, "freeform-arc",
                 "Office <a:arcTo> is sampled into line segments; verify this curve visually.")

    def parametric(angle: float) -> float:
        return math.atan2(radius_x * math.sin(angle), radius_y * math.cos(angle))

    phi = parametric(start)
    center = (current[0] - radius_x * math.cos(phi), current[1] - radius_y * math.sin(phi))
    steps = max(2, min(64, int(abs(swing) / (math.pi / 18)) + 1))
    points = []
    for index in range(1, steps + 1):
        phi = parametric(start + swing * index / steps)
        points.append((center[0] + radius_x * math.cos(phi), center[1] + radius_y * math.sin(phi)))
    return points


def custgeom_to_svg(geometry: Any, loc: str, report: "Report") -> tuple[list[float], str]:
    """Convert an Office <a:custGeom> into PPTD's custom viewBox and SVG path.

    PPTD v2 defines a freeform shape as ``shapeName: custom`` plus a viewBox and
    an SVG path that the renderer stretches onto ``bounds``. Office stores the
    same geometry as DrawingML moveTo/lnTo/quadBezTo/cubicBezTo/arcTo/close
    commands inside one or more <a:path> subpaths, which map onto SVG subpaths.
    """
    tag = "{" + NS["a"] + "}"
    subpaths = geometry.findall(tag + "pathLst/" + tag + "path")
    if not subpaths:
        raise ValueError("Custom geometry has no <a:path>")
    width = float(subpaths[0].get("w") or 0)
    height = float(subpaths[0].get("h") or 0)
    if width <= 0 or height <= 0:
        raise ValueError("Custom geometry path space has no positive extent")
    commands: list[str] = []
    point = (0.0, 0.0)
    start = (0.0, 0.0)
    for subpath in subpaths:
        space_w = float(subpath.get("w") or width)
        space_h = float(subpath.get("h") or height)
        scale_x = width / space_w if space_w > 0 else 1.0
        scale_y = height / space_h if space_h > 0 else 1.0

        def points_of(node: Any) -> list[tuple[float, float]]:
            return [(float(pt.get("x") or 0) * scale_x, float(pt.get("y") or 0) * scale_y)
                    for pt in node.findall(tag + "pt")]

        for node in subpath:
            name = etree.QName(node).localname
            points = points_of(node)
            if name == "moveTo" and points:
                commands.append("M " + " ".join(compact_number(value) for value in points[0]))
                point = start = points[0]
            elif name == "lnTo" and points:
                commands.append("L " + " ".join(compact_number(value) for value in points[-1]))
                point = points[-1]
            elif name == "quadBezTo" and len(points) == 2:
                commands.append("Q " + " ".join(compact_number(value) for pt in points for value in pt))
                point = points[-1]
            elif name == "cubicBezTo" and len(points) == 3:
                commands.append("C " + " ".join(compact_number(value) for pt in points for value in pt))
                point = points[-1]
            elif name == "arcTo":
                sampled = custgeom_arc_points(node, point, scale_x, scale_y, loc, report)
                if sampled:
                    commands.extend("L " + " ".join(compact_number(value) for value in item) for item in sampled)
                    point = sampled[-1]
            elif name == "close":
                commands.append("Z")
                point = start
    return [width, height], " ".join(commands)


def arc_to_cubics(x0: float, y0: float, rx: float, ry: float, rotation: float,
                  large_arc: bool, sweep: bool, x1: float, y1: float) -> list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]]:
    """Convert one SVG elliptical arc into cubic Bezier segments (SVG spec F.6.5)."""
    if rx == 0 or ry == 0:
        return []
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(rotation % 360)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx, dy = (x0 - x1) / 2, (y0 - y1) / 2
    x1p = cos_phi * dx + sin_phi * dy
    y1p = -sin_phi * dx + cos_phi * dy
    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1:
        scale = math.sqrt(lam)
        rx, ry = rx * scale, ry * scale
    numerator = max(0.0, rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p)
    denominator = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coefficient = math.sqrt(numerator / denominator) if denominator else 0.0
    if large_arc == sweep:
        coefficient = -coefficient
    cxp = coefficient * rx * y1p / ry
    cyp = -coefficient * ry * x1p / rx
    cx = cos_phi * cxp - sin_phi * cyp + (x0 + x1) / 2
    cy = sin_phi * cxp + cos_phi * cyp + (y0 + y1) / 2

    def angle(ux: float, uy: float, vx: float, vy: float) -> float:
        norm = math.hypot(ux, uy) * math.hypot(vx, vy)
        value = max(-1.0, min(1.0, (ux * vx + uy * vy) / norm)) if norm else 1.0
        return (-1.0 if ux * vy - uy * vx < 0 else 1.0) * math.acos(value)

    ux, uy = (x1p - cxp) / rx, (y1p - cyp) / ry
    vx, vy = (-x1p - cxp) / rx, (-y1p - cyp) / ry
    theta = angle(1, 0, ux, uy)
    delta = angle(ux, uy, vx, vy)
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi

    def point(angle_value: float) -> tuple[float, float]:
        cos_t, sin_t = math.cos(angle_value), math.sin(angle_value)
        return (cx + rx * cos_phi * cos_t - ry * sin_phi * sin_t,
                cy + rx * sin_phi * cos_t + ry * cos_phi * sin_t)

    def derivative(angle_value: float) -> tuple[float, float]:
        cos_t, sin_t = math.cos(angle_value), math.sin(angle_value)
        return (-rx * cos_phi * sin_t - ry * sin_phi * cos_t,
                -rx * sin_phi * sin_t + ry * cos_phi * cos_t)

    segments = max(1, math.ceil(abs(delta) / (math.pi / 2)))
    step = delta / segments
    result = []
    for index in range(segments):
        alpha = 4 / 3 * math.tan(step / 4)
        begin, end = point(theta + step * index), point(theta + step * (index + 1))
        begin_derivative, end_derivative = derivative(theta + step * index), derivative(theta + step * (index + 1))
        result.append((((begin[0] + alpha * begin_derivative[0]), (begin[1] + alpha * begin_derivative[1])),
                       ((end[0] - alpha * end_derivative[0]), (end[1] - alpha * end_derivative[1])), end))
    return result


def svg_path_to_custgeom(path: str, view_box: Any) -> Any:
    """Build native DrawingML custom geometry from a PPTD SVG path."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Custom shape requires a non-empty SVG path")
    try:
        width, height = float(view_box[0]), float(view_box[1])
    except (TypeError, IndexError, ValueError):
        raise ValueError("Custom shape requires a viewBox [width, height]") from None
    if width <= 0 or height <= 0:
        raise ValueError("Custom shape viewBox must be positive")
    tokens = SVG_PATH_TOKEN.findall(path)
    if not tokens:
        raise ValueError("Custom shape SVG path has no drawing commands")
    segments: list[tuple[str, list[tuple[float, float]]]] = []
    index = 0
    current = (0.0, 0.0)
    start = (0.0, 0.0)
    control: tuple[float, float] | None = None
    command = ""

    def numbers(count: int) -> list[float]:
        nonlocal index
        if index + count > len(tokens):
            raise ValueError(f"Custom shape command {command!r} is missing coordinates")
        values = []
        for offset in range(count):
            letter, value = tokens[index + offset]
            if letter or not value:
                raise ValueError(f"Custom shape command {command!r} is missing coordinates")
            try:
                values.append(float(value))
            except ValueError:
                raise ValueError(f"Custom shape command {command!r} has an invalid coordinate {value!r}") from None
        index += count
        return values

    counts = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7}
    while index < len(tokens):
        letter, value = tokens[index]
        if letter:
            command = letter
            index += 1
            if command in "Zz":
                segments.append(("close", []))
                current = start
                control = None
                continue
        elif not command:
            raise ValueError("Custom shape SVG path must begin with a command")
        upper = command.upper()
        if upper not in counts:
            raise ValueError(f"Custom shape SVG command {command!r} is not supported")
        parameter = numbers(counts[upper])
        relative = upper != command
        if relative:
            def offset(pair: tuple[float, float]) -> tuple[float, float]:
                return (current[0] + pair[0], current[1] + pair[1])
            if upper == "H":
                target = (current[0] + parameter[0], current[1])
            elif upper == "V":
                target = (current[0], current[1] + parameter[0])
            else:
                target = offset((parameter[-2], parameter[-1]))
            pairs = [offset((parameter[i], parameter[i + 1])) for i in range(0, len(parameter) - 2, 2)] if upper in {"C", "S", "Q"} else []
        else:
            if upper == "H":
                target = (parameter[0], current[1])
            elif upper == "V":
                target = (current[0], parameter[0])
            else:
                target = (parameter[-2], parameter[-1])
            pairs = [(parameter[i], parameter[i + 1]) for i in range(0, len(parameter) - 2, 2)] if upper in {"C", "S", "Q"} else []
        if upper == "M":
            segments.append(("moveTo", [target]))
            start = current = target
            control = None
            command = "L" if command == "M" else "l"
        elif upper in {"L", "H", "V"}:
            segments.append(("lnTo", [target]))
            current = target
            control = None
        elif upper == "C":
            segments.append(("cubicBezTo", [pairs[0], pairs[1], target]))
            control = pairs[1]
            current = target
        elif upper == "S":
            reflected = (2 * current[0] - control[0], 2 * current[1] - control[1]) if control else current
            segments.append(("cubicBezTo", [reflected, pairs[0], target]))
            control = pairs[0]
            current = target
        elif upper == "Q":
            segments.append(("quadBezTo", [pairs[0], target]))
            control = pairs[0]
            current = target
        elif upper == "T":
            reflected = (2 * current[0] - control[0], 2 * current[1] - control[1]) if control else current
            segments.append(("quadBezTo", [reflected, target]))
            control = reflected
            current = target
        elif upper == "A":
            radius_x, radius_y, rotation, large, sweep = parameter[:5]
            curves = arc_to_cubics(current[0], current[1], radius_x, radius_y, rotation,
                                   bool(large), bool(sweep), target[0], target[1])
            if not curves:
                segments.append(("lnTo", [target]))
            for first, second, end in curves:
                segments.append(("cubicBezTo", [first, second, end]))
            current = target
            control = None

    geometry = OxmlElement("a:custGeom")
    for name in ("avLst", "gdLst", "ahLst", "cxnLst"):
        geometry.append(OxmlElement("a:" + name))
    rect = OxmlElement("a:rect")
    for side in ("l", "t", "r", "b"):
        rect.set(side, side)
    geometry.append(rect)
    path_list = OxmlElement("a:pathLst")
    geometry.append(path_list)
    path_node = OxmlElement("a:path")
    path_node.set("w", str(round(width)))
    path_node.set("h", str(round(height)))
    path_node.set("fill", "norm")
    path_list.append(path_node)
    for name, points in segments:
        node = OxmlElement("a:" + name)
        for x, y in points:
            point = OxmlElement("a:pt")
            point.set("x", str(round(x)))
            point.set("y", str(round(y)))
            node.append(point)
        path_node.append(node)
    if not path_node.findall("{" + NS["a"] + "}moveTo"):
        raise ValueError("Custom shape SVG path must begin with a move command")
    return geometry


def group_transform(shape: Any) -> tuple | None:
    """Affine map from a group's child coordinate space into its parent space."""
    xfrm = shape._element.find("{" + NS["p"] + "}grpSpPr/{" + NS["a"] + "}xfrm")
    if xfrm is None:
        return None
    off = xfrm.find("{" + NS["a"] + "}off")
    ext = xfrm.find("{" + NS["a"] + "}ext")
    child_off = xfrm.find("{" + NS["a"] + "}chOff")
    child_ext = xfrm.find("{" + NS["a"] + "}chExt")
    if any(item is None for item in (off, ext, child_off, child_ext)):
        return None
    child_w = float(child_ext.get("cx") or 0)
    child_h = float(child_ext.get("cy") or 0)
    if child_w <= 0 or child_h <= 0:
        return None
    scale_x = float(ext.get("cx") or 0) / child_w
    scale_y = float(ext.get("cy") or 0) / child_h
    offset_x = float(off.get("x") or 0) - float(child_off.get("x") or 0) * scale_x
    offset_y = float(off.get("y") or 0) - float(child_off.get("y") or 0) * scale_y
    if xfrm.get("flipH") in {"1", "true"}:
        offset_x += child_w * scale_x
        scale_x = -scale_x
    if xfrm.get("flipV") in {"1", "true"}:
        offset_y += child_h * scale_y
        scale_y = -scale_y
    return (scale_x, scale_y, offset_x, offset_y, xfrm.get("rot"))


def compose_transform(outer: tuple | None, inner: tuple | None) -> tuple | None:
    if outer is None or inner is None:
        return outer or inner
    return (inner[0] * outer[0], inner[1] * outer[1],
            outer[2] + inner[2] * outer[0], outer[3] + inner[3] * outer[1], inner[4])


def map_box(transform: tuple, x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
    """Map an EMU box through a group transform; the result is an EMU box."""
    scale_x, scale_y, offset_x, offset_y = transform[:4]
    left, right = offset_x + x * scale_x, offset_x + (x + w) * scale_x
    top, bottom = offset_y + y * scale_y, offset_y + (y + h) * scale_y
    return (min(left, right), min(top, bottom), abs(right - left), abs(bottom - top))


def normalized_ratios(sizes: list[Any]) -> list[float]:
    """Turn absolute table track sizes into fractions that sum to exactly one."""
    values = [float(size) for size in sizes]
    total = sum(values)
    if not values or total <= 0:
        return [round(1 / len(values), 6)] * len(values) if values else []
    ratios = [value / total for value in values]
    ratios[-1] = 1 - sum(ratios[:-1])
    return ratios


def fold_picture_outset(crop: dict[str, float], bounds: list[float], loc: str,
                        report: "Report") -> tuple[dict[str, float], list[float]]:
    """Fold a negative Office crop (outset) into the picture box.

    A negative srcRect makes the image fill only part of its frame, leaving
    transparent padding around it. Freeform PPTD images have no outset field, so
    the padding is dropped from the box and the crop becomes a plain rectangle.
    The folded box matches what Office renders, verified against a real PDF.
    """
    if all(crop[side] >= 0 for side in crop):
        return crop, bounds
    horizontal = 1 - crop["left"] - crop["right"]
    vertical = 1 - crop["top"] - crop["bottom"]
    if horizontal <= 0 or vertical <= 0:
        raise ValueError("Picture crop leaves no visible source area")
    x, y, w, h = bounds
    report.issue(loc, "image-outset",
                 "Office picture outset is folded into the picture box; the transparent padding is dropped.")
    return ({side: 0 for side in ("left", "top", "right", "bottom")},
            [x + w * (-crop["left"] / horizontal), y + h * (-crop["top"] / vertical),
             w / horizontal, h / vertical])


def dump_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


class Report:
    def __init__(self, direction: str, source: Path):
        self.data = {
            "direction": direction, "source": str(source.resolve()), "spec": SPEC,
            "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "engine": f"python-pptx {pptx.__version__}",
            "status": "partial-fidelity-experiment", "lossless": False,
            "wholeSlideRasterization": False, "unsupported": [], "notes": [],
            "objects": [],
        }

    def issue(self, location: str, feature: str, detail: str) -> None:
        entry = {"location": location, "feature": feature, "detail": detail}
        if entry not in self.data["unsupported"]:
            self.data["unsupported"].append(entry)

    def note(self, detail: str) -> None:
        if detail not in self.data["notes"]:
            self.data["notes"].append(detail)

    def extras(self, value: dict, supported: set[str], location: str) -> None:
        for key in value.keys() - supported:
            self.issue(location, key, "This field is not mapped by this bounded adapter.")


def local_resource(base: Path, value: str) -> Path:
    if not isinstance(value, str) or "://" in value:
        raise ValueError("This probe reads local project resources only; URLs are not fetched")
    if Path(value).is_absolute():
        raise ValueError("PPTD resource paths must be relative")
    path = (base / value).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError("Resource escapes the PPTD directory")
    if not path.is_file():
        raise ValueError(f"Missing PPTD resource: {value}")
    return path


from .document import load_yaml

class RichText(HTMLParser):
    """Small explicitly bounded HTML-to-runs parser; unsupported tags are reported."""

    def __init__(self, text: str, base: dict, report: Report, loc: str):
        super().__init__(convert_charrefs=True)
        self.report, self.loc, self.base = report, loc, base
        self.stack: list[tuple[str, dict]] = []
        self.paragraphs: list[dict] = []
        self.current: dict | None = None
        if not re.search(r"</?[a-zA-Z][^>]*>", text):
            self.paragraphs = [{"style": {}, "runs": [(line, {})]} for line in text.splitlines()]
        else:
            self.feed(text)
            self.close()
        if not self.paragraphs:
            self.paragraphs = [{"style": {}, "runs": []}]

    def css(self, value: str, tag: str) -> dict:
        result: dict = {}
        mapping = {"color": "color", "font-size": "fontSize", "font-family": "fontFamily",
                   "text-align": "paragraphAlign", "margin-top": "marginTop"}
        for bit in value.split(";"):
            if ":" not in bit:
                continue
            key, val = [x.strip() for x in bit.split(":", 1)]
            if key == "line-height" and tag == "p":
                result["lineHeightPx" if val.endswith("px") else "lineHeight"] = float(val.removesuffix("px"))
            elif key in mapping:
                if tag == "p" and key not in {"text-align", "margin-top"}:
                    self.report.issue(self.loc, f"rich-text.{tag}.{key}", "Not a supported paragraph style in this probe.")
                    continue
                result[mapping[key]] = float(val.removesuffix("px")) if key in {"font-size", "margin-top"} else val.strip("\"'")
            else:
                self.report.issue(self.loc, f"rich-text.{tag}.{key}", "CSS property omitted.")
        return result

    def start_paragraph(self, style: dict | None = None) -> None:
        self.current = {"style": style or {}, "runs": []}
        self.paragraphs.append(self.current)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        style = self.css(attr.get("style") or "", tag)
        semantic = {"strong": {"bold": True}, "em": {"italic": True}, "u": {"underline": True}}
        if tag == "p":
            self.start_paragraph(style)
        elif tag == "br":
            self.report.issue(self.loc, "rich-text.br", "br is outside PPTD's listed tags; treated as a new paragraph.")
            self.start_paragraph()
            return
        elif tag == "a":
            style.update({"href": attr.get("href"), "color": "#0563C1", "underline": True})
        elif tag in semantic:
            style.update(semantic[tag])
        elif tag not in {"span"}:
            self.report.issue(self.loc, f"rich-text.{tag}", "Tag semantics omitted; contained text remains editable.")
            if tag == "li":
                self.start_paragraph()
        self.stack.append((tag, style))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self.stack = self.stack[:index]
                break
        if tag in {"p", "li"}:
            self.current = None

    def handle_data(self, data: str) -> None:
        if not data.strip() and self.current is None:
            return
        if self.current is None:
            self.start_paragraph()
        style: dict = {}
        for _, level in self.stack:
            style.update(level)
        self.current["runs"].append((data, style))


class Exporter:
    def __init__(self, source: Path, output: Path, *, allow_vector=False):
        self.allow_vector = allow_vector
        self.source, self.output = source.resolve(), output.resolve()
        self.doc = load_yaml(self.source)
        if self.doc.get("version") != "v2":
            raise ValueError('Only version: "v2" is accepted')
        self.theme = self.doc.get("theme", {})
        self.report = Report("PPTD-v2-to-PPTX", self.source)
        self.report.note("Shared safe YAML 1.2 core scalar loader; duplicate keys, aliases and non-JSON scalars are rejected.")
        self.report.note("px maps to pt at PPTD's documented 72 PPI; 1 px = 12700 EMU.")
        self.report.note("Fonts are resolved from explicit customFonts or installed faces; 思源黑体 aliases Noto Sans CJK SC. MiSans default uses the public preset CDN.")
        self.report.extras(self.doc, {"version", "title", "size", "theme", "pages", "customFonts"}, "presentation")
        self.font_resolver = FontResolver(ROOT / "runtime/fonts", self.doc.get("customFonts", []))
        self.font_uses = {}
        self.font_substitutions = {}
        self.chart_audits = {}
        self.report.data.update({"status": "advanced-capabilities-export", "animations": [], "charts": []})
        self.prs = Presentation()
        self.prs.slide_width, self.prs.slide_height = map(Pt, self.doc["size"])
        self.prs.core_properties.title = self.doc.get("title", "")

    def resolved_color(self, value):
        seen=set()
        while value.startswith('$'):
            if value in seen:raise ValueError(f'Cyclic theme color {value}')
            seen.add(value);value=self.theme.get('colors',{})[value[1:]]
        if not re.fullmatch(r'#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?',value):raise ValueError(f'Invalid color {value}')
        return value

    def set_color(self, target: Any, value: str, loc: str) -> None:
        value = self.resolved_color(value)
        target.rgb = RGBColor.from_string(value[1:7])
        # ColorFormat exposes RGB but no transparency setter. DrawingML stores
        # opacity as a:alpha under a:srgbClr (0 transparent, 100000 opaque).
        # Use the public RGB setter first, then its exact color-choice node so
        # fills, borders, text and table cells share the same native behavior.
        color_node = target._xFill.eg_colorChoice
        for transform in list(color_node):
            if etree.QName(transform).localname in {"alpha", "alphaMod", "alphaOff"}:
                color_node.remove(transform)
        alpha = int(value[7:9], 16) if len(value) == 9 else 255
        if alpha < 255:
            etree.SubElement(color_node, "{" + NS["a"] + "}alpha", val=str(round(alpha / 255 * 100000)))
            self.report.note("HEX8 opacity is preserved natively as DrawingML a:alpha on RGB colors; source objects remain editable.")

    def fill(self, target: Any, fill: dict | None, loc: str) -> None:
        if fill is None:
            target.background()
        elif fill.get("type") == "solid":
            target.solid()
            self.set_color(target.fore_color, fill["color"], loc)
        elif fill.get("type") == "gradient":
            self.gradient(target, fill, loc)
        else:
            self.report.issue(loc, "fill", f"{fill.get('type')} fill omitted; solid and gradient fills are mapped.")
            target.background()

    def gradient(self, target: Any, fill: dict, loc: str) -> None:
        """Write a native DrawingML gradient for PPTD linear and radial fills."""
        stops = [stop for stop in fill.get("stops", []) if isinstance(stop, dict)]
        if len(stops) < 2:
            raise ValueError("Gradient fill requires at least two color stops")
        target.gradient()
        node = target._xPr.find("{" + NS["a"] + "}gradFill")
        if node is None:
            raise ValueError("Gradient fill could not be created")
        listing = node.find("{" + NS["a"] + "}gsLst")
        for child in list(listing):
            listing.remove(child)
        for index, stop in enumerate(stops):
            position = float(stop.get("position", index / (len(stops) - 1)))
            gs = OxmlElement("a:gs")
            gs.set("pos", str(round(max(0.0, min(1.0, position)) * 100000)))
            value = self.resolved_color(stop.get("color", "#000000"))
            color = OxmlElement("a:srgbClr")
            color.set("val", value[1:7].upper())
            if len(value) == 9 and value[7:9].upper() != "FF":
                etree.SubElement(color, "{" + NS["a"] + "}alpha",
                                 val=str(round(int(value[7:9], 16) / 255 * 100000)))
            gs.append(color)
            listing.append(gs)
        for child in list(node):
            if etree.QName(child).localname in {"lin", "path"}:
                node.remove(child)
        if fill.get("gradientType", "linear") == "radial":
            path = OxmlElement("a:path")
            path.set("path", "circle")
            rect = OxmlElement("a:fillToRect")
            for side in ("l", "t", "r", "b"):
                rect.set(side, "50000")
            path.append(rect)
            node.append(path)
        else:
            line = OxmlElement("a:lin")
            line.set("ang", str(round(float(fill.get("angle", 0)) * 60000)))
            line.set("scaled", "1")
            node.append(line)

    def cell_border(self, cell: Any, spec: Any, loc: str) -> None:
        """Write explicit cell borders; python-pptx has no public setter for them."""
        sides = spec if isinstance(spec, list) else [spec, spec, spec, spec]
        if len(sides) == 2:
            sides = [sides[0], sides[1], sides[0], sides[1]]
        if len(sides) != 4:
            raise ValueError("Table cell border must be a Border, a two-element or a four-element array")
        properties = cell._tc.get_or_add_tcPr()
        for child in list(properties):
            if etree.QName(child).localname in {"lnL", "lnR", "lnT", "lnB"}:
                properties.remove(child)
        # a:tcPr child order is lnL, lnR, lnT, lnB before the fill properties;
        # PPTD lists sides clockwise from the top.
        anchor = next((child for child in properties
                       if etree.QName(child).localname in {"lnTlToBr", "lnBlToTr", "cell3D", "noFill",
                                                            "solidFill", "gradFill", "blipFill", "pattFill",
                                                            "grpFill", "headers", "extLst"}), None)
        index = list(properties).index(anchor) if anchor is not None else len(properties)
        for offset, (name, border) in enumerate(zip(("lnL", "lnR", "lnT", "lnB"),
                                                    (sides[3], sides[1], sides[0], sides[2]))):
            node = OxmlElement("a:" + name)
            if border is None:
                node.append(OxmlElement("a:noFill"))
            else:
                node.set("w", str(round(float(border.get("width", 1)) * 12700)))
                solid = OxmlElement("a:solidFill")
                color = OxmlElement("a:srgbClr")
                color.set("val", self.resolved_color(border.get("color", "#000000"))[1:7].upper())
                solid.append(color)
                node.append(solid)
                dash = {"dash": "dash", "dot": "sysDot"}.get(border.get("style", "solid"))
                if dash:
                    preset = OxmlElement("a:prstDash")
                    preset.set("val", dash)
                    node.append(preset)
            properties.insert(index + offset, node)

    def border(self, target: Any, border: dict | None, loc: str) -> None:
        if border is None:
            target.fill.background()
            return
        self.set_color(target.color, border.get("color", "#000000"), loc)
        width = border.get("width", 1)
        target.width = Pt(width)
        if width == 0:
            # python-pptx omits its default w=0 attribute. Keep an authored zero
            # explicit so it cannot fall back to the Office theme's line width.
            target._get_or_add_ln().set("w", "0")
        target.dash_style = DASH[border.get("style", "solid")]

    def font(self, font: Any, style: dict, loc: str, text="") -> None:
        names = style.get("fontFamily", "MiSans")
        names = {"latin": names, "ea": names} if isinstance(names, str) else names
        for script in ("latin", "ea"):
            requested = names.get(script, names.get("latin", "MiSans"))
            name = ALIASES.get(requested, requested)
            if name != requested:
                self.font_substitutions.setdefault(requested, name)
            prop = font._rPr.find("{" + NS["a"] + "}" + script)
            if prop is None: prop = etree.SubElement(font._rPr, "{" + NS["a"] + "}" + script)
            prop.set("typeface", name)
            key = (name, 700 if style.get("bold") else 400, bool(style.get("italic")))
            chars = {c for c in text if is_east_asian(c) == (script == "ea")}
            self.font_uses.setdefault(key, set()).update(chars)
        font.size = Pt(style.get("fontSize", 18))
        font.bold = style.get("bold", False)
        font.italic = style.get("italic", False)
        font.underline = style.get("underline", False)
        if 'letterSpacing' in style:
            spacing = style['letterSpacing']
            if type(spacing) not in (int, float) or not math.isfinite(spacing) or not -4000 <= spacing <= 4000:
                raise ValueError('letterSpacing must be a finite number in the DrawingML range -4000..4000')
            # DrawingML spc is in hundredths of a point; PPTD uses 72 PPI.
            # Apply to both paragraph defaults and runs, retaining theme values.
            font._rPr.set('spc', str(round(spacing * 100)))
        self.set_color(font.color, style.get("color", "#000000"), loc)

    def text(self, tf: Any, content: dict, loc: str) -> None:
        reference = content.get("style", content.get("textStyle", ""))
        themed = self.theme.get("textStyles", {}).get(reference.lstrip("$"), {})
        base = {**themed, **content}
        if not isinstance(base.get("text", ""), str):
            raise ValueError("Text must be a string; quote YAML 1.1 implicit values such as on/off/yes/no")
        self.report.extras(base, TEXT_KEYS, loc)
        tf.clear()
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.word_wrap = content.get("wrap", True)
        align = base.get("align", ["left", "top"])
        tf.vertical_anchor = VALIGN[align[1]]
        for n, para in enumerate(RichText(str(base.get("text", "")), base, self.report, loc).paragraphs):
            p = tf.paragraphs[0] if n == 0 else tf.add_paragraph()
            style = {**base, **para["style"]}
            p.alignment = ALIGN[style.get("paragraphAlign", align[0])]
            p.line_spacing = Pt(style["lineHeightPx"]) if "lineHeightPx" in style else float(style.get("lineHeight", 1))
            p.space_before = Pt(style.get("marginTop", 0))
            p.space_after = 0
            self.font(p.font, style, loc)
            for text, inline in para["runs"]:
                run_style = {**style, **inline}
                for is_math, part in split_math(text):
                    if is_math:
                        self.report.note("Native Office Math requires a compatible Office consumer; this LibreOffice build drops a14:m on PDF conversion. Use the browser PDF exporter for complete formula rendering.")
                        append_math(p, part, run_style.get("fontSize", 18), self.resolved_color(run_style.get("color", "#000000")))
                    else:
                        run = p.add_run()
                        run.text = part
                        self.font(run.font, run_style, loc, part)
                        if inline.get("href"): run.hyperlink.address = inline["href"]

    def table(self, slide: Any, e: dict, loc: str) -> Any:
        x, y, w, h = map(Pt, e["bounds"])
        widths, heights = e["columnWidths"], e["rowHeights"]
        if abs(sum(widths) - 1) > 1e-6 or abs(sum(heights) - 1) > 1e-6:
            raise ValueError("Table width/height ratios must sum to 1")
        shape = slide.shapes.add_table(len(heights), len(widths), x, y, w, h)
        table = shape.table
        table.first_row = table.last_row = table.first_col = table.last_col = False
        table.horz_banding = table.vert_banding = False
        for col, ratio in zip(table.columns, widths):
            col.width = int(w * ratio)
        for row, ratio in zip(table.rows, heights):
            row.height = int(h * ratio)
        config = e.get("style", {})
        if isinstance(config, str):
            config = self.theme.get("tableStyles", {}).get(config.lstrip("$"), {})
        used: set[tuple[int, int]] = set()
        for ri, row in enumerate(e["rows"]):
            ci = 0
            for data in row:
                while (ri, ci) in used:
                    ci += 1
                if ci >= len(widths):
                    raise ValueError("Too many table cells after merge placement")
                baseline = {**config.get("cellStyle", {})}
                body = config.get("bodyStyles", [])
                if 0 < ri < len(heights) - 1 and body:
                    baseline.update(body[(ri - 1) % len(body)])
                row_style = {**(config.get("firstRowStyle", {}) if ri == 0 else {}), **(config.get("lastRowStyle", {}) if ri == len(heights) - 1 else {})}
                col_style = {**(config.get("firstColumnStyle", {}) if ci == 0 else {}), **(config.get("lastColumnStyle", {}) if ci == len(widths) - 1 else {})}
                for rule in ([col_style, row_style] if config.get("rowOverColumn", True) else [row_style, col_style]):
                    baseline.update(rule)
                ts = self.theme.get("textStyles", {}).get(data.get("textStyle", "").lstrip("$"), {})
                baseline.update(ts)
                baseline.update(data)
                cell = table.cell(ri, ci)
                rs, cs = data.get("rowSpan", 1), data.get("colSpan", 1)
                if rs > 1 or cs > 1:
                    cell.merge(table.cell(ri + rs - 1, ci + cs - 1))
                for r in range(ri, ri + rs):
                    for c in range(ci, ci + cs):
                        used.add((r, c))
                cloc = f"{loc}.rows[{ri}][{ci}]"
                self.fill(cell.fill, baseline.get("fill", e.get("fill")), cloc)
                if "border" in baseline:
                    self.cell_border(cell, baseline["border"], cloc)
                text_fields = {k: v for k, v in baseline.items() if k not in {"fill", "border", "rowSpan", "colSpan"}}
                text_fields.setdefault("align", ["center", "middle"])
                text_fields.setdefault("fontSize", min(18, e["bounds"][3] * heights[ri] * 0.4))
                cell.vertical_anchor = VALIGN[text_fields["align"][1]]
                self.text(cell.text_frame, text_fields, cloc)
                ci += cs
        self.report.note("PPTD auto table font sizing is approximated when fontSize is omitted; table default borders use Office style, not a guaranteed PPTD 1px black grid.")
        return shape

    def image_resource(self, value):
        if not isinstance(value, str) or not value.startswith(('http://', 'https://')):
            return local_resource(self.source.parent, value)
        import requests
        from io import BytesIO
        cache=ROOT / 'runtime/pptd-assets';cache.mkdir(parents=True,exist_ok=True)
        path=cache / hashlib.sha256(value.encode()).hexdigest()
        if not path.exists():
            with requests.get(value, stream=True, timeout=30) as response:
                response.raise_for_status();payload=bytearray()
                for chunk in response.iter_content(65536):
                    payload.extend(chunk)
                    if len(payload)>40_000_000:raise ValueError('Image resource exceeds 40 MB')
            with Image.open(BytesIO(payload)) as im:im.verify()
            path.write_bytes(payload)
        return path

    def assets(self, value):
        result = {}
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "src" and isinstance(item, str): result[item] = self.image_resource(item)
                else: result.update(self.assets(item))
        elif isinstance(value, list):
            for item in value: result.update(self.assets(item))
        return result

    def chart(self, slide: Any, e: dict, loc: str) -> Any:
        from .charts import add_chart
        shape, audit = add_chart(slide, e, self.theme, allow_vector=self.allow_vector, asset_index=self.assets(e))
        self.chart_audits[loc] = audit
        self.report.data["charts"].append({"location": loc, **audit})
        return shape

    def element(self, slide: Any, e: dict, loc: str) -> Any:
        kind = e["elementType"]
        common = {"elementId", "elementType", "bounds"}
        fields = {
            "text": {"content", "rotation"},
            "shape": {"shapeName", "fill", "border", "rotation", "adjustments", "viewBox", "path"},
            "line": {"viewBox", "points", "border", "rotation", "curve"},
            "image": {"src", "fit", "crop", "rotation", "border"},
            "table": {"rows", "columnWidths", "rowHeights", "style", "fill"},
            "chart": set(e),
            "icon": set(e),
        }
        self.report.extras(e, common | fields.get(kind, set()), loc)
        if kind == "text":
            shape = slide.shapes.add_textbox(*map(Pt, e["bounds"]))
            self.text(shape.text_frame, e["content"], loc + ".content")
        elif kind == "shape":
            custom = e["shapeName"] == "custom"
            if not custom and e["shapeName"] not in SHAPES:
                raise ValueError(f"Unsupported native shape: {e['shapeName']}")
            shape = slide.shapes.add_shape(
                MSO_AUTO_SHAPE_TYPE.RECTANGLE if custom else SHAPES[e["shapeName"]], *map(Pt, e["bounds"]))
            if custom:
                # PPTD custom shapes carry editable SVG geometry; write the same
                # native DrawingML path the renderer previews from the viewBox.
                geometry = svg_path_to_custgeom(e.get("path"), e.get("viewBox"))
                properties = shape._element.spPr
                for existing in properties.xpath("./a:prstGeom | ./a:custGeom"):
                    properties.remove(existing)
                properties.insert(1, geometry)
            self.fill(shape.fill, e.get("fill"), loc)
            self.border(shape.line, e.get("border"), loc)
            for index, value in enumerate([] if custom else e.get("adjustments", [])):
                if index >= len(shape.adjustments):
                    self.report.issue(loc, "adjustments", "Adjustment count exceeds python-pptx shape definition.")
                    break
                shape.adjustments[index] = value / 100000
        elif kind == "line":
            points = [tuple(map(float, p.split(","))) for p in e["points"].split()]
            x, y, w, h = e["bounds"]
            vw, vh = e["viewBox"]
            if len(points) < 2 or vw <= 0 or vh <= 0 or any(len(p) != 2 or not all(math.isfinite(v) for v in p) for p in points):
                raise ValueError('Line requires at least two finite points and a positive viewBox')
            if len(points) == 2:
                shape = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Pt(x + w * points[0][0] / vw), Pt(y + h * points[0][1] / vh), Pt(x + w * points[1][0] / vw), Pt(y + h * points[1][1] / vh))
            else:
                # Use DrawingML paths with the same normalized control points
                # as the browser SVG, retaining editable native geometry.
                shape = slide.shapes.add_shape(MSO_AUTO_SHAPE_TYPE.RECTANGLE, *map(Pt, e['bounds']))
                shape.fill.background()
                sp = shape._element.spPr
                for geometry in sp.xpath('./a:prstGeom | ./a:custGeom'):
                    sp.remove(geometry)
                geometry = OxmlElement('a:custGeom')
                for name in ('avLst', 'gdLst', 'ahLst', 'cxnLst'):
                    geometry.append(OxmlElement('a:' + name))
                rect = OxmlElement('a:rect')
                for key, value in {'l':'l','t':'t','r':'r','b':'b'}.items(): rect.set(key, value)
                geometry.append(rect)
                paths = OxmlElement('a:pathLst'); geometry.append(paths)
                path = OxmlElement('a:path')
                for key, value in {'w':'1000000','h':'1000000','fill':'none','stroke':'1'}.items(): path.set(key, value)
                paths.append(path)
                def command(name, coords):
                    node = OxmlElement('a:' + name)
                    for px, py in coords:
                        point = OxmlElement('a:pt'); point.set('x', str(round(px / vw * 1000000))); point.set('y', str(round(py / vh * 1000000)))
                        node.append(point)
                    path.append(node)
                command('moveTo', points[:1])
                if e.get('curve') == 'smooth' and len(points) in (3, 4):
                    command('quadBezTo' if len(points) == 3 else 'cubicBezTo', points[1:])
                else:
                    for point in points[1:]: command('lnTo', [point])
                sp.insert(1, geometry)
            self.border(shape.line, e.get("border"), loc)
        elif kind == "image":
            path = self.image_resource(e["src"])
            crop = {side: float(e.get("crop", {}).get(side, 0)) for side in ("left", "top", "right", "bottom")}
            if any(v < 0 for v in crop.values()):
                raise ValueError("Negative crop/outset needs transparent padding and is not mapped")
            if crop["left"] + crop["right"] >= 1 or crop["top"] + crop["bottom"] >= 1:
                raise ValueError("Degenerate crop source rectangle")
            x, y, w, h = e["bounds"]
            with Image.open(path) as im:
                iw, ih = im.size
            cw, ch = 1 - crop["left"] - crop["right"], 1 - crop["top"] - crop["bottom"]
            aspect = iw * cw / (ih * ch)
            mode = e.get("fit", {}).get("mode", "cover")
            if mode == "contain":
                nw, nh = (h * aspect, h) if aspect < w / h else (w, w / aspect)
                x, y, w, h = x + (w - nw) / 2, y + (h - nh) / 2, nw, nh
                self.report.note("Image contain fit shrinks the native picture into its PPTD container; the original outer container is not an Office object.")
            elif mode == "cover":
                if aspect > w / h:
                    delta = cw * (1 - (w / h) / aspect) / 2
                    crop["left"] += delta
                    crop["right"] += delta
                else:
                    delta = ch * (1 - aspect / (w / h)) / 2
                    crop["top"] += delta
                    crop["bottom"] += delta
            elif mode != "fill":
                raise ValueError(f"Unknown image fit {mode}")
            shape = slide.shapes.add_picture(str(path), *map(Pt, (x, y, w, h)))
            for side, value in crop.items():
                setattr(shape, "crop_" + side, value)
            self.border(shape.line, e.get("border"), loc)
        elif kind == "table":
            shape = self.table(slide, e, loc)
        elif kind == "chart":
            shape = self.chart(slide, e, loc)
        elif kind == "icon":
            shape = add_icon(slide, e, self.theme, asset_index=self.assets(e))
        else:
            raise ValueError(f"Element type {kind!r} is not mapped; no raster substitute is created")
        shape.name = e["elementId"]
        if kind in {"text", "shape", "line", "image", "icon"} and not e.get("shadow"):
            # The default Office theme can attach a shadow even when PPTD's
            # shadow field is absent. Public API explicitly disables inheritance.
            shape.shadow.inherit = False
            # LibreOfficeDev 26.8 still renders the theme effect selected by
            # p:style/a:effectRef despite the explicit empty a:effectLst above.
            # OOXML effectRef index 0 means no theme effect. This bounded patch
            # removes that reference without changing geometry/fills/text.
            for effect_ref in shape._element.xpath("./p:style/a:effectRef"):
                effect_ref.set("idx", "0")
                self.report.note("OOXML compatibility patch: p:style/a:effectRef/@idx is explicitly set to 0 (no theme effect), in addition to public shadow.inherit=False. LibreOfficeDev 26.8 otherwise renders default shape/line shadows despite empty a:effectLst. Explicit PPTD shadow remains unsupported.")
        if "rotation" in e:
            shape.rotation = e["rotation"]
        return shape

    def run(self) -> dict:
        self.output.mkdir(parents=True, exist_ok=True)
        for index, page_ref in enumerate(self.doc["pages"]):
            page = load_yaml(local_resource(self.source.parent, page_ref))
            slide = self.prs.slides.add_slide(self.prs.slide_layouts[6])
            self.report.extras(page, {"pageType", "background", "notes", "elements", "animations"}, page_ref)
            self.fill(slide.background.fill, page.get("background", {"type": "solid", "color": "#FFFFFF"}), page_ref)
            if page.get("notes"):
                slide.notes_slide.notes_text_frame.text = page["notes"]
            element_shapes = {}
            ids: set[str] = set()
            for e in page["elements"]:
                loc = f"{page_ref}#{e['elementId']}"
                if e["elementId"] in ids:
                    raise ValueError(f"Duplicate elementId {loc}")
                ids.add(e["elementId"])
                entry = {"page": index + 1, "elementId": e["elementId"], "elementType": e["elementType"]}
                try:
                    shape = self.element(slide, e, loc)
                    entry.update({"status": "editable-vector" if self.chart_audits.get(loc, {}).get("mode") == "editable-vector" else "native-object", "shapeType": str(shape.shape_type)})
                    element_shapes[e["elementId"]] = shape
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    self.report.issue(loc, "element-export", str(exc))
                    entry.update({"status": "unsupported", "reason": str(exc)})
                    if e["elementType"] in {"icon", "chart"} or "\\(" in str(e):
                        raise ValueError(f"{loc}: {exc}") from exc
                self.report.data["objects"].append(entry)
            if page.get("animations"):
                from .animations import apply_animations
                audit = apply_animations(slide, page["animations"], element_shapes, self.doc["size"])
                self.report.data["animations"].append({"page": page_ref, **audit})
        import uuid
        dest = self.output / (".pending-" + uuid.uuid4().hex + ".pptx")
        # Include chart/vector text face references, which are not written by text().
        for slide in self.prs.slides:
            roots = [slide._element] + [shape.chart._chartSpace for shape in slide.shapes if shape.has_chart]
            for root in roots:
                for prop in root.iter():
                    if etree.QName(prop).localname not in ("rPr", "defRPr", "endParaRPr"): continue
                    for face in prop:
                        if etree.QName(face).localname not in ("latin", "ea", "cs"): continue
                        name = face.get("typeface")
                        if name and not name.startswith("+"):
                            replaced = ALIASES.get(name, name); face.set("typeface", replaced)
                            if replaced != name:
                                self.font_substitutions.setdefault(name, replaced)
                            name = replaced
                            parent = prop.getparent()
                            value = "".join(parent.itertext()) if etree.QName(parent).localname == "r" else ""
                            if etree.QName(root).localname == "chartSpace":
                                value = "".join(root.xpath(".//a:t/text() | .//c:v/text()"))
                            script = etree.QName(face).localname
                            has_ea = any(etree.QName(f).localname == "ea" for f in prop)
                            chars = {c for c in value if not has_ea or is_east_asian(c) == (script == "ea")}
                            self.font_uses.setdefault((name,700 if prop.get("b")=="1" else 400,prop.get("i")=="1"),set()).update(chars)
        assets = []
        for (name, weight, italic), chars in self.font_uses.items():
            asset = self.font_resolver.resolve(name,weight,italic)
            asset.check_coverage("".join(chars)); assets.append(asset)
            if asset.synthesis: self.report.note(asset.synthesis)
        if self.font_substitutions:
            substitutions = [f'{source} -> {target}: the source family is not installed; the open equivalent is embedded'
                             for source, target in sorted(self.font_substitutions.items())]
            self.report.data["fontSubstitutions"] = substitutions
            for item in substitutions:
                self.report.note("Font substitution: " + item)
        self.prs.save(dest)
        self.report.data["fonts"] = embed_fonts(dest, assets)
        from .source_archive import embed_source
        self.report.data["sourceArchive"] = embed_source(dest, self.source)
        evidence = inspect_pptx(dest)
        completed = self.output / "export.pptx"
        dest.replace(completed)
        self.report.data["output"] = str(completed)
        self.report.data["ooxmlEvidence"] = evidence
        self.report.data["summary"] = dict(Counter(o["status"] for o in self.report.data["objects"]))
        dump_json(self.output / "export-report.json", self.report.data)
        return self.report.data


def inspect_pptx(path: Path) -> dict:
    evidence = {"slides": [], "charts": [], "embeddedWorkbooks": [], "embeddedFonts": []}
    with ZipFile(path) as z:
        evidence["embeddedWorkbooks"] = [n for n in z.namelist() if n.startswith("ppt/embeddings/")]
        evidence["embeddedFonts"] = [n for n in z.namelist() if n.startswith("ppt/fonts/")]
        for name in sorted(n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)):
            root = etree.fromstring(z.read(name))
            evidence["slides"].append({
                "part": name,
                "nativeTextShapes": len(root.xpath("//p:sp[p:txBody/a:p/a:r/a:t]", namespaces=NS)),
                "nativeShapes": len(root.xpath("//p:sp", namespaces=NS)),
                "nativeLines": len(root.xpath("//p:cxnSp", namespaces=NS)),
                "nativePictures": len(root.xpath("//p:pic", namespaces=NS)),
                "nativeTables": len(root.xpath("//a:tbl", namespaces=NS)),
                "nativeCharts": len(root.xpath("//c:chart", namespaces=NS)),
                "textRuns": root.xpath("//a:r/a:t/text()", namespaces=NS),
                "fonts": sorted(set(root.xpath("//a:latin/@typeface", namespaces=NS))),
                "fontSizesHundredthsPt": sorted(set(root.xpath("//a:rPr/@sz", namespaces=NS))),
                "boldRuns": root.xpath("//a:r[a:rPr/@b='1']/a:t/text()", namespaces=NS),
                "crops": [dict(c.attrib) for c in root.xpath("//a:srcRect", namespaces=NS)],
                "tables": [{"rows": len(t.xpath("a:tr", namespaces=NS)), "columns": len(t.xpath("a:tblGrid/a:gridCol", namespaces=NS)), "cells": t.xpath("a:tr/a:tc/a:txBody/a:p/a:r/a:t/text()", namespaces=NS)} for t in root.xpath("//a:tbl", namespaces=NS)],
                "nativeEquations": len(root.xpath("//*[local-name()='oMath']")),
                "editablePaths": len(root.xpath("//a:custGeom", namespaces=NS)),
                "animationTimingNodes": len(root.xpath("//p:timing", namespaces=NS)),
            })
        for name in sorted(n for n in z.namelist() if re.fullmatch(r"ppt/charts/chart\d+\.xml", n)):
            root = etree.fromstring(z.read(name))
            evidence["charts"].append({"part": name, "barChartNodes": len(root.xpath("//c:barChart", namespaces=NS)),
                "categories": root.xpath("//c:cat//c:pt/c:v/text()", namespaces=NS),
                "seriesNames": root.xpath("//c:ser/c:tx//c:v/text()", namespaces=NS),
                "values": root.xpath("//c:val//c:pt/c:v/text()", namespaces=NS)})
    # Reopen through the evaluated library as a second structural sanity check.
    loaded = Presentation(path)
    evidence["reopenSlideCount"] = len(loaded.slides)
    evidence["sizePt"] = [loaded.slide_width.pt, loaded.slide_height.pt]
    return evidence


class Importer:
    def __init__(self, source: Path, output: Path):
        self.source, self.output = source.resolve(), output.resolve()
        self.prs = Presentation(self.source)
        # The package's own theme: used both to resolve scheme colours that would
        # otherwise be dropped, and to publish PPTD theme defaults.
        try:
            with ZipFile(self.source) as package:
                self.theme_data = theme_module.extract(package)
        except (OSError, BadZipFile):
            self.theme_data = {}
        self.scheme = self.theme_data.get('scheme', {})
        self.report = Report("PPTX-to-PPTD-v2", source)
        self.report.note("The caller supplies the --import-pptx source; its path and SHA-256 are recorded. The experiment uses the project's independently existing design-layouts/presentation.pptx.")
        self.report.data["sourceIsExportOutput"] = self.source == (self.output.parent / "export.pptx").resolve()
        self.report.note("PPTD has no text-bearing shape: source shapes containing text are split into native shape + separate text elements.")
        self.report.issue("presentation", "theme-and-master-inheritance", "Office master/layout inheritance, theme fonts/colors, paragraph defaults and effects are not fully resolved. Only exposed direct formatting is mapped; defaults may differ visually.")
        self.media_count = 0

    def color(self, color: Any, loc: str) -> str | None:
        if color.type == MSO_COLOR_TYPE.RGB:
            return "#" + str(color.rgb)
        if color.type is not None:
            resolved = theme_module.resolve(self.scheme, getattr(color, 'theme_color', None))
            if resolved:
                # A scheme reference is exact once the package's own theme is read.
                return resolved
        if color.type is not None:
            self.report.issue(loc, "theme-color", "Color is not an explicit sRGB color; inherited/theme color omitted.")
        return None

    def fill(self, fill: Any, loc: str) -> dict | None:
        if fill.type == MSO_FILL_TYPE.SOLID:
            color = self.color(fill.fore_color, loc)
            return {"type": "solid", "color": color} if color else None
        if fill.type == MSO_FILL_TYPE.GRADIENT:
            return self.gradient(fill, loc)
        if fill.type not in (None, MSO_FILL_TYPE.BACKGROUND):
            self.report.issue(loc, "fill", f"Office fill type {fill.type} is not imported.")
        return None

    def gradient(self, fill: Any, loc: str) -> dict | None:
        """Read an Office gradient as PPTD stops; PPTD angle and Office angle agree."""
        stops = []
        for stop in fill.gradient_stops:
            color = self.color(stop.color, loc)
            if color:
                stops.append({"position": round(float(stop.position), 4), "color": color})
        if len(stops) < 2:
            self.report.issue(loc, "fill", "Gradient has fewer than two explicit sRGB stops; the fill is omitted.")
            return None
        try:
            angle = round(float(fill.gradient_angle), 4)
        except (TypeError, ValueError, AttributeError):
            angle = None
        result = {"type": "gradient", "gradientType": "linear" if angle is not None else "radial", "stops": stops}
        if angle is not None:
            # DrawingML measures clockwise from left-to-right, matching PPTD.
            result["angle"] = angle % 360
        return result

    def line_style(self, node: Any, loc: str) -> dict | None:
        """Read one DrawingML line element (<a:ln*>) as a PPTD Border, or None."""
        if node is None:
            return None
        solid = node.find("{" + NS["a"] + "}solidFill")
        if solid is None:
            return None
        explicit = solid.find("{" + NS["a"] + "}srgbClr")
        if explicit is None:
            self.report.issue(loc, "table-border-color",
                              "A table cell border uses an inherited/theme color; black is used.")
            color = "#000000"
        else:
            color = "#" + (explicit.get("val") or "000000").upper()
        dash = node.find("{" + NS["a"] + "}prstDash")
        name = dash.get("val") if dash is not None else "solid"
        style = {"dash": "dash", "lgDash": "dash", "sysDash": "dash", "dot": "dot", "sysDot": "dot"}.get(name, "solid")
        width = round(float(node.get("w") or 12700) / 12700, 2)
        return {"width": width, "color": color, "style": style} if width > 0 else None

    def cell_border(self, cell: Any, loc: str) -> list:
        """Read all four explicit cell borders; PPTD lists them clockwise from the top."""
        properties = cell._tc.find("{" + NS["a"] + "}tcPr")
        if properties is None:
            return [None, None, None, None]
        return [self.line_style(properties.find("{" + NS["a"] + "}" + name), loc)
                for name in ("lnT", "lnR", "lnB", "lnL")]

    def border(self, line: Any, loc: str) -> dict | None:
        if line.fill.type == MSO_FILL_TYPE.BACKGROUND:
            return None
        try:
            color = self.color(line.color, loc)
        except (TypeError, AttributeError):
            color = None
        if color is None:
            if line.width:
                self.report.issue(loc, "line-color-inheritance", "Line color unresolved; PPTD default black is used.")
            else:
                return None
        dash = {v: k for k, v in DASH.items()}.get(line.dash_style, "solid")
        if line.dash_style not in (*DASH.values(), None):
            self.report.issue(loc, "line-dash", f"{line.dash_style} approximated as solid.")
        return {"width": line.width.pt if line.width else 1, "color": color or "#000000", "style": dash}

    def font(self, font: Any, loc: str) -> dict:
        result = {}
        if font.name:
            result["fontFamily"] = font.name
        if font.size:
            result["fontSize"] = font.size.pt
        if font.bold is not None:
            result["bold"] = font.bold
        if font.italic is not None:
            result["italic"] = font.italic
        color = self.color(font.color, loc)
        if color:
            result["color"] = color
        return result

    def text(self, tf: Any, loc: str) -> dict:
        paragraphs = []
        for p in tf.paragraphs:
            para_style = self.font(p.font, loc)
            styles = []
            if p.alignment in ALIGN.values():
                styles.append("text-align:" + next(k for k, v in ALIGN.items() if v == p.alignment))
            if p.space_before:
                styles.append(f"margin-top:{p.space_before.pt:g}px")
            if p.line_spacing is not None:
                styles.append("line-height:" + (f"{p.line_spacing.pt:g}px" if hasattr(p.line_spacing, "pt") else str(p.line_spacing)))
            bits = []
            runs = {run._r: run for run in p.runs}
            paragraph_open = '<p' + (' style="' + ";".join(styles) + '"' if styles else "") + '>'
            for child in p._p:
                if child.tag.endswith("}br"):
                    # PPTD's documented HTML subset has no soft-break tag. Keep
                    # the text boundary using paragraphs and report the semantic change.
                    bits.append("</p>" + paragraph_open)
                    self.report.issue(loc, "soft-line-break", "Office soft line break converted to a PPTD paragraph boundary; paragraph spacing may differ.")
                    continue
                if child not in runs:
                    if child.tag.endswith("}fld"):
                        text = "".join(child.xpath(".//a:t/text()"))
                        bits.append(escape(text))
                        self.report.issue(loc, "dynamic-field", "Office field converted to its cached text value.")
                    continue
                run = runs[child]
                style = {**para_style, **self.font(run.font, loc)}
                ea = run._r.xpath("./a:rPr/a:ea/@typeface")
                if ea and ea[0] and ea[0] != style.get("fontFamily"):
                    if any(is_east_asian(char) for char in run.text):
                        # Office stores separate Latin and East Asian faces per run,
                        # but one font-family declaration can only carry one. The
                        # run's own script decides which face is actually painted;
                        # keeping the Latin face here leaves Chinese text in a font
                        # without CJK glyphs.
                        style["fontFamily"] = ea[0]
                    else:
                        self.report.issue(loc, "mixed-script-font", "Different East Asian and Latin fonts in one inline run; the Latin face is used for this Latin run.")
                css = []
                for key, csskey in (("fontFamily", "font-family"), ("fontSize", "font-size"), ("color", "color")):
                    if key in style:
                        value = str(style[key]) + ("px" if key == "fontSize" else "")
                        css.append(f"{csskey}:{escape(value, quote=True)}")
                bit = escape(run.text)
                if css:
                    bit = '<span style="' + ";".join(css) + '">' + bit + "</span>"
                if style.get("bold"):
                    bit = "<strong>" + bit + "</strong>"
                if style.get("italic"):
                    bit = "<em>" + bit + "</em>"
                if run.font.underline:
                    bit = "<u>" + bit + "</u>"
                if run.hyperlink.address:
                    bit = '<a href="' + escape(run.hyperlink.address, quote=True) + '">' + bit + "</a>"
                bits.append(bit)
            paragraphs.append(paragraph_open + "".join(bits) + '</p>')
        result = {"text": "".join(paragraphs), "wrap": tf.word_wrap if tf.word_wrap is not None else True,
                  "align": ["left", next((k for k, v in VALIGN.items() if v == tf.vertical_anchor), "top")]}
        if any((tf.margin_left, tf.margin_right, tf.margin_top, tf.margin_bottom)):
            self.report.issue(loc, "text-frame-margins", "Office internal text margins have no exact TextContent field and are omitted.")
        return result

    def chart(self, shape: Any, base: dict, loc: str) -> dict:
        chart = shape.chart
        if chart.chart_type not in CHART_IMPORT_TYPES:
            raise ValueError(f"Import of chart type {chart.chart_type} is not mapped")
        kind, stack, orientation = CHART_IMPORT_TYPES[chart.chart_type]
        hole = 0
        start_angle = 0
        slice_fills: dict[int, str] = {}
        if kind == "pie":
            plot = chart._chartSpace.find(".//{%s}doughnutChart" % NS["c"])
            if plot is None:
                plot = chart._chartSpace.find(".//{%s}pieChart" % NS["c"])
            sizes = chart._chartSpace.findall(".//{%s}holeSize" % NS["c"])
            if sizes:
                hole = max(10, min(90, round(float(sizes[0].get("val") or 50)))) / 100
            elif "DOUGHNUT" in str(chart.chart_type):
                # Office defaults a doughnut plot to a 50% hole when it is implicit.
                hole = 0.5
            angles = chart._chartSpace.findall(".//{%s}firstSliceAng" % NS["c"])
            if angles:
                start_angle = round(float(angles[0].get("val") or 0)) % 360
            if plot is not None:
                # Per-slice colors are Office data points, not a series fill.
                for node in plot.findall(".//{%s}dPt" % NS["c"]):
                    marker = node.find("{%s}idx" % NS["c"])
                    color = node.find(".//{%s}srgbClr" % NS["a"])
                    if marker is not None and color is not None and color.get("val"):
                        slice_fills[int(marker.get("val") or 0)] = "#" + color.get("val").upper()
            if "EXPLODED" in str(chart.chart_type):
                self.report.issue(loc, "chart-explode",
                                  "Exploded pie/doughnut slices are imported as a plain chart; slice offsets are not preserved.")
        # lxml returns str subclasses for cached chart text; normalize so the
        # page YAML only ever contains plain scalars.
        def plain(value: Any) -> Any:
            return str(value) if isinstance(value, str) else value

        def label_config(series_node: Any) -> dict | None:
            """Office data-label flags as PPTD dataLabels; the exporter writes them back."""
            labels = series_node.find("{%s}dLbls" % NS["c"])
            if labels is None:
                return None

            def flag(name: str) -> bool:
                node = labels.find("{%s}%s" % (NS["c"], name))
                return node is not None and node.get("val") not in {"0", "false"}

            if not (flag("showVal") or flag("showPercent") or flag("showCatName")):
                return None
            content = "percentage" if flag("showPercent") else "category" if flag("showCatName") else "value"
            return {"show": True, "content": content}

        categories = [plain(c.label) for c in chart.plots[0].categories]
        cols = ["category"] + [f"value_{i + 1}" for i in range(len(chart.series))]
        rows = [[c] + [plain(s.values[i]) for s in chart.series] for i, c in enumerate(categories)]
        series = []
        for i, s in enumerate(chart.series):
            if kind == "pie":
                item = {"type": "pie", "name": plain(s.name) or "系列", "encode": {"category": "category", "value": cols[i + 1]}}
                if hole:
                    item["innerRadius"] = hole
                if start_angle:
                    item["startAngle"] = start_angle
                if len(chart.series) == 1 and slice_fills and all(index in slice_fills for index in range(len(categories))):
                    item["fill"] = [slice_fills[index] for index in range(len(categories))]
            elif orientation == "bars":
                item = {"type": kind, "name": plain(s.name) or "系列", "encode": {"x": cols[i + 1], "y": "category"}}
                if stack:
                    item["stack"] = stack
            else:
                item = {"type": kind, "name": plain(s.name) or "系列", "encode": {"x": "category", "y": cols[i + 1]}}
                if stack:
                    item["stack"] = stack
            fill = self.fill(s.format.fill, loc)
            if fill and "fill" not in item:
                item["fill"] = fill["color"]
            labels = label_config(s._element)
            if labels:
                item["dataLabels"] = labels
            series.append(item)
        self.report.issue(loc, "chart-presentation", "Series/category/value data imported; detailed axes, title, labels, legend placement and number formats are not fully preserved.")
        return {**base, "elementType": "chart", "data": {"cols": cols, "rows": rows}, "series": series, "legend": chart.has_legend}

    def place(self, shape: Any, mapper: tuple | None) -> list[float]:
        """Element bounds in points, mapped through enclosing group transforms."""
        if mapper is None:
            return [shape.left.pt, shape.top.pt, shape.width.pt, shape.height.pt]
        box = map_box(mapper, int(shape.left), int(shape.top), int(shape.width), int(shape.height))
        return [round(value / 12700, 4) for value in box]

    def collect(self, shape: Any, loc: str, mapper: tuple | None) -> list[dict]:
        """Expand one Office shape, flattening groups into their children.

        PPTD v2 has no group container, so an Office group becomes a sequence of
        elements placed in slide coordinates. Child text keeps its absolute run
        properties: Office group scaling applies to geometry only.
        """
        if shape.shape_type != MSO_SHAPE_TYPE.GROUP:
            return self.element(shape, loc, mapper)
        transform = group_transform(shape)
        if transform is None:
            raise ValueError("Group shape has no usable child transform")
        if transform[4]:
            self.report.issue(loc, "group-rotation",
                              "Group rotation is not composed into flattened children; verify this group visually.")
        self.report.note("Office group shapes are flattened into flat PPTD elements; PPTD v2 has no group container.")
        combined = compose_transform(mapper, transform)
        elements = []
        for child in shape.shapes:
            elements.extend(self.collect(child, f"{loc}/{child.shape_id}:{child.name}", combined))
        return elements

    def element(self, shape: Any, loc: str, mapper: tuple | None = None) -> list[dict]:
        base = {"elementId": f"s{shape.shape_id}", "bounds": self.place(shape, mapper)}
        rotation = {"rotation": shape.rotation} if shape.rotation else {}
        result = []
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            self.media_count += 1
            filename = f"media/image-{self.media_count}.{shape.image.ext}"
            (self.output / filename).write_bytes(shape.image.blob)
            crop = {side: getattr(shape, "crop_" + side) for side in ("left", "top", "right", "bottom")}
            crop, bounds = fold_picture_outset(crop, base["bounds"], loc, self.report)
            element = {"elementId": base["elementId"], "bounds": bounds, **rotation, "elementType": "image",
                       "src": filename, "fit": {"mode": "fill"},
                       "crop": {side: round(value, 6) for side, value in crop.items()}}
            result.append(element)
        elif shape.has_table:
            table = shape.table
            rows = []
            for ri, row in enumerate(table.rows):
                cells = []
                for ci, cell in enumerate(row.cells):
                    if cell.is_spanned:
                        continue
                    text = self.text(cell.text_frame, loc)
                    text.pop("wrap", None)
                    text["align"][1] = next((k for k, v in VALIGN.items() if v == cell.vertical_anchor), "middle")
                    fill = self.fill(cell.fill, loc)
                    if fill:
                        text["fill"] = fill
                    text["border"] = self.cell_border(cell, loc)
                    if cell.is_merge_origin:
                        text.update({"rowSpan": cell.span_height, "colSpan": cell.span_width})
                    cells.append(text)
                rows.append(cells)
            result.append({**base, "elementType": "table", "rows": rows,
                           "columnWidths": normalized_ratios([c.width for c in table.columns]),
                           "rowHeights": normalized_ratios([r.height for r in table.rows])})
            self.report.note("Table cell fills and four explicit cell borders are mapped; Office table style ids are not imported, so only explicit cell formatting is reproduced.")
        elif shape.has_chart:
            result.append(self.chart(shape, base, loc))
        elif shape.shape_type == MSO_SHAPE_TYPE.LINE:
            xfrm = shape._element.xpath(".//a:xfrm")
            flip_h = bool(xfrm and xfrm[0].get("flipH") in {"1", "true"})
            flip_v = bool(xfrm and xfrm[0].get("flipV") in {"1", "true"})
            x0, x1 = (1, 0) if flip_h else (0, 1)
            y0, y1 = (1, 0) if flip_v else (0, 1)
            result.append({**base, **rotation, "elementType": "line", "viewBox": [1, 1], "points": f"{x0},{y0} {x1},{y1}", "curve": "sharp"})
            border = self.border(shape.line, loc)
            if border:
                result[-1]["border"] = border
            if shape._element.xpath(".//a:headEnd | .//a:tailEnd"):
                self.report.issue(loc, "line-arrow", "Source line arrowheads are not imported.")
        elif shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE:
            result.append({**base, **rotation, "elementType": "shape", "shapeName": shape.auto_shape_type.xml_value})
            fill, border = self.fill(shape.fill, loc), self.border(shape.line, loc)
            if fill:
                result[-1]["fill"] = fill
            if border:
                result[-1]["border"] = border
            if len(shape.adjustments):
                result[-1]["adjustments"] = [round(shape.adjustments[i] * 100000) for i in range(len(shape.adjustments))]
        elif shape.shape_type == MSO_SHAPE_TYPE.FREEFORM:
            # PPTD v2 has no separate freeform element; it expresses freeform
            # geometry as an editable custom shape whose SVG path is stretched
            # onto bounds, which is the same model the exporter writes back.
            geometry = shape._element.find("{" + NS["p"] + "}spPr/{" + NS["a"] + "}custGeom")
            if geometry is None:
                raise ValueError("Freeform shape has no <a:custGeom>")
            view_box, path = custgeom_to_svg(geometry, loc, self.report)
            result.append({**base, **rotation, "elementType": "shape", "shapeName": "custom",
                           "viewBox": view_box, "path": path})
            fill, border = self.fill(shape.fill, loc), self.border(shape.line, loc)
            if fill:
                result[-1]["fill"] = fill
            if border:
                result[-1]["border"] = border
        elif shape.shape_type not in {MSO_SHAPE_TYPE.TEXT_BOX, MSO_SHAPE_TYPE.PLACEHOLDER}:
            raise ValueError(f"Source Office shape type {shape.shape_type} not mapped")
        if shape.has_text_frame and shape.text_frame.text:
            result.append({**base, **rotation, "elementId": base["elementId"] + ("-text" if result else ""), "elementType": "text", "content": self.text(shape.text_frame, loc)})
        if shape._element.xpath(".//a:effectLst/* | .//a:effectDag/*"):
            self.report.issue(loc, "effects", "Office visual effects are not imported.")
        if shape._element.xpath(".//a:rPr[@baseline or @strike or @spc]"):
            self.report.issue(loc, "advanced-text-format", "Baseline/strike/character spacing are not imported.")
        if shape._element.xpath(".//a:pPr/a:buChar | .//a:pPr/a:buAutoNum"):
            self.report.issue(loc, "list-markers", "Source bullet/numbering markers are not imported.")
        if shape.shape_type != MSO_SHAPE_TYPE.LINE and shape._element.xpath(".//a:xfrm[@flipH='1' or @flipH='true' or @flipV='1' or @flipV='true']"):
            self.report.issue(loc, "flip", "Source reflection is not imported.")
        if shape._element.xpath(".//a:alpha"):
            self.report.issue(loc, "opacity", "Source alpha/opacity is not imported.")
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE and shape._element.xpath(".//a:prstGeom[@prst!='rect'] | .//a:custGeom"):
            self.report.issue(loc, "cropShape", "Source image mask/crop shape is not imported.")
        if shape.shape_type == MSO_SHAPE_TYPE.LINE and shape._element.xpath(".//a:prstGeom[@prst!='line'] | .//a:custGeom"):
            self.report.issue(loc, "connector-geometry", "Non-straight connector approximated by its endpoints.")
        # A shape and its separate text element share one source box; give each
        # element its own copy so the page YAML never needs anchors or aliases.
        for item in result:
            item["bounds"] = list(item["bounds"])
        return result

    def run(self) -> dict:
        (self.output / "pages").mkdir(parents=True, exist_ok=True)
        (self.output / "media").mkdir(parents=True, exist_ok=True)
        doc = {"version": "v2", "title": self.prs.core_properties.title or self.source.stem,
               "size": [self.prs.slide_width.pt, self.prs.slide_height.pt], "pages": []}
        if self.theme_data.get('theme'):
            # Only the colour scheme travels with the document. Master text styles
            # are Office's stock defaults in most hand-designed decks (Calibri,
            # 44pt titles), so publishing them as the deck's design would mislead
            # every consumer that falls back to them - charts included. They are
            # kept in the report instead, and the real text styles are derived from
            # what the pages actually paint.
            colors = self.theme_data['theme'].get('colors')
            if colors:
                doc["theme"] = {"colors": colors}
        source_count = Counter()
        output_count = Counter()
        for si, slide in enumerate(self.prs.slides):
            page = {"elements": []}
            bg = self.fill(slide.background.fill, f"slide-{si + 1}.background")
            if bg:
                page["background"] = bg
            if slide.has_notes_slide:
                page["notes"] = slide.notes_slide.notes_text_frame.text
            for shape in slide.shapes:
                loc = f"slide-{si + 1}/{shape.shape_id}:{shape.name}"
                source_count[str(shape.shape_type)] += 1
                try:
                    elements = self.collect(shape, loc, None)
                    page["elements"].extend(elements)
                    output_count.update(e["elementType"] for e in elements)
                    self.report.data["objects"].append({"source": loc, "status": "imported", "elementIds": [e["elementId"] for e in elements]})
                except (ValueError, TypeError, AttributeError, KeyError, IndexError) as exc:
                    self.report.issue(loc, "object", str(exc))
                    self.report.data["objects"].append({"source": loc, "status": "unsupported", "reason": str(exc)})
            if slide._element.xpath(".//p:timing"):
                self.report.issue(f"slide-{si + 1}", "animations", "Source timing tree not imported.")
            if slide._element.xpath(".//p:transition"):
                self.report.issue(f"slide-{si + 1}", "transition", "Page transitions have no mapped PPTD v2 field.")
            filename = f"pages/{si + 1:02d}.page"
            (self.output / filename).write_text(yaml.safe_dump(page, allow_unicode=True, sort_keys=False))
            doc["pages"].append(filename)
        (self.output / "doc.pptd").write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False))
        self.report.data.update({"output": str(self.output / "doc.pptd"), "sourceShapeCounts": dict(source_count), "importedElementCounts": dict(output_count), "pageCount": len(doc["pages"]), "sourceOOXMLEvidence": inspect_pptx(self.source)})
        if self.theme_data:
            # Reference material only: the package's own theme colours, colour map
            # and master text styles, kept verbatim for auditing and extraction.
            self.report.data["officeTheme"] = self.theme_data
        dump_json(self.output / "import-report.json", self.report.data)
        return self.report.data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--import-pptx", type=Path)
    parser.add_argument("--allow-vector-charts", action="store_true", help="Export chart types without an equivalent native Office chart as editable vectors and retain source data")
    args = parser.parse_args()
    exported = Exporter(args.input, args.output, allow_vector=args.allow_vector_charts).run()
    result = {"export": exported["summary"], "exportUnsupportedCount": len(exported["unsupported"])}
    if args.import_pptx:
        imported = Importer(args.import_pptx, args.output / "imported").run()
        result["import"] = {"pages": imported["pageCount"], "elements": imported["importedElementCounts"], "unsupportedCount": len(imported["unsupported"])}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
