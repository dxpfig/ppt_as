"""Compile a declarative page spec into deterministic PPTD v2 elements."""
from __future__ import annotations

import math
import re
from typing import Any


DEFAULT_PALETTE = {
    'accent': '#C62828',
    'text': '#1F2430',
    'muted': '#5B6472',
    'surface': '#F4F5F7',
    'surface_alt': '#E9EDF2',
    'white': '#FFFFFF',
}

TEXT_STYLES = {
    'display': {'fontSize': 32, 'bold': True},
    'title': {'fontSize': 24, 'bold': True},
    'heading': {'fontSize': 16, 'bold': True},
    'body': {'fontSize': 12, 'bold': False},
    'caption': {'fontSize': 10, 'bold': False},
    'metric': {'fontSize': 26, 'bold': True},
}


def _number(value, default=None):
    return (
        value
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else default
    )


def _box(value, container):
    values = value if isinstance(value, list) and len(value) == 4 else None
    if not values or not all(_number(item) is not None for item in values):
        return dict(container)
    x, y, width, height = (float(item) for item in values)
    return {
        'x': container['x'] + x * container['width'],
        'y': container['y'] + y * container['height'],
        'width': max(1, width * container['width']),
        'height': max(1, height * container['height']),
    }


def _weights(children, key='weight'):
    values = []
    for child in children:
        value = _number(child.get(key), 1) if isinstance(child, dict) else 1
        values.append(max(0.05, float(value)))
    return values or [1]


def _split(total, weights, gap):
    gap_total = gap * max(0, len(weights) - 1)
    usable = max(1, total - gap_total)
    weight_total = sum(weights)
    return [usable * value / weight_total for value in weights]


def _text_units(value: str) -> float:
    return sum(0.55 if ord(character) < 128 else 1.0 for character in value)


def _estimate_lines(value: str, width: float, font_size: float) -> int:
    capacity = max(1.0, width / max(1.0, font_size))
    lines = 0
    for paragraph in str(value or '').splitlines() or ['']:
        units = max(1.0, _text_units(paragraph))
        lines += max(1, math.ceil(units / capacity))
    return max(1, lines)


def _fit_font(
    value: str,
    box: dict,
    role: str,
    requested=None,
    minimum=None,
    styles=None,
):
    styles = styles or TEXT_STYLES
    base = styles.get(role, styles['body'])['fontSize']
    size = _number(requested, base)
    floor = _number(minimum, min(12, base if role != 'caption' else 9))
    size = max(floor, min(float(size), base))
    while size > floor:
        lines = _estimate_lines(value, box['width'], size)
        if lines * size * 1.45 <= box['height'] + 2:
            break
        size -= 1
    return round(max(floor, size), 1)


def _safe_id(value, prefix, ordinal):
    text = re.sub(r'[^A-Za-z0-9_-]+', '-', str(value or '')).strip('-')
    return f'{prefix}-{ordinal}-{text or "element"}'[:120]


def _container_box(node, box):
    padding = _number(node.get('padding'), 0)
    padding = max(0, min(float(padding), min(box['width'], box['height']) / 3))
    return {
        'x': box['x'] + padding,
        'y': box['y'] + padding,
        'width': max(1, box['width'] - padding * 2),
        'height': max(1, box['height'] - padding * 2),
    }


def _tone(node, palette, *, filled=False):
    tone = str(node.get('tone') or 'neutral')
    if tone == 'accent':
        return palette['accent']
    if tone in ('muted', 'subtle'):
        return palette['surface_alt']
    if tone in ('dark', 'inverse'):
        return '#202632'
    return palette['surface'] if filled else palette['text']


