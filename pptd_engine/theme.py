"""Read a package's own theme, master defaults and colour map.

Two different jobs live here, and they are not the same thing:

* **Resolving colours.** Elements may reference `schemeClr`/`sysClr` instead of
  literal hex. Without the theme map those references were dropped, which is why
  a deck could import with "theme color omitted" warnings. This module resolves
  them through `p:clrMap` + the theme colour scheme.
* **Extracting defaults.** PPTD has its own `theme.colors/textStyles/tableStyles`
  block. Many hand-designed decks leave Office's stock theme in place and put the
  real design in direct formatting, so these defaults are a *fallback*, not the
  design system - the caller still has to derive the design from what is actually
  painted on the pages.
"""
from __future__ import annotations

from zipfile import ZipFile

from lxml import etree

A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
P = 'http://schemas.openxmlformats.org/presentationml/2006/main'

SCHEME_ORDER = ('dk1', 'lt1', 'dk2', 'lt2', 'accent1', 'accent2', 'accent3', 'accent4',
                'accent5', 'accent6', 'hlink', 'folHlink')
# Office's logical colour names -> the colour-map slots they resolve through.
LOGICAL = {'bg1': 1, 'tx1': 0, 'bg2': 3, 'tx2': 2, 'accent1': 4, 'accent2': 5, 'accent3': 6,
           'accent4': 7, 'accent5': 8, 'accent6': 9, 'hlink': 10, 'folHlink': 11}


def _clark(namespace: str, tag: str) -> str:
    return f'{{{namespace}}}{tag}'


def _colour(node) -> str | None:
    """One `<a:srgbClr|sysClr|prstClr>` entry as HEX6, honouring `sysClr` fallbacks."""
    if node is None:
        return None
    kind = etree.QName(node).localname
    value = node.get('val') or node.get('lastClr') or ''
    if kind == 'sysClr':
        value = node.get('lastClr') or node.get('val') or ''
    if kind == 'prstClr':
        preset = {'black': '000000', 'white': 'FFFFFF'}
        value = preset.get(value, '')
    value = value.strip().lstrip('#')
    return '#' + value.upper() if len(value) == 6 else None


def _scheme(root) -> dict[str, str]:
    scheme = root.find('.//' + _clark(A, 'clrScheme'))
    if scheme is None:
        return {}
    result = {}
    for child in scheme:
        name = etree.QName(child).localname
        if name in SCHEME_ORDER and len(child):
            value = _colour(child[0])
            if value:
                result[name] = value
    return result


def _fonts(root) -> dict:
    fonts = root.find('.//' + _clark(A, 'fontScheme'))
    if fonts is None:
        return {}
    result = {}
    for kind in ('majorFont', 'minorFont'):
        node = fonts.find(_clark(A, kind))
        if node is None:
            continue
        entry = {}
        for script in ('latin', 'ea', 'cs'):
            face = node.find(_clark(A, script))
            if face is not None and face.get('typeface'):
                entry[script] = face.get('typeface')
        if entry:
            result['major' if kind == 'majorFont' else 'minor'] = entry
    return result


def _colour_map(master) -> dict[str, str]:
    """`p:clrMap` maps logical names onto scheme slots; follow it instead of assuming."""
    node = master.find(_clark(P, 'clrMap')) if master is not None else None
    mapping = {}
    if node is not None:
        for logical, attribute in (('bg1', 'bg1'), ('tx1', 'tx1'), ('bg2', 'bg2'), ('tx2', 'tx2'),
                                   *((f'accent{i}', f'accent{i}') for i in range(1, 7)),
                                   ('hlink', 'hlink'), ('folHlink', 'folHlink')):
            value = node.get(attribute)
            if value:
                mapping[logical] = value
    return mapping


