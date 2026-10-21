"""Normalize PPTD markup that the bounded native exporter cannot represent."""
from __future__ import annotations

import html
import json
import re

from pptd_engine.fonts import is_east_asian

BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
PARAGRAPH = re.compile(r"<p\b([^>]*)>(.*?)</p>", re.IGNORECASE | re.DOTALL)
SPAN = re.compile(r"<span\b([^>]*)>(.*?)</span>", re.IGNORECASE | re.DOTALL)
TAG = re.compile(r"<[^>]+>")
FONT_DECLARATION = re.compile(r"(font-family\s*:\s*)([^;\"]+)", re.IGNORECASE)

CJK_FALLBACK = "微软雅黑"
# Common Latin-only faces. A CJK glyph cannot be painted by these faces, so a
# mixed run needs an explicit East-Asian route instead of failing native export.
LATIN_ONLY_FAMILIES = {
    "arial", "arial bold", "helvetica", "helvetica neue", "calibri",
    "calibri light", "times new roman", "liberation sans", "liberation serif",
    "dejavu sans", "dejavu serif", "georgia", "verdana", "tahoma",
    "trebuchet ms", "courier new", "roboto", "open sans", "lato",
}
CJK_FAMILY_MARKERS = (
    "微软雅黑", "思源黑体", "黑体", "宋体", "等线", "楷体", "仿宋",
    "noto sans cjk", "noto serif cjk", "source han", "pingfang",
    "microsoft yahei", "simhei", "simsun",
)


def _plain_text(markup: str) -> str:
    return html.unescape(TAG.sub("", markup or ""))


def _contains_cjk(value: str) -> bool:
    return any(is_east_asian(char) for char in value)


def _cjk_family(value: str) -> bool:
    folded = value.casefold()
    return any(marker in folded for marker in CJK_FAMILY_MARKERS)


def _routed_family(family, text: str):
    """Route CJK glyphs to an installed East-Asian face without losing Latin intent."""
    if not isinstance(family, str) or not family.strip() or not _contains_cjk(text):
        return family
    parts = [part.strip().strip("\"'") for part in family.split(",") if part.strip()]
    if len(parts) > 1:
        latin = next((part for part in parts if not _cjk_family(part)), parts[0])
        east_asian = next((part for part in parts if _cjk_family(part)), None)
        return {"latin": latin, "ea": east_asian or CJK_FALLBACK}
    if _cjk_family(family) or family.casefold() not in LATIN_ONLY_FAMILIES:
        return family
    return {"latin": family.strip(), "ea": CJK_FALLBACK}


def _route_inline_fonts(markup: str) -> str:
    """Replace an inline Latin-only family on CJK-dominant spans.

    Inline ``font-family`` only carries one face. Mixed CJK-dominant runs use
    the East-Asian fallback; the element-level dictionary route preserves both
    scripts whenever the style is inherited instead of declared inline.
    """

    def replace_span(match: re.Match) -> str:
        attrs, body = match.group(1), match.group(2)
        text = _plain_text(body)
        if not _contains_cjk(text):
            return match.group(0)
        cjk = sum(1 for char in text if is_east_asian(char))
        latin = sum(1 for char in text if char.isascii() and char.isalnum())
        if cjk <= latin:
            return match.group(0)

        def replace_declaration(declaration: re.Match) -> str:
            family = declaration.group(2).strip().strip("\"'")
            routed = _routed_family(family, text)
            if isinstance(routed, dict):
                return declaration.group(1) + routed["ea"]
            return declaration.group(0)

        normalized = FONT_DECLARATION.sub(replace_declaration, attrs)
        return f"<span{normalized}>{body}</span>" if normalized != attrs else match.group(0)

    return SPAN.sub(replace_span, markup)


