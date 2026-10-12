"""KaTeX MathML to editable Office Math, for text and table paragraphs.

The a14:m extension is CT_TextMath, documented by Microsoft at
https://learn.microsoft.com/dotnet/api/documentformat.openxml.office2010.drawing.textmath
OMML structures follow ISO/IEC 29500-1 §22.1. Unsupported presentation nodes
raise ValueError rather than silently losing their content or layout.
"""
from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import re
import subprocess
from lxml import etree

NS = {
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "a14": "http://schemas.microsoft.com/office/drawing/2010/main",
}
MATHML = "http://www.w3.org/1998/Math/MathML"
RUNTIME = Path(__file__).resolve().parents[1] / "packages/pptd-runtime/src/math-icons.js"
NARY = set("∫∬∭∮∯∰∑∏∐⋂⋃⋀⋁⨀⨁⨂⨄⨆")


def _el(name, *children, text=None, **attributes):
    prefix, local = name.split(":")
    node = etree.Element(f"{{{NS[prefix]}}}{local}")
    for key, value in attributes.items():
        if ":" in key:
            p, k = key.split(":"); key = f"{{{NS[p]}}}{k}"
        node.set(key, str(value))
    node.extend(children)
    if text is not None:
        node.text = text
    return node


def _val(name, value):
    return _el("m:" + name, **{"m:val": value})


def split_math(text: str) -> list[tuple[bool, str]]:
    """Split one rich-text run without changing any ordinary text."""
    result = []
    cursor = 0
    while cursor < len(text):
        start, stray = text.find(r"\(", cursor), text.find(r"\)", cursor)
        if stray >= 0 and (start < 0 or stray < start):
            raise ValueError("Unbalanced math delimiter \\)")
        if start < 0:
            result.append((False, text[cursor:])); break
        if start > cursor:
            result.append((False, text[cursor:start]))
        end, nested = text.find(r"\)", start + 2), text.find(r"\(", start + 2)
        if end < 0 or 0 <= nested < end:
            raise ValueError("Unbalanced math delimiter \\(")
        result.append((True, text[start + 2:end]))
        cursor = end + 2
    return result


@lru_cache(maxsize=512)
def _latex_mathml(latex: str) -> str:
    if not isinstance(latex, str) or not latex.strip():
        raise ValueError("Invalid math: empty formula")
    if re.search(r"</?[a-z][^>]*>", latex, flags=re.I):
        raise ValueError("Rich text tags are not allowed inside a math formula")
    try:
        result = subprocess.run(
            [os.environ.get("PPTD_NODE", "node"), str(RUNTIME), "--mathml"],
            input=latex, text=True, capture_output=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"Cannot parse formula with the shared KaTeX runtime: {error}") from error
    if result.returncode:
        raise ValueError(f"Invalid math formula: {result.stderr.strip()}")
    return result.stdout


