"""Incremental browser previews for pages written by the PPT expert."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import time

import yaml
from pptd_engine.document import load_yaml

from .kimi_validation import _render_pages
from .kimi_workspace import deck_status


MANIFEST_NAME = '.draft-preview.json'
PREVIEW_DIRECTORY = 'draft-previews'


def _write_manifest(workspace: Path, value: dict) -> None:
    target = workspace / MANIFEST_NAME
    temporary = target.with_suffix('.tmp')
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    temporary.replace(target)


def _page_titles(workspace: Path, paths: list[str]) -> dict[str, str]:
    title_map = {}
    plan_path = workspace / 'deck' / 'plan.json'
    try:
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return title_map
    items = plan.get('pages') if isinstance(plan, dict) else None
    if not isinstance(items, list):
        return title_map
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        path = item.get('path')
        if path in paths:
            title_map[path] = str(item.get('title') or f'第 {index + 1} 页')[:120]
    return title_map


def _media_references(value, result: set[str]) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _media_references(item, result)
    elif isinstance(value, list):
        for item in value:
            _media_references(item, result)
    elif isinstance(value, str) and value.startswith('media/'):
        path = Path(value)
        if '..' not in path.parts:
            result.add(value)


def _write_preview_deck(
    temporary: Path,
    entry: Path,
    document: dict,
    path: str,
    page_data: dict,
) -> Path:
    preview = temporary / 'deck'
    preview.mkdir(parents=True, exist_ok=True)
    media = set()
    _media_references(page_data, media)
    for relative in media:
        source = entry.parent / relative
        if not source.is_file():
            continue
        target = preview / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    target = preview / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(page_data, allow_unicode=True, sort_keys=False),
        encoding='utf-8',
    )
    mini_entry = preview / entry.name
    mini_entry.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding='utf-8',
    )
    return mini_entry


def _render_page(
    workspace: Path,
    entry: Path,
    document: dict,
    path: str,
    index: int,
) -> tuple[bool, str, str]:
    """Render one page in an isolated one-page deck."""
    try:
        page_data = load_yaml(entry.parent / path)
    except (OSError, UnicodeError, ValueError):
        return False, '', '页面尚未形成完整 YAML'
    digest = hashlib.sha256(
        json.dumps(page_data, ensure_ascii=False, sort_keys=True).encode('utf-8')
    ).hexdigest()[:16]
    temporary = workspace / '.draft-preview-render' / f'{index + 1}-{digest}'
    shutil.rmtree(temporary, ignore_errors=True)
    preview_document = deepcopy(document)
    preview_document['pages'] = [path]
    try:
        mini_entry = _write_preview_deck(
            temporary,
            entry,
            preview_document,
            path,
            page_data,
        )
        rendered = _render_pages(mini_entry)
        image = rendered / 'pages' / 'page-1.png' if rendered else None
        if image is None or not image.is_file():
            return False, digest, '浏览器预览未能生成'
        preview_directory = workspace / PREVIEW_DIRECTORY
        preview_directory.mkdir(parents=True, exist_ok=True)
        final_name = f'page-{index + 1}.png'
        target = preview_directory / final_name
        temporary_image = target.with_suffix('.tmp')
        temporary_image.write_bytes(image.read_bytes())
        temporary_image.replace(target)
        return True, digest, final_name
    except Exception:
        return False, digest, '浏览器预览生成失败'
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


class DraftPreviewPublisher:
    """Poll page files while the agent works and publish actual browser renders."""

    def __init__(self, workspace: Path, entry: Path, job_id: str, on_event=None):
        self.workspace = workspace
        self.entry = entry
        self.job_id = job_id
        self.on_event = on_event
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._manifest = self._load_manifest()
        self._hashes: dict[str, str] = {
            str(item.get('path')): str(item.get('sourceHash'))
            for item in self._manifest.get('pages', [])
            if item.get('status') == 'ready'
            and item.get('sourceHash')
            and item.get('previewFile')
        }

    def _load_manifest(self) -> dict:
        path = self.workspace / MANIFEST_NAME
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        pages = value.get('pages')
        return {
            'status': 'draft',
            'completedPages': int(value.get('completedPages') or 0),
            'totalPages': int(value.get('totalPages') or 0),
            'pages': [item for item in pages if isinstance(item, dict)]
            if isinstance(pages, list)
            else [],
            'updatedAt': value.get('updatedAt') or 0,
        }

    async def _emit(self, page: dict) -> None:
        if not self.on_event:
            return
        try:
            result = self.on_event(
                {
                    'phase': 'pages',
                    'status': 'completed',
                    'message': f"第 {page['index'] + 1} 页已生成，可查看实际预览。",
                    'pagePath': page['path'],
                    'pageIndex': page['index'],
                    'title': page['title'],
                }
            )
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            # A cancelled or terminal job may reject late progress events.
            return

    async def scan(self) -> None:
        try:
            document, paths, existing, _, _ = deck_status(self.entry)
        except (OSError, UnicodeError, ValueError):
            return
        titles = _page_titles(self.workspace, paths)
        previous = {
            str(item.get('path')): item
            for item in self._manifest.get('pages', [])
            if isinstance(item, dict) and item.get('path')
        }
        pages = []
        changed: list[tuple[int, str]] = []
        for index, path in enumerate(paths):
            item = dict(previous.get(path) or {})
            item.update(
                path=path,
                index=index,
                title=titles.get(path) or item.get('title') or f'第 {index + 1} 页',
            )
            if path not in existing:
                item.update(status='pending', previewFile=None, renderHash=None)
                self._hashes.pop(path, None)
            else:
                target = self.entry.parent / path
                try:
                    digest = hashlib.sha256(target.read_bytes()).hexdigest()
                except OSError:
                    digest = ''
                preview = self.workspace / PREVIEW_DIRECTORY / f'page-{index + 1}.png'
                if (
                    digest
                    and digest == self._hashes.get(path)
                    and preview.is_file()
                ):
                    item.update(
                        status='ready',
                        previewFile=preview.name,
                        renderHash=item.get('renderHash') or digest[:16],
                    )
                else:
                    item['status'] = 'rendering'
                    changed.append((index, path))
            pages.append(item)
        self._manifest.update(
            completedPages=sum(item.get('status') == 'ready' for item in pages),
            totalPages=len(pages),
            pages=pages,
            updatedAt=time.time(),
        )
        _write_manifest(self.workspace, self._manifest)
        for index, path in changed:
            ok, digest, value = await asyncio.to_thread(
                _render_page,
                self.workspace,
                self.entry,
                document,
                path,
                index,
            )
            item = next(
                (page for page in self._manifest['pages'] if page.get('path') == path),
                None,
            )
            if item is None:
                continue
            if ok:
                try:
                    self._hashes[path] = hashlib.sha256(
                        (self.entry.parent / path).read_bytes()
                    ).hexdigest()
                except OSError:
                    self._hashes.pop(path, None)
                item.update(
                    status='ready',
                    previewFile=value,
                    renderHash=digest,
                    sourceHash=self._hashes.get(path),
                    renderedAt=time.time(),
                    error=None,
                )
                await self._emit(item)
            else:
                item.update(status='failed', error=value)
            self._manifest.update(
                completedPages=sum(
                    page.get('status') == 'ready'
                    for page in self._manifest['pages']
                ),
                totalPages=len(self._manifest['pages']),
                updatedAt=time.time(),
            )
            _write_manifest(self.workspace, self._manifest)

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                await self.scan()

    async def start(self) -> None:
        await self.scan()
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None
        await self.scan()
