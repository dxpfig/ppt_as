"""Non-mutating structural/semantic preflight for the authoritative PPTD bundle.

Rendering/font coverage/export validation is a separate mandatory gate, not a
warning attached to a supposedly successful generation.
"""
from __future__ import annotations
import math
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

ELEMENT_TYPES = {"text", "shape", "line", "image", "icon", "table", "chart"}
ANIMATIONS = {"appear", "fade-in", "fly-in", "zoom-in", "wipe-in", "float-in", "peek-in", "rise-in", "pulse", "grow-shrink", "spin", "teeter", "fill-color", "transparency", "color-pulse", "disappear", "fade-out", "fly-out", "zoom-out", "wipe-out", "float-out", "motion-path"}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def safe_relative(value):
    return isinstance(value, str) and bool(value) and not any(c in value for c in ("\\", "\x00", ":")) and not PurePosixPath(value).is_absolute() and ".." not in PurePosixPath(value).parts


def validate_document(bundle, *, speaker_notes=None, animation_requested=None, existing=None, resource_root: Path | None = None):
    issues = []
    def fail(code, location, message):
        issues.append({"code": code, "location": location, "message": message, "severity": "error"})
    if not isinstance(bundle, dict):
        fail("InvalidType", "bundle", "Expected an object"); return issues
    document = bundle.get("document")
    if not isinstance(document, dict):
        fail("MissingField", "document", "Expected the authoritative PPTD entry"); return issues
    if document.get("version") != "v2": fail("InvalidVersion", "document.version", "Expected v2")
    size = document.get("size")
    if not isinstance(size, list) or len(size) != 2 or not all(number(v) and v > 0 for v in size):
        fail("InvalidSize", "document.size", "Expected two positive finite dimensions")
    refs = document.get("pages")
    if not isinstance(refs, list) or not refs or not all(isinstance(p, str) for p in refs):
        fail("MissingField", "document.pages", "Expected a nonempty list of page paths"); return issues
    if len(refs) != len(set(refs)): fail("DuplicatePage", "document.pages", "Each page path must be unique")
    for ref in refs:
        if not safe_relative(ref) or not ref.endswith(".page"): fail("UnsafePath", ref, "Page references must stay inside the PPTD directory and end in .page")
    pages = bundle.get("pages")
    if not isinstance(pages, list): fail("MissingField", "pages", "Expected page objects"); return issues
    index = {}
    for item in pages:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            fail("InvalidType", "pages", "Expected {path,data}"); continue
        if item["path"] in index: fail("DuplicatePage", item["path"], "Duplicate page payload")
        index[item["path"]] = item.get("data")
    for path in index.keys() - set(refs): fail("UnreferencedPage", path, "Page is not referenced by the entry")
    theme = document.get("theme", {})
    if not isinstance(theme, dict): fail("InvalidType", "document.theme", "Theme must be an object"); theme = {}
    theme = dict(theme)
    for category in ("colors", "textStyles", "tableStyles"):
        if category in theme and not isinstance(theme[category],dict):
            fail("InvalidType", f"document.theme.{category}", "Theme category must be an object")
            theme[category]={}
    old_index = {p["path"]: p["data"] for p in (existing or {}).get("pages", [])}

    def walk(value, location, parent=None):
        if isinstance(value, float) and not math.isfinite(value): fail("InvalidType", location, "Non-finite number")
        if isinstance(value, dict):
            for key, child in value.items():
                where = f"{location}.{key}"
                if key in {"color", "backgroundColor", "lineColor", "areaColor"} and not isinstance(child,str):
                    fail("InvalidColor",where,"Color must be a HEX6/HEX8 string or theme reference")
                if key in {"color", "backgroundColor", "lineColor", "areaColor"} and isinstance(child, str):
                    actual = child; seen = set()
                    while actual.startswith("$"):
                        if actual in seen or actual[1:] not in theme.get("colors", {}):
                            fail("InvalidTheme", where, f"Unresolved/cyclic color {child}"); break
                        seen.add(actual); actual = theme["colors"][actual[1:]]
                        if not isinstance(actual,str): fail("InvalidType",where,"Theme color must be a string"); break
                    else:
                        if not re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", actual):
                            # Animation color also accepts HEX6 without #.
                            if not (".animations[" in where and re.fullmatch(r"[0-9a-fA-F]{6}", actual)):
                                fail("InvalidColor", where, f"Expected HEX6/HEX8: {child}")
                if key in {"style", "textStyle"} and isinstance(child, str) and child.startswith("$"):
                    category = "tableStyles" if parent == "table" and key == "style" else "textStyles"
                    if child[1:] not in theme.get(category, {}): fail("InvalidTheme", where, f"Missing {category}.{child[1:]}")
                if key == "fontSize" and not (number(child) and child > 0): fail("InvalidType", where, "fontSize must be positive")
                if key == "opacity" and not (number(child) and 0 <= child <= 1): fail("OutOfRange", where, "opacity must be between 0 and 1")
                if key == "src" and isinstance(child, str) and "customFonts" not in where:
                    url = urlparse(child)
                    if url.scheme and url.scheme != "https": fail("UnsafePath", where, "Only HTTPS or local relative media paths are permitted")
                    elif not url.scheme:
                        if not safe_relative(child): fail("UnsafePath", where, "Media must stay inside the PPTD directory")
                        elif resource_root is not None:
                            candidate = (resource_root / child).resolve()
                            if not candidate.is_relative_to(resource_root.resolve()) or not candidate.is_file(): fail("SrcNotFound", where, f"Resource not found: {child}")
                walk(child, where, value.get("elementType", parent))
        elif isinstance(value, list):
            for i, child in enumerate(value): walk(child, f"{location}[{i}]", parent)

    walk(document, "document")
    for path in refs:
        page = index.get(path)
        if not isinstance(page, dict): fail("PageNotFound", path, "Page data is missing or not an object"); continue
        old = old_index.get(path, {})
        if speaker_notes is False and page.get("notes") and page.get("notes") != old.get("notes"):
            fail("UnrequestedNotes", path, "Speaker notes were not requested; do not add or rewrite them")
        if animation_requested is False and page.get("animations") and page.get("animations") != old.get("animations"):
            fail("UnrequestedAnimations", path, "Animations were not requested; do not add or rewrite them")
        if "note" in page: fail("UnknownField", path + ".note", "The format uses notes, not note")
        elements = page.get("elements")
        if not isinstance(elements, list): fail("MissingField", path + ".elements", "Expected an array"); continue
        ids = set()
        for i, element in enumerate(elements):
            loc = f"{path}.elements[{i}]"
            if not isinstance(element, dict): fail("InvalidType", loc, "Element must be an object"); continue
            identity = element.get("elementId")
            if not isinstance(identity, str) or not identity: fail("MissingField", loc, "elementId must be a nonempty string")
            elif identity in ids: fail("DuplicateId", loc, f"Duplicate elementId {identity}")
            else: ids.add(identity)
            kind = element.get("elementType")
            if not isinstance(kind,str) or kind not in ELEMENT_TYPES: fail("InvalidType", loc, "elementType must be one of the seven PPTD types")
            bounds = element.get("bounds")
            if not isinstance(bounds, list) or len(bounds) != 4 or not all(number(v) for v in bounds) or any(v <= 0 for v in bounds[2:]):
                fail("InvalidBounds", loc, "bounds must be four finite numbers with positive width and height")
            if kind == "text" and (not isinstance(element.get("content"), dict) or not isinstance(element["content"].get("text"), str)):
                fail("MissingField", loc, "Text requires content.text")
            if kind == "shape":
                if not isinstance(element.get("shapeName"), str): fail("MissingField", loc, "Shape requires shapeName")
                if "content" in element or "text" in element: fail("InvalidShape", loc, "Shape cannot contain text; add a separate text element")
                if element.get("shapeName") == "custom" and not (isinstance(element.get("path"), str) and isinstance(element.get("viewBox"), list) and len(element["viewBox"]) == 2): fail("InvalidShape", loc, "Custom shape requires path and viewBox")
            if kind == "image" and not isinstance(element.get("src"), str): fail("MissingField", loc, "Image requires src")
            if kind == "line":
                try:
                    view = element["viewBox"]; points = [[float(n) for n in p.split(",")] for p in element["points"].split()]
                    if len(view) != 2 or not all(number(v) and v>0 for v in view) or len(points)<2 or any(len(p)!=2 or not all(math.isfinite(n) and 0<=n<=view[j] for j,n in enumerate(p)) for p in points): raise ValueError()
                except (ValueError, TypeError, KeyError, AttributeError): fail("InvalidLine", loc, "Line requires viewBox and at least two points within it")
            if kind == "icon":
                if not isinstance(element.get("iconName"), str) or not re.fullmatch(r"(?:fas|far|fab):[a-z0-9-]+", element["iconName"]): fail("InvalidIcon", loc, "Expected a Font Awesome 7 free iconName")
            if kind == "table": _table(element, loc, fail)
            if kind == "chart":
                data = element.get("data")
                if not isinstance(data, dict) or not isinstance(data.get("cols"), list) or not isinstance(data.get("rows"), list) or not isinstance(element.get("series"), list) or not element["series"]:
                    fail("InvalidChart", loc, "Chart requires data.cols, data.rows and series")
            walk(element, loc, kind)
        animations = page.get("animations", [])
        if not isinstance(animations, list): fail("InvalidAnimation", path, "animations must be an array")
        else:
            for i, animation in enumerate(animations):
                loc=f"{path}.animations[{i}]"
                if not isinstance(animation,dict): fail("InvalidAnimation",loc,"Animation must be an object"); continue
                if not isinstance(animation.get("elementId"),str) or animation["elementId"] not in ids: fail("InvalidAnimation",loc,"Animation references a missing element")
                if not isinstance(animation.get("effect"),str) or animation["effect"] not in ANIMATIONS: fail("InvalidAnimation",loc,"Unknown animation effect")
                for field, allowed in {"trigger":{"onClick","withPrevious","afterPrevious"},"direction":{"up","down","left","right"},"easing":{"linear","ease-in","ease-out","ease-in-out"}}.items():
                    if field in animation and (not isinstance(animation[field],str) or animation[field] not in allowed): fail("InvalidAnimation",loc,f"Invalid {field}")
                for field, minimum in (("durationMs",0),("delayMs",0),("repeat",0)):
                    if field in animation and (not number(animation[field]) or animation[field] < minimum or field != "delayMs" and animation[field] == 0 or field == "repeat" and not isinstance(animation[field],int)): fail("InvalidAnimation",loc,f"Invalid {field}")
                effect=animation.get("effect")
                if effect in ("fill-color","color-pulse") and not re.fullmatch(r"#?[0-9a-fA-F]{6}",str(animation.get("color",""))): fail("InvalidAnimation",loc,"A HEX6 color is required")
                if effect == "transparency" and not (number(animation.get("amount")) and 0<=animation["amount"]<=1): fail("InvalidAnimation",loc,"amount must be in [0,1]")
                if effect == "motion-path" and not re.match(r"^M\s+0[ ,]+0(?:\s|$)",str(animation.get("path",""))): fail("InvalidAnimation",loc,"motion-path must start at M 0 0")
            walk(animations,path+".animations")
        walk(page.get("background"),path+".background")
    return issues


