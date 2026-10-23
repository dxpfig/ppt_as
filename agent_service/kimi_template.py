"""Company-template composition and host transforms for expert mode."""
from __future__ import annotations

from collections import Counter
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re

from PIL import Image

from .pptd_compat import normalize_pages
from .pptd_validation import normalize_table_tracks

ROOT = Path(__file__).resolve().parents[1]

def _corner(bounds, size):
    """Classify an element into the top-right / bottom-right company chrome band."""
    if not (isinstance(bounds, list) and len(bounds) == 4 and all(isinstance(v, (int, float)) for v in bounds)):
        return False, False
    x, y, w, h = bounds
    width, height = size
    # Deliberately tight: chrome is a small element glued to the right edge, not a
    # full-page background, a wide title, or a large content shape.
    right = x >= width * 0.72 and w <= width * 0.4
    top = right and h <= height * 0.4 and y <= height * 0.03
    bottom = right and h <= height * 0.12 and y + h >= height * 0.97
    return bool(top), bool(bottom)


def _chrome_signature(element):
    if element.get("elementType") == "image":
        return ("image", element.get("src"))
    if element.get("elementType") == "text":
        text = re.sub(r"<[^>]+>", "", str((element.get("content") or {}).get("text", ""))).strip()
        return ("text", text[:40])
    return (element.get("elementType"), element.get("shapeName") or element.get("elementId"))


def _extract_chrome(template_dir: Path) -> list[dict]:
    """Recurring top-right / bottom-right elements shared by the template's pages."""
    from pptd_engine.document import load_yaml

    entries = sorted(template_dir.glob("*.pptd"))
    if not entries:
        return []
    document = load_yaml(entries[0])
    size = document.get("size")
    if not (isinstance(size, list) and len(size) == 2):
        return []
    top: dict[tuple, list] = {}
    bottom: dict[tuple, list] = {}
    for ref in document.get("pages", []):
        data = load_yaml(template_dir / ref)
        for element in data.get("elements", []):
            if not isinstance(element, dict):
                continue
            in_top, in_bottom = _corner(element.get("bounds"), size)
            if not (in_top or in_bottom):
                continue
            bucket = top if in_top else bottom
            signature = _chrome_signature(element)
            count, representative = bucket.get(signature, (0, element))
            bucket[signature] = (count + 1, representative)
    chrome: list[dict] = []
    for bucket in (top, bottom):
        if not bucket:
            continue
        highest = max(count for count, _ in bucket.values())
        threshold = 2 if highest >= 2 else 1
        chrome.extend(representative for count, representative in bucket.values() if count >= threshold)
    return chrome


def _covers_page(element: dict, size) -> bool:
    bounds = element.get("bounds")
    if not (isinstance(bounds, list) and len(bounds) == 4) or len(size) != 2:
        return False
    x, y, w, h = (float(value) for value in bounds)
    return w >= size[0] * 0.9 and h >= size[1] * 0.9 and x <= size[0] * 0.05 and y <= size[1] * 0.05


