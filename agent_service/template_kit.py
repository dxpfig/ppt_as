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
                          and element is not (background or {}).get('element')
                          and element is not (logo or {}).get('element')
                          and abs((element.get('bounds') or [0, 0, 0, 0])[1] - band) <= 40
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
    paragraph = f'<p style="line-height:1.35{"".join([";text-align:" + align]) if align else ""}">'
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
    width, height = kit.get('size') or [960, 540]
    elements: list[dict] = []

    def copy_fixed(element: dict | None, suffix: str) -> None:
        if element:
            clone = copy.deepcopy(element)
            clone['elementId'] = f'{page_id}-{suffix}'
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
        content = clone.setdefault('content', {})
        from .pptd_generation import _merge_markup, _styled_text, _dominant_style
        text = str(content.get('text') or '')
        content['text'] = _merge_markup(text, str(plan['title'])) or _styled_text(str(plan['title']), _dominant_style(text))
        elements.append(clone)
    for index, decoration in enumerate(kit.get('titleDecorations') or []):
        clone = copy.deepcopy(decoration)
        clone['elementId'] = f'{page_id}-deco{index}'
        elements.append(clone)

    area = content_area(kit, tokens)
    cursor = area['y']
    if plan.get('lead'):
        lead = _style(tokens, 'lead')
        lead['bold'] = True
        elements.append(_element(f'{page_id}-lead', 'text', [area['x'], cursor, area['width'], 34],
                                 content={'text': _markup(str(plan['lead']), lead), 'wrap': True,
                                          'align': ['left', 'top']}))
        cursor += 46

    layout = plan.get('layout') or 'bullets'
    if layout not in LAYOUTS:
        raise ValueError(f'未知版式 {layout!r}；可用：{LAYOUTS}')
    room = area['y'] + area['height'] - cursor
    if layout == 'cards':
        _cards(kit, tokens, elements, plan, page_id, area, cursor, room)
    elif layout == 'steps':
        _steps(kit, tokens, elements, plan, page_id, area, cursor, room)
    elif layout == 'keyNumbers':
        _key_numbers(tokens, elements, plan, page_id, area, cursor, room)
    elif layout == 'table':
        _table(kit, tokens, elements, plan, page_id, area, cursor, room)
    else:
        _bullets(tokens, elements, plan, page_id, area, cursor, room, columns=2 if layout == 'comparison' else 1)
    return {'pageType': 'body', 'elements': elements}


def _columns(count: int, maximum: int = 3) -> tuple[int, int]:
    """Rows and columns for ``count`` items: 2x3 beats a row of five."""
    columns = min(maximum, max(1, count))
    columns = min(columns, 4)
    rows = (count + columns - 1) // columns
    return rows, columns


def _cards(kit: dict, tokens: dict, elements: list[dict], plan: dict, page_id: str,
           area: dict, top: float, room: float) -> None:
    items = plan.get('items') or []
    if not items:
        return
    rows, columns = _columns(len(items))
    gap = 20
    card_width = (area['width'] - gap * (columns - 1)) / columns
    card_height = (room - gap * (rows - 1)) / rows
    label = _style(tokens, 'lead')
    label['color'] = (tokens.get('colors') or {}).get('primary') or label['color']
    label['bold'] = True
    body = _style(tokens, 'body')
    accent = (tokens.get('colors') or {}).get('primary') or '#E50011'
    for index, item in enumerate(items):
        row, column = divmod(index, columns)
        x = area['x'] + column * (card_width + gap)
        y = top + row * (card_height + gap)
        identity = f'{page_id}-card{index + 1}'
        elements.append(_element(f'{identity}-bg', 'shape', [x, y, card_width, card_height],
                                 shapeName='roundRect', adjustments=[6000],
                                 fill={'type': 'solid', 'color': '#FFFFFF'},
                                 border={'width': 1, 'color': accent, 'style': 'solid'}))
        elements.append(_element(f'{identity}-dot', 'shape', [x + 16, y + 16, 14, 14],
                                 shapeName='ellipse', fill={'type': 'solid', 'color': accent}))
        elements.append(_element(f'{identity}-label', 'text', [x + 40, y + 12, card_width - 56, 24],
                                 content={'text': _markup(str(item.get('label', '')), label), 'wrap': True,
                                          'align': ['left', 'top']}))
        bullets = [str(value) for value in (item.get('bullets') or []) if str(value).strip()]
        if bullets:
            text = ''.join(_markup('· ' + bullet, body) for bullet in bullets)
            elements.append(_element(f'{identity}-body', 'text', [x + 18, y + 44, card_width - 36, card_height - 56],
                                     content={'text': text, 'wrap': True, 'align': ['left', 'top']}))


