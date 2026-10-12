"""Surgical PPTX editing: rewrite only the named paragraphs, clone the rest.

This is the reverse-engineered `pptx-surgical` bypass shipped with kimi-slides,
vendored byte-for-byte under `skills/pptx-surgical`. It exists because turning a
real corporate deck into PPTD is lossy: add-in data (`ppt/tags/`, think-cell),
embedded workbooks and embedded fonts do not survive the round trip, and the
loss is silent. Editing in place rewrites only the slides that are actually
named and clones every other zip part byte for byte.

The vendored scripts use only the Python standard library; this module is a thin
guard around them so the application can call the same three-step loop
(prep -> edit -> verify) with validated operations.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
import zipfile
from xml.etree import ElementTree as ET
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'pptx-surgical' / 'scripts'
TEXT_LIMIT = 5000
# One deck can legitimately rewrite every paragraph of every page it keeps; a
# company template page often carries 40-60 of them, so this bound only guards
# against pathological output instead of normal fills.
OPERATION_LIMIT = 2000

P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'

ET.register_namespace('p', P)
ET.register_namespace('r', R)


def _serialize(root, default: str) -> bytes:
    """Serialize with the file's own default namespace as the prefix-less form."""
    ET.register_namespace('', default)
    body = ET.tostring(root, encoding='UTF-8', xml_declaration=False)
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n' + body


class SurgicalError(ValueError):
    """A refused or failed surgical edit; the source file is never modified."""


def _run(script: str, *arguments: object, timeout: int = 600) -> str:
    environment = {**os.environ, 'PYTHONPATH': str(SCRIPTS)}
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / script), *[str(argument) for argument in arguments]],
            capture_output=True, text=True, timeout=timeout, env=environment)
    except subprocess.TimeoutExpired:
        raise SurgicalError(f'{script} 超时；源文件未改动。') from None
    if result.returncode:
        detail = [line for line in (result.stderr or result.stdout).strip().splitlines() if line.strip()]
        raise SurgicalError(detail[-1] if detail else f'{script} 失败；源文件未改动。')
    return result.stdout


def inspect(pptx: Path) -> dict:
    """Page list, stable paragraph addresses and un-touchable parts."""
    return json.loads(_run('prep.py', pptx, '--json'))


def validate(operations: object) -> list[dict]:
    """Accept only the two documented operations, with bounded text."""
    if not isinstance(operations, list) or not operations:
        raise SurgicalError('需要非空的 ops 列表。')
    if len(operations) > OPERATION_LIMIT:
        raise SurgicalError(f'一次最多 {OPERATION_LIMIT} 条 ops，收到 {len(operations)} 条。')
    cleaned = []
    for item in operations:
        if not isinstance(item, dict):
            raise SurgicalError('每条 op 必须是对象。')
        if set(item) == {'id', 'text'}:
            identity, text = item['id'], item['text']
            if not isinstance(identity, str) or not identity.strip():
                raise SurgicalError('op.id 必须是非空字符串。')
            if not isinstance(text, str) or len(text) > TEXT_LIMIT:
                raise SurgicalError('op.text 必须是字符串且不超过 5000 字。')
            cleaned.append({'id': identity.strip(), 'text': text})
            continue
        if set(item) == {'slide', 'find', 'replace'}:
            slide, find, replace = item['slide'], item['find'], item['replace']
            if type(slide) is not int or slide < 1:
                raise SurgicalError('op.slide 必须是正整数页号。')
            if not isinstance(find, str) or not find or len(find) > TEXT_LIMIT:
                raise SurgicalError('op.find 必须是非空字符串。')
            if not isinstance(replace, str) or len(replace) > TEXT_LIMIT:
                raise SurgicalError('op.replace 必须是字符串。')
            cleaned.append({'slide': slide, 'find': find, 'replace': replace})
            continue
        raise SurgicalError('只支持 {"id","text"} 或 {"slide","find","replace"} 两种 op。')
    return cleaned


def _resolve(base: str, target: str) -> str:
    if target.startswith('/'):
        return target.lstrip('/')
    return posixpath.normpath(posixpath.join(base, target))


def _repack_without(source: Path, destination: Path, rewritten: dict[str, bytes], dropped: set[str]) -> None:
    """Clone every entry byte-faithfully except the rewritten and dropped ones."""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(destination, 'w') as dst:
        for info in src.infolist():
            if info.filename in dropped:
                continue
            if info.flag_bits & 0x1:
                raise SurgicalError(f'包内有加密条目，拒绝改写：{info.filename}')
            clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            clone.compress_type = info.compress_type
            clone.external_attr = info.external_attr
            clone.internal_attr = info.internal_attr
            clone.create_system = info.create_system
            clone.create_version = info.create_version
            clone.extract_version = info.extract_version
            clone.flag_bits = info.flag_bits
            clone.extra = info.extra
            clone.comment = info.comment
            dst.writestr(clone, rewritten.get(info.filename, src.read(info.filename)))


