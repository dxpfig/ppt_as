"""Template extraction, step 2: render the deck and build a contact sheet.

Reading parameters alone is not enough to understand a template: the skill's
workflow renders every page and stitches a thumbnail overview, then reads the key
pages closely with both the picture and the `.page` source in hand. This module
produces the pictures for that step, using the same renderer the publishing
pipeline already trusts (LibreOffice/Chromium PDF, then PyMuPDF rasterisation).
"""
from __future__ import annotations

import base64
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import pymupdf
from agentscope.message import Msg
from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field

from .rendering import render_pdf

THUMB_WIDTH = 480
MARGIN = 14
CAPTION = 22
HTML_TAG = re.compile(r'<[^>]+>')
FONT_FAMILY = re.compile(r'font-family\s*:\s*([^;"\']+)')
FONT_SIZE = re.compile(r'font-size\s*:\s*([0-9.]+)px')
TEXT_COLOR = re.compile(r'(?<![-\w])color\s*:\s*(#[0-9a-fA-F]{6,8})')
REPEAT_THRESHOLD = 3


class OverviewUnavailable(ValueError):
    """The renderer could not produce pages; extraction can still continue."""


async def overview(pptx: Path, folder: Path, *, columns: int = 4) -> dict:
    """Render every page to PNG and stitch one contact sheet.

    Returns the per-page images plus the montage. The caller feeds both to the
    model so it can cross-check "what it reads" against "what it sees".
    """
    pptx, folder = Path(pptx).resolve(), Path(folder).resolve()
    if not pptx.is_file():
        raise OverviewUnavailable('未找到要渲染的模板文件。')
    folder.mkdir(parents=True, exist_ok=True)
    await render_pdf(pptx)
    pdf = pptx.with_suffix('.pdf')
    if not pdf.is_file():
        raise OverviewUnavailable('渲染器没有生成 PDF，无法生成模板总览。')
    document = pymupdf.open(pdf)
    try:
        pages = []
        thumbs = []
        for index in range(document.page_count):
            pixmap = document[index].get_pixmap(matrix=pymupdf.Matrix(1.2, 1.2), alpha=False)
            name = f'page-{index + 1:02d}.png'
            target = folder / name
            pixmap.save(target)
            pages.append(name)
            thumbs.append(Image.open(target).convert('RGB'))
    finally:
        document.close()
    sheet = _montage(thumbs, columns)
    overview_path = folder / 'overview.png'
    sheet.save(overview_path)
    for image in thumbs:
        image.close()
    return {'pdf': pdf.name, 'pages': pages, 'overview': overview_path.name,
            'pageCount': len(pages), 'columns': columns,
            'overviewSize': list(sheet.size)}


def _montage(thumbs: list[Image.Image], columns: int) -> Image.Image:
    """Lay the pages out in a labelled grid, scaled to a uniform thumb width."""
    if not thumbs:
        raise OverviewUnavailable('模板没有可渲染的页面。')
    columns = max(1, min(int(columns), len(thumbs)))
    width = THUMB_WIDTH
    heights = []
    scaled = []
    for image in thumbs:
        ratio = width / image.width
        resized = image.resize((width, max(1, round(image.height * ratio))), Image.LANCZOS)
        scaled.append(resized)
        heights.append(resized.height)
    cell_height = max(heights)
    rows = (len(scaled) + columns - 1) // columns
    sheet = Image.new('RGB', (columns * width + (columns + 1) * MARGIN,
                              rows * (cell_height + CAPTION) + (rows + 1) * MARGIN), '#f2f3f5')
    draw = ImageDraw.Draw(sheet)
    for index, image in enumerate(scaled):
        row, column = divmod(index, columns)
        x = MARGIN + column * (width + MARGIN)
        y = MARGIN + row * (cell_height + CAPTION + MARGIN)
        sheet.paste(image, (x, y))
        draw.text((x + 4, y + image.height + 4), f'P{index + 1}', fill='#333333')
        image.close()
    return sheet


