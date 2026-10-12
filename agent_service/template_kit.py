"""The template's body-page kit: what every new page must keep, and what it may compose.

The user's contract for rebuilt body pages is narrow and explicit: keep the page
title treatment, the top-right logo, the bottom-right company mark, the template's
type sizes/families/colours and the page background. Everything else - how many
cards, how they are arranged, whether a page is a table or a set of key numbers -
is composed from the content.

So this module extracts those fixed pieces verbatim from the template's own pages
(they are copied, not redrawn) and exposes the tokens the composer styles new
elements with. Nothing else is carried over.
"""
from __future__ import annotations

import copy
import math
import re
from collections import Counter, defaultdict

from . import template_spec

FULL_PAGE_RATIO = 0.9
CHROME_TOP = 0.16
CHROME_BOTTOM = 0.88


def _role_of(element: dict, tokens: dict) -> str | None:
    """Which token role a template element plays, from its own formatting."""
    content = element.get('content') or {}
    sizes = template_spec.FONT_SIZE.findall(str(content.get('text', '')))
    if not sizes:
        return None
    size = max(float(value) for value in sizes)
    styles = tokens.get('textStyles') or {}
    best, distance = None, None
    for name, style in styles.items():
        if style.get('fontSize') is None:
            continue
        gap = abs(style['fontSize'] - size)
        if distance is None or gap < distance:
            best, distance = name, gap
    return best


def extract(pages: list[dict], tokens: dict, components: list[dict]) -> dict:
    """Pull the fixed pieces out of a template's page set.

    ``pages`` are the template's own page payloads; ``tokens`` and ``components``
    come from the measured analysis (`template_spec`).
    """
    total = len(pages)
    counter: dict[tuple, list[dict]] = defaultdict(list)
    for page in pages:
        for element in page.get('elements') or []:
            bounds = element.get('bounds') or []
            if len(bounds) != 4:
                continue
            key = (element.get('elementType'), element.get('shapeName'), round(bounds[2]), round(bounds[3]))
            counter[key].append({'element': element, 'bounds': bounds})

    def repeated(predicate, *, minimum: int = 3) -> dict | None:
        hits = [(key, items) for key, items in counter.items() if predicate(key, items) and len(items) >= minimum]
        if not hits:
            return None
        key, items = max(hits, key=lambda item: len(item[1]))
        return {'key': key, 'uses': len(items), 'element': items[0]['element']}

    width, height = 960, 540
    if pages and isinstance(pages[0].get('size'), list):
        width, height = pages[0]['size']
    background = repeated(
        lambda key, items: key[0] == 'image' and key[2] >= width * FULL_PAGE_RATIO
        and key[3] >= height * FULL_PAGE_RATIO)
    logo = repeated(
        lambda key, items: key[0] == 'image' and 0.1 <= key[2] / width <= 0.35 and 0.05 <= key[3] / height <= 0.3
        and items[0]['bounds'][1] <= height * CHROME_TOP and items[0]['bounds'][0] >= width * 0.5)
    footer_image = repeated(
        lambda key, items: key[0] == 'image' and items[0]['bounds'][1] >= height * CHROME_BOTTOM
        and key[2] <= width * 0.2)
    footer_text = repeated(
        lambda key, items: key[0] == 'text' and items[0]['bounds'][1] >= height * CHROME_BOTTOM,
        minimum=2)

    # The page title treatment: the biggest text block in the top band of the
    # content pages, together with the decorations hugging it (rule + accent block).
    # Fixed pieces are matched by their measured key, not by object identity: the
    # same background/logo shape is a different object on every page, so an
    # identity test would copy page 3's own logo into the title band.
    chrome_keys = {tuple(item['key']) for item in (background, logo, footer_image, footer_text) if item}

    def _key(element: dict) -> tuple:
        bounds = element.get('bounds') or [0, 0, 0, 0]
        return (element.get('elementType'), element.get('shapeName'), round(bounds[2]), round(bounds[3]))

    title_element = None
    title_band = []
    for page in pages[1:-1] if total > 2 else pages:
        texts = [element for element in page.get('elements') or []
                 if element.get('elementType') == 'text'
                 and (element.get('bounds') or [0, 0, 0, 0])[1] <= height * CHROME_TOP]
        if not texts:
            continue
        biggest = max(texts, key=lambda element: max(
            (float(value) for value in template_spec.FONT_SIZE.findall(
                str((element.get('content') or {}).get('text', '')))), default=0.0))
        if title_element is None:
            title_element = biggest
            band = (biggest.get('bounds') or [0, 0, 0, 0])[1]
            # Only what hugs the title itself: the accent block, the hairline rule
            # and any small label. Background, logo and the lead paragraph are
            # carried separately, and body copy is never "chrome".
            title_band = [element for element in page.get('elements') or []
                          if element is not biggest
                          and _key(element) not in chrome_keys
                          # The accent block sits beside the title and the hairline
                          # rule rides just under it, so the band is measured a
                          # little wider than the title's own height.
                          and abs((element.get('bounds') or [0, 0, 0, 0])[1] - band) <= 70
                          and (element.get('bounds') or [0, 0, 0, 0])[1] <= height * CHROME_TOP
                          and (element.get('elementType') != 'text'
                               or max((float(value) for value in template_spec.FONT_SIZE.findall(
                                   str((element.get('content') or {}).get('text', '')))), default=99) <= 14)]
    title_style = _role_of(title_element, tokens) if title_element else None
    return {
        'size': [width, height],
        'background': copy.deepcopy(background['element']) if background else None,
        'logo': copy.deepcopy(logo['element']) if logo else None,
        'footerImage': copy.deepcopy(footer_image['element']) if footer_image else None,
        'footerText': copy.deepcopy(footer_text['element']) if footer_text else None,
        'titleElement': copy.deepcopy(title_element) if title_element else None,
        'titleDecorations': copy.deepcopy(title_band),
        'titleStyle': title_style,
        'tokens': tokens,
        'components': components,
        'evidence': {
            'pages': total,
            'logoUses': (logo or {}).get('uses', 0),
            'backgroundUses': (background or {}).get('uses', 0),
            'footerUses': (footer_text or {}).get('uses', 0),
        },
    }


