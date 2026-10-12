"""Preset design systems shipped with the app.

These are the MIT-licensed style specs from `open-kimi-ppt-skill`, vendored under
`skills/design-systems/specs`. They give the "指定设计规范" mode something
concrete to start from instead of an empty textarea, and they stay ordinary text:
whatever the user picks is sent as the request's ``designSystem`` and can be
edited first.

The upstream package ships two parallel trees. Where a style appears in both, the
longer (newer numbered catalogue) file wins, so the picker never shows a name
twice.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPECS = ROOT / 'skills' / 'design-systems' / 'specs'
MAX_SPEC_BYTES = 200_000

# Upstream directory name -> the label shown in the picker. The package ships two
# parallel catalogues, so their labels are merged into one set of buckets.
CATEGORY_LABELS = {
    '01_strategy': '战略与咨询',
    '02_business': '商业与财务',
    '03_work': '职场与年报',
    '04_promotion': '品牌与推广',
    '05_academic': '学术与教育',
    'consulting': '战略与咨询',
    'finance': '商业与财务',
    'work': '职场与年报',
    'promotion': '品牌与推广',
    'academic': '学术与教育',
}
NAME_PART = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 _-]*$')


class DesignSystemError(ValueError):
    """Unknown or unreadable preset."""


def _entry(path: Path) -> dict | None:
    relative = path.relative_to(SPECS)
    parts = relative.parts
    if len(parts) == 3 and parts[-1] == 'design.md':
        category, name = parts[0], parts[1]
    elif len(parts) == 4 and parts[0] in CATEGORY_LABELS and parts[-1].endswith('.md'):
        category, name = parts[0], parts[-1][:-3]
    else:
        return None
    text = path.read_bytes()[:MAX_SPEC_BYTES].decode('utf-8', 'ignore')
    # The numbered catalogue uses sample deck titles as headings, so the file name
    # is the only reliable style name across both trees.
    title = ' '.join(word.capitalize() for word in re.split(r'[-_]', name) if word)
    return {
        'id': name,
        'name': title,
        'label': name.replace('-', ' '),
        'category': CATEGORY_LABELS.get(category, category),
        'characters': len(text),
        'path': relative.as_posix(),
        '_source': path,
    }


def _specs() -> dict[str, dict]:
    found: dict[str, dict] = {}
    if not SPECS.is_dir():
        return found
    for path in sorted(SPECS.rglob('*.md')):
        entry = _entry(path)
        if entry is None or not NAME_PART.match(entry['id']):
            continue
        current = found.get(entry['id'])
        # Prefer the richer file when both catalogues describe the same style.
        if current is None or entry['characters'] > current['characters']:
            found[entry['id']] = entry
    return found


def catalog() -> list[dict]:
    """Every preset, ordered by category then name."""
    entries = [{key: value for key, value in entry.items() if not key.startswith('_')}
               for entry in _specs().values()]
    return sorted(entries, key=lambda entry: (entry['category'], entry['id']))


def load(identifier: str) -> dict:
    """The full spec text for one preset."""
    entry = _specs().get(str(identifier))
    if entry is None:
        raise DesignSystemError('未找到该预设设计规范。')
    text = entry['_source'].read_text(encoding='utf-8')
    if len(text.encode()) > MAX_SPEC_BYTES:
        raise DesignSystemError('该预设设计规范过大，无法注入。')
    return {'id': entry['id'], 'name': entry['name'], 'category': entry['category'], 'text': text}