def normalize_text_markup(markup: str) -> str:
    """Turn unsupported line breaks into paragraphs and route inline CJK fonts."""
    if not isinstance(markup, str):
        return markup
    if BR.search(markup):
        if not PARAGRAPH.search(markup):
            markup = "".join(f"<p>{part}</p>" for part in BR.split(markup))
        else:
            def replace(match: re.Match) -> str:
                attrs, body = match.group(1), match.group(2)
                parts = BR.split(body)
                return "".join(f"<p{attrs}>{part}</p>" for part in parts)

            markup = PARAGRAPH.sub(replace, markup)
    return _route_inline_fonts(markup)


def _route_style(style, text: str) -> bool:
    if not isinstance(style, dict):
        return False
    before = style.get("fontFamily")
    routed = _routed_family(before, text)
    if routed == before:
        return False
    style["fontFamily"] = routed
    return True


def _route_table_style(style, text: str) -> bool:
    if not isinstance(style, dict):
        return False
    changed = False
    changed |= _route_style(style.get("cellStyle"), text)
    changed |= _route_style(style.get("firstRowStyle"), text)
    changed |= _route_style(style.get("lastRowStyle"), text)
    changed |= _route_style(style.get("firstColumnStyle"), text)
    changed |= _route_style(style.get("lastColumnStyle"), text)
    for item in style.get("bodyStyles") or []:
        changed |= _route_style(item, text)
    return changed


def _table_text(element: dict) -> str:
    values = []
    for row in element.get("rows") or []:
        for cell in row or []:
            if isinstance(cell, dict) and isinstance(cell.get("text"), str):
                values.append(_plain_text(cell["text"]))
    return "".join(values)


def normalize_pages(pages: list[dict]) -> dict[str, int]:
    """Apply native-export-safe transformations to page text and shapes."""
    changed = {"text": 0, "opacity": 0, "table": 0, "font": 0, "native": 0}
    for page in pages:
        data = page.get("data") if isinstance(page, dict) else None
        if not isinstance(data, dict):
            continue
        for element in data.get("elements", []):
            if not isinstance(element, dict):
                continue
            if "opacity" in element:
                element.pop("opacity", None)
                changed["opacity"] += 1
            if element.get("elementType") == "line" and "arrow" in element:
                element.pop("arrow", None)
                changed["native"] += 1
            content = element.get("content")
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                text = content["text"]
                normalized = normalize_text_markup(text)
                if normalized != text:
                    content["text"] = normalized
                    changed["text" if "<br" in text.lower() else "font"] += 1
                    text = normalized
                if _route_style(content, _plain_text(text)):
                    changed["font"] += 1
            if element.get("elementType") == "table":
                table_text = _table_text(element)
                if _route_table_style(element.get("style"), table_text):
                    changed["font"] += 1
                for row in element.get("rows") or []:
                    for cell in row or []:
                        if not isinstance(cell, dict):
                            continue
                        text = cell.get("text")
                        if isinstance(text, str):
                            normalized = normalize_text_markup(text)
                            if normalized != text:
                                cell["text"] = normalized
                                changed["table" if "<br" in text.lower() else "font"] += 1
                                text = normalized
                        if isinstance(text, str) and _route_style(cell, _plain_text(text)):
                            changed["font"] += 1
            if element.get("elementType") == "chart":
                values = " ".join(
                    str(value) for row in (element.get("data") or {}).get("rows", [])
                    for value in row if value is not None
                )
                values += " ".join(str(item.get("name", "")) for item in element.get("series") or [])
                if _route_style(element, values):
                    changed["font"] += 1
    return changed


def normalize_custom_fonts(document: dict, pages: list[dict]) -> int:
    """Drop declared custom fonts that no page actually references."""
    custom = document.get("customFonts")
    if not isinstance(custom, list) or not custom:
        return 0
    content = json.dumps(pages, ensure_ascii=False, sort_keys=True)
    kept = [
        item
        for item in custom
        if isinstance(item, dict)
        and isinstance(item.get("family"), str)
        and item["family"] in content
    ]
    removed = len(custom) - len(kept)
    if kept:
        document["customFonts"] = kept
    else:
        document.pop("customFonts", None)
    return removed
