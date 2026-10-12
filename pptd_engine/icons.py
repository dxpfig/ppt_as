"""Font Awesome Free paths as editable DrawingML custom geometry.

The catalog is generated from the installed FA 7 packages, not copied here.
python-pptx provides packaging; fontTools.svgLib.path parses SVG path commands
(including relative curves/arcs) into native DrawingML path segments.
"""
from __future__ import annotations

from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
import subprocess

from fontTools.pens.basePen import BasePen
from fontTools.svgLib.path import parse_path
from lxml import etree
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE
from pptx.util import Pt

NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
ROOT = Path(__file__).resolve().parents[1]
SCALE = 1000


def _a(name, *children, **attrs):
    node = etree.Element(f"{{{NS_A}}}{name}", **{key: str(value) for key, value in attrs.items()})
    node.extend(children)
    return node


class _DrawingPathPen(BasePen):
    def __init__(self, path):
        super().__init__(None)
        self.path = path

    def point(self, p):
        if not all(math.isfinite(x) for x in p):
            raise ValueError("Non-finite SVG path coordinate")
        return _a("pt", x=round(p[0] * SCALE), y=round(p[1] * SCALE))

    def _moveTo(self, p): self.path.append(_a("moveTo", self.point(p)))
    def _lineTo(self, p): self.path.append(_a("lnTo", self.point(p)))
    def _curveToOne(self, p1, p2, p3): self.path.append(_a("cubicBezTo", self.point(p1), self.point(p2), self.point(p3)))
    def _qCurveToOne(self, p1, p2): self.path.append(_a("quadBezTo", self.point(p1), self.point(p2)))
    def _closePath(self): self.path.append(_a("close"))
    def _endPath(self): pass


def svg_path_geometry(paths, width: float, height: float):
    """Public helper: SVG path(s) -> a:custGeom, with viewBox 0 0 width height.

    Coordinates stay in the caller's viewBox; the shape transform chooses its
    final size. Paths and contours remain independently editable Office paths.
    """
    if not all(math.isfinite(float(v)) and float(v) > 0 for v in (width, height)):
        raise ValueError("SVG viewBox dimensions must be positive")
    if isinstance(paths, str): paths = [paths]
    geometry = _a("custGeom", _a("avLst"), _a("gdLst"), _a("ahLst"), _a("cxnLst"), _a("rect", l="0", t="0", r="r", b="b"))
    path_list = _a("pathLst")
    for data in paths:
        path = _a("path", w=round(width * SCALE), h=round(height * SCALE))
        parse_path(data, _DrawingPathPen(path))
        if not len(path): raise ValueError("Empty icon SVG path")
        path_list.append(path)
    if not len(path_list): raise ValueError("An icon must contain SVG paths")
    geometry.append(path_list)
    return geometry