def select(source: Path, pages: object, destination: Path) -> dict:
    """Keep only the chosen template pages, byte-preserving everything else.

    A company template usually carries more example pages than the new deck needs.
    The reference skill's surgical path only replaces text, so this step does the
    one page-level thing it cannot: `p:sldIdLst` is rewritten to the chosen pages
    in the chosen order, the dropped slides and their notes go away, and every
    other part keeps its original ``ZipInfo`` byte for byte.
    """
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise SurgicalError('未找到待选页的 PPTX。')
    if source == destination:
        raise SurgicalError('目标必须是另一份文件；surgical 不改写源文件。')
    if not isinstance(pages, list) or not pages:
        raise SurgicalError('需要非空的页码列表（放映顺序，从 1 起）。')
    if len(pages) > 200 or any(type(page) is not int or page < 1 for page in pages):
        raise SurgicalError('页码必须是 1 起的正整数。')
    if len(set(pages)) != len(pages):
        raise SurgicalError('页码不能重复。')
    with zipfile.ZipFile(source) as archive:
        presentation = ET.fromstring(archive.read('ppt/presentation.xml'))
        rels = ET.fromstring(archive.read('ppt/_rels/presentation.xml.rels'))
        listing = presentation.find(f'{{{P}}}sldIdLst')
        if listing is None:
            raise SurgicalError('这份包没有 p:sldIdLst，无法判断页序。')
        entries = list(listing)
        if not entries or max(pages) > len(entries):
            raise SurgicalError(f'页码超出范围：这份文件有 {len(entries)} 页。')
        targets = {rel.get('Id'): rel.get('Target') or '' for rel in rels.findall(f'{{{REL}}}Relationship')}
        ordered = []
        for node in entries:
            rid = node.get(f'{{{R}}}id') or ''
            target = targets.get(rid)
            if not target:
                raise SurgicalError(f'页 {rid} 缺少关系目标，包不一致。')
            ordered.append((node, rid, _resolve('ppt', target)))
        keep = [ordered[index - 1] for index in pages]
        keep_ids = {rid for _, rid, _ in keep}
        dropped_parts = {part for _, _, part in ordered if part not in {item[2] for item in keep}}
        # Notes/comments ride along with the slide they belong to.
        for part in list(dropped_parts):
            rels_name = posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part) + '.rels')
            if rels_name not in archive.namelist():
                continue
            for relation in ET.fromstring(archive.read(rels_name)).findall(f'{{{REL}}}Relationship'):
                target = relation.get('Target') or ''
                if relation.get('TargetMode') == 'External' or not target:
                    continue
                resolved = _resolve(posixpath.dirname(part), target)
                if '/notesSlides/' in '/' + resolved or '/comments/' in '/' + resolved:
                    dropped_parts.add(resolved)
        for element in list(listing):
            listing.remove(element)
        for node, _, _ in keep:
            listing.append(node)
        # Only the slide relationships go away. The master, theme, notes master,
        # table styles and property parts stay: dropping them silently loses
        # layout-inherited artwork such as the company copyright line.
        for relation in list(rels):
            target = relation.get('Target') or ''
            if relation.get('TargetMode') == 'External' or not target:
                continue
            if _resolve('ppt', target) in dropped_parts:
                rels.remove(relation)
        content_types = ET.fromstring(archive.read('[Content_Types].xml'))
        dropped_overrides = {'/' + part for part in dropped_parts}
        for override in list(content_types):
            if override.get('PartName') in dropped_overrides:
                content_types.remove(override)
        rewritten = {
            'ppt/presentation.xml': _serialize(presentation, P),
            'ppt/_rels/presentation.xml.rels': _serialize(rels, REL),
            '[Content_Types].xml': _serialize(content_types, CT),
        }
        for part in dropped_parts:
            rewritten.pop(part, None)
        dropped = set(dropped_parts)
        for part in dropped_parts:
            dropped.add(posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part) + '.rels'))
    destination.parent.mkdir(parents=True, exist_ok=True)
    _repack_without(source, destination, rewritten, dropped)
    self_check = _run('verify.py', '--self', destination)
    return {'output': str(destination), 'pages': pages, 'dropped': sorted(dropped),
            'verification': {'self': self_check.strip().splitlines()}}


def apply(source: Path, operations: object, destination: Path) -> dict:
    """Rewrite the named paragraphs into ``destination`` and verify the result."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise SurgicalError('未找到待修改的 PPTX。')
    if source == destination:
        raise SurgicalError('目标必须是另一份文件；surgical 不改写源文件。')
    cleaned = validate(operations)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', suffix='.ops.json', delete=False, encoding='utf-8') as handle:
        json.dump(cleaned, handle, ensure_ascii=False)
        operations_path = Path(handle.name)
    try:
        edit_output = _run('edit.py', source, '--ops', operations_path, '--out', destination)
        self_check = _run('verify.py', '--self', destination)
        comparison = _run('verify.py', '--before', source, '--after', destination)
    finally:
        operations_path.unlink(missing_ok=True)
    return {
        'output': str(destination),
        'operations': cleaned,
        'edit': edit_output.strip().splitlines(),
        'verification': {
            'self': self_check.strip().splitlines(),
            'comparison': comparison.strip().splitlines(),
            'untouchedPartsIdentical': any('未点名的条目字节全等' in line for line in comparison.splitlines()),
        },
    }
