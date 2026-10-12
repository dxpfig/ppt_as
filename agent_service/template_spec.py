"""Template extraction, step 2: render the deck and build a contact sheet.

Reading parameters alone is not enough to understand a template: the skill's
workflow renders every page and stitches a thumbnail overview, then reads the key
pages closely with both the picture and the `.page` source in hand. This module
produces the pictures for that step, using the same renderer the publishing
pipeline already trusts (LibreOffice/Chromium PDF, then PyMuPDF rasterisation).
"""
from __future__ import annotations

from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

from .rendering import render_pdf

THUMB_WIDTH = 480
MARGIN = 14
CAPTION = 22


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