@lru_cache(maxsize=1)
def load_icon_index():
    built = ROOT / "packages/pptd-runtime/build/icons.json"
    if built.is_file():
        return json.loads(built.read_text())
    script = ROOT / "packages/pptd-runtime/src/math-icons.js"
    result = subprocess.run([os.environ.get("PPTD_NODE", "node"), str(script), "--icons"], text=True, capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise ValueError(f"Cannot load Font Awesome Free catalog: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _color(value, theme, opacity=1.0):
    seen = set()
    while isinstance(value, str) and value.startswith("$"):
        if value in seen: raise ValueError(f"Cyclic theme color: {value}")
        seen.add(value)
        value = theme.get("colors", {}).get(value[1:])
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", value):
        raise ValueError(f"Invalid icon color: {value}")
    result = _a("srgbClr", val=value[1:7].upper())
    alpha = opacity * (int(value[7:9], 16) / 255 if len(value) == 9 else 1)
    if alpha < 1: result.append(_a("alpha", val=round(alpha * 100000)))
    return result


def _paint(slide, fill, theme, opacity, asset_index, bounds):
    kind = fill.get("type", "solid")
    if kind == "solid": return _a("solidFill", _color(fill.get("color", "#000000"), theme, opacity))
    if kind == "none": return _a("noFill")
    if kind == "gradient":
        stops = fill.get("stops")
        if not isinstance(stops, list) or len(stops) < 2: raise ValueError("Icon gradient requires at least two stops")
        values = _a("gsLst")
        for stop in stops:
            position = float(stop.get("position", stop.get("offset", -1)))
            if not 0 <= position <= 1: raise ValueError("Gradient stop position must be within [0, 1]")
            values.append(_a("gs", _color(stop["color"], theme, opacity), pos=round(position * 100000)))
        direction = _a("path", _a("fillToRect", l="50000", t="50000", r="50000", b="50000"), path="circle") if fill.get("gradientType") == "radial" else _a("lin", ang=round(float(fill.get("angle", 0)) % 360 * 60000), scaled="1")
        return _a("gradFill", values, direction, rotWithShape="1")
    if kind == "image":
        source = (asset_index or {}).get(fill.get("src"))
        if isinstance(source, dict): source = source.get("path")
        if source is None: raise ValueError(f"Icon image fill needs a resolved asset: {fill.get('src')}")
        image_part, rid = slide.part.get_or_add_image_part(str(source))
        blip = _a("blip")
        blip.set("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed", rid)
        alpha = opacity * float(fill.get("opacity", 1))
        if not 0 <= alpha <= 1: raise ValueError("Invalid icon image-fill opacity")
        if alpha < 1: blip.append(_a("alphaModFix", amt=round(alpha * 100000)))
        crop = {key: float(fill.get("crop", {}).get(key, 0)) for key in ("left", "top", "right", "bottom")}
        cw, ch = 1 - crop['left'] - crop['right'], 1 - crop['top'] - crop['bottom']
        if cw <= 0 or ch <= 0: raise ValueError("Degenerate icon image crop")
        iw, ih = image_part.image.size
        source_ratio, target_ratio = iw * cw / (ih * ch), bounds[2] / bounds[3]
        mode = fill.get("fit", {}).get("mode", "cover")
        dest = {}
        if mode == "cover":
            if source_ratio > target_ratio:
                inset = cw * (1 - target_ratio / source_ratio) / 2; crop['left'] += inset; crop['right'] += inset
            else:
                inset = ch * (1 - source_ratio / target_ratio) / 2; crop['top'] += inset; crop['bottom'] += inset
        elif mode == "contain":
            if source_ratio > target_ratio:
                dest = {'t': round((1 - target_ratio / source_ratio) * 50000), 'b': round((1 - target_ratio / source_ratio) * 50000)}
            else:
                dest = {'l': round((1 - source_ratio / target_ratio) * 50000), 'r': round((1 - source_ratio / target_ratio) * 50000)}
        elif mode != "fill": raise ValueError("Invalid icon image-fill fit mode")
        src = {short: round(crop[key] * 100000) for short, key in zip(('l', 't', 'r', 'b'), ('left', 'top', 'right', 'bottom'))}
        return _a("blipFill", blip, _a("srcRect", **src), _a("stretch", _a("fillRect", **dest)))
    raise ValueError(f"Unsupported icon fill: {kind}")


def add_icon(slide, element, theme=None, asset_index=None):
    """Add a native editable icon; preserve its semantic name in cNvPr metadata.

    Bounds use the existing adapter's point convention. asset_index optionally
    maps image-fill source names to local resolved paths.
    """
    theme = theme or {}
    name = element.get("iconName")
    icon = load_icon_index().get(name)
    if icon is None: raise ValueError(f"Unknown Font Awesome 7 Free icon: {name}")
    x, y, width, height = element["bounds"]
    if width <= 0 or height <= 0: raise ValueError("Icon bounds must have positive dimensions")
    opacity = float(element.get("opacity", 1))
    if not 0 <= opacity <= 1: raise ValueError("Icon opacity must be within [0, 1]")
    geometry = svg_path_geometry(icon["paths"], icon["width"], icon["height"])
    paint = _paint(slide, element.get("fill") or {"type": "solid", "color": "#000000"}, theme, opacity, asset_index, element['bounds'])
    shape = slide.shapes.add_shape(MSO_AUTO_SHAPE_TYPE.RECTANGLE, Pt(x), Pt(y), Pt(width), Pt(height))
    shape.name = element.get("elementId", name)
    props = shape._element.spPr
    props.remove(props.prstGeom)
    props.append(geometry)
    props.append(paint)
    shape.rotation = float(element.get("rotation", 0))
    flip = element.get("flip", [False, False])
    if flip[0]: props.xfrm.set("flipH", "1")
    if flip[1]: props.xfrm.set("flipV", "1")
    border = element.get("border")
    line = _a("ln", w=round(float(border.get("width", 1)) * 12700) if border else 0)
    line.append(_a("solidFill", _color(border.get("color", "#000000"), theme, opacity)) if border else _a("noFill"))
    if border:
        style = {"solid": "solid", "dash": "dash", "dot": "sysDot"}.get(border.get("style", "solid"))
        if style is None: raise ValueError("Invalid icon border style")
        line.append(_a("prstDash", val=style))
    props.append(line)
    shadow = element.get("shadow")
    if shadow:
        offset = shadow.get("offset", [0, 0])
        sx, sy = offset if isinstance(offset, (list, tuple)) else (float(offset), float(offset))
        effect = _a("outerShdw", _color(shadow.get("color", "#00000080"), theme, opacity), blurRad=round(float(shadow.get("blur", 0)) * 12700), dist=round(math.hypot(sx, sy) * 12700), dir=round(math.degrees(math.atan2(sy, sx)) % 360 * 60000), algn="ctr", rotWithShape="0")
        props.append(_a("effectLst", effect))
    # Theme reference effects would otherwise add a second default shadow.
    for effect in shape._element.xpath("p:style/a:effectRef"): effect.set("idx", "0")
    nv = shape._element.xpath("p:nvSpPr/p:cNvPr")[0]
    nv.set("descr", json.dumps({"iconName": name}, ensure_ascii=False))
    return shape