def content_area(kit: dict, tokens: dict) -> dict:
    """Where composed content may live: below the title, above the footer."""
    width, height = kit.get('size') or [960, 540]
    title = kit.get('titleElement') or {}
    bounds = title.get('bounds') or [40, 20, 600, 36]
    metrics = (tokens or {}).get('metrics') or {}
    left = metrics.get('leftMargin') or bounds[0] or 40
    top = round(bounds[1] + bounds[3] + 30, 1)
    bottom = round(height * CHROME_BOTTOM - 40, 1)
    return {'x': left, 'y': top, 'width': round(width - left * 2, 1), 'height': round(bottom - top, 1)}


LAYOUTS = ('cards', 'bullets', 'steps', 'keyNumbers', 'table', 'comparison')

# The renderer draws each composed paragraph at this line height, so the height a
# text needs is computed here exactly the way the browser will measure it: lines
# that fit the box width, times the line height, times the font size. The review
# renders the deck and compares the two, so both sides must agree.
LINE_SPACING = 1.45
# What the renderer uses when a text element names no size of its own.
DEFAULT_TEXT_SIZE = 18.0
SLACK = 4
CJK_RATIO = 1.0
LATIN_RATIO = 0.56


def plain(markup: object) -> str:
    """The words inside a rich-text string."""
    return re.sub(r'<[^>]+>', '', str(markup or '')).strip()


def text_width(text: str, size: float) -> float:
    """Rough advance width: a CJK glyph is one em, latin letters about half."""
    return sum(CJK_RATIO if ord(character) > 0x2E7F else LATIN_RATIO for character in str(text)) * size