def _table(element, loc, fail):
    widths, heights, rows = element.get("columnWidths"), element.get("rowHeights"), element.get("rows")
    if not all(isinstance(v,list) and v and all(number(n) and 0<=n<=1 for n in v) and abs(sum(v)-1)<1e-6 for v in (widths,heights)):
        fail("InvalidTable",loc,"columnWidths and rowHeights must be ratios summing to 1"); return
    if not isinstance(rows,list) or len(rows)!=len(heights): fail("InvalidTable",loc,"Rows must match rowHeights"); return
    occupied=set(); count=len(widths)
    for y,row in enumerate(rows):
        if not isinstance(row,list): fail("InvalidTable",loc,"Each row must be an array"); continue
        x=0
        for cell in row:
            while (y,x) in occupied: x+=1
            if not isinstance(cell,dict): fail("InvalidTable",loc,"Cells must be objects"); continue
            cs,rs=cell.get("colSpan",1),cell.get("rowSpan",1)
            if any(not isinstance(n,int) or isinstance(n,bool) or n<1 for n in (cs,rs)) or x+cs>count or y+rs>len(rows): fail("InvalidTable",loc,"Merge exceeds table or has invalid span"); continue
            cells={(r,c) for r in range(y,y+rs) for c in range(x,x+cs)}
            if cells & occupied: fail("InvalidTable",loc,"Merged cells overlap")
            occupied.update(cells); x+=cs
    if occupied != {(r,c) for r in range(len(rows)) for c in range(count)}: fail("InvalidTable",loc,"Rows do not cover the complete table grid")