def _bullets(tokens: dict, elements: list[dict], plan: dict, page_id: str,
             area: dict, top: float, room: float, *, columns: int) -> None:
    items = plan.get('items') or []
    body = _style(tokens, 'body')
    heading = _style(tokens, 'lead')
    heading['color'] = (tokens.get('colors') or {}).get('primary') or heading['color']
    heading['bold'] = True
    column_width = (area['width'] - 24 * (columns - 1)) / columns
    perspective = max(1, (len(items) + columns - 1) // columns)
    step = room / perspective if perspective else room
    for index, item in enumerate(items):
        column, row = divmod(index, perspective)
        x = area['x'] + column * (column_width + 24)
        y = top + row * step
        blocks = []
        if item.get('label'):
            blocks.append(_markup(str(item['label']), heading))
        for bullet in item.get('bullets') or []:
            blocks.append(_markup('· ' + str(bullet), body))
        if not blocks:
            continue
        elements.append(_element(f'{page_id}-item{index + 1}', 'text', [x, y, column_width, step - 6],
                                 content={'text': ''.join(blocks), 'wrap': True, 'align': ['left', 'top']}))


def _steps(kit: dict, tokens: dict, elements: list[dict], plan: dict, page_id: str,
           area: dict, top: float, room: float) -> None:
    items = plan.get('items') or []
    if not items:
        return
    body = _style(tokens, 'body')
    label = _style(tokens, 'lead')
    accent = (tokens.get('colors') or {}).get('primary') or '#E50011'
    height = min(80, (room - 14 * (len(items) - 1)) / max(1, len(items)))
    for index, item in enumerate(items):
        y = top + index * (height + 14)
        identity = f'{page_id}-step{index + 1}'
        elements.append(_element(f'{identity}-bg', 'shape', [area['x'], y, area['width'], height],
                                 shapeName='roundRect', adjustments=[8000],
                                 fill={'type': 'solid', 'color': '#FFFFFF'},
                                 border={'width': 1, 'color': accent, 'style': 'solid'}))
        elements.append(_element(f'{identity}-num', 'text', [area['x'] + 14, y + height / 2 - 14, 34, 28],
                                 content={'text': _markup(f'{index + 1:02d}', label), 'wrap': False,
                                          'align': ['center', 'middle']}))
        blocks = [_markup(str(item.get('label', '')), label)]
        for bullet in item.get('bullets') or []:
            blocks.append(_markup(str(bullet), body))
        elements.append(_element(f'{identity}-text', 'text', [area['x'] + 56, y + 10, area['width'] - 72, height - 20],
                                 content={'text': ''.join(blocks), 'wrap': True, 'align': ['left', 'middle']}))


def _key_numbers(tokens: dict, elements: list[dict], plan: dict, page_id: str,
                 area: dict, top: float, room: float) -> None:
    items = plan.get('items') or []
    if not items:
        return
    big = _style(tokens, 'page-title')
    caption = _style(tokens, 'caption')
    columns = max(1, min(4, len(items)))
    width = area['width'] / columns
    for index, item in enumerate(items):
        x = area['x'] + index * width
        elements.append(_element(f'{page_id}-num{index + 1}', 'text', [x, top + room * 0.18, width - 20, 60],
                                 content={'text': _markup(str(item.get('value', '')), big), 'wrap': False,
                                          'align': ['left', 'top']}))
        elements.append(_element(f'{page_id}-cap{index + 1}', 'text', [x, top + room * 0.18 + 62, width - 20, 40],
                                 content={'text': _markup(str(item.get('label', '')), caption), 'wrap': True,
                                          'align': ['left', 'top']}))


def _table(kit: dict, tokens: dict, elements: list[dict], plan: dict, page_id: str,
           area: dict, top: float, room: float) -> None:
    table = plan.get('table') or {}
    columns = [str(value) for value in table.get('columns') or []]
    rows = [[str(cell) for cell in row] for row in table.get('rows') or []]
    if not columns or not rows:
        return
    body = _style(tokens, 'body')
    accent = (tokens.get('colors') or {}).get('primary') or '#E50011'
    border = {'style': 'solid', 'width': 1, 'color': '#333333'}
    cells = []
    header = [{'text': _markup(value, {**body, 'color': '#FFFFFF', 'bold': True}), 'fill': {'type': 'solid', 'color': accent},
               'border': border, 'align': ['center', 'middle']} for value in columns]
    cells.append(header)
    for row in rows:
        cells.append([{'text': _markup(value, body), 'border': border, 'align': ['center', 'middle']} for value in row])
    height_per_row = min(46, room / max(1, len(cells)))
    elements.append(_element(f'{page_id}-table', 'table', [area['x'], top, area['width'], height_per_row * len(cells)],
                             rows=cells,
                             columnWidths=[round(1 / len(columns), 4)] * (len(columns) - 1)
                             + [round(1 - round(1 / len(columns), 4) * (len(columns) - 1), 4)],
                             rowHeights=[round(1 / len(cells), 4)] * (len(cells) - 1)
                             + [round(1 - round(1 / len(cells), 4) * (len(cells) - 1), 4)]))