class _Converter:
    def __init__(self, font_size, color):
        self.font_size = font_size
        self.color = str(color or "#000000").lstrip("#")
        if not re.fullmatch(r"[a-fA-F0-9]{6}(?:[a-fA-F0-9]{2})?", self.color):
            raise ValueError("Math color must be a resolved HEX6/HEX8 value")
        if font_size is not None and float(font_size) <= 0:
            raise ValueError("Math font size must be positive")

    def drawing_style(self):
        props = _el("a:rPr", lang="en-US", b="0", i="0", u="none", strike="noStrike", baseline="0")
        if self.font_size is not None:
            props.set("sz", str(round(float(self.font_size) * 100)))
        rgb = _el("a:srgbClr", val=self.color[:6].upper())
        if len(self.color) == 8:
            rgb.append(_el("a:alpha", val=round(int(self.color[6:], 16) / 255 * 100000)))
        props.append(_el("a:solidFill", rgb))
        props.append(_el("a:latin", typeface="STIX Two Math"))
        return props

    def control(self):
        return _el("m:ctrlPr", self.drawing_style())

    def run(self, text, normal=False, variant=None):
        properties = _el("m:rPr")
        if normal or variant == "normal":
            properties.append(_val("nor", "1"))
            properties.append(_val("sty", "p"))
        elif variant:
            styles = {"bold": "b", "italic": "i", "bold-italic": "bi", "double-struck": "p", "script": "p", "fraktur": "p", "sans-serif": "p", "monospace": "p", "bold-script": "b", "bold-fraktur": "b", "sans-serif-bold": "b", "sans-serif-italic": "i", "sans-serif-bold-italic": "bi"}
            if variant not in styles:
                raise ValueError(f"unsupported MathML mathvariant: {variant}")
            properties.append(_val("sty", styles[variant]))
            scripts = {"double-struck": "double-struck", "script": "script", "fraktur": "fraktur", "sans-serif": "sans-serif", "monospace": "monospace"}
            for source, target in scripts.items():
                if source in variant:
                    properties.append(_val("scr", target)); break
        drawing = self.drawing_style()
        if any('\u2e80' <= char <= '\u9fff' or '\uac00' <= char <= '\ud7af' or '\uff00' <= char <= '\uffef' or ord(char) >= 0x20000 for char in text):
            drawing.append(_el("a:ea", typeface="Noto Sans CJK SC"))
        run = _el("m:r", properties, drawing, _el("m:t", text=text))
        if text != text.strip():
            run[-1].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        return run

    @staticmethod
    def tag(node):
        return etree.QName(node).localname

    def sequence(self, nodes):
        output = []
        for node in nodes:
            converted = self.convert(node)
            # An n-ary's operand is a following math object in Presentation
            # MathML; bind that object, preserving remaining siblings in order.
            if output and self.tag(output[-1]) == "nary" and not len(output[-1][-1]):
                output[-1][-1].extend(converted)
            else:
                output.extend(converted)
        return output

    def arg(self, name, node):
        return _el("m:" + name, *self.convert(node))

    def convert(self, node):
        tag, children = self.tag(node), list(node)
        text = "".join(node.itertext())
        if etree.QName(node).namespace != MATHML:
            raise ValueError(f"unsupported MathML namespace: {node.tag}")
        if tag == "annotation":
            return []  # KaTeX source annotation, never displayed math.
        if tag == "semantics":
            if not children:
                return []
            return self.convert(children[0])
        if tag in {"math", "mrow", "mtd"}:
            if tag == "mrow" and len(children) >= 2 and children[0].get("fence") == "true" and children[-1].get("fence") == "true":
                left, right = "".join(children[0].itertext()), "".join(children[-1].itertext())
                pr = _el("m:dPr", _val("begChr", left), _val("endChr", right), self.control())
                return [_el("m:d", pr, _el("m:e", *self.sequence(children[1:-1])))]
            return self.sequence(children)
        if tag in {"mi", "mn", "mo", "mtext", "ms"}:
            if tag == "mo" and text in NARY:
                return [self.nary(text, None, None, "subSup")]
            return [self.run(text, tag in {"mtext", "ms"}, node.get("mathvariant"))]
        if tag == "mfrac":
            if len(children) != 2:
                raise ValueError("Invalid MathML fraction")
            kind = "noBar" if node.get("linethickness") in {"0", "0px", "0em"} else "skw" if node.get("bevelled") == "true" else "bar"
            return [_el("m:f", _el("m:fPr", _val("type", kind), self.control()), self.arg("num", children[0]), self.arg("den", children[1]))]
        if tag in {"msqrt", "mroot"}:
            root = tag == "mroot"
            if root and len(children) != 2:
                raise ValueError("Invalid MathML radical")
            return [_el("m:rad", _el("m:radPr", _val("degHide", "0" if root else "1"), self.control()), self.arg("deg", children[1]) if root else _el("m:deg"), self.arg("e", children[0]) if root else _el("m:e", *self.sequence(children)))]
        if tag in {"msub", "msup", "msubsup", "munder", "mover", "munderover"}:
            expected = 3 if tag in {"msubsup", "munderover"} else 2
            if len(children) != expected:
                raise ValueError(f"Invalid MathML {tag}")
            base = "".join(children[0].itertext())
            lower = children[1] if tag in {"msub", "msubsup", "munder", "munderover"} else None
            upper = children[-1] if tag in {"msup", "msubsup", "mover", "munderover"} else None
            if base in NARY:
                return [self.nary(base, lower, upper, "undOvr" if "under" in tag or "over" in tag else "subSup")]
            if tag in {"mover", "munder"} and node.get("accent" if tag == "mover" else "accentunder") == "true":
                accent = "".join(children[1].itertext())
                if tag == "mover":
                    return [_el("m:acc", _el("m:accPr", _val("chr", accent), self.control()), self.arg("e", children[0]))]
                return [_el("m:groupChr", _el("m:groupChrPr", _val("chr", accent), _val("pos", "bot"), _val("vertJc", "top"), self.control()), self.arg("e", children[0]))]
            if tag in {"mover", "munder", "munderover"}:
                result = self.convert(children[0])
                if lower is not None:
                    result = [_el("m:limLow", _el("m:limLowPr", self.control()), _el("m:e", *result), self.arg("lim", lower))]
                if upper is not None:
                    result = [_el("m:limUpp", _el("m:limUppPr", self.control()), _el("m:e", *result), self.arg("lim", upper))]
                return result
            typ = {"msub": "sSub", "msup": "sSup", "msubsup": "sSubSup"}[tag]
            out = _el("m:" + typ, _el("m:" + typ + "Pr", self.control()), self.arg("e", children[0]))
            if lower is not None: out.append(self.arg("sub", lower))
            if upper is not None: out.append(self.arg("sup", upper))
            return [out]
        if tag == "mtable":
            rows = []
            for row in children:
                if self.tag(row) != "mtr" or any(self.tag(cell) != "mtd" for cell in row):
                    raise ValueError("unsupported MathML labeled matrix row")
                rows.append(_el("m:mr", *(self.arg("e", cell) for cell in row)))
            count = max((len(row) for row in rows), default=1)
            mc = _el("m:mc", _el("m:mcPr", _val("count", count), _val("mcJc", "center")))
            return [_el("m:m", _el("m:mPr", _el("m:mcs", mc), self.control()), *rows)]
        if tag == "mspace":
            width = node.get("width", "0em")
            match = re.fullmatch(r"(-?[\d.]+)(em|mu|px|pt)?", width)
            if not match:
                raise ValueError(f"unsupported MathML space width {width}")
            size = float(match[1]) / (18 if match[2] == "mu" else 1)
            if size < 0:
                raise ValueError("unsupported MathML negative spacing")
            return [self.run("\u2009" if size <= .25 else "\u2005" if size <= .5 else "\u2003", True)] if size else []
        if tag == "mstyle":
            allowed = {"displaystyle", "scriptlevel", "mathcolor", "mathsize", "mathvariant"}
            if set(node.attrib) - allowed:
                raise ValueError(f"unsupported MathML mstyle attributes: {set(node.attrib) - allowed}")
            if node.get("mathsize") or node.get("mathvariant"):
                raise ValueError("unsupported MathML mstyle mathsize/mathvariant")
            original = self.color
            if node.get("mathcolor"):
                self.color = node.get("mathcolor").lstrip("#")
                if not re.fullmatch(r"[\da-fA-F]{6}", self.color):
                    raise ValueError("unsupported MathML non-hex color")
            result = self.sequence(children); self.color = original
            return result
        if tag == "mphantom":
            return [_el("m:phant", _el("m:phantPr", _val("show", "0"), self.control()), _el("m:e", *self.sequence(children)))]
        if tag == "menclose":
            notation = node.get("notation", "longdiv").split()
            if notation == ["box"]:
                return [_el("m:borderBox", _el("m:borderBoxPr", self.control()), _el("m:e", *self.sequence(children)))]
            if notation == ["top"] or notation == ["bottom"]:
                return [_el("m:bar", _el("m:barPr", _val("pos", "top" if notation == ["top"] else "bot"), self.control()), _el("m:e", *self.sequence(children)))]
            raise ValueError(f"unsupported MathML enclosure: {notation}")
        raise ValueError(f"unsupported MathML node: {tag}")

    def nary(self, char, lower, upper, location):
        return _el("m:nary", _el("m:naryPr", _val("chr", char), _val("limLoc", location), _val("subHide", "1" if lower is None else "0"), _val("supHide", "1" if upper is None else "0"), self.control()), self.arg("sub", lower) if lower is not None else _el("m:sub"), self.arg("sup", upper) if upper is not None else _el("m:sup"), _el("m:e"))


def mathml_to_omml(mathml: str, font_size=None, color=None):
    """Return an m:oMath tree, never a raster or a literal-TeX text run."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(mathml.encode(), parser)
    converter = _Converter(font_size, color)
    return _el("m:oMath", *converter.convert(root))


def append_math(paragraph, latex: str, font_size=None, color=None):
    """Append native math at the current run position, keeping other text intact.

    Size uses the adapter's PowerPoint point convention. The caller resolves
    PPTD theme colors before passing them. Only size/color inherit from runs.
    """
    wrapper = etree.Element(f"{{{NS['a14']}}}m", nsmap=NS)
    wrapper.append(mathml_to_omml(_latex_mathml(latex), font_size, color))
    paragraph._p.insert_element_before(wrapper, "a:endParaRPr")
    return wrapper
