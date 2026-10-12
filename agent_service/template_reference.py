"""Company templates become a bounded design reference, never the user's deck.

Kimi's "use a template" workflow converts the upload, reads its visual style,
page archetypes and reusable components, then organizes a *new* presentation.
The template's own pages stay reference material: they must not become the
project document, and the whole template must not travel with model requests.

This module keeps that contract. ``build`` imports the PPTX once into
``template_references/<template id>``; ``load`` derives a compact digest plus
per-page sources, so a model can read one exact page on demand instead of
receiving all of them.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import Counter
from pathlib import Path

from pptd_engine.document import load_yaml
from pptd_engine.pptx import Importer

REFERENCE_DIRECTORY = 'template_references'
SOURCE_PREFIX = 'template-page-'
DOCUMENT_NAMES = ('doc.pptd', 'document.pptd')
# Bump when the importer changes in a way that must be re-derived from the upload.
REFERENCE_FORMAT = 'pptd-importer-2'

FONT_FAMILY = re.compile(r'font-family\s*:\s*([^;"]+)')
FONT_SIZE = re.compile(r'font-size\s*:\s*([0-9.]+)px')
TEXT_COLOR = re.compile(r'(?<![-\w])color\s*:\s*(#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?)')
TAG = re.compile(r'<[^>]+>')
ENTITIES = {'&nbsp;': ' ', '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'"}
ELEMENT_LABELS = {'image': '图片', 'table': '表格', 'chart': '图表', 'icon': '图标',
                  'text': '文本', 'shape': '形状', 'line': '线条'}


def reference_path(root: Path, template_id: str) -> Path:
    return Path(root) / REFERENCE_DIRECTORY / template_id


def _document_path(directory: Path) -> Path | None:
    """The importer writes ``doc.pptd``; the document store renames it."""
    return next((directory / name for name in DOCUMENT_NAMES if (directory / name).is_file()), None)


def build(pptx: Path, root: Path, template_id: str) -> dict:
    """Import the upload once (idempotent per template) and return its audit."""
    destination = reference_path(root, template_id)
    if _document_path(destination) and _stamp(destination).get('format') == REFERENCE_FORMAT:
        return _report(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        report = Importer(pptx, destination).run()
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    if any(item.get('status') == 'unsupported' for item in report.get('objects', [])):
        shutil.rmtree(destination, ignore_errors=True)
        raise ValueError('模板包含尚不能保留的对象，请先使用简化模板；未导入参考模板。')
    (destination / 'reference.json').write_text(
        json.dumps({'format': REFERENCE_FORMAT,
                    'sourceSha256': hashlib.sha256(pptx.read_bytes()).hexdigest(),
                    'pageCount': report.get('pageCount'), 'report': report}, ensure_ascii=False) + '\n',
        encoding='utf-8')
    return report


def _report(destination: Path) -> dict:
    return _stamp(destination).get('report', {})


def _stamp(destination: Path) -> dict:
    try:
        return json.loads((destination / 'reference.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def load(root: Path | None, template_id: str | None) -> dict | None:
    """Return ``{'summary':…, 'sources':…}`` for an imported template, or None."""
    if not root or not template_id:
        return None
    directory = reference_path(Path(root), str(template_id))
    entry = _document_path(directory)
    if entry is None:
        return None
    document = load_yaml(entry)
    pages = [{'path': path, 'data': load_yaml(directory / path)} for path in document['pages']]
    media = [{'path': path.relative_to(directory).as_posix(), 'sourcePath': str(path), 'template': True}
             for path in sorted((directory / 'media').rglob('*')) if path.is_file()]
    return {'summary': summarize(document, pages), 'sources': sources(pages), 'media': media}


def sources(pages: list[dict]) -> dict[str, str]:
    """Per-page originals for on-demand ``contextRequest`` reads."""
    return {f'{SOURCE_PREFIX}{index + 1:02d}': json.dumps(page['data'], ensure_ascii=False, separators=(',', ':'))
            for index, page in enumerate(pages)}


def _style(value, colors: Counter, families: Counter, sizes: Counter) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ('color', 'fill', 'backgroundColor', 'lineColor', 'areaColor') and isinstance(child, str) and child.startswith('#'):
                colors[child.upper()] += 1
            else:
                _style(child, colors, families, sizes)
    elif isinstance(value, list):
        for child in value:
            _style(child, colors, families, sizes)
    elif isinstance(value, str) and ('font-' in value or 'color:' in value):
        for match in FONT_FAMILY.findall(value):
            families[match.strip().strip('"\'').split(',')[0].strip()] += 1
        for match in FONT_SIZE.findall(value):
            sizes[round(float(match), 1)] += 1
        for match in TEXT_COLOR.findall(value):
            colors[match.upper()] += 1


def _plain(value: str) -> str:
    text = TAG.sub('', value)
    for entity, replacement in ENTITIES.items():
        text = text.replace(entity, replacement)
    return ' '.join(text.split())


def _font_size(element: dict) -> float:
    sizes = FONT_SIZE.findall(str((element.get('content') or {}).get('text', '')))
    return max((float(value) for value in sizes), default=0.0)


def _archetype(index: int, total: int, elements: list[dict], characters: int) -> str:
    kinds = {element.get('elementType') for element in elements}
    texts = sum(1 for element in elements if element.get('elementType') == 'text')
    if 'chart' in kinds:
        return 'data-chart'
    if 'table' in kinds:
        return 'data-table'
    if index == 0:
        return 'cover'
    if index == total - 1:
        return 'closing'
    # A divider/section page is one short headline and nothing else; a page with
    # body copy, cards or imagery stays a content page even when it is short.
    if texts <= 1 or (texts <= 2 and characters <= 24):
        return 'section'
    return 'content'


def _is_neutral(color: str) -> bool:
    value = color[1:7]
    red, green, blue = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    return max(red, green, blue) - min(red, green, blue) <= 12


def _composition(elements: list[dict]) -> str:
    kinds = Counter(element.get('elementType') for element in elements)
    shapes = Counter(element.get('shapeName') for element in elements if element.get('elementType') == 'shape')
    parts = []
    for kind in ('image', 'table', 'chart', 'icon', 'text', 'shape', 'line'):
        count = kinds.get(kind, 0)
        if not count:
            continue
        detail = ''
        if kind == 'shape' and shapes:
            detail = '（' + '、'.join(f'{name}×{count}' for name, count in shapes.most_common(3)) + '）'
        parts.append(f'{ELEMENT_LABELS[kind]}×{count}{detail}')
    return ' + '.join(parts)


def summarize(document: dict, pages: list[dict]) -> dict:
    """Bounded digest: style tokens, page archetypes and reusable components."""
    colors, families, sizes = Counter(), Counter(), Counter()
    _style(document.get('theme', {}), colors, families, sizes)
    entries = []
    for index, page in enumerate(pages):
        data = page.get('data') or {}
        elements = data.get('elements') or []
        _style(elements, colors, families, sizes)
        texts = [{'text': _plain(str((element.get('content') or {}).get('text', ''))), 'size': _font_size(element)}
                 for element in elements if element.get('elementType') == 'text']
        characters = sum(len(item['text']) for item in texts)
        headline = max(texts, key=lambda item: item['size'], default={'text': '', 'size': 0})
        slots = [len(_plain(str((element.get('content') or {}).get('text', '')))) for element in elements
                 if element.get('elementType') == 'text']
        entries.append({
            'path': page['path'],
            'sourceId': f'{SOURCE_PREFIX}{index + 1:02d}',
            'title': headline['text'][:40],
            'archetype': _archetype(index, len(pages), elements, characters),
            'composition': _composition(elements),
            'textSizes': sorted({item['size'] for item in texts if item['size']}, reverse=True)[:4],
            # Slot capacity lets the planner match a page's structure to content.
            'textSlots': len(slots),
            'slotLimits': sorted(slots, reverse=True)[:6],
            'characters': characters,
        })
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(entry['archetype'], []).append(entry)
    page_types = [{
        'archetype': archetype,
        'count': len(items),
        'pages': [item['path'] for item in items],
        'representatives': [{key: item[key] for key in ('path', 'sourceId', 'title', 'composition', 'textSizes', 'characters')}
                            for item in items[:2]],
    } for archetype, items in grouped.items()]
    accents = [(color, count) for color, count in colors.most_common() if not _is_neutral(color)]
    neutrals = [(color, count) for color, count in colors.most_common() if _is_neutral(color)]
    return {
        'kind': 'company-template-reference',
        'pageCount': len(pages),
        'size': document.get('size'),
        # Brand accents first: near-black/white/grey are background noise here.
        'palette': [{'color': color, 'uses': count} for color, count in accents[:6]],
        'neutralColors': [{'color': color, 'uses': count} for color, count in neutrals[:3]],
        'fonts': [{'family': family, 'uses': count} for family, count in families.most_common(6)],
        'textSizes': sorted({size for size in sizes if size}, reverse=True)[:8],
        'pageTypes': page_types,
        'pages': [{key: item[key] for key in ('path', 'sourceId', 'title', 'archetype', 'textSlots', 'slotLimits')}
                  for item in entries],
        'guidance': ('模板仅作为风格、组件与版式参考。按本次需求重新决定页数、标题与正文，'
                     '不要照搬模板页面、页数或内容；需要精确复用时用 contextRequest 读取 pages 里对应的 sourceId。'),
    }