def _extract_body_background(template_dir: Path) -> dict | None:
    """The full-page background artwork that the template's body pages share."""
    from pptd_engine.document import load_yaml

    entries = sorted(template_dir.glob("*.pptd"))
    if not entries:
        return None
    document = load_yaml(entries[0])
    size = document.get("size")
    refs = document.get("pages", [])
    if not (isinstance(size, list) and len(size) == 2) or len(refs) < 3:
        return None
    counts: Counter = Counter()
    representatives: dict[tuple, dict] = {}
    for ref in refs[1:-1]:
        data = load_yaml(template_dir / ref)
        for element in data.get("elements", []):
            if not isinstance(element, dict) or element.get("elementType") != "image":
                continue
            if not _covers_page(element, size):
                continue
            signature = (element.get("src"),)
            counts[signature] += 1
            representatives.setdefault(signature, element)
    if not counts:
        return None
    signature, count = counts.most_common(1)[0]
    if count < max(2, len(refs[1:-1]) // 2):
        return None
    element = representatives[signature]
    if not isinstance(element.get("src"), str):
        return None
    return element


def _is_decorative_band(element: dict, size) -> bool:
    """A synthetic full-width bottom band that duplicates the template background."""
    if not isinstance(element, dict) or element.get("elementType") != "shape":
        return False
    bounds = element.get("bounds")
    if not (isinstance(bounds, list) and len(bounds) == 4) or len(size) != 2:
        return False
    x, y, w, h = (float(value) for value in bounds)
    return (w >= size[0] * 0.9 and h <= size[1] * 0.2 and y + h >= size[1] * 0.95
            and (element.get("shapeName") or "") == "custom" and isinstance(element.get("path"), str))


def _apply_background(deck_dir: Path, document: dict, pages: list[dict], background: dict | None,
                      template_dir: Path) -> None:
    """Give every generated body page the template's own page background.

    Without this step the generated pages keep their plain white background and
    the decorative band the model invented, so the deck no longer matches the
    company template even though the corner chrome does. A page that already
    paints a *different* slide-sized image is repointed at the template's own
    background instead of being left alone: the template uses one image for the
    whole body, and mixing a second, more saturated variant is what made the
    contents page look off-template.
    """
    size = document.get("size")
    if background is None or not (isinstance(size, list) and len(size) == 2):
        return
    src = background.get("src")
    if not isinstance(src, str) or not src.startswith("media/"):
        return
    source = template_dir / src
    if not source.is_file():
        return
    canonical = source.read_bytes()
    deck_src = _share_media(deck_dir, src, canonical)
    for index, page in enumerate(pages):
        if index in (0, len(pages) - 1):
            continue
        data = page.get("data")
        if not isinstance(data, dict):
            continue
        _install_background_image(data, size, deck_src)


def _install_background_image(data: dict, size, src: str) -> None:
    """Store a page background as a normal image so browser and PPTX agree."""
    elements = [
        element for element in data.get("elements", [])
        if isinstance(element, dict)
        and element.get("elementId") != "host-background"
        and not _is_decorative_band(element, size)
        and not (element.get("elementType") == "image" and _covers_page(element, size))
    ]
    page_background = data.get("background")
    if isinstance(page_background, dict) and (
        page_background.get("elementType") == "image" or page_background.get("type") == "image"
    ):
        data.pop("background", None)
    data["elements"] = [{
        "elementId": "host-background",
        "bounds": [0, 0, size[0], size[1]],
        "elementType": "image",
        "src": src,
        "fit": {"mode": "fill"},
        "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
    }, *elements]


def _share_media(deck_dir: Path, preferred: str, content: bytes) -> str:
    """Reuse an identical image already in the deck, else copy the template's."""
    media = deck_dir / "media"
    target = deck_dir / preferred
    if target.is_file() and target.read_bytes() == content:
        return preferred
    if media.is_dir():
        for candidate in sorted(media.iterdir()):
            if candidate.is_file() and candidate.read_bytes() == content:
                return f"media/{candidate.name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return preferred


def _visible_box(element: dict, base_dir: Path):
    """Element box trimmed to the opaque pixels of its image, if any."""
    bounds = element.get("bounds")
    if not (isinstance(bounds, list) and len(bounds) == 4):
        return None
    values = [float(value) for value in bounds]
    if element.get("elementType") == "image" and isinstance(element.get("src"), str):
        try:
            with Image.open(base_dir / element["src"]) as image:
                if image.mode not in ("RGBA", "LA"):
                    return values
                alpha = image.getchannel("A")
                box = alpha.getbbox()
                if box is None:
                    return values
                if box == (0, 0, image.width, image.height):
                    return values
                scale_x, scale_y = values[2] / image.width, values[3] / image.height
                left, top, right, bottom = box
                return [values[0] + left * scale_x, values[1] + top * scale_y,
                        (right - left) * scale_x, (bottom - top) * scale_y]
        except (OSError, ValueError, ZeroDivisionError):
            return values
    return values


def _intersects(bounds, box, tolerance: float = 2.0) -> bool:
    if not (isinstance(bounds, list) and len(bounds) == 4 and isinstance(box, list) and len(box) == 4):
        return False
    width = min(bounds[0] + bounds[2], box[0] + box[2]) - max(bounds[0], box[0])
    height = min(bounds[1] + bounds[3], box[1] + box[3]) - max(bounds[1], box[1])
    return width > tolerance and height > tolerance


def _relocate(bounds, box, size, gap: float = 4.0):
    """Move one of our elements out of the template's mandatory corner band."""
    x, y, w, h = (float(value) for value in bounds)
    box_x, box_y, box_w, box_h = (float(value) for value in box)
    trimmed = box_x - gap - x
    if w > 48 and 48 <= trimmed < w:
        return [x, y, trimmed, h]
    shifted = box_x - gap - w
    if shifted >= 0 and shifted != x:
        return [shifted, y, w, h]
    above = box_y - gap - h
    if above >= 0:
        return [x, above, w, h]
    below = box_y + box_h + gap
    if below + h <= size[1]:
        return [x, below, w, h]
    return None


def _clear_corner_conflicts(elements: list[dict], chrome: list[dict], template_dir: Path, size) -> list[str]:
    """Relocate our own content that the template's mandatory chrome would cover."""
    boxes = [box for box in (_visible_box(element, template_dir) for element in chrome) if box]
    chrome_signatures = {
        _chrome_signature(element)
        for element in chrome
        if isinstance(element, dict)
    }
    moved: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        if _chrome_signature(element) in chrome_signatures:
            continue
        # Page-covering artwork is the background the chrome deliberately sits
        # on top of; only foreground content is pushed out of the corner band.
        if _covers_page(element, size):
            continue
        bounds = element.get("bounds")
        if not (isinstance(bounds, list) and len(bounds) == 4):
            continue
        for box in boxes:
            if not _intersects(bounds, box):
                continue
            replacement = _relocate(bounds, box, size)
            if replacement is None:
                continue
            element["bounds"] = replacement
            bounds = replacement
            moved.append(str(element.get("elementId")))
    return moved


def _apply_chrome(deck_dir: Path, document: dict, pages: list[dict], chrome: list[dict], template_dir: Path,
                  *, skip_first: bool = False, skip_last: bool = False) -> None:
    """Re-impose the template's company chrome onto every generated page."""
    if not chrome:
        return
    size = document.get("size")
    for element in chrome:
        src = element.get("src")
        if isinstance(src, str) and src.startswith("media/"):
            source = template_dir / src
            target = deck_dir / src
            if source.is_file() and not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
    top = [e for e in chrome if _corner(e.get("bounds"), size)[0]]
    bottom = [e for e in chrome if _corner(e.get("bounds"), size)[1]]
    start = 1 if skip_first else 0
    stop = len(pages) - 1 if skip_last else len(pages)
    for index in range(start, max(start, stop)):
        page = pages[index]
        data = page.get("data")
        if not isinstance(data, dict):
            continue
        elements = data.setdefault("elements", [])
        _clear_corner_conflicts(elements, chrome, template_dir, size)
        has_top = any(_corner(e.get("bounds"), size)[0] for e in elements if isinstance(e, dict))
        has_bottom = any(_corner(e.get("bounds"), size)[1] for e in elements if isinstance(e, dict))
        signatures = {
            _chrome_signature(e)
            for e in elements
            if isinstance(e, dict)
        }
        for group, present in ((top, has_top), (bottom, has_bottom)):
            present = present or bool(
                signatures
                & {
                    _chrome_signature(element)
                    for element in group
                }
            )
            if present:
                continue
            for element in group:
                clone = json.loads(json.dumps(element))
                ids = {e.get("elementId") for e in elements if isinstance(e, dict)}
                base = str(clone.get("elementId") or "chrome")
                new_id = f"{base}-chrome"
                while new_id in ids:
                    new_id += "x"
                clone["elementId"] = new_id
                _grow_text_box(clone)
                elements.append(clone)


def _grow_text_box(element: dict) -> None:
    """Grow a chrome text box that is too short for its own font size."""
    if element.get("elementType") != "text":
        return
    bounds = element.get("bounds")
    size = _font_size(element.get("content") or {})
    if not size or not (isinstance(bounds, list) and len(bounds) == 4):
        return
    required = round(size * 1.8, 2)
    if bounds[3] < required:
        element["bounds"] = [bounds[0], bounds[1], bounds[2], required]


def _deck_body_font(document: dict, pages: list[dict]) -> str | None:
    """The font the deck's own body text uses, for tables that inherit nothing."""
    theme = document.get("theme") or {}
    styles = theme.get("textStyles") or {}
    declared = styles.get("body", {}).get("fontFamily")
    if isinstance(declared, str) and declared:
        return declared
    counts: Counter = Counter()
    for page in pages:
        for element in page.get("data", {}).get("elements", []):
            if not isinstance(element, dict) or element.get("elementType") != "text":
                continue
            content = element.get("content") or {}
            family = content.get("fontFamily")
            if isinstance(family, str) and not family.startswith("$"):
                counts[family] += 1
            else:
                counts.update(name.strip() for name in re.findall(r"font-family:\s*([^;\"]+)", str(content.get("text", ""))))
    return counts.most_common(1)[0][0] if counts else None


def _normalize_table_fonts(document: dict, pages: list[dict]) -> None:
    """Tables that declare no font keep the deck's own typeface.

    The renderer falls back to its built-in default for a table cell that names
    no font, so a table silently used a different typeface from every other text
    element on the page. The company template's own tables name their font
    explicitly; this does the same for generated ones.
    """
    font = _deck_body_font(document, pages)
    if not font:
        return
    for page in pages:
        for element in page.get("data", {}).get("elements", []):
            if not isinstance(element, dict) or element.get("elementType") != "table":
                continue
            style = element.get("style")
            if not isinstance(style, dict):
                continue
            parts = [style.get("firstRowStyle"), style.get("lastRowStyle"), style.get("cellStyle"),
                     style.get("firstColumnStyle"), style.get("lastColumnStyle")]
            body = style.get("bodyStyles")
            parts.extend(body if isinstance(body, list) else [])
            for part in parts:
                if isinstance(part, dict):
                    part.setdefault("fontFamily", font)


CLOSING_MARKERS = ("感谢", "谢谢", "请领导", "敬请", "thank you", "thanks", "q&a")


def _font_size(content: dict) -> float:
    value = content.get("fontSize")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = re.search(r"font-size:\s*([0-9.]+)px", str(content.get("text", "")))
    return float(match.group(1)) if match else 0.0


def _visible_text(content: dict) -> str:
    text = re.sub(r"<[^>]+>", "", str(content.get("text", "")))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _color_of(content: dict) -> str:
    """The explicit colour of a text element, from its field or its markup."""
    if isinstance(content.get("color"), str):
        return content["color"]
    match = re.search(r"color:\s*([^;\"]+)", str(content.get("text", "")))
    return match.group(1).strip() if match else ""


def _text_slots(page_data: dict) -> list[dict]:
    """Text elements ordered by prominence: largest font first, then top to bottom."""
    slots = [e for e in page_data.get("elements", []) if isinstance(e, dict) and e.get("elementType") == "text"]
    slots.sort(key=lambda e: (-_font_size(e.get("content") or {}), (e.get("bounds") or [0, 0, 0, 0])[1]))
    return slots


def _relabel(content: dict, text: str) -> dict:
    """Replace one slot's visible text while keeping the template's paragraphs.

    Trailing empty paragraphs are load-bearing: a vertically centred title box
    relies on them to sit above the subtitle. Collapsing the markup to a single
    paragraph re-centres the line onto the subtitle, which is exactly how the
    black overlapping cover title was produced.
    """
    markup = str(content.get("text", ""))
    paragraphs = re.findall(r"<p\b[^>]*>[\s\S]*?</p>", markup, re.IGNORECASE)
    escaped = html.escape(text)
    span = re.search(r'<span[^>]*\sstyle="([^"]*)"[^>]*>', markup)

    def body() -> str:
        return f'<span style="{span.group(1)}">{escaped}</span>' if span else escaped

    if not paragraphs:
        return {**content, "text": f"<p>{body()}</p>"}
    rebuilt: list[str] = []
    filled = False
    for paragraph in paragraphs:
        open_end = paragraph.find(">") + 1
        if not _visible_text({"text": paragraph}):
            # An empty paragraph is an empty *line* in PowerPoint, and templates
            # use it to offset a centred title. Keep its run size so the browser
            # gives it the same line box instead of collapsing it.
            rebuilt.append(_blank_paragraph(paragraph, span))
            continue
        if filled:
            rebuilt.append(paragraph)
            continue
        filled = True
        rebuilt.append(paragraph[:open_end] + body() + "</p>")
    start, end = markup.find(paragraphs[0]), markup.rfind(paragraphs[-1]) + len(paragraphs[-1])
    return {**content, "text": markup[:start] + "".join(rebuilt) + markup[end:]}


def _blank_paragraph(paragraph: str, span) -> str:
    """An empty paragraph that still occupies a line at the slot's font size."""
    open_end = paragraph.find(">") + 1
    if span is None or "font-size" not in span.group(1):
        return paragraph
    return paragraph[:open_end] + f'<span style="{span.group(1)}">&nbsp;</span></p>'


DATE_PATTERN = re.compile(r"(?:19|20)\d{2}\s*(?:[年./\-]|$)")


def _fits(text: str, bounds, content: dict) -> bool:
    """One-line fit test so a slot is never flooded with a long sentence."""
    if not (isinstance(bounds, list) and len(bounds) == 4):
        return True
    size = _font_size(content) or 16.0
    return _text_width(text, size) <= float(bounds[2]) + 2


def _text_width(text: str, size: float) -> float:
    """Rough one-line width: full width for CJK, about half for Latin."""
    return sum(size * (1.0 if ord(character) > 0x2E80 else 0.55) for character in text)


def _fill_template_page(template_page: dict, texts: list[str], *, keep_unfilled: bool) -> dict:
    """Write new text into the template's own slots, keeping its typography.

    A slot is only filled when the text fits its single-line capacity; the
    template's narrow date slot is not flooded with a full sentence.
    """
    data = json.loads(json.dumps(template_page))
    for index, element in enumerate(_text_slots(data)):
        content = element.get("content") or {}
        # The headline slot always takes the model's title, even if the rough
        # width estimate is pessimistic; only the secondary slots are gated.
        if index < len(texts) and texts[index] and (index == 0 or _fits(texts[index], element.get("bounds"), content)):
            element["content"] = _relabel(content, texts[index])
        elif not keep_unfilled:
            element["content"] = _relabel(element.get("content") or {}, "")
    return data


COVER_BREAK_WORDS = ("业务", "发展", "建设", "工作", "体系", "报告", "方案", "规划")


def _cover_letter_spacing(content: dict) -> float:
    if isinstance(content.get("letterSpacing"), (int, float)):
        return float(content["letterSpacing"])
    match = re.search(r"letter-spacing:\s*(-?[0-9.]+)px", str(content.get("text", "")))
    return float(match.group(1)) if match else 0.0


def _measured_text_width(text: str, size: float, letter_spacing: float = 0.0) -> float:
    return _text_width(text, size) + max(0, len(text) - 1) * letter_spacing


def _cover_fit_size(text: str, available: float, maximum: float, letter_spacing: float) -> float:
    """Largest whole-pixel size that keeps a cover line inside the canvas."""
    size = max(12, int(maximum))
    while size > 12 and _measured_text_width(text, size, letter_spacing) > available:
        size -= 1
    return float(size)


def _cover_split(text: str) -> tuple[str, str]:
    """Prefer a semantic break near the middle, then balance the two lines."""
    midpoint = len(text) / 2
    candidates = []
    for word in COVER_BREAK_WORDS:
        start = text.find(word)
        while start >= 0:
            split = start + len(word)
            if len(text) * 0.28 <= split <= len(text) * 0.72:
                candidates.append(split)
            start = text.find(word, start + 1)
    if candidates:
        split = min(candidates, key=lambda value: abs(value - midpoint))
    else:
        split = round(midpoint)
    return text[:split].strip(), text[split:].strip()


def _cover_markup(markup: str, lines: list[str], size: float) -> str:
    span = re.search(r'<span[^>]*\sstyle="([^"]*)"[^>]*>', markup)
    style = span.group(1) if span else f"font-size:{size}px"
    if re.search(r"font-size:\s*[-0-9.]+px", style):
        style = re.sub(r"font-size:\s*[-0-9.]+px", f"font-size:{size}px", style)
    else:
        style = f"font-size:{size}px;{style}"
    strong = "<strong>" in markup
    paragraphs = []
    for line in lines:
        run = f'<span style="{style}">{html.escape(line)}</span>'
        paragraphs.append(
            '<p style="text-align:center;line-height:1.2">'
            + (f"<strong>{run}</strong>" if strong else run)
            + "</p>"
        )
    return "".join(paragraphs)


def _fit_cover_slots(page_data: dict, size) -> None:
    """Fit a long cover headline without letting it wrap into the subtitle."""
    if not (isinstance(size, list) and len(size) == 2):
        return
    width, height = (float(value) for value in size)
    slots = _text_slots(page_data)
    if not slots:
        return
    title = slots[0]
    content = title.get("content") or {}
    text = _visible_text(content)
    bounds = title.get("bounds")
    if not text or not (isinstance(bounds, list) and len(bounds) == 4):
        return
    maximum = _font_size(content) or 66.0
    spacing = _cover_letter_spacing(content)
    available = width * 0.88
    one_line = _cover_fit_size(text, available, maximum, spacing)
    if one_line >= maximum * 0.82:
        line_height = one_line * 1.2
        title_height = line_height + 32
        title_top = max(88.0, round(height * 0.20, 2))
        title["bounds"] = [0.0, title_top, width, title_height]
        title["content"] = {
            **content,
            "text": _cover_markup(
                str(content.get("text", "")),
                [text],
                one_line,
            ),
            "align": ["center", "middle"],
            "wrap": True,
        }
        title_bottom = title_top + title_height
        subtitle_y = title_bottom + max(16.0, round(one_line * 0.30, 2))
        if len(slots) > 1:
            subtitle = slots[1]
            subtitle_bounds = subtitle.get("bounds")
            if isinstance(subtitle_bounds, list) and len(subtitle_bounds) == 4:
                subtitle["bounds"] = [
                    0.0,
                    subtitle_y,
                    width,
                    max(36.0, subtitle_bounds[3]),
                ]
        date_y = subtitle_y + max(54.0, round(height * 0.12, 2))
        if len(slots) > 2:
            date = slots[2]
            date_bounds = date.get("bounds")
            if isinstance(date_bounds, list) and len(date_bounds) == 4:
                date["bounds"] = [
                    date_bounds[0],
                    min(date_y, height - date_bounds[3] - 8),
                    date_bounds[2],
                    date_bounds[3],
                ]
        return

    first, second = _cover_split(text)
    two_line = _cover_fit_size(max((first, second), key=lambda line: _measured_text_width(line, maximum, spacing)),
                               available, min(maximum, 60.0), spacing)
    two_line = max(40.0, two_line)
    line_height = two_line * 1.2
    title_height = line_height * 2 + 24
    title_top = max(88.0, round(height * 0.20, 2))
    title["bounds"] = [0.0, title_top, width, title_height]
    title["content"] = {
        **content,
        "text": _cover_markup(str(content.get("text", "")), [first, second], two_line),
        "align": ["center", "middle"],
        "wrap": True,
    }

    title_bottom = title_top + title_height
    subtitle_y = title_bottom + max(16.0, round(two_line * 0.30, 2))
    if len(slots) > 1:
        subtitle = slots[1]
        subtitle_bounds = subtitle.get("bounds")
        if isinstance(subtitle_bounds, list) and len(subtitle_bounds) == 4:
            subtitle["bounds"] = [0.0, subtitle_y, width, max(36.0, subtitle_bounds[3])]
    date_y = subtitle_y + max(54.0, round(height * 0.12, 2))
    if len(slots) > 2:
        date = slots[2]
        date_bounds = date.get("bounds")
        if isinstance(date_bounds, list) and len(date_bounds) == 4:
            date["bounds"] = [date_bounds[0], min(date_y, height - date_bounds[3] - 8), date_bounds[2], date_bounds[3]]


def _date_line(texts: list[str]) -> str:
    """The cleanest date-like line, preferring one that starts with a year."""
    matches = [text for text in texts if DATE_PATTERN.search(text)]
    if not matches:
        return ""
    return next((text for text in matches if re.match(r"\s*(?:19|20)\d{2}", text)), min(matches, key=len))


def _template_texts(page_data: dict, *, closing: bool = False) -> list[str]:
    """Texts for the template's slots: headline, subtitle, then the date slot.

    The closing template's second slot is its own date line, not a subtitle, so
    a long sentence must never be written there.
    """
    texts = [text for text in (_visible_text(e.get("content") or {}) for e in _text_slots(page_data)) if text]
    if not texts:
        return []
    if closing:
        return [texts[0], _date_line(texts[1:])]
    return [*texts[:2], _date_line(texts[2:])]


def _copy_page_media(page_data: dict, template_dir: Path, deck_dir: Path) -> None:
    for element in page_data.get("elements", []):
        src = element.get("src") if isinstance(element, dict) else None
        if isinstance(src, str) and src.startswith("media/"):
            source, target = template_dir / src, deck_dir / src
            if source.is_file() and not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())