def lines_needed(text: str, width: float, size: float) -> int:
    if not str(text).strip() or width <= 0 or size <= 0:
        return 0
    # Paragraph breaks are lines of their own, and a long paragraph wraps.
    total = 0
    for paragraph in str(text).split('\n'):
        total += max(1, math.ceil(text_width(paragraph, size) / (width * 0.97)))
    return total


def block_height(text: str, width: float, size: float) -> float:
    """Height the renderer will need for this text at this width."""
    return round(lines_needed(text, width, size) * LINE_SPACING * size, 1)


def characters_that_fit(width: float, height: float, size: float) -> int:
    """How many CJK characters stay inside a box at this size."""
    if size <= 0:
        return 0
    lines = int((height - 1) / (LINE_SPACING * size))
    return max(0, lines) * max(0, int(width * 0.97 / size))

def _style(tokens: dict, role: str, *, fallback: str = 'body') -> dict:
    styles = (tokens or {}).get('textStyles') or {}
    style = styles.get(role) or styles.get(fallback) or {}
    return {
        'fontFamily': style.get('fontFamily') or '微软雅黑',
        'fontSize': style.get('fontSize') or 13,
        'color': style.get('color') or '#000000',
        'bold': bool(style.get('bold')),
    }


def _markup(text: str, style: dict, *, align: str | None = None) -> str:
    """Rich text in the template's own shape: paragraph + styled span."""
    declarations = [f"font-family:{style['fontFamily']}", f"font-size:{style['fontSize']}px",
                    f"color:{style['color']}"]
    paragraph = f'<p style="line-height:{LINE_SPACING}{";text-align:" + align if align else ""}">'
    body = f'<span style="{";".join(declarations)}">{text}</span>'
    wrapped = f'<strong>{body}</strong>' if style.get('bold') else body
    return paragraph + wrapped + '</p>'


def _element(identity: str, kind: str, bounds, **fields) -> dict:
    return {'elementId': identity, 'elementType': kind, 'bounds': [round(value, 1) for value in bounds], **fields}


def compose_body(kit: dict, plan: dict, *, page_id: str) -> dict:
    """Compose one body page: template chrome and tokens, free layout inside.

    ``plan`` carries ``title``, an optional ``lead``, a ``layout`` from LAYOUTS and
    the layout's own content. Only the fixed pieces from the kit are copied - the
    background, the top-right logo, the bottom-right company mark, the title
    treatment - and every composed element is styled with the template's tokens.
    """
    tokens = kit.get('tokens') or {}
    elements: list[dict] = []

    def copy_fixed(element: dict | None, suffix: str) -> None:
        if element:
            clone = copy.deepcopy(element)
            clone['elementId'] = f'{page_id}-{suffix}'
            # A piece of chrome is copied, not redrawn - but it still has to hold
            # its own words at the size the template drew them.
            if clone.get('elementType') == 'text':
                fit(clone, kit)
            elements.append(clone)

    # The kit's contract: these are kept, not redrawn.
    copy_fixed(kit.get('background'), 'bg')
    copy_fixed(kit.get('logo'), 'logo')
    copy_fixed(kit.get('footerImage'), 'footer-logo')
    copy_fixed(kit.get('footerText'), 'footer-text')
    title = kit.get('titleElement')
    if title and plan.get('title'):
        clone = copy.deepcopy(title)
        clone['elementId'] = f'{page_id}-title'
        clone['content'] = rewrite(clone.get('content') or {}, str(plan['title']))
        fit(clone, kit)
        elements.append(clone)
    for index, decoration in enumerate(kit.get('titleDecorations') or []):
        clone = copy.deepcopy(decoration)
        clone['elementId'] = f'{page_id}-deco{index}'
        elements.append(clone)

    layout = plan.get('layout') or 'bullets'
    if layout not in LAYOUTS:
        raise ValueError(f'未知版式 {layout!r}；可用：{LAYOUTS}')
    for part in layout_parts(layout, plan, kit):
        identity = f"{page_id}-{part['identity']}"
        if part['type'] == 'text':
            elements.append(_text_element(identity, part['bounds'], part['text'], part['style'],
                                          align=part.get('align', 'left')))
        elif part['type'] == 'table':
            _table(tokens, elements, part, identity)
        else:
            elements.append(_element(identity, part['type'], part['bounds'], **part['fields']))
    return {'pageType': 'body', 'elements': elements}