def read_overview(folder: Path, *, limit: int = 6) -> list[dict]:
    """Pictures to attach to a model request: the sheet first, then key pages."""
    folder = Path(folder)
    attachments = []
    sheet = folder / 'overview.png'
    if sheet.is_file():
        attachments.append({'name': 'overview.png', 'path': sheet, 'role': 'template-overview'})
    for path in sorted(folder.glob('page-*.png'))[:limit]:
        attachments.append({'name': path.name, 'path': path, 'role': 'template-page'})
    return attachments


async def ensure(folder: Path, pptx: Path, *, columns: int = 4) -> dict | None:
    """Build the overview once per template; never fail the caller."""
    try:
        return await overview(pptx, folder, columns=columns)
    except (OverviewUnavailable, OSError, RuntimeError):
        return None


def _plain(text) -> str:
    return ' '.join(HTML_TAG.sub('', str(text or '')).split())


def _dominant(text) -> dict:
    """Font size, family, colour and weight of the largest run in one text field."""
    value = str(text or '')
    sizes = [float(item) for item in FONT_SIZE.findall(value)]
    families = [item.strip().strip('"\'') for item in FONT_FAMILY.findall(value)]
    colors = [item.upper() for item in TEXT_COLOR.findall(value)]
    return {
        'fontSize': max(sizes) if sizes else None,
        'fontFamily': families[0] if families else None,
        'color': colors[0] if colors else None,
        'bold': '<strong>' in value,
    }


def tokens(pages: list[dict]) -> dict:
    """The design system the pages actually paint, not the package's stock theme.

    A hand-designed deck keeps Office's theme in place and puts the real design in
    direct formatting, so the usable token table has to be derived from the text
    and shapes themselves: size ladder, per-role typography, palette and metrics.
    """
    texts = []
    for index, page in enumerate(pages):
        for element in page.get('elements') or []:
            if element.get('elementType') != 'text':
                continue
            content = element.get('content') or {}
            words = _plain(content.get('text'))
            if not words:
                continue
            style = _dominant(content.get('text'))
            style.update({'page': index + 1, 'elementId': element.get('elementId'),
                          'bounds': element.get('bounds') or [], 'characters': len(words), 'text': words[:40]})
            texts.append(style)
    if not texts:
        return {'textStyles': {}, 'colors': {}, 'metrics': {}, 'sizeLadder': []}
    sizes = Counter(item['fontSize'] for item in texts if item['fontSize'])
    ladder = [size for size, _ in sizes.most_common()]
    roles: dict[str, dict] = {}

    def adopt(role: str, items: list[dict]) -> None:
        """One role = the most common look among its candidates."""
        items = [item for item in items if item]
        if not items:
            return
        signature = Counter((item['fontSize'], item['fontFamily'], item['color'], item['bold']) for item in items)
        (size, family, color, bold), count = signature.most_common(1)[0]
        style: dict = {'bold': bold}
        if size:
            style['fontSize'] = size
        if family:
            style['fontFamily'] = family
        if color:
            style['color'] = color
        left = [item['bounds'][0] for item in items if len(item['bounds']) == 4]
        top = [item['bounds'][1] for item in items if len(item['bounds']) == 4]
        if left:
            style['x'] = round(min(left), 1)
        if top:
            style['y'] = round(min(top), 1)
        roles[role] = {**style, 'samples': count}

    by_page: dict[int, list[dict]] = defaultdict(list)
    for item in texts:
        by_page[item['page']].append(item)
    for items in by_page.values():
        items.sort(key=lambda item: item['fontSize'] or 0, reverse=True)
    # Footer/header chrome is real, but it is not the body copy: keep it in its own
    # roles so "the most common small size" means the card text, not the page number.
    height = 540.0
    def band(item: dict) -> str | None:
        bounds = item['bounds']
        if len(bounds) != 4:
            return None
        if bounds[1] >= height * 0.9:
            return 'footer'
        # Only small running heads count as header chrome; the 30px page title also
        # sits near the top and must stay content.
        if bounds[1] + bounds[3] <= height * 0.12 and (item['fontSize'] or 0) <= 14:
            return 'header'
        return None

    for role in ('footer', 'header'):
        adopt(role, [item for item in texts if band(item) == role])
    content_texts = [item for item in texts if band(item) is None]
    last = max(by_page)
    adopt('cover-title', by_page.get(1, [])[:1])
    adopt('closing-title', by_page.get(last, [])[:1])
    content_pages = [page for page in sorted(by_page)
                     if page not in (1, last) and any(band(item) is None for item in by_page[page])]
    adopt('page-title', [next(item for item in by_page[page] if band(item) is None) for page in content_pages])
    adopt('lead', [item for page in content_pages
                   for item in [next((candidate for candidate in by_page[page][1:] if band(candidate) is None), None)][:1]
                   if item])
    content_sizes = Counter(item['fontSize'] for item in content_texts if item['fontSize'])
    title_size = (roles.get('page-title') or {}).get('fontSize')
    # Body copy is the most common size *below* the page title; on a tie the title
    # would otherwise win simply because it appears first on the page.
    candidates = Counter({size: count for size, count in content_sizes.items() if size != title_size}) or content_sizes
    body_size = candidates.most_common(1)[0][0] if candidates else None
    adopt('body', [item for item in content_texts if item['fontSize'] == body_size])
    small = [size for size, count in sorted(content_sizes.items()) if count >= REPEAT_THRESHOLD]
    adopt('caption', [item for item in content_texts if item['fontSize'] == (small[0] if small else None)])

    palette = Counter()
    for page in pages:
        for element in page.get('elements') or []:
            fill = element.get('fill') if isinstance(element.get('fill'), dict) else None
            if fill and fill.get('color'):
                palette[fill['color'].upper()] += 1
            border = element.get('border') if isinstance(element.get('border'), dict) else None
            if border and border.get('color'):
                palette[border['color'].upper()] += 1
    text_colors = Counter(item['color'] for item in texts if item['color'])
    # Page margins come from the column the content pages use, not from a cover
    # title that deliberately spans the full width.
    placed = [item['bounds'] for item in content_texts
              if len(item['bounds']) == 4 and item['page'] not in (1, max(by_page))]
    return {
        'textStyles': roles,
        'colors': {
            'textColors': [{'color': color, 'uses': count} for color, count in text_colors.most_common(6)],
            'fills': [{'color': color, 'uses': count} for color, count in palette.most_common(8)],
            'primary': (roles.get('page-title') or {}).get('color') or (text_colors.most_common(1)[0][0] if text_colors else None),
        },
        'metrics': {
            'leftMargin': round(min(bounds[0] for bounds in placed), 1) if placed else None,
            'titleTop': (roles.get('page-title') or {}).get('y'),
            'sizeLadder': [size for size, _ in content_sizes.most_common()],
            'averageCharacters': round(sum(item['characters'] for item in texts) / len(texts)),
        },
        'sizeLadder': [size for size, _ in content_sizes.most_common()],
    }