def build_template_cover(
    template_dir: Path,
    deck_dir: Path,
    *,
    title: str,
    subtitle: str = "",
    organization: str = "",
    date: str = "",
) -> dict:
    """Build one cover page from the template's own first-page design."""
    from pptd_engine.document import load_yaml

    entries = sorted(template_dir.glob("*.pptd"))
    if not entries:
        raise ValueError("模板转换结果缺少 PPTD 入口。")
    template = load_yaml(entries[0])
    refs = template.get("pages") or []
    if not refs:
        raise ValueError("模板没有可复用的封面页。")
    page = load_yaml(template_dir / refs[0])
    _copy_page_media(page, template_dir, deck_dir)
    page = _fill_template_page(
        page,
        [title, subtitle, date],
        keep_unfilled=False,
    )
    _fit_cover_slots(page, template.get("size"))

    if organization:
        slots = _text_slots(page)
        if len(slots) >= 3:
            date_slot = slots[2]
            unit_slot = json.loads(json.dumps(date_slot))
            existing_ids = {
                item.get("elementId")
                for item in page.get("elements") or []
                if isinstance(item, dict)
            }
            unit_id = f"{unit_slot.get('elementId') or 'cover-slot'}-unit"
            while unit_id in existing_ids:
                unit_id += "x"
            unit_slot["elementId"] = unit_id
            unit_slot["content"] = _relabel(
                unit_slot.get("content") or {},
                organization,
            )
            bounds = unit_slot.get("bounds") or [0, 0, 960, 36]
            subtitle_bottom = 0.0
            if len(slots) >= 2:
                subtitle_bounds = slots[1].get("bounds") or [0, 0, 0, 0]
                subtitle_bottom = float(subtitle_bounds[1]) + float(subtitle_bounds[3])
            unit_y = max(float(bounds[1]), subtitle_bottom + 16.0)
            date_bounds = date_slot.get("bounds") or bounds
            date_height = max(30.0, float(date_bounds[3]))
            date_y = unit_y + date_height + 10.0
            unit_slot["bounds"] = [
                float(bounds[0]),
                unit_y,
                float(bounds[2]),
                date_height,
            ]
            canvas_height = (
                float(template["size"][1])
                if isinstance(template.get("size"), list)
                and len(template["size"]) == 2
                else 540.0
            )
            date_slot["bounds"] = [
                float(date_bounds[0]),
                min(date_y, canvas_height - date_height - 12.0),
                float(date_bounds[2]),
                date_height,
            ]
            page.setdefault("elements", []).append(unit_slot)
    return page