class _Compiler:
    def __init__(
        self,
        page_index: int,
        palette: dict,
        *,
        font_family: str = '',
        styles: dict | None = None,
    ):
        self.page_index = page_index
        self.palette = palette
        self.font_family = str(font_family or '').strip()
        self.styles = styles or TEXT_STYLES
        self.elements: list[dict] = []
        self.sequence = 0

    def text_style(self, role: str) -> dict:
        return self.styles.get(role, self.styles['body'])

    def identifier(self, node, name):
        self.sequence += 1
        return _safe_id(
            node.get('id') or name,
            f'p{self.page_index + 1}',
            self.sequence,
        )

    def add(self, element):
        self.elements.append(element)
        return element

    def compile(self, node, box):
        if not isinstance(node, dict):
            return
        kind = str(node.get('type') or 'text').strip().lower()
        if kind in ('columns', 'row'):
            self.stack(node, box, horizontal=True)
        elif kind in ('rows', 'stack', 'column'):
            self.stack(node, box, horizontal=False)
        elif kind == 'grid':
            self.grid(node, box)
        elif kind == 'overlay':
            for child in node.get('children') or []:
                self.compile(child, box)
        elif kind == 'free':
            for child in node.get('children') or []:
                self.compile(child, _box(child.get('bounds'), box))
        elif kind in ('card', 'panel'):
            self.card(node, box)
        elif kind in ('text', 'heading', 'kicker', 'caption', 'display', 'metric'):
            self.text(node, box, kind)
        elif kind in ('bullets', 'bulletList'):
            self.bullets(node, box)
        elif kind == 'table':
            self.table(node, box)
        elif kind == 'shape':
            self.shape(node, box)
        elif kind == 'line':
            self.line(node, box)
        elif kind == 'image':
            self.image(node, box)
        elif kind == 'spacer':
            return

    def stack(self, node, box, *, horizontal):
        children = [
            child for child in node.get('children') or []
            if isinstance(child, dict)
        ]
        if not children:
            return
        gap = max(0, float(_number(node.get('gap'), 12)))
        weights = _weights(children)
        sizes = _split(
            box['width'] if horizontal else box['height'],
            weights,
            gap,
        )
        offset = box['x'] if horizontal else box['y']
        for child, size in zip(children, sizes):
            target = {
                'x': offset,
                'y': box['y'],
                'width': size if horizontal else box['width'],
                'height': box['height'] if horizontal else size,
            }
            self.compile(child, target)
            offset += size + gap

    def grid(self, node, box):
        children = [
            child for child in node.get('children') or []
            if isinstance(child, dict)
        ]
        if not children:
            return
        columns = max(1, int(_number(node.get('columns'), 2)))
        rows = math.ceil(len(children) / columns)
        gap = max(0, float(_number(node.get('gap'), 10)))
        width = (box['width'] - gap * (columns - 1)) / columns
        height = (box['height'] - gap * (rows - 1)) / rows
        for index, child in enumerate(children):
            target = {
                'x': box['x'] + (index % columns) * (width + gap),
                'y': box['y'] + (index // columns) * (height + gap),
                'width': width,
                'height': height,
            }
            self.compile(child, target)

    def card(self, node, box):
        fill = str(
            node.get('fill')
            or _tone(node, self.palette, filled=True)
        )
        shape_name = str(node.get('shapeName') or 'roundRect')
        self.add(
            {
                'elementId': self.identifier(node, 'card'),
                'elementType': 'shape',
                'shapeName': shape_name,
                'bounds': [
                    round(box['x'], 2),
                    round(box['y'], 2),
                    round(box['width'], 2),
                    round(box['height'], 2),
                ],
                'fill': {'type': 'solid', 'color': fill},
                'border': {
                    'width': 1,
                    'color': str(
                        node.get('borderColor')
                        or self.palette['surface_alt']
                    ),
                    'style': 'solid',
                },
            }
        )
        children = [
            child
            for child in node.get('children') or []
            if isinstance(child, dict)
        ]
        if not children:
            return
        inner = _container_box(node, box)
        mode = str(node.get('layout') or 'rows').strip().lower()
        if mode in {'overlay', 'stacked'}:
            for child in children:
                self.compile(child, inner)
            return
        if mode in {'columns', 'row'}:
            gap = max(0, float(_number(node.get('gap'), 8)))
            widths = _split(inner['width'], _weights(children), gap)
            offset = inner['x']
            for child, width in zip(children, widths):
                self.compile(
                    child,
                    {
                        'x': offset,
                        'y': inner['y'],
                        'width': width,
                        'height': inner['height'],
                    },
                )
                offset += width + gap
            return
        gap = max(0, float(_number(node.get('gap'), 8)))
        heights = _split(inner['height'], _weights(children), gap)
        offset = inner['y']
        for child, height in zip(children, heights):
            self.compile(
                child,
                {
                    'x': inner['x'],
                    'y': offset,
                    'width': inner['width'],
                    'height': height,
                },
            )
            offset += height + gap

    def text(self, node, box, kind):
        value = str(node.get('text') or '').strip()
        if not value:
            return
        role = (
            str(node.get('role') or '').strip()
            or ('heading' if kind == 'heading' else
                'caption' if kind == 'kicker' else
                'metric' if kind == 'metric' else
                'display' if kind == 'display' else
                'body')
        )
        style = self.text_style(role)
        font_size = _fit_font(
            value,
            box,
            role,
            node.get('fontSize'),
            node.get('minFontSize'),
            self.styles,
        )
        color = str(
            node.get('color')
            or (
                self.palette['accent']
                if (
                    kind == 'metric'
                    or role in ('display', 'title')
                    or node.get('emphasis')
                )
                else self.palette['muted']
                if kind in ('kicker', 'caption')
                else self.palette['text']
            )
        )
        content = {
            'text': value,
            'fontSize': font_size,
            'bold': bool(node.get('bold', style['bold'])),
            'color': color,
            'wrap': True,
            'align': [
                str(node.get('align') or 'left'),
                str(node.get('verticalAlign') or 'top'),
            ],
        }
        if self.font_family:
            content['fontFamily'] = self.font_family
        self.add(
            {
                'elementId': self.identifier(node, kind),
                'elementType': 'text',
                'bounds': [
                    round(box['x'], 2),
                    round(box['y'], 2),
                    round(box['width'], 2),
                    round(box['height'], 2),
                ],
                'content': content,
            }
        )

    def bullets(self, node, box):
        items = [
            str(item).strip()
            for item in node.get('items') or []
            if str(item).strip()
        ]
        if not items:
            return
        text = '\n'.join(f'· {item}' for item in items)
        self.text(
            {
                **node,
                'type': 'text',
                'role': node.get('role') or 'body',
                'text': text,
            },
            box,
            'text',
        )

    def table(self, node, box):
        rows = node.get('rows') or []
        if not isinstance(rows, list) or not rows:
            return
        normalized = []
        columns = 0
        for row in rows:
            if not isinstance(row, list):
                continue
            columns = max(columns, len(row))
            normalized.append([
                {'text': str(cell)}
                for cell in row
            ])
        if not normalized or not columns:
            return
        for row in normalized:
            row.extend({'text': ''} for _ in range(columns - len(row)))
        self.add(
            {
                'elementId': self.identifier(node, 'table'),
                'elementType': 'table',
                'bounds': [
                    round(box['x'], 2),
                    round(box['y'], 2),
                    round(box['width'], 2),
                    round(box['height'], 2),
                ],
                'columnWidths': [1 / columns] * columns,
                'rowHeights': [1 / len(normalized)] * len(normalized),
                'rows': normalized,
            }
        )

    def shape(self, node, box):
        self.add(
            {
                'elementId': self.identifier(node, 'shape'),
                'elementType': 'shape',
                'shapeName': str(node.get('shapeName') or 'rect'),
                'bounds': [
                    round(box['x'], 2),
                    round(box['y'], 2),
                    round(box['width'], 2),
                    round(box['height'], 2),
                ],
                'fill': {
                    'type': 'solid',
                    'color': str(
                        node.get('fill')
                        or _tone(node, self.palette, filled=True)
                    ),
                },
            }
        )

    def line(self, node, box):
        self.add(
            {
                'elementId': self.identifier(node, 'line'),
                'elementType': 'line',
                'bounds': [
                    round(box['x'], 2),
                    round(box['y'], 2),
                    round(box['width'], 2),
                    max(1, round(box['height'], 2)),
                ],
                'viewBox': [box['width'], max(1, box['height'])],
                'points': f'0,0 {box["width"]},{box["height"]}',
                'border': {
                    'style': 'solid',
                    'width': 1,
                    'color': str(node.get('color') or self.palette['accent']),
                },
            }
        )

    def image(self, node, box):
        source = node.get('src')
        if not isinstance(source, str) or not source.startswith('media/'):
            return
        self.add(
            {
                'elementId': self.identifier(node, 'image'),
                'elementType': 'image',
                'bounds': [
                    round(box['x'], 2),
                    round(box['y'], 2),
                    round(box['width'], 2),
                    round(box['height'], 2),
                ],
                'src': source,
                'fit': {'mode': str(node.get('fit') or 'cover')},
            }
        )


def _style_palette(style_profile: dict | None) -> dict:
    colors = (style_profile or {}).get('colors')
    if not isinstance(colors, dict):
        return {}
    mapping = {
        'accent': ('accent',),
        'text': ('text',),
        'muted': ('muted', 'text'),
        'surface': ('surface',),
        'surface_alt': ('subtle', 'surface'),
        'white': ('surface',),
    }
    palette = {}
    for target, sources in mapping.items():
        for source in sources:
            value = colors.get(source)
            if isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value):
                palette[target] = value
                break
    return palette


