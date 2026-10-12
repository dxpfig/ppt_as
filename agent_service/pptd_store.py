"""Authoritative PPTD documents with atomic publication and immutable versions.

``documents/<id>/document.pptd`` is a stable path into the current snapshot.
Only the directory pointer is replaced when publishing. Application metadata and
version history live outside the source tree; no JSON document shadow exists.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import html
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import uuid
from zipfile import ZipFile, ZIP_DEFLATED

import yaml
from pptd_engine.document import load_yaml, ProbeDumper, Yaml12SafeLoader


class RevisionConflict(ValueError):
    """The client's base revision is older than the authoritative document."""


def safe_path(root: Path, reference: str) -> Path:
    if not isinstance(reference, str) or not reference or any(c in reference for c in ('\\', ':', '\0')):
        raise ValueError('A safe relative document path is required')
    relative = PurePosixPath(reference)
    if relative.is_absolute() or '..' in relative.parts or str(relative) != reference or reference == '.':
        raise ValueError('Document paths must remain inside the project')
    target = root.joinpath(*relative.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('Document paths must remain inside the project')
    # Source trees are self-contained. The store's own parent pointer is allowed,
    # while user-created links within a source tree are rejected.
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Symbolic links inside documents are not supported')
    return target


def _serialize(value):
    json.dumps(value, allow_nan=False)
    result = yaml.dump(value, Dumper=ProbeDumper, allow_unicode=True, sort_keys=False, width=1000)
    if yaml.load(result, Loader=Yaml12SafeLoader) != value:
        raise ValueError('YAML serialization changed document values')
    return result


def _copy_source(source, destination):
    if any(path.is_symlink() for path in source.rglob('*')):
        raise ValueError('Symbolic links inside documents are not supported')
    shutil.copytree(source, destination)


class DocumentStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.documents = self.root / 'documents'
        self.versions = self.root / 'document_versions'
        self.locks = self.root / 'document_locks'
        for directory in (self.documents, self.versions, self.locks):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _id(project_id):
        if not isinstance(project_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', project_id):
            raise ValueError('Invalid project id')
        return project_id

    @contextmanager
    def _lock(self, project_id):
        with (self.locks / (self._id(project_id) + '.lock')).open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def source_path(self, project_id):
        return self.documents / self._id(project_id) / 'document.pptd'

    def exists(self, project_id):
        return self.source_path(project_id).is_file()

    def _current(self, project_id):
        pointer = self.documents / self._id(project_id)
        source = pointer.resolve(strict=True)
        expected_parent = (self.versions / project_id).resolve()
        if not source.is_relative_to(expected_parent) or source.name != 'source':
            raise ValueError('Invalid document publication pointer')
        return source

    @staticmethod
    def _validate(document, pages):
        if not isinstance(document, dict) or document.get('version') != 'v2':
            raise ValueError('PPTD version v2 is required')
        size = document.get('size')
        if not isinstance(size, list) or len(size) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in size):
            raise ValueError('Document size must contain two positive finite numbers')
        references = document.get('pages')
        if not isinstance(references, list) or not references or len(references) > 60:
            raise ValueError('Document must contain between 1 and 60 pages')
        if not isinstance(pages, list) or [p.get('path') for p in pages if isinstance(p, dict)] != references:
            raise ValueError('Page records must match the main document page order')
        if any(not isinstance(ref, str) for ref in references):
            raise ValueError('Page paths must be strings')
        if len(set(references)) != len(references):
            raise ValueError('Page paths must be unique')
        for page in pages:
            path = safe_path(Path('/virtual-pptd-root'), page['path'])
            if path.suffix != '.page':
                raise ValueError('Page references must use .page files')
            data = page.get('data')
            if not isinstance(data, dict) or not isinstance(data.get('elements'), list):
                raise ValueError('Page data must contain an elements array')
            ids = [e.get('elementId') if isinstance(e, dict) else None for e in data['elements']]
            if any(not isinstance(v, str) or not v for v in ids) or len(set(ids)) != len(ids):
                raise ValueError('Elements require unique non-empty elementId values')
            _serialize(data)
        _serialize(document)

    @classmethod
    def _read_source(cls, source):
        document = load_yaml(source / 'document.pptd')
        pages = [{'path': ref, 'data': load_yaml(safe_path(source, ref))} for ref in document.get('pages', [])]
        cls._validate(document, pages)
        return {'document': document, 'pages': pages}

    def read(self, project_id):
        # Resolve the pointer once. A concurrent publication cannot change this
        # immutable source/metadata pair while it is read.
        source = self._current(project_id)
        state = self._read_source(source)
        metadata = json.loads((source.parent / 'metadata.json').read_text(encoding='utf-8'))
        return {**state, 'revision': metadata['revision'], 'versions': metadata['versions']}

    def resource_path(self, project_id, reference):
        return safe_path(self._current(project_id), reference)

    @staticmethod
    def _check_revision(state, expected_revision):
        actual = state['revision'] if state else 0
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision != actual):
            raise RevisionConflict(f'文档已更新（当前版本 {actual}）；请重新加载后再保存。')

    def commit(self, project_id, document, pages, label, expected_revision=None, *, assets=None):
        with self._lock(project_id):
            return self._commit(project_id, document, pages, label, expected_revision, assets=assets)

    def _commit(self, project_id, document, pages, label, expected_revision=None, *, assets=None, base_source=None):
        self._validate(document, pages)
        previous = self.read(project_id) if self.exists(project_id) else None
        self._check_revision(previous, expected_revision)
        version_id = uuid.uuid4().hex
        parent = self.versions / project_id
        parent.mkdir(parents=True, exist_ok=True)
        version_dir = parent / version_id
        version_dir.mkdir()
        source = version_dir / 'source'
        pending_pointer = self.documents / ('.' + project_id + '-' + version_id)
        published = False
        try:
            if base_source is None and previous:
                base_source = self._current(project_id)
            if base_source:
                _copy_source(base_source, source)
            else:
                source.mkdir()
            for reference, value in (assets or {}).items():
                target = safe_path(source, reference)
                if not reference.startswith('media/'):
                    raise ValueError('Asset paths must be inside media/')
                target.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(value, Path):
                    if value.is_symlink() or not value.is_file():
                        raise ValueError('Asset must be a regular local file')
                    target.write_bytes(value.read_bytes())
                elif isinstance(value, bytes):
                    target.write_bytes(value)
                else:
                    raise ValueError('Asset values must be bytes or Path')
            # Preserve bytes (including comments) for every untouched source file.
            for reference, value in [('document.pptd', document)] + [(p['path'], p['data']) for p in pages]:
                target = safe_path(source, reference)
                if target.is_file() and load_yaml(target) == value:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(_serialize(value), encoding='utf-8')
            self._read_source(source)
            revision = previous['revision'] + 1 if previous else 1
            version = {'id': version_id, 'revision': revision, 'label': str(label)[:300], 'createdAt': datetime.now(timezone.utc).isoformat(), 'pageCount': len(pages)}
            history = (previous['versions'] if previous else []) + [version]
            (version_dir / 'metadata.json').write_text(json.dumps({'revision': revision, 'versions': history}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            # Flush the entire snapshot before making it reachable by readers.
            for path in version_dir.rglob('*'):
                if path.is_file():
                    with path.open('rb') as handle:
                        os.fsync(handle.fileno())
            pending_pointer.symlink_to(os.path.relpath(source, self.documents), target_is_directory=True)
            os.replace(pending_pointer, self.documents / project_id)
            published = True
            return {'document': deepcopy(document), 'pages': deepcopy(pages), 'revision': revision, 'versions': history}
        finally:
            pending_pointer.unlink(missing_ok=True)
            if not published:
                shutil.rmtree(version_dir, ignore_errors=True)

    def edit(self, project_id, patch, expected_revision=None):
        if not isinstance(patch, dict) or set(patch) - {'page', 'pageIndex', 'elementId', 'text', 'bounds'}:
            raise ValueError('Unsupported document edit fields')
        if not {'text', 'bounds'} & set(patch):
            raise ValueError('Provide text and/or bounds')
        with self._lock(project_id):
            state = self.read(project_id)
            self._check_revision(state, expected_revision)
            pages = deepcopy(state['pages'])
            reference = patch.get('page')
            if reference is None:
                index = patch.get('pageIndex')
                if type(index) is not int or not 0 <= index < len(pages):
                    raise ValueError('Select a page path or zero-based pageIndex')
                reference = pages[index]['path']
            selected = next((p for p in pages if p['path'] == reference), None)
            if selected is None:
                raise ValueError('Page is not part of this document')
            element = next((e for e in selected['data']['elements'] if e['elementId'] == patch.get('elementId')), None)
            if element is None:
                raise ValueError('Element was not found on the selected page')
            if 'text' in patch:
                if element.get('elementType') != 'text' or not isinstance(patch['text'], str) or len(patch['text']) > 100000:
                    raise ValueError('Text edits require a text element and a string up to 100,000 characters')
                element.setdefault('content', {})['text'] = patch['text']
            if 'bounds' in patch:
                values = patch['bounds']
                if not isinstance(values, list) or len(values) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in values) or any(v < 0 for v in values[2:]):
                    raise ValueError('Bounds must be [x,y,width,height] with finite values and non-negative dimensions')
                element['bounds'] = values
            return self._commit(project_id, state['document'], pages, '编辑 ' + element['elementId'], state['revision'])

    def restore(self, project_id, version_id, expected_revision=None):
        with self._lock(project_id):
            current = self.read(project_id)
            self._check_revision(current, expected_revision)
            if not any(v['id'] == version_id for v in current['versions']):
                raise ValueError('Unknown document version')
            source = self.versions / project_id / version_id / 'source'
            restored = self._read_source(source)
            return self._commit(project_id, restored['document'], restored['pages'], '恢复版本 ' + version_id[:8], current['revision'], base_source=source)

    def snapshot(self, project_id, destination):
        source = self._current(project_id)
        metadata = json.loads((source.parent / 'metadata.json').read_text(encoding='utf-8'))
        state = self._read_source(source)
        destination = Path(destination)
        if destination.exists():
            raise ValueError('Snapshot destination must not exist')
        _copy_source(source, destination)
        return {**state, 'revision': metadata['revision'], 'versions': metadata['versions']}

    def bundle(self, project_id, output_zip):
        source = self._current(project_id)
        output_zip = Path(output_zip)
        output_zip.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output_zip.parent, suffix='.zip', delete=False) as temporary:
            pending = Path(temporary.name)
        try:
            with ZipFile(pending, 'w', ZIP_DEFLATED) as archive:
                for file in sorted(source.rglob('*')):
                    if file.is_symlink():
                        raise ValueError('Symbolic links inside documents are not supported')
                    if file.is_file():
                        archive.write(file, file.relative_to(source).as_posix())
            os.replace(pending, output_zip)
        finally:
            pending.unlink(missing_ok=True)
        return output_zip

    def migrate_legacy(self, project_id, project_dict):
        """Explicit one-time migration; legacy JSON can never replace a PPTD."""
        with self._lock(project_id):
            if self.exists(project_id):
                return self.read(project_id)
            slides = project_dict.get('slides') or []
            if not slides:
                raise ValueError('Legacy project has no slides to migrate')
            colors = {'forest': ('#166854', '#F6F5EF', '#17342D'), 'ink': ('#2563EB', '#F8FAFC', '#102038'), 'clay': ('#B75C3C', '#FAF5EE', '#3D2923')}
            accent, paper, ink = colors.get(project_dict.get('theme'), colors['forest'])
            pages, assets = [], {}
            used = set()
            for index, slide in enumerate(slides):
                identifier = re.sub(r'[^A-Za-z0-9_-]', '-', str(slide.get('id') or f'slide-{index + 1}'))[:100]
                if not identifier or identifier in used:
                    identifier = f'slide-{index + 1}-{uuid.uuid4().hex[:8]}'
                used.add(identifier)
                def text(suffix, bounds, value, style):
                    return {'elementId': identifier + '-' + suffix, 'elementType': 'text', 'bounds': bounds, 'content': {'text': value, 'style': style}}
                title = text('title', [48, 42, 864, 92], '<p>' + html.escape(str(slide.get('title', ''))) + '</p>', '$title')
                body_values = ([slide['subtitle']] if slide.get('subtitle') else []) + list(slide.get('points', []))
                body = text('body', [52, 156, 852, 316], ''.join('<p>' + html.escape(str(v)) + '</p>' for v in body_values), '$body')
                notes = str(slide.get('notes', ''))
                if slide.get('source'):
                    notes += '\n\n来源：' + str(slide['source'])
                elements = [title, body]
                picture = slide.get('image')
                if picture and not slide.get('imageDisabled'):
                    asset_id = picture.get('assetId', '')
                    if not re.fullmatch(r'[a-f0-9]{64}', asset_id):
                        raise ValueError('旧项目图片资源 ID 无效，无法完整迁移。')
                    original = self.root / 'images' / (asset_id + '.jpg')
                    if not original.is_file() or original.is_symlink():
                        raise ValueError('旧项目图片缓存缺失，请恢复图片后再迁移。')
                    from PIL import Image
                    with Image.open(original) as image:
                        scale = min(398 / image.width, 300 / image.height)
                        width, height = image.width * scale, image.height * scale
                    reference = 'media/' + asset_id + '.jpg'
                    assets[reference] = original
                    body['bounds'] = [52, 156, 408, 316]
                    elements.append({'elementId': identifier + '-image', 'elementType': 'image', 'bounds': [510 + (398 - width) / 2, 166 + (300 - height) / 2, width, height], 'src': reference})
                    notes += '\n\n配图：' + str(picture.get('title', '')) + '\n来源：' + str(picture.get('sourceUrl', '')) + '\n作者：' + str(picture.get('author', '')) + '\n许可：' + str(picture.get('license', ''))
                pages.append({'path': 'pages/' + identifier + '.page', 'data': {'pageType': 'cover' if index == 0 else 'content', 'background': {'type': 'solid', 'color': paper}, 'elements': elements, 'notes': notes}})
            document = {'version': 'v2', 'title': project_dict.get('title', '迁移的演示文稿'), 'size': [960, 540], 'theme': {'colors': {'primary': accent, 'ink': ink, 'paper': paper}, 'textStyles': {'title': {'fontFamily': 'Noto Sans CJK SC', 'fontSize': 32, 'color': '$ink', 'bold': True}, 'body': {'fontFamily': 'Noto Sans CJK SC', 'fontSize': 22, 'color': '$ink', 'lineHeight': 1.4}}}, 'pages': [p['path'] for p in pages]}
            return self._commit(project_id, document, pages, '从旧项目明确迁移', 0, assets=assets)