def build_template_closing(
    template_dir: Path,
    deck_dir: Path,
    *,
    text: str,
) -> dict:
    """Build one closing page from the template's own final-page design."""
    from pptd_engine.document import load_yaml

    entries = sorted(template_dir.glob("*.pptd"))
    if not entries:
        raise ValueError("模板转换结果缺少 PPTD 入口。")
    template = load_yaml(entries[0])
    refs = template.get("pages") or []
    if not refs:
        raise ValueError("模板没有可复用的结束页。")
    page = load_yaml(template_dir / refs[-1])
    _copy_page_media(page, template_dir, deck_dir)
    page = _fill_template_page(
        page,
        [text],
        keep_unfilled=False,
    )
    slots = _text_slots(page)
    if slots:
        main = slots[0]
        bounds = main.get("bounds")
        size = template.get("size") or [960, 540]
        font_size = _font_size(main.get("content") or {})
        if (
            isinstance(bounds, list)
            and len(bounds) == 4
            and font_size > 0
        ):
            canvas_width = float(size[0])
            canvas_height = float(size[1])
            required_height = max(float(bounds[3]), font_size * 1.45)
            center_y = float(bounds[1]) + float(bounds[3]) / 2
            main["bounds"] = [
                0.0,
                max(0.0, center_y - required_height / 2),
                canvas_width,
                min(
                    required_height,
                    canvas_height
                    - max(0.0, center_y - required_height / 2),
                ),
            ]
    return page


def _text_center(element: dict) -> tuple[float, float]:
    bounds = element.get("bounds") or [0, 0, 0, 0]
    return (
        float(bounds[0]) + float(bounds[2]) / 2,
        float(bounds[1]) + float(bounds[3]) / 2,
    )


def _agenda_rows(page: dict) -> list[dict]:
    """Pair each template agenda number with its row and title elements."""
    elements = [
        item for item in page.get("elements") or []
        if isinstance(item, dict)
    ]
    numbers = [
        item for item in elements
        if item.get("elementType") == "text"
        and re.fullmatch(
            r"\s*\d{2}\s*",
            _visible_text(item.get("content") or {}),
        )
    ]
    rows = []
    for number in numbers:
        center_x, center_y = _text_center(number)
        red = min(
            (
                item for item in elements
                if item.get("elementType") == "shape"
                and abs(_text_center(item)[1] - center_y) < 8
                and abs(_text_center(item)[0] - center_x) < 12
            ),
            key=lambda item: abs(_text_center(item)[1] - center_y),
            default=None,
        )
        white = min(
            (
                item for item in elements
                if item.get("elementType") == "shape"
                and abs(_text_center(item)[1] - center_y) < 8
                and _text_center(item)[0] > center_x + 20
            ),
            key=lambda item: abs(_text_center(item)[1] - center_y),
            default=None,
        )
        title = min(
            (
                item for item in elements
                if item.get("elementType") == "text"
                and item is not number
                and abs(_text_center(item)[1] - center_y) < 9
                and (
                    white is None
                    or _text_center(item)[0]
                    > float((white.get('bounds') or [0, 0, 0, 0])[0])
                )
            ),
            key=lambda item: abs(_text_center(item)[1] - center_y),
            default=None,
        )
        if red is not None and white is not None and title is not None:
            rows.append({
                'number': number,
                'red': red,
                'white': white,
                'title': title,
            })
    return sorted(rows, key=lambda row: _text_center(row['number'])[1])


def _agenda_title_markup(template: str, text: str, size: float) -> str:
    family = re.search(r'font-family:\s*([^;\"]+)', template)
    font = family.group(1).strip() if family else '微软雅黑'
    return (
        '<p style="text-align:left;line-height:1.0"><strong>'
        f'<span style="font-family:{font};font-size:{size}px;color:#000000">'
        f'{html.escape(text)}</span></strong></p>'
    )


def build_template_agenda(
    template_dir: Path,
    deck_dir: Path,
    *,
    items: list[str],
) -> dict:
    """Fill the template agenda and extend its row pattern as needed."""
    from pptd_engine.document import load_yaml

    entries = sorted(template_dir.glob("*.pptd"))
    if not entries:
        raise ValueError("模板转换结果缺少 PPTD 入口。")
    template = load_yaml(entries[0])
    refs = template.get("pages") or []
    if len(refs) < 2:
        raise ValueError("模板没有可复用的目录页。")
    page = load_yaml(template_dir / refs[1])
    _copy_page_media(page, template_dir, deck_dir)
    rows = _agenda_rows(page)
    if not rows:
        raise ValueError("模板目录页没有可识别的编号行。")
    if len(items) > len(rows):
        last = rows[-1]
        gap = (
            _text_center(last['number'])[1]
            - _text_center(rows[-2]['number'])[1]
            if len(rows) > 1
            else 44.0
        )
        existing_ids = {
            str(item.get('elementId'))
            for item in page.get('elements') or []
            if isinstance(item, dict)
        }
        for _ in range(len(items) - len(rows)):
            cloned = {}
            for role in ('red', 'number', 'white', 'title'):
                source = rows[-1][role]
                clone = json.loads(json.dumps(source))
                base = str(clone.get('elementId') or role)
                clone_id = f'{base}-next'
                while clone_id in existing_ids:
                    clone_id += 'x'
                clone['elementId'] = clone_id
                clone['bounds'] = [
                    clone['bounds'][0],
                    clone['bounds'][1] + gap,
                    clone['bounds'][2],
                    clone['bounds'][3],
                ]
                existing_ids.add(clone_id)
                page.setdefault('elements', []).append(clone)
                cloned[role] = clone
            rows.append(cloned)
    for index, (row, text) in enumerate(zip(rows, items), 1):
        number = f'{index:02d}'
        for role in ('red', 'white', 'number'):
            bounds = row[role].get('bounds')
            if isinstance(bounds, list) and len(bounds) == 4:
                row[role]['bounds'] = [
                    bounds[0],
                    bounds[1],
                    bounds[2],
                    max(38.0, bounds[3]),
                ]
        row['number']['content'] = _relabel(
            row['number'].get('content') or {},
            number,
        )
        white_bounds = row['white'].get('bounds') or [0, 0, 0, 0]
        title_x = float(white_bounds[0]) + 6.0
        title_y = float(white_bounds[1]) + 2.0
        title_width = max(40.0, float(white_bounds[2]) - 12.0)
        title_height = max(38.0, float(white_bounds[3]))
        size = 15.0
        while size > 12 and _text_width(text, size) > title_width:
            size -= 1
        row['title']['bounds'] = [
            title_x,
            title_y,
            title_width,
            title_height,
        ]
        row['title']['content'] = {
            **(row['title'].get('content') or {}),
            'text': _agenda_title_markup(
                str((row['title'].get('content') or {}).get('text', '')),
                text,
                size,
            ),
            'wrap': True,
            'align': ['left', 'middle'],
        }
    for element in page.get('elements') or []:
        if not isinstance(element, dict) or element.get('elementType') != 'text':
            continue
        element['content'] = {
            **(element.get('content') or {}),
            'inset': [0.0, 0.0, 0.0, 0.0],
        }
    return page


def build_template_cards(
    template_dir: Path,
    deck_dir: Path,
    *,
    title: str,
    items: list[str],
) -> dict:
    """Build an equal-width goal-card page on the template body artwork."""
    from pptd_engine.document import load_yaml

    entries = sorted(template_dir.glob("*.pptd"))
    if not entries:
        raise ValueError("模板转换结果缺少 PPTD 入口。")
    template = load_yaml(entries[0])
    size = template.get("size") or [960, 540]
    width, height = float(size[0]), float(size[1])
    elements: list[dict] = []
    background = _extract_body_background(template_dir)
    if (
        isinstance(background, dict)
        and isinstance(background.get("src"), str)
        and (template_dir / background["src"]).is_file()
    ):
        source = template_dir / background["src"]
        deck_src = _share_media(
            deck_dir,
            background["src"],
            source.read_bytes(),
        )
        _install_background_image(
            {"elements": elements},
            size,
            deck_src,
        )
        elements = [{
            'elementId': 'host-background',
            'bounds': [0, 0, width, height],
            'elementType': 'image',
            'src': deck_src,
            'fit': {'mode': 'fill'},
            'crop': {'left': 0, 'top': 0, 'right': 0, 'bottom': 0},
        }]
    elements.append({
        'elementId': 'cards-title',
        'elementType': 'text',
        'bounds': [64, 42, 720, 52],
        'content': {
            'text': title,
            'fontSize': 28,
            'bold': True,
            'color': '#1F2430',
            'wrap': True,
            'align': ['left', 'middle'],
        },
    })
    elements.append({
        'elementId': 'cards-accent',
        'elementType': 'shape',
        'shapeName': 'rect',
        'bounds': [64, 106, 54, 5],
        'fill': {'type': 'solid', 'color': '#C62828'},
    })
    count = max(1, len(items))
    gap = 24.0
    left = 64.0
    available = width - left * 2
    card_width = (available - gap * (count - 1)) / count
    card_y = 156.0
    card_height = min(160.0, height - card_y - 76.0)
    for index, item in enumerate(items, 1):
        x = left + (index - 1) * (card_width + gap)
        shape_id = f'goal-card-{index}'
        number_id = f'goal-number-{index}'
        title_id = f'goal-title-{index}'
        elements.append({
            'elementId': shape_id,
            'elementType': 'shape',
            'shapeName': 'roundRect',
            'bounds': [x, card_y, card_width, card_height],
            'fill': {'type': 'solid', 'color': '#F2F4F7'},
        })
        elements.append({
            'elementId': number_id,
            'elementType': 'text',
            'bounds': [x + 20, card_y + 20, card_width - 40, 28],
            'content': {
                'text': f'{index:02d}',
                'fontSize': 15,
                'bold': True,
                'color': '#C62828',
                'wrap': True,
                'align': ['left', 'middle'],
            },
        })
        text_width = card_width - 40
        font_size = 20
        while font_size > 14 and _text_width(item, font_size) > text_width:
            font_size -= 1
        elements.append({
            'elementId': title_id,
            'elementType': 'text',
            'bounds': [
                x + 20,
                card_y + 58,
                text_width,
                max(64.0, card_height - 78.0),
            ],
            'content': {
                'text': item,
                'fontSize': font_size,
                'bold': True,
                'color': '#1F2430',
                'wrap': True,
                'align': ['left', 'top'],
            },
        })
    return {'elements': elements, 'notes': ''}