def _text_styles(master, scheme: dict[str, str], fonts: dict) -> dict:
    """Master `txStyles` levels as PPTD text styles (px == pt in this format)."""
    styles: dict = {}
    styles_node = master.find(_clark(P, 'txStyles')) if master is not None else None
    if styles_node is None:
        return styles
    names = {'titleStyle': 'title', 'bodyStyle': 'body', 'otherStyle': 'other'}
    for node in styles_node:
        base = names.get(etree.QName(node).localname)
        if base is None:
            continue
        for index, level in enumerate(list(node), start=1):
            run = level.find(_clark(A, 'defRPr'))
            if run is None:
                continue
            style: dict = {}
            if run.get('sz'):
                style['fontSize'] = round(int(run.get('sz')) / 100, 2)
            if run.get('b') is not None:
                style['bold'] = run.get('b') in {'1', 'true'}
            if run.get('i') is not None:
                style['italic'] = run.get('i') in {'1', 'true'}
            colour = run.find(_clark(A, 'solidFill') + '/' + _clark(A, 'schemeClr'))
            if colour is not None and scheme.get(colour.get('val') or ''):
                style['color'] = scheme[colour.get('val')]
            for script in ('latin', 'ea'):
                face = run.find(_clark(A, script))
                if face is None or not face.get('typeface'):
                    continue
                typeface = face.get('typeface')
                if typeface.startswith('+'):
                    group, slot = typeface[1:3], typeface[3:]
                    family = fonts.get({'mj': 'major', 'mn': 'minor'}.get(group, 'minor'), {}).get(
                        {'lt': 'latin', 'ea': 'ea', 'cs': 'cs'}.get(slot, 'latin'))
                    if family:
                        style['fontFamily'] = family
                else:
                    style['fontFamily'] = typeface
                break
            if style and (index == 1 or len(styles) < 12):
                styles[base if index == 1 else f'{base}-{index}'] = style
    return styles


def extract(package: ZipFile) -> dict:
    """Theme colours, master text styles and the scheme map for one package."""
    try:
        theme = etree.fromstring(package.read('ppt/theme/theme1.xml'))
    except (KeyError, etree.XMLSyntaxError):
        return {}
    scheme = _scheme(theme)
    if not scheme:
        return {}
    masters = sorted(name for name in package.namelist()
                     if name.startswith('ppt/slideMasters/slideMaster') and name.endswith('.xml'))
    master = None
    if masters:
        try:
            master = etree.fromstring(package.read(masters[0]))
        except etree.XMLSyntaxError:
            master = None
    fonts = _fonts(theme)
    colour_map = _colour_map(master)
    colors = {name: value for name, value in scheme.items()}
    # Logical names resolve through clrMap, so a deck that swapped tx1/bg1 still reads right.
    for logical, slot in colour_map.items():
        if scheme.get(slot):
            colors.setdefault(logical, scheme[slot])
            # Colour resolution needs the same aliases, not just the raw scheme.
            scheme.setdefault(logical, scheme[slot])
    colors.setdefault('text', colors.get('tx1', '#000000'))
    colors.setdefault('background', colors.get('bg1', '#FFFFFF'))
    colors.setdefault('primary', colors.get('accent1', '#000000'))
    text_styles = _text_styles(master, scheme, fonts)
    theme_block = {'colors': colors}
    if text_styles:
        theme_block['textStyles'] = text_styles
    if fonts:
        theme_block['fonts'] = fonts
    return {'theme': theme_block, 'scheme': scheme, 'colorMap': colour_map}


def resolve(scheme: dict[str, str], theme_color) -> str | None:
    """Map a python-pptx theme colour enum onto the package's own scheme."""
    name = getattr(theme_color, 'name', None)
    if not name:
        return None
    key = {
        'ACCENT_1': 'accent1', 'ACCENT_2': 'accent2', 'ACCENT_3': 'accent3', 'ACCENT_4': 'accent4',
        'ACCENT_5': 'accent5', 'ACCENT_6': 'accent6', 'TEXT_1': 'tx1', 'TEXT_2': 'tx2',
        'BACKGROUND_1': 'bg1', 'BACKGROUND_2': 'bg2', 'DARK_1': 'dk1', 'LIGHT_1': 'lt1',
        'DARK_2': 'dk2', 'LIGHT_2': 'lt2', 'HYPERLINK': 'hlink', 'FOLLOWED_HYPERLINK': 'folHlink',
    }.get(str(name))
    return scheme.get(key) if key else None
