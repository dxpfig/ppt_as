"""Structure-aware DOCX text extraction for long presentation sources."""
from __future__ import annotations

from pathlib import Path
import re
from zipfile import ZipFile

from lxml import etree


W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
NS = {'w': W}


def _q(name):
    return f'{{{W}}}{name}'


def _paragraph_text(paragraph):
    values = []
    for node in paragraph.iter():
        if node.tag == _q('t'):
            values.append(node.text or '')
        elif node.tag == _q('tab'):
            values.append('\t')
        elif node.tag in (_q('br'), _q('cr')):
            values.append('\n')
    return ''.join(values).strip()


def _style_names(styles_xml):
    if styles_xml is None:
        return {}
    root = etree.fromstring(styles_xml)
    names = {}
    for style in root.findall('w:style', NS):
        identifier = style.get(_q('styleId'))
        name = style.find('w:name', NS)
        if identifier and name is not None:
            names[identifier] = name.get(_q('val')) or identifier
    return names


def _heading_style(style_name):
    value = (style_name or '').strip().lower()
    match = re.fullmatch(r'(?:heading|标题)\s*([1-6])', value)
    if match:
        return int(match.group(1))
    if value in ('title', '标题'):
        return 1
    if value in ('subtitle', '副标题'):
        return 2
    return None


def _cell_text(cell):
    paragraphs = []
    for paragraph in cell.findall('.//w:p', NS):
        text = _paragraph_text(paragraph).replace('\n', ' / ')
        if text:
            paragraphs.append(text)
    return '<br>'.join(paragraphs).replace('|', '\\|')


def _table_markdown(table):
    rows = []
    for row in table.findall('w:tr', NS):
        rows.append([_cell_text(cell) for cell in row.findall('w:tc', NS)])
    rows = [row for row in rows if any(row)]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    rows = [row + [''] * (width - len(row)) for row in rows]
    output = ['| ' + ' | '.join(rows[0]) + ' |',
              '| ' + ' | '.join('---' for _ in rows[0]) + ' |']
    output.extend('| ' + ' | '.join(row) + ' |' for row in rows[1:])
    return output


def docx_to_markdown(path):
    """Preserve heading levels, lists and tables from WordprocessingML.

    The output is source material for a model, not a visual rendering. It keeps
    the document's argument hierarchy and table relationships that raw-text
    extraction loses, while intentionally omitting Word-only visual styling.
    """
    path = Path(path)
    with ZipFile(path) as archive:
        document_xml = archive.read('word/document.xml')
        styles_xml = archive.read('word/styles.xml') if 'word/styles.xml' in archive.namelist() else None
    styles = _style_names(styles_xml)
    root = etree.fromstring(document_xml)
    body = root.find('w:body', NS)
    if body is None:
        return ''
    output = []
    for child in body:
        if child.tag == _q('p'):
            text = _paragraph_text(child)
            if not text:
                continue
            properties = child.find('w:pPr', NS)
            style_id = None
            numbered = False
            if properties is not None:
                style = properties.find('w:pStyle', NS)
                style_id = style.get(_q('val')) if style is not None else None
                numbered = properties.find('w:numPr', NS) is not None
            heading = _heading_style(styles.get(style_id, style_id or ''))
            if heading:
                output.extend(['#' * heading + ' ' + text, ''])
            elif numbered:
                output.append('- ' + text)
            else:
                output.extend([text, ''])
        elif child.tag == _q('tbl'):
            rows = _table_markdown(child)
            if rows:
                output.extend(['', *rows, ''])
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(output)).strip() + '\n'