def build_template_comparison(
    template_dir: Path,
    deck_dir: Path,
    *,
    title: str,
    left_title: str,
    left_items: list[str],
    right_title: str,
    right_items: list[str],
    conclusion: str,
) -> dict:
    """Build a two-column range comparison on the template body artwork."""
    from pptd_engine.document import load_yaml

    entries = sorted(template_dir.glob("*.pptd"))
    if not entries:
        raise ValueError("模板转换结果缺少 PPTD 入口。")
    template = load_yaml(entries[0])
    size = template.get("size") or [960, 540]
    width = float(size[0])
    elements: list[dict] = []
    background = _extract_body_background(template_dir)
    if (
        isinstance(background, dict)
        and isinstance(background.get("src"), str)
        and (template_dir / background["src"]).is_file()
    ):
        source = template_dir / background["src"]
        deck_src = _share_media(
            deck_dir,
            background["src"],
            source.read_bytes(),
        )
        elements.append({
            'elementId': 'host-background',
            'bounds': [0, 0, width, float(size[1])],
            'elementType': 'image',
            'src': deck_src,
            'fit': {'mode': 'fill'},
            'crop': {'left': 0, 'top': 0, 'right': 0, 'bottom': 0},
        })
    elements.append({
        'elementId': 'comparison-title',
        'elementType': 'text',
        'bounds': [64, 42, 680, 52],
        'content': {
            'text': title,
            'fontSize': 28,
            'bold': True,
            'color': '#1F2430',
            'wrap': True,
            'align': ['left', 'middle'],
        },
    })
    columns = [
        (64.0, left_title, left_items),
        (504.0, right_title, right_items),
    ]
    card_y = 154.0
    card_height = 236.0
    card_width = 392.0
    for side, (x, heading, items) in zip(('left', 'right'), columns):
        elements.append({
            'elementId': f'comparison-{side}-card',
            'elementType': 'shape',
            'shapeName': 'roundRect',
            'bounds': [x, card_y, card_width, card_height],
            'fill': {'type': 'solid', 'color': '#F2F4F7'},
        })
        elements.append({
            'elementId': f'comparison-{side}-heading',
            'elementType': 'text',
            'bounds': [x + 20, card_y + 20, card_width - 40, 34],
            'content': {
                'text': heading,
                'fontSize': 17,
                'bold': True,
                'color': '#C62828',
                'wrap': True,
                'align': ['left', 'middle'],
            },
        })
        for index, item in enumerate(items, 1):
            available = card_width - 60
            font_size = 15
            while font_size > 12 and _text_width(item, font_size) > available:
                font_size -= 1
            elements.append({
                'elementId': f'comparison-{side}-item-{index}',
                'elementType': 'text',
                'bounds': [
                    x + 20,
                    card_y + 72 + (index - 1) * 46,
                    card_width - 40,
                    38,
                ],
                'content': {
                    'text': item,
                    'fontSize': font_size,
                    'bold': False,
                    'color': '#333B47',
                    'wrap': True,
                    'align': ['left', 'middle'],
                },
            })
    elements.extend([
        {
            'elementId': 'comparison-conclusion-bg',
            'elementType': 'shape',
            'shapeName': 'roundRect',
            'bounds': [64, 414, 832, 64],
            'fill': {'type': 'solid', 'color': '#FBE9E9'},
        },
        {
            'elementId': 'comparison-conclusion',
            'elementType': 'text',
            'bounds': [84, 428, 792, 36],
            'content': {
                'text': conclusion,
                'fontSize': 15,
                'bold': True,
                'color': '#9B1C1C',
                'wrap': True,
                'align': ['left', 'middle'],
            },
        },
    ])
    return {'elements': elements, 'notes': ''}


def build_template_metrics_matrix(
    template_dir: Path,
    deck_dir: Path,
    *,
    title: str,
    kpis: list[str],
    matrix_items: list[str],
    conclusion: str,
) -> dict:
    """Build the KPI row, 2x2 matrix and conclusion on template body art."""
    from pptd_engine.document import load_yaml

    entries = sorted(template_dir.glob("*.pptd"))
    if not entries:
        raise ValueError("模板转换结果缺少 PPTD 入口。")
    template = load_yaml(entries[0])
    size = template.get("size") or [960, 540]
    elements: list[dict] = []
    background = _extract_body_background(template_dir)
    if (
        isinstance(background, dict)
        and isinstance(background.get("src"), str)
        and (template_dir / background["src"]).is_file()
    ):
        source = template_dir / background["src"]
        deck_src = _share_media(
            deck_dir,
            background["src"],
            source.read_bytes(),
        )
        elements.append({
            'elementId': 'host-background',
            'bounds': [0, 0, float(size[0]), float(size[1])],
            'elementType': 'image',
            'src': deck_src,
            'fit': {'mode': 'fill'},
            'crop': {'left': 0, 'top': 0, 'right': 0, 'bottom': 0},
        })
    elements.append({
        'elementId': 'metrics-title',
        'elementType': 'text',
        'bounds': [64, 34, 680, 44],
        'content': {
            'text': title,
            'fontSize': 28,
            'bold': True,
            'color': '#1F2430',
            'wrap': True,
            'align': ['left', 'middle'],
        },
    })
    kpi_gap = 24.0
    kpi_width = (832.0 - kpi_gap * 2) / 3
    for index, kpi in enumerate(kpis[:3], 1):
        x = 64.0 + (index - 1) * (kpi_width + kpi_gap)
        elements.append({
            'elementId': f'kpi-card-{index}',
            'elementType': 'shape',
            'shapeName': 'roundRect',
            'bounds': [x, 118, kpi_width, 78],
            'fill': {'type': 'solid', 'color': '#F2F4F7'},
        })
        font_size = 17
        while font_size > 12 and _text_width(kpi, font_size) > kpi_width - 28:
            font_size -= 1
        elements.append({
            'elementId': f'kpi-text-{index}',
            'elementType': 'text',
            'bounds': [x + 14, 132, kpi_width - 28, 50],
            'content': {
                'text': kpi,
                'fontSize': font_size,
                'bold': True,
                'color': '#9B1C1C',
                'wrap': True,
                'align': ['center', 'middle'],
            },
        })
    matrix_width = 392.0
    matrix_height = 106.0
    matrix_gap_x = 48.0
    for index, item in enumerate(matrix_items[:4], 1):
        column = (index - 1) % 2
        row = (index - 1) // 2
        x = 64.0 + column * (matrix_width + matrix_gap_x)
        y = 214.0 + row * (matrix_height + 14.0)
        elements.append({
            'elementId': f'matrix-card-{index}',
            'elementType': 'shape',
            'shapeName': 'roundRect',
            'bounds': [x, y, matrix_width, matrix_height],
            'fill': {'type': 'solid', 'color': '#F2F4F7'},
        })
        elements.append({
            'elementId': f'matrix-number-{index}',
            'elementType': 'text',
            'bounds': [x + 16, y + 10, 52, 24],
            'content': {
                'text': f'{index:02d}',
                'fontSize': 13,
                'bold': True,
                'color': '#C62828',
                'wrap': True,
                'align': ['left', 'middle'],
            },
        })
        font_size = 19
        while font_size > 14 and _text_width(item, font_size) > matrix_width - 120:
            font_size -= 1
        elements.append({
            'elementId': f'matrix-title-{index}',
            'elementType': 'text',
            'bounds': [x + 72, y + 28, matrix_width - 92, 52],
            'content': {
                'text': item,
                'fontSize': font_size,
                'bold': True,
                'color': '#1F2430',
                'wrap': True,
                'align': ['left', 'middle'],
            },
        })
    elements.append({
        'elementId': 'metrics-conclusion',
        'elementType': 'text',
        'bounds': [64, 458, 832, 42],
        'content': {
            'text': conclusion,
            'fontSize': 15,
            'bold': True,
            'color': '#9B1C1C',
            'wrap': True,
            'align': ['center', 'middle'],
        },
    })
    return {'elements': elements, 'notes': ''}