def _style_text_styles(style_profile: dict | None) -> dict:
    profile = style_profile or {}
    sizes = profile.get('sizes') if isinstance(profile.get('sizes'), dict) else {}
    title = round(float(_number(sizes.get('title'), TEXT_STYLES['title']['fontSize'])), 1)
    body = round(float(_number(sizes.get('body'), TEXT_STYLES['body']['fontSize'])), 1)
    subtitle = round(
        float(_number(sizes.get('subtitle'), max(body + 1, 14))),
        1,
    )
    styles = {
        key: dict(value)
        for key, value in TEXT_STYLES.items()
    }
    styles.update({
        'display': {'fontSize': max(title, 30.0), 'bold': True},
        'title': {'fontSize': max(title, 22.0), 'bold': True},
        'heading': {'fontSize': max(body + 3, subtitle), 'bold': True},
        'body': {'fontSize': max(body, 12.0), 'bold': False},
        'caption': {'fontSize': max(9.0, body - 2), 'bold': False},
        'metric': {'fontSize': max(title * 0.9, 24.0), 'bold': True},
    })
    return styles


def compile_page(
    spec: dict,
    *,
    page_index: int,
    page_path: str,
    style_profile: dict | None = None,
) -> dict:
    """Compile one semantic page spec to a canonical PPTD page document."""
    if not isinstance(spec, dict):
        raise ValueError('Page spec must be an object')
    supplied_palette = (
        spec.get('palette')
        if isinstance(spec.get('palette'), dict)
        else {}
    )
    palette = {
        **DEFAULT_PALETTE,
        **_style_palette(style_profile),
        **supplied_palette,
    }
    font_family = (
        str((style_profile or {}).get('fontFamily') or '').strip()
        or str(spec.get('fontFamily') or '').strip()
    )
    width, height = 960, 540
    margin = max(32, min(80, float(_number(spec.get('margin'), 56))))
    compiler = _Compiler(
        page_index,
        palette,
        font_family=font_family,
        styles=_style_text_styles(style_profile),
    )
    title = str(spec.get('title') or '').strip()
    subtitle = str(spec.get('subtitle') or '').strip()
    y = 30.0
    if title:
        title_height = 46 if subtitle else 56
        compiler.text(
            {
                'id': 'page-title',
                'role': 'title',
                'text': title,
                'fontSize': spec.get('titleSize') or 24,
            },
            {
                'x': margin,
                'y': y,
                'width': width - margin * 2,
                'height': title_height,
            },
            'heading',
        )
        y += title_height + 2
    if subtitle:
        compiler.text(
            {
                'id': 'page-subtitle',
                'role': 'body',
                'text': subtitle,
                'color': palette['muted'],
            },
            {
                'x': margin,
                'y': y,
                'width': width - margin * 2,
                'height': 24,
            },
            'text',
        )
        y += 28
    y += 8
    content = {
        'x': margin,
        'y': y,
        'width': width - margin * 2,
        'height': max(80, height - y - 32),
    }
    compiler.compile(spec.get('root') or {}, content)
    return {
        'title': title,
        'meta': {
            'page': page_index + 1,
            'kind': str(spec.get('kind') or 'content'),
            'archetype': str(spec.get('archetype') or 'custom')[:40],
            'section': str(spec.get('section') or '')[:120],
            'sourcePath': page_path,
        },
        'elements': compiler.elements,
    }


def layout_family(page_data: dict) -> str:
    """Classify the visible composition for cross-page rhythm checks."""
    if not isinstance(page_data, dict):
        return 'empty'
    elements = [
        element
        for element in page_data.get('elements') or []
        if isinstance(element, dict)
    ]
    if any(element.get('elementType') == 'table' for element in elements):
        return 'table'
    if any(element.get('elementType') == 'chart' for element in elements):
        return 'chart'
    round_cards = [
        element
        for element in elements
        if element.get('elementType') == 'shape'
        and str(element.get('shapeName') or '').lower()
        in {'roundrect', 'roundedrectangle'}
    ]
    if len(round_cards) >= 4:
        return 'matrix'
    if len(round_cards) == 2:
        return 'comparison'
    if round_cards:
        return 'cards'
    lines = [
        element
        for element in elements
        if element.get('elementType') == 'line'
    ]
    if len(lines) >= 2:
        return 'process'
    metric_texts = [
        element
        for element in elements
        if element.get('elementType') == 'text'
        and isinstance(element.get('content'), dict)
        and _number(element['content'].get('fontSize'), 0) >= 28
    ]
    if len(metric_texts) >= 2:
        return 'metrics'
    return 'editorial'