def _columns(count: int, maximum: int = 4) -> tuple[int, int]:
    """Rows and columns for ``count`` cards: fill the grid, then balance it.

    Five cards read as a 2x3 grid with one hole, so the shape is chosen by how
    many cells the last row would leave empty - and a partial row is centred
    rather than left hanging under the first column.
    """
    if count <= 1:
        return 1, 1
    best = None
    for columns in range(2, min(maximum, count) + 1):
        rows = -(-count // columns)
        # Fewest rows first, then the fewest empty cells in the last one.
        score = (rows, rows * columns - count)
        if best is None or score < best[0]:
            best = (score, rows, columns)
    return best[1], best[2]


def _item_text(item: dict) -> tuple[str, str]:
    """An item's own words: its heading and its bullet lines."""
    label = str(item.get('label') or '').strip()
    bullets = [str(value).strip() for value in (item.get('bullets') or []) if str(value).strip()]
    return label, '\n'.join(bullets)


def _bulleted(bullets: str) -> str:
    return ('· ' + bullets.replace('\n', '\n· ')) if bullets else ''


def layout_parts(layout: str, plan: dict, kit: dict) -> list[dict]:
    """Every element a composed page needs, sized so its own words fit.

    The composer builds these parts and the plan validator measures the same
    boxes, so a plan the validator accepts is a page the renderer will not report
    as overflowing.
    """
    tokens = kit.get('tokens') or {}
    area = content_area(kit, tokens)
    items = [item for item in (plan.get('items') or []) if isinstance(item, dict)]
    parts: list[dict] = []
    top = area['y']
    if plan.get('lead'):
        style = _style(tokens, 'lead')
        style['bold'] = True
        height = block_height(str(plan['lead']), area['width'] - 4, style['fontSize']) + SLACK
        parts.append(_text_part('lead', [area['x'], top, area['width'], height], str(plan['lead']), style))
        top += height + 12
    room = area['y'] + area['height'] - top
    if layout == 'table':
        columns = [str(value) for value in (plan.get('table') or {}).get('columns') or []]
        rows = [[str(cell) for cell in row] for row in (plan.get('table') or {}).get('rows') or []]
        if columns and rows:
            row_height = min(46.0, room / (len(rows) + 1))
            parts.append({'type': 'table', 'identity': 'table',
                          'bounds': [area['x'], top, area['width'], round(row_height * (len(rows) + 1), 1)],
                          'columns': columns, 'rows': rows})
        return parts
    if layout == 'cards':
        return parts + _centre(_card_parts(tokens, items, area, top, room), top, room)
    if layout == 'steps':
        return parts + _centre(_step_parts(tokens, items, area, top, room), top, room)
    if layout == 'keyNumbers':
        return parts + _centre(_key_number_parts(tokens, items, area, top, room), top, room)
    return parts + _centre(_bullet_parts(tokens, items, area, top, room,
                                         columns=2 if layout == 'comparison' else 1), top, room)


def _centre(parts: list[dict], top: float, room: float) -> list[dict]:
    """Balance a composed block in the band it was given.

    A page whose words need less room than the template leaves should read as a
    deliberate band, not as content clinging to the title with a hole beneath it.
    """
    if not parts:
        return parts
    bottom = max(part['bounds'][1] + part['bounds'][3] for part in parts)
    start = min(part['bounds'][1] for part in parts)
    leftover = room - (bottom - start)
    if leftover < 24:
        return parts
    offset = round(leftover / 2, 1)
    for part in parts:
        part['bounds'][1] = round(part['bounds'][1] + offset, 1)
    return parts


def _text_part(identity: str, bounds, text: str, style: dict, *, align: str = 'left') -> dict:
    return {'type': 'text', 'identity': identity, 'bounds': [round(value, 1) for value in bounds],
            'text': text, 'style': style, 'align': align}


def _card_parts(tokens: dict, items: list[dict], area: dict, top: float, room: float) -> list[dict]:
    if not items:
        return []
    rows, columns = _columns(len(items))
    gap = 20.0
    width = (area['width'] - gap * (columns - 1)) / columns
    label_style = _style(tokens, 'lead')
    label_style['color'] = (tokens.get('colors') or {}).get('primary') or label_style['color']
    label_style['bold'] = True
    body_style = _style(tokens, 'body')
    accent = (tokens.get('colors') or {}).get('primary') or '#E50011'
    needed = []
    for item in items:
        label, bullets = _item_text(item)
        needed.append(block_height(label, width - 56, label_style['fontSize'])
                      + block_height(_bulleted(bullets), width - 36, body_style['fontSize']) + 58)
    height = max(needed)
    if rows * height + gap * (rows - 1) > room > 0:
        height = (room - gap * (rows - 1)) / rows
    parts = []
    in_row = {row: len([index for index in range(len(items)) if index // columns == row])
              for row in range(rows)}
    for index, item in enumerate(items):
        row, column = divmod(index, columns)
        # A last row with fewer cards than the grid sits centred under it.
        offset = (columns - in_row[row]) * (width + gap) / 2
        x = area['x'] + offset + column * (width + gap)
        y = top + row * (height + gap)
        identity = f'card{index + 1}'
        parts.append({'type': 'shape', 'identity': f'{identity}-bg', 'bounds': [x, y, width, height],
                      'fields': {'shapeName': 'roundRect', 'adjustments': [6000],
                                 'fill': {'type': 'solid', 'color': '#FFFFFF'},
                                 'border': {'width': 1, 'color': accent, 'style': 'solid'}}})
        parts.append({'type': 'shape', 'identity': f'{identity}-dot', 'bounds': [x + 16, y + 16, 14, 14],
                      'fields': {'shapeName': 'ellipse', 'fill': {'type': 'solid', 'color': accent}}})
        label, bullets = _item_text(item)
        label_height = block_height(label, width - 56, label_style['fontSize']) + SLACK
        parts.append(_text_part(f'{identity}-label', [x + 40, y + 12, width - 56, label_height], label, label_style))
        parts.append(_text_part(f'{identity}-body', [x + 18, y + 44, width - 36, max(20.0, height - 56)],
                                _bulleted(bullets), body_style))
    return parts


def _step_parts(tokens: dict, items: list[dict], area: dict, top: float, room: float) -> list[dict]:
    if not items:
        return []
    label_style = _style(tokens, 'lead')
    body_style = _style(tokens, 'body')
    accent = (tokens.get('colors') or {}).get('primary') or '#E50011'
    gap = 14.0
    inner = area['width'] - 80
    needed = []
    for item in items:
        label, bullets = _item_text(item)
        needed.append(block_height(label, inner, label_style['fontSize'])
                      + block_height(_bulleted(bullets), inner, body_style['fontSize']) + 24)
    height = max(needed)
    if len(items) * height + gap * (len(items) - 1) > room > 0:
        height = (room - gap * (len(items) - 1)) / len(items)
    parts = []
    for index, item in enumerate(items):
        y = top + index * (height + gap)
        identity = f'step{index + 1}'
        parts.append({'type': 'shape', 'identity': f'{identity}-bg', 'bounds': [area['x'], y, area['width'], height],
                      'fields': {'shapeName': 'roundRect', 'adjustments': [8000],
                                 'fill': {'type': 'solid', 'color': '#FFFFFF'},
                                 'border': {'width': 1, 'color': accent, 'style': 'solid'}}})
        parts.append({**_text_part(f'{identity}-num', [area['x'] + 16, y + height / 2 - 16, 34, 32],
                                   f'{index + 1:02d}', {**label_style, 'bold': True}, align='center'),
                      'generated': True})
        label, bullets = _item_text(item)
        label_height = block_height(label, inner, label_style['fontSize']) + SLACK
        parts.append(_text_part(f'{identity}-label', [area['x'] + 62, y + 10, inner, label_height], label, label_style))
        parts.append(_text_part(f'{identity}-body',
                                [area['x'] + 62, y + 10 + label_height, inner, max(20.0, height - 20 - label_height)],
                                _bulleted(bullets), body_style))
    return parts


def _key_number_parts(tokens: dict, items: list[dict], area: dict, top: float, room: float) -> list[dict]:
    if not items:
        return []
    big = _style(tokens, 'page-title')
    caption = _style(tokens, 'caption')
    columns = max(1, min(4, len(items)))
    width = area['width'] / columns - 20
    parts = []
    for index, item in enumerate(items):
        x = area['x'] + index * (width + 20)
        value = str(item.get('value') or '').strip()
        label = str(item.get('label') or item.get('text') or '').strip()
        value_height = block_height(value, width, big['fontSize']) + SLACK
        parts.append(_text_part(f'num{index + 1}', [x, top, width, value_height], value, big))
        caption_height = max(24.0, block_height(label, width, caption['fontSize']) + SLACK)
        parts.append(_text_part(f'cap{index + 1}', [x, top + value_height, width, caption_height], label, caption))
    return parts


def _bullet_parts(tokens: dict, items: list[dict], area: dict, top: float, room: float,
                  *, columns: int) -> list[dict]:
    body_style = _style(tokens, 'body')
    heading_style = _style(tokens, 'lead')
    heading_style['color'] = (tokens.get('colors') or {}).get('primary') or heading_style['color']
    heading_style['bold'] = True
    width = (area['width'] - 24 * (columns - 1)) / columns
    perspective = max(1, (len(items) + columns - 1) // columns)
    needed = []
    for item in items:
        label, bullets = _item_text(item)
        needed.append(block_height(label, width, heading_style['fontSize'])
                      + block_height(_bulleted(bullets), width, body_style['fontSize']) + 16)
    if not needed:
        return []
    step = max(needed)
    if perspective * step > room > 0:
        step = room / perspective
    parts = []
    for index, item in enumerate(items):
        column, row = divmod(index, perspective)
        x = area['x'] + column * (width + 24)
        y = top + row * step
        label, bullets = _item_text(item)
        label_height = block_height(label, width, heading_style['fontSize']) + SLACK
        if label:
            parts.append(_text_part(f'item{index + 1}-label', [x, y, width, label_height], label, heading_style))
        parts.append(_text_part(f'item{index + 1}', [x, y + label_height, width, max(20.0, step - 8 - label_height)],
                                _bulleted(bullets), body_style))
    return parts


def _text_element(identity: str, bounds, text: str, style: dict, *, align: str = 'left') -> dict:
    """A composed text box: the words, and the container's own type metrics.

    The renderer styles the box itself from the element's ``fontSize`` and
    ``lineHeight``; leaving them out makes every paragraph fall back to the page
    default and wraps into a taller box than the one drawn for it.
    """
    content = {'text': _markup(str(text), style, align=align or 'left'), 'wrap': True,
               'align': [align or 'left', 'top'], 'fontSize': style['fontSize'],
               'fontFamily': style['fontFamily'], 'color': style['color'], 'bold': bool(style.get('bold')),
               'lineHeight': LINE_SPACING}
    return _element(identity, 'text', bounds, content=content)


def _table(tokens: dict, elements: list[dict], part: dict, identity: str) -> None:
    """The one layout that is a real table element rather than composed boxes."""
    columns = part['columns']
    rows = part['rows']
    body = _style(tokens, 'body')
    accent = (tokens.get('colors') or {}).get('primary') or '#E50011'
    border = {'style': 'solid', 'width': 1, 'color': '#333333'}
    cells = []
    header = [{'text': _markup(value, {**body, 'color': '#FFFFFF', 'bold': True}), 'fill': {'type': 'solid', 'color': accent},
               'border': border, 'align': ['center', 'middle']} for value in columns]
    cells.append(header)
    for row in rows:
        cells.append([{'text': _markup(value, body), 'border': border, 'align': ['center', 'middle']} for value in row])
    elements.append(_element(identity, 'table', part['bounds'], rows=cells,
                             columnWidths=[round(1 / len(columns), 4)] * (len(columns) - 1)
                             + [round(1 - round(1 / len(columns), 4) * (len(columns) - 1), 4)],
                             rowHeights=[round(1 / len(cells), 4)] * (len(cells) - 1)
                             + [round(1 - round(1 / len(cells), 4) * (len(cells) - 1), 4)]))


def rewrite(content: dict, text: str) -> dict:
    """The template's own markup with this deck's words, and its type metrics.

    Keeping the paragraph tags is what keeps the example's alignment and its
    trailing empty paragraphs, and the metrics on the content let the renderer
    draw the box at the template's line height instead of the page default.
    """
    from .pptd_generation import _merge_markup, _styled_text, _dominant_style
    result = copy.deepcopy(content)
    template_text = str(result.get('text') or '')
    style = _dominant_style(template_text)
    result['text'] = _merge_markup(template_text, text) or _styled_text(text, style)
    if style.get('fontSize'):
        result['fontSize'] = float(style['fontSize'])
        result['lineHeight'] = LINE_SPACING
        if style.get('fontFamily'):
            result['fontFamily'] = style['fontFamily']
        if style.get('color'):
            result['color'] = style['color']
    return result


def fit(element: dict, kit: dict | None = None, *, widen: bool = True) -> dict:
    """Grow a template text box so the words it now holds stay inside it.

    A slot filled with this deck's wording may be a line longer than the example
    the designer drew for; the box follows the words instead of the words being
    trimmed to the box. The width is never shrunk, nothing moves, and ``widen``
    lets a repeated row keep the width its own column allows.
    """
    bounds = element.get('bounds')
    content = element.get('content')
    if not isinstance(bounds, list) or len(bounds) != 4 or not isinstance(content, dict):
        return element
    if not content.get('fontSize'):
        from .pptd_generation import _dominant_style
        style = _dominant_style(str(content.get('text') or ''))
        # A template element that names no size of its own is drawn at the
        # renderer's default, so the box has to be measured at that size too.
        content['fontSize'] = float(style.get('fontSize') or DEFAULT_TEXT_SIZE)
        content['lineHeight'] = LINE_SPACING
        if style.get('fontFamily'):
            content.setdefault('fontFamily', style['fontFamily'])
        if style.get('color'):
            content.setdefault('color', style['color'])
    size = content.get('fontSize')
    if not size:
        return element
    size = float(size)
    text = plain(content.get('text'))
    width, height = 960.0, 540.0
    if kit and isinstance(kit.get('size'), list):
        width, height = kit['size']
    x, y, w, h = bounds
    # A one-line phrase in a narrow box gets the width it needs, centred on its
    # old position, before the height is measured against what is left.
    if widen and text_width(text, size) > w > 0 and '\n' not in text:
        wanted = min(width - x, max(w, text_width(text, size) * 1.03))
        if wanted > w:
            bounds[0] = round(max(0.0, x - (wanted - w) / 2), 1)
            bounds[2] = round(wanted, 1)
            x, w = bounds[0], bounds[2]
    needed = block_height(text, w - 2, size) + SLACK
    if needed > h:
        bounds[3] = round(min(needed, max(h, height - y)), 1)
    return element