def build_template_table_process(
    template_dir: Path,
    deck_dir: Path,
    *,
    title: str,
    columns: list[str],
    rows: list[list[str]],
    stages: list[str],
    request_text: str,
) -> dict:
    """Build one editable table, three-stage process and request box."""
    from pptd_engine.document import load_yaml

    entries = sorted(template_dir.glob("*.pptd"))
    if not entries:
        raise ValueError("模板转换结果缺少 PPTD 入口。")
    template = load_yaml(entries[0])
    size = template.get("size") or [960, 540]
    elements: list[dict] = []
    background = _extract_body_background(template_dir)
    if (
        isinstance(background, dict)
        and isinstance(background.get("src"), str)
        and (template_dir / background["src"]).is_file()
    ):
        source = template_dir / background["src"]
        deck_src = _share_media(
            deck_dir,
            background["src"],
            source.read_bytes(),
        )
        elements.append({
            'elementId': 'host-background',
            'bounds': [0, 0, float(size[0]), float(size[1])],
            'elementType': 'image',
            'src': deck_src,
            'fit': {'mode': 'fill'},
            'crop': {'left': 0, 'top': 0, 'right': 0, 'bottom': 0},
        })
    elements.append({
        'elementId': 'table-page-title',
        'elementType': 'text',
        'bounds': [64, 34, 680, 44],
        'content': {
            'text': title,
            'fontSize': 28,
            'bold': True,
            'color': '#1F2430',
            'wrap': True,
            'align': ['left', 'middle'],
        },
    })
    table_rows = [
        [
            {'text': str(cell), 'fontSize': 12}
            for cell in row
        ]
        for row in [columns, *rows]
    ]
    row_count = len(table_rows)
    elements.append({
        'elementId': 'tasks-table',
        'elementType': 'table',
        'bounds': [64, 112, 832, 140],
        'columnWidths': [0.36, 0.42, 0.22],
        'rowHeights': [1 / row_count] * row_count,
        'rows': table_rows,
    })
    elements.append({
        'elementId': 'process-label',
        'elementType': 'text',
        'bounds': [64, 258, 200, 20],
        'content': {
            'text': '实施流程',
            'fontSize': 13,
            'bold': True,
            'color': '#4B5563',
            'wrap': True,
            'align': ['left', 'middle'],
        },
    })
    node_width = 238.0
    node_height = 62.0
    arrow_width = 24.0
    node_y = 286.0
    node_xs = [64.0, 358.0, 652.0]
    for index, (x, stage) in enumerate(zip(node_xs, stages[:3]), 1):
        elements.append({
            'elementId': f'process-card-{index}',
            'elementType': 'shape',
            'shapeName': 'roundRect',
            'bounds': [x, node_y, node_width, node_height],
            'fill': {'type': 'solid', 'color': '#F2F4F7'},
        })
        elements.append({
            'elementId': f'process-text-{index}',
            'elementType': 'text',
            'bounds': [x + 16, node_y + 15, node_width - 32, 32],
            'content': {
                'text': stage,
                'fontSize': 17,
                'bold': True,
                'color': '#1F2430',
                'wrap': True,
                'align': ['center', 'middle'],
            },
        })
    for index, x in enumerate((318.0, 612.0), 1):
        elements.append({
            'elementId': f'process-arrow-{index}',
            'elementType': 'text',
            'bounds': [x, node_y + 15, arrow_width, 32],
            'content': {
                'text': '→',
                'fontSize': 24,
                'bold': True,
                'color': '#4B5563',
                'wrap': False,
                'align': ['center', 'middle'],
            },
        })
    elements.extend([
        {
            'elementId': 'request-box',
            'elementType': 'shape',
            'shapeName': 'roundRect',
            'bounds': [64, 362, 832, 92],
            'fill': {'type': 'solid', 'color': '#FBE9E9'},
        },
        {
            'elementId': 'request-title',
            'elementType': 'text',
            'bounds': [84, 374, 180, 28],
            'content': {
                'text': '请示事项',
                'fontSize': 14,
                'bold': True,
                'color': '#9B1C1C',
                'wrap': True,
                'align': ['left', 'middle'],
            },
        },
        {
            'elementId': 'request-text',
            'elementType': 'text',
            'bounds': [84, 406, 792, 34],
            'content': {
                'text': request_text,
                'fontSize': 15,
                'bold': True,
                'color': '#333B47',
                'wrap': True,
                'align': ['left', 'middle'],
            },
        },
    ])
    return {'elements': elements, 'notes': ''}


def build_template_dense_regression(
    template_dir: Path,
    deck_dir: Path,
    *,
    title: str,
    subtitle: str,
    stages: list[str],
    core_judgment: str,
    metrics_title: str,
    metrics: list[dict],
    table_columns: list[str],
    table_rows: list[list[str]],
) -> dict:
    """Build the dense diagnostic regression page without a page model."""
    from pptd_engine.document import load_yaml

    entries = sorted(template_dir.glob("*.pptd"))
    if not entries:
        raise ValueError("模板转换结果缺少 PPTD 入口。")
    template = load_yaml(entries[0])
    size = template.get("size") or [960, 540]
    elements: list[dict] = []
    background = _extract_body_background(template_dir)
    if (
        isinstance(background, dict)
        and isinstance(background.get("src"), str)
        and (template_dir / background["src"]).is_file()
    ):
        source = template_dir / background["src"]
        deck_src = _share_media(
            deck_dir,
            background["src"],
            source.read_bytes(),
        )
        elements.append({
            'elementId': 'host-background',
            'bounds': [0, 0, float(size[0]), float(size[1])],
            'elementType': 'image',
            'src': deck_src,
            'fit': {'mode': 'fill'},
            'crop': {'left': 0, 'top': 0, 'right': 0, 'bottom': 0},
        })
    elements.extend([
        {
            'elementId': 'dense-title',
            'elementType': 'text',
            'bounds': [56, 20, 620, 34],
            'content': {
                'text': title,
                'fontSize': 22,
                'bold': True,
                'color': '#1F2430',
                'wrap': True,
                'align': ['left', 'middle'],
            },
        },
        {
            'elementId': 'dense-subtitle',
            'elementType': 'text',
            'bounds': [56, 58, 680, 28],
            'content': {
                'text': subtitle,
                'fontSize': 12,
                'bold': False,
                'color': '#4B5563',
                'wrap': True,
                'align': ['left', 'middle'],
            },
        },
    ])
    for index, stage in enumerate(stages[:3], 1):
        y = 96.0 + (index - 1) * 46.0
        elements.append({
            'elementId': f'dense-stage-card-{index}',
            'elementType': 'shape',
            'shapeName': 'roundRect',
            'bounds': [56, y, 344, 40],
            'fill': {'type': 'solid', 'color': '#F2F4F7'},
        })
        elements.append({
            'elementId': f'dense-stage-text-{index}',
            'elementType': 'text',
            'bounds': [68, y + 6, 320, 28],
            'content': {
                'text': stage,
                'fontSize': 13,
                'bold': True,
                'color': '#333B47',
                'wrap': True,
                'align': ['left', 'middle'],
            },
        })
    elements.extend([
        {
            'elementId': 'dense-core-bg',
            'elementType': 'shape',
            'shapeName': 'roundRect',
            'bounds': [56, 242, 344, 164],
            'fill': {'type': 'solid', 'color': '#FBE9E9'},
        },
        {
            'elementId': 'dense-core-text',
            'elementType': 'text',
            'bounds': [68, 252, 320, 144],
            'content': {
                'text': core_judgment,
                'fontSize': 12,
                'bold': False,
                'color': '#333B47',
                'wrap': True,
                'align': ['left', 'top'],
            },
        },
        {
            'elementId': 'dense-metrics-title',
            'elementType': 'text',
            'bounds': [424, 92, 400, 24],
            'content': {
                'text': metrics_title,
                'fontSize': 14,
                'bold': True,
                'color': '#9B1C1C',
                'wrap': True,
                'align': ['left', 'middle'],
            },
        },
    ])
    for index, metric in enumerate(metrics[:4], 1):
        column = (index - 1) % 2
        row = (index - 1) // 2
        x = 424.0 + column * 244.0
        y = 120.0 + row * 92.0
        elements.append({
            'elementId': f'dense-metric-card-{index}',
            'elementType': 'shape',
            'shapeName': 'roundRect',
            'bounds': [x, y, 236, 82],
            'fill': {'type': 'solid', 'color': '#F2F4F7'},
        })
        elements.append({
            'elementId': f'dense-metric-value-{index}',
            'elementType': 'text',
            'bounds': [x + 10, y + 8, 216, 28],
            'content': {
                'text': metric['value'],
                'fontSize': 20,
                'bold': True,
                'color': '#9B1C1C',
                'wrap': True,
                'align': ['left', 'middle'],
            },
        })
        elements.append({
            'elementId': f'dense-metric-label-{index}',
            'elementType': 'text',
            'bounds': [x + 10, y + 40, 216, 34],
            'content': {
                'text': metric['label'],
                'fontSize': 12,
                'bold': False,
                'color': '#333B47',
                'wrap': True,
                'align': ['left', 'top'],
            },
        })
    table_rows = [
        [
            {'text': str(cell), 'fontSize': 11}
            for cell in row
        ]
        for row in [table_columns, *table_rows]
    ]
    row_count = len(table_rows)
    elements.append({
        'elementId': 'dense-comparison-table',
        'elementType': 'table',
        'bounds': [424, 306, 480, 210],
        'columnWidths': [0.18, 0.41, 0.41],
        'rowHeights': [28 / 210, *([182 / 3 / 210] * (row_count - 1))],
        'rows': table_rows,
    })
    return {'elements': elements, 'notes': ''}