def components(pages: list[dict], *, threshold: int = REPEAT_THRESHOLD) -> list[dict]:
    """Parts that repeat often enough to count as the template's language.

    The reference workflow keeps only patterns seen three times or more: one-offs
    are page-specific decisions rather than the template's reusable components.
    """
    found: dict[tuple, list[dict]] = defaultdict(list)
    for index, page in enumerate(pages):
        for element in page.get('elements') or []:
            kind = element.get('elementType')
            bounds = element.get('bounds') or [0, 0, 0, 0]
            if kind == 'shape':
                key = ('shape', element.get('shapeName'), round(bounds[2]), round(bounds[3]))
            elif kind == 'image':
                key = ('image', '', round(bounds[2]), round(bounds[3]))
            elif kind == 'table':
                key = ('table', f"{len(element.get('columnWidths') or [])}x{len(element.get('rows') or [])}", 0, 0)
            elif kind == 'chart':
                key = ('chart', str(len(element.get('series') or [])), 0, 0)
            else:
                continue
            found[key].append({'page': index + 1, 'elementId': element.get('elementId')})
    result = []
    for (kind, name, width, height), uses in found.items():
        if len(uses) < threshold:
            continue
        result.append({'kind': kind, 'name': name, 'size': [width, height], 'uses': len(uses),
                       'pages': sorted({item['page'] for item in uses}), 'sample': uses[0]['elementId']})
    return sorted(result, key=lambda item: item['uses'], reverse=True)


class SlotSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=40)
    purpose: str = Field(min_length=1, max_length=200)
    characters: int = Field(ge=0, le=4000)
    repeatable: bool = False


class PageTypeSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=40)
    purpose: str = Field(min_length=1, max_length=300)
    templatePages: list[int] = Field(min_length=1, max_length=20)
    skeleton: str = Field(min_length=1, max_length=600)
    slots: list[SlotSpec] = Field(min_length=1, max_length=12)


class ComponentSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=60)
    kind: str = Field(min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=300)
    evidence: list[int] = Field(min_length=1, max_length=20)


class TemplateSpec(BaseModel):
    """The written spec the rebuild route composes from."""

    model_config = ConfigDict(extra='forbid')
    summary: str = Field(min_length=1, max_length=600)
    pageTypes: list[PageTypeSpec] = Field(min_length=1, max_length=20)
    components: list[ComponentSpec] = Field(default_factory=list, max_length=40)
    rules: list[str] = Field(default_factory=list, max_length=30)
    prohibitions: list[str] = Field(default_factory=list, max_length=20)


def attachments(folder: Path, *, pages: list[int] | None = None, limit: int = 6) -> list[dict]:
    """Overview sheet plus chosen page renders, as data URLs for the model."""
    folder = Path(folder)
    chosen = [folder / f'page-{number:02d}.png' for number in (pages or [])]
    chosen = [path for path in chosen if path.is_file()]
    if not chosen:
        chosen = [item['path'] for item in read_overview(folder, limit=limit) if item['role'] == 'template-page']
    ordered = [folder / 'overview.png'] if (folder / 'overview.png').is_file() else []
    encoded = []
    for path in [*ordered, *chosen]:
        encoded.append({'name': path.name,
                        'imageData': 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode()})
    return encoded


def spec_context(summary: dict, tokens: dict, components_found: list[dict]) -> dict:
    return {
        'task': ('归纳这份公司模板的版面规范：页型、槽位、组件、设计规则与禁止项。'
                 '只依据提供的总览图与页面渲染、以及给定的参数统计；不要臆造模板没有的元素。'),
        'rules': [
            '每个页型必须给出 templatePages（模板里对应页码）与 skeleton（版式骨架的一句话描述）',
            'slots 只列该页型真正存在的文字槽位，characters 用参数统计里的容量上界',
            'repeatable=true 表示该槽位可以按内容条数复制（例如目录条目、卡片列）',
            'components 只收录重复 ≥3 次的结构；evidence 写出现过的模板页码',
            'prohibitions 写这份模板明确不使用的做法（例如没有的装饰、没有的配色）',
        ],
        'templateReference': {'pageCount': summary.get('pageCount'), 'pageTypes': summary.get('pageTypes'),
                              'palette': summary.get('palette'), 'fonts': summary.get('fonts')},
        'design': tokens,
        'components': components_found,
    }


async def induce(model, rules, *, folder: Path, summary: dict, tokens: dict,
                 components_found: list[dict], pages: list[int] | None = None,
                 session_id: str | None = None, scenario: str = 'general') -> dict:
    """One strict JSON round trip that writes the template's spec."""
    from .agents import make_agent
    from .pptd_generation import _await_model, _message_content, _parse_json_response
    contract = ('\n\nJSON TRANSPORT CONTRACT\nReturn exactly one JSON object as assistant text, no tool call, '
                'no XML, no explanation, no code fence. Schema:\n'
                + json.dumps(TemplateSpec.model_json_schema(), ensure_ascii=False))
    system = rules.system('template-spec', scenario, template=True) + contract
    agent = make_agent('TemplateSpec', system, model, session_id)
    payload = {**spec_context(summary, tokens, components_found), 'attachments': attachments(folder, pages=pages)}
    message = Msg(name='user', content=_message_content(payload), role='user')
    response = await _await_model(agent, message, {'phase': 'context'}, None,
                                  timeout=min(float(getattr(model, 'pptd_request_timeout', 180)), 480))
    parsed = _parse_json_response(response.get_text_content(),
                                  reasoning_prefix='minimax' in str(getattr(model, 'model', '')).lower())
    spec = TemplateSpec.model_validate(parsed, strict=True).model_dump()
    (Path(folder) / 'spec.json').write_text(json.dumps(spec, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return spec


def load_spec(folder: Path) -> dict | None:
    path = Path(folder) / 'spec.json'
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        return None
