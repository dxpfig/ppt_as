"""Company-template composition and host transforms for expert mode."""
from __future__ import annotations

from collections import Counter
import hashlib
import html
import json
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
    moved: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
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
        for group, present in ((top, has_top), (bottom, has_bottom)):
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
        if one_line < maximum:
            title["content"] = {**content, "text": _cover_markup(str(content.get("text", "")), [text], one_line)}
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


def _is_closing_like(page_data: dict, path: str = "") -> bool:
    """Only explicit closing wording or a closing-style page name counts."""
    texts = [_visible_text(e.get("content") or {}) for e in page_data.get("elements", []) if isinstance(e, dict) and e.get("elementType") == "text"]
    joined = (" ".join(t for t in texts if t) + " " + str(path)).lower()
    return any(marker in joined for marker in CLOSING_MARKERS)


def _apply_template_cover_closing(template_dir: Path, deck_dir: Path, document: dict, pages: list[dict]) -> None:
    """Use the template's own cover and closing designs for the first/last page."""
    from pptd_engine.document import load_yaml

    entries = sorted(template_dir.glob("*.pptd"))
    if not entries:
        return
    template = load_yaml(entries[0])
    refs = template.get("pages", [])
    if len(refs) < 1 or not pages:
        return
    cover_template = load_yaml(template_dir / refs[0])
    _copy_page_media(cover_template, template_dir, deck_dir)
    cover = _fill_template_page(cover_template, _template_texts(pages[0]["data"]), keep_unfilled=False)
    _fit_cover_slots(cover, document.get("size"))
    pages[0]["data"] = cover
    if len(refs) < 2:
        document["pages"] = [p["path"] for p in pages]
        return
    closing_template = load_yaml(template_dir / refs[-1])
    _copy_page_media(closing_template, template_dir, deck_dir)
    if len(pages) >= 2 and _is_closing_like(pages[-1]["data"], pages[-1]["path"]):
        pages[-1]["data"] = _fill_template_page(
            closing_template, _template_texts(pages[-1]["data"], closing=True), keep_unfilled=False)
    else:
        existing = {p["path"] for p in pages}
        path, n = "pages/closing.page", 1
        while path in existing:
            path = f"pages/closing-{n}.page"
            n += 1
        pages.append({"path": path, "data": _fill_template_page(closing_template, ["感谢观看"], keep_unfilled=False)})
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