def _planned_body_blocks(text: str) -> list[str]:
    """Split plan copy into at most four ordered blocks without rewriting it."""
    source = str(text or '').strip()
    if not source:
        return []
    segments = [
        value.strip()
        for value in re.findall(r'.*?[。；;]|.+$', source, re.S)
        if value.strip()
    ]
    if not segments:
        return [source]
    if len(segments) <= 4:
        return segments
    groups = []
    for group_index in range(4):
        start = round(group_index * len(segments) / 4)
        stop = round((group_index + 1) * len(segments) / 4)
        block = ''.join(segments[start:stop]).strip()
        if block:
            groups.append(block)
    return groups


def _hex_rgb(value: str):
    match = re.fullmatch(r'#([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?', str(value or ''))
    if not match:
        return None
    digits = match.group(1)
    return tuple(int(digits[index:index + 2], 16) for index in (0, 2, 4))


def _luminance(rgb) -> float:
    def channel(value: float) -> float:
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    red, green, blue = (channel(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _template_body_page_refs(
    template_dir: Path,
    refs: list[str],
) -> list[str]:
    """Select user-template body pages without assuming fixed page numbers."""
    from pptd_engine.document import load_yaml

    candidates = list(refs[1:-1])
    if not candidates:
        candidates = list(refs)
    body_pages = []
    for ref in candidates:
        try:
            page = load_yaml(Path(template_dir) / ref)
        except (OSError, ValueError):
            continue
        elements = [
            item for item in page.get("elements") or []
            if isinstance(item, dict)
        ]
        visible = " ".join(
            _visible_text(item.get("content") or {})
            for item in elements
            if item.get("elementType") == "text"
        ).strip()
        normalized = re.sub(r"\s+", "", visible).lower()
        if any(
            marker in normalized
            for marker in ("目录", "目錄", "contents", "agenda", "汇报提纲")
        ):
            continue
        text_elements = sum(
            item.get("elementType") == "text"
            for item in elements
        )
        if text_elements >= 2 and visible:
            body_pages.append(ref)
    return body_pages or candidates


def template_style_profile(template_dir: Path) -> dict:
    """Extract reusable body typography and colors from the template itself."""
    from pptd_engine.document import load_yaml

    entries = sorted(Path(template_dir).glob("*.pptd"))
    if not entries:
        raise ValueError("模板转换结果缺少 PPTD 入口。")
    document = load_yaml(entries[0])
    refs = list(document.get("pages") or [])
    colors: Counter = Counter()
    fonts: Counter = Counter()
    sizes: Counter = Counter()
    for ref in _template_body_page_refs(Path(template_dir), refs):
        try:
            page = load_yaml(Path(template_dir) / ref)
        except (OSError, ValueError):
            continue
        for element in page.get("elements") or []:
            if not isinstance(element, dict):
                continue
            fill = (element.get("fill") or {}).get("color")
            if isinstance(fill, str) and re.fullmatch(r'#[0-9a-fA-F]{6,8}', fill):
                colors[fill[:7].upper()] += 1
            border = (element.get("border") or {}).get("color")
            if isinstance(border, str) and re.fullmatch(r'#[0-9a-fA-F]{6,8}', border):
                colors[border[:7].upper()] += 1
            if element.get("elementType") != "text":
                continue
            content = element.get("content") or {}
            if isinstance(content.get("color"), str):
                color = content["color"]
                if re.fullmatch(r'#[0-9a-fA-F]{6,8}', color):
                    colors[color[:7].upper()] += 1
            markup = str(content.get("text") or "")
            colors.update(
                value[:7].upper()
                for value in re.findall(
                    r'color:\s*(#[0-9a-fA-F]{6,8})',
                    markup,
                )
            )
            fonts.update(
                value.strip().strip('"\'')
                for value in re.findall(r'font-family:([^;\"]+)', markup)
                if value.strip()
            )
            sizes.update(
                round(float(value), 1)
                for value in re.findall(r'font-size:([0-9.]+)px', markup)
            )
            if isinstance(content.get("fontSize"), (int, float)):
                sizes[round(float(content["fontSize"]), 1)] += 1
    color_info = {
        value: (_hex_rgb(value), _luminance(_hex_rgb(value)))
        for value in colors
        if _hex_rgb(value)
    }
    non_neutral = [
        (value, count)
        for value, count in colors.items()
        if value in color_info
        and max(color_info[value][0]) - min(color_info[value][0]) > 20
    ]
    dark_neutral = [
        (value, count)
        for value, count in colors.items()
        if value in color_info
        and max(color_info[value][0]) - min(color_info[value][0]) <= 20
        and color_info[value][1] < 0.4
    ]
    light_neutral = [
        (value, count)
        for value, count in colors.items()
        if value in color_info
        and max(color_info[value][0]) - min(color_info[value][0]) <= 20
        and color_info[value][1] > 0.8
    ]
    strong_accents = [
        (value, count)
        for value, count in non_neutral
        if color_info[value][1] < 0.45
    ]
    accent = max(
        strong_accents or non_neutral,
        key=lambda item: item[1],
        default=('#E50011', 0),
    )[0]
    text = max(dark_neutral, key=lambda item: item[1], default=('#000000', 0))[0]
    muted = max(
        (
            (value, count)
            for value, count in dark_neutral
            if value != text
        ),
        key=lambda item: item[1],
        default=(text, 0),
    )[0]
    surface = max(light_neutral, key=lambda item: item[1], default=('#FFFFFF', 0))[0]
    light_accents = [
        (value, count)
        for value, count in non_neutral
        if value != accent and color_info[value][1] > 0.3
    ]
    border = max(light_accents, key=lambda item: item[1], default=(accent, 0))[0]
    subtle_candidates = [
        (value, count)
        for value, count in light_accents
        if value != border
    ]
    subtle = max(
        subtle_candidates,
        key=lambda item: item[1],
        default=(border, 0),
    )[0]
    title_sizes = [size for size in sizes if size >= 24]
    body_sizes = [size for size in sizes if 11 <= size <= 16]
    title_size = max(
        title_sizes,
        key=lambda size: sizes[size],
        default=30.0,
    )
    body_size = max(
        body_sizes,
        key=lambda size: sizes[size],
        default=13.0,
    )
    subtitle_size = max(12.0, min(18.0, body_size + 3.0))
    return {
        'fontFamily': fonts.most_common(1)[0][0] if fonts else '微软雅黑',
        'colors': {
            'accent': accent,
            'text': text,
            'muted': muted,
            'surface': surface,
            'border': border,
            'subtle': subtle,
        },
        'sizes': {
            'title': title_size,
            'subtitle': subtitle_size,
            'body': body_size,
        },
    }


def template_theme(profile: dict) -> dict:
    """Convert the extracted profile into a PPTD theme."""
    colors = profile.get('colors') or {}
    sizes = profile.get('sizes') or {}
    family = profile.get('fontFamily') or '微软雅黑'
    return {
        'colors': {
            'primary': colors.get('accent', '#E50011'),
            'ink': colors.get('text', '#000000'),
            'muted': colors.get('muted', '#444444'),
            'paper': colors.get('surface', '#FFFFFF'),
            'border': colors.get('border', '#E50011'),
        },
        'textStyles': {
            'title': {
                'fontFamily': family,
                'fontSize': sizes.get('title', 30),
                'color': '$primary',
                'bold': True,
            },
            'subtitle': {
                'fontFamily': family,
                'fontSize': sizes.get('subtitle', 16),
                'color': '$muted',
            },
            'body': {
                'fontFamily': family,
                'fontSize': sizes.get('body', 13),
                'color': '$ink',
                'lineHeight': 1.45,
            },
        },
    }


def build_template_body_flow(
    template_dir: Path,
    deck_dir: Path,
    *,
    title: str,
    subtitle: str,
    content: str,
    style_profile: dict | None = None,
    layout: str = 'editorial',
) -> dict:
    """Build a compact editorial body page from the approved plan copy."""
    from pptd_engine.document import load_yaml

    entries = sorted(template_dir.glob("*.pptd"))
    if not entries:
        raise ValueError("模板转换结果缺少 PPTD 入口。")
    template = load_yaml(entries[0])
    profile = style_profile or template_style_profile(template_dir)
    colors = profile.get('colors') or {}
    sizes = profile.get('sizes') or {}
    family = profile.get('fontFamily') or '微软雅黑'
    size = template.get("size") or [960, 540]
    blocks = _planned_body_blocks(content)
    if not blocks:
        raise ValueError("计划正文为空，无法构建正文页。")
    elements: list[dict] = [
        {
            'elementId': 'body-flow-title',
            'elementType': 'text',
            'bounds': [64, 34, 700, 44],
            'content': {
                'text': title,
                'fontFamily': family,
                'fontSize': sizes.get('title', 30),
                'bold': True,
                'color': colors.get('accent', '#E50011'),
                'wrap': True,
                'align': ['left', 'middle'],
            },
        },
        {
            'elementId': 'body-flow-subtitle',
            'elementType': 'text',
            'bounds': [64, 82, 820, 24],
            'content': {
                'text': subtitle,
                'fontFamily': family,
                'fontSize': sizes.get('subtitle', 16),
                'bold': False,
                'color': colors.get('muted', '#444444'),
                'wrap': True,
                'align': ['left', 'middle'],
            },
        },
    ]
    top = 122.0
    bottom = float(size[1]) - 72.0
    gap = 12.0
    if layout in {'cards', 'columns'} and all(
        len(block) <= 150 for block in blocks
    ):
        columns = (
            2 if layout == 'columns' or len(blocks) in (2, 4)
            else 3
        )
        columns = min(columns, len(blocks))
        rows = math.ceil(len(blocks) / columns)
        card_gap = 14.0
        horizontal_gap = 16.0
        usable_width = 832.0
        card_width = (
            usable_width - horizontal_gap * (columns - 1)
        ) / columns
        card_height = (
            bottom - top - card_gap * (rows - 1)
        ) / rows
        for index, block in enumerate(blocks, 1):
            column = (index - 1) % columns
            row = (index - 1) // columns
            x = 64.0 + column * (card_width + horizontal_gap)
            y = top + row * (card_height + card_gap)
            elements.append({
                'elementId': f'body-card-{index}',
                'elementType': 'shape',
                'shapeName': 'roundRect',
                'bounds': [x, y, card_width, card_height],
                'fill': {
                    'type': 'solid',
                    'color': colors.get('surface', '#FFFFFF'),
                },
                'border': {
                    'width': 1,
                    'color': colors.get(
                        'border',
                        colors.get('accent', '#E50011'),
                    ),
                    'style': 'solid',
                },
            })
            font_size = float(sizes.get('body', 13))
            while font_size > 12 and len(block) > (
                (card_width - 32) / font_size
                * ((card_height - 28) / (font_size * 1.45))
            ):
                font_size -= 1
            elements.append({
                'elementId': f'body-card-text-{index}',
                'elementType': 'text',
                'bounds': [
                    x + 16,
                    y + 14,
                    card_width - 32,
                    card_height - 28,
                ],
                'content': {
                    'text': block,
                    'fontFamily': family,
                    'fontSize': font_size,
                    'bold': False,
                    'color': colors.get('text', '#000000'),
                    'wrap': True,
                    'align': ['left', 'top'],
                },
            })
        return {'elements': elements, 'notes': ''}
    row_height = bottom - top
    if len(blocks) > 1:
        row_height = (
            bottom - top - gap * (len(blocks) - 1)
        ) / len(blocks)
    for index, block in enumerate(blocks, 1):
        y = top + (index - 1) * (row_height + gap)
        elements.append({
            'elementId': f'body-flow-accent-{index}',
            'elementType': 'shape',
            'shapeName': 'rect',
            'bounds': [64, y, 4, row_height],
            'fill': {
                'type': 'solid',
                'color': colors.get('accent', '#E50011'),
            },
        })
        font_size = float(sizes.get('body', 13))
        while (
            font_size > 12
            and len(block) * font_size * 1.45
            > (780 / font_size) * row_height
        ):
            font_size -= 1
        elements.append({
            'elementId': f'body-flow-text-{index}',
            'elementType': 'text',
            'bounds': [84, y, 780, row_height],
            'content': {
                'text': block,
                'fontFamily': family,
                'fontSize': font_size,
                'bold': False,
                'color': colors.get('text', '#000000'),
                'wrap': True,
                'align': ['left', 'top'],
            },
        })
    return {'elements': elements, 'notes': ''}


def _is_closing_like(page_data: dict, path: str = "") -> bool:
    """Only explicit closing wording or a closing-style page name counts."""
    texts = [_visible_text(e.get("content") or {}) for e in page_data.get("elements", []) if isinstance(e, dict) and e.get("elementType") == "text"]
    joined = (" ".join(t for t in texts if t) + " " + str(path)).lower()
    return any(marker in joined for marker in CLOSING_MARKERS)


def _apply_template_cover_closing(template_dir: Path, deck_dir: Path, document: dict, pages: list[dict]) -> None:
    """Use template cover/closing only when the deck declares those roles."""
    from pptd_engine.document import load_yaml

    entries = sorted(template_dir.glob("*.pptd"))
    if not entries:
        return
    template = load_yaml(entries[0])
    refs = template.get("pages", [])
    if len(refs) < 1 or not pages:
        return
    def kind(page):
        meta = page.get("data", {}).get("meta")
        return str(meta.get("kind") or "").strip().lower() if isinstance(meta, dict) else ""

    typed_deck = any(kind(page) for page in pages)
    first_kind = kind(pages[0])
    cover_template = None
    if not typed_deck or first_kind in {"", "cover"}:
        cover_template = load_yaml(template_dir / refs[0])
        _copy_page_media(cover_template, template_dir, deck_dir)
        cover_ids = {
            element.get("elementId")
            for element in cover_template.get("elements", [])
            if isinstance(element, dict) and element.get("elementId")
        }
        current_cover_ids = {
            element.get("elementId")
            for element in pages[0].get("data", {}).get("elements", [])
            if isinstance(element, dict) and element.get("elementId")
        }
        cover = (
            json.loads(json.dumps(pages[0]["data"]))
            if cover_ids and cover_ids.issubset(current_cover_ids)
            else _fill_template_page(
                cover_template,
                _template_texts(pages[0]["data"]),
                keep_unfilled=False,
            )
        )
        _fit_cover_slots(cover, document.get("size"))
        pages[0]["data"] = cover
    if len(refs) < 2:
        document["pages"] = [p["path"] for p in pages]
        return
    if len(pages) >= 2 and _is_closing_like(pages[-1]["data"], pages[-1]["path"]):
        closing_template = load_yaml(template_dir / refs[-1])
        _copy_page_media(closing_template, template_dir, deck_dir)
        closing_ids = {
            element.get("elementId")
            for element in closing_template.get("elements", [])
            if isinstance(element, dict) and element.get("elementId")
        }
        current_closing_ids = {
            element.get("elementId")
            for element in pages[-1].get("data", {}).get("elements", [])
            if isinstance(element, dict) and element.get("elementId")
        }
        pages[-1]["data"] = (
            json.loads(json.dumps(pages[-1]["data"]))
            if closing_ids and closing_ids.issubset(current_closing_ids)
            else _fill_template_page(
                closing_template,
                _template_texts(pages[-1]["data"], closing=True),
                keep_unfilled=False,
            )
        )
    elif not typed_deck:
        closing_template = load_yaml(template_dir / refs[-1])
        _copy_page_media(closing_template, template_dir, deck_dir)
        existing = {p["path"] for p in pages}
        path, n = "pages/closing.page", 1
        while path in existing:
            path = f"pages/closing-{n}.page"
            n += 1
        pages.append({
            "path": path,
            "data": build_template_closing(
                template_dir,
                deck_dir,
                text="感谢观看",
            ),
        })
    document["pages"] = [p["path"] for p in pages]


def _apply_host_transforms(entry: Path, document: dict, pages: list[dict], template_info: dict | None) -> None:
    """Layer the company template onto the model's deck, in one place."""
    for page in pages:
        normalize_table_tracks(page.get("data") or {})
    if template_info:
        template_dir = Path(template_info["pptd"])
        _apply_template_cover_closing(template_dir, entry.parent, document, pages)
        _apply_background(entry.parent, document, pages, _extract_body_background(template_dir), template_dir)
        _normalize_table_fonts(document, pages)
        _apply_chrome(entry.parent, document, pages, _extract_chrome(template_dir), template_dir,
                      skip_first=True, skip_last=True)
        configured_background = _configured_background_path()
        if configured_background:
            _apply_configured_background(entry.parent, document, pages, configured_background)
    normalize_pages(pages)


def _configured_background_path() -> Path | None:
    """Resolve an explicit or conventional workspace background image."""
    candidates = []
    configured = os.getenv("PPT_BACKGROUND_IMAGE", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend((ROOT / "tmp" / "background.png", ROOT / "tmp" / "backgound.png"))
    for candidate in candidates:
        path = candidate.resolve()
        if path.is_file() and path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            return path
    return None


def _apply_configured_background(entry: Path, document: dict, pages: list[dict], source: Path) -> None:
    """Use the configured image for body pages instead of the template's body art."""
    size = document.get("size")
    if not isinstance(size, list) or len(size) != 2:
        return
    content = source.read_bytes()
    suffix = source.suffix.lower()
    media_name = f"host-background-{hashlib.sha256(content).hexdigest()[:12]}{suffix}"
    target = entry / "media" / media_name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file() or target.read_bytes() != content:
        target.write_bytes(content)
    for page in pages[1:-1]:
        data = page.get("data")
        if not isinstance(data, dict):
            continue
        _install_background_image(data, size, f"media/{media_name}")
