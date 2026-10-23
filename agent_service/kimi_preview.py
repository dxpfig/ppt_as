"""Incremental browser previews for pages written by the PPT expert."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import time
from typing import Any

import yaml
from pptd_engine.document import load_yaml

from .benchmark_timing import BenchmarkTrace
from .kimi_validation import _render_pages, final_layout_report
from .kimi_workspace import deck_status
from .job_execution import redact
from .pptd_validation import validate_document


MANIFEST_NAME = '.draft-preview.json'
PREVIEW_DIRECTORY = 'draft-previews'
HTML_TAG = re.compile(r'<[^>]+>')


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


def _plain_text(value: Any, result: list[str]) -> None:
    if isinstance(value, str):
        text = html.unescape(HTML_TAG.sub(' ', value))
        text = re.sub(r'\s+', ' ', text).strip()
        if text:
            result.append(text)
    elif isinstance(value, list):
        for item in value:
            _plain_text(item, result)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {'text', 'label', 'title'}:
                _plain_text(item, result)


def _page_text(page_data: dict) -> str:
    """Extract readable page copy without exposing layout/style internals."""
    values: list[str] = []
    elements = page_data.get('elements') if isinstance(page_data, dict) else None
    for element in elements if isinstance(elements, list) else []:
        if not isinstance(element, dict):
            continue
        element_type = element.get('elementType')
        if element_type == 'text':
            _plain_text((element.get('content') or {}).get('text'), values)
        elif element_type == 'table':
            _plain_text(element.get('rows'), values)
        elif element_type in {'chart', 'diagram'}:
            _plain_text(element.get('labels'), values)
    text = '\n'.join(dict.fromkeys(values))
    return text[:6000]


def _dedupe_issues(issues: list[dict]) -> list[dict]:
    unique: dict[tuple, dict] = {}
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        key = (
            issue.get('pagePath') or issue.get('page'),
            issue.get('code'),
            issue.get('elementId'),
            tuple(issue.get('elementIds') or []),
            issue.get('location'),
            issue.get('message'),
        )
        unique[key] = issue
    return list(unique.values())


def _safe_issues(issues: list[dict]) -> list[dict]:
    allowed = (
        'pagePath',
        'page',
        'pageIndex',
        'location',
        'elementId',
        'elementIds',
        'severity',
        'code',
        'message',
    )
    return [
        {
            key: value
            for key, value in issue.items()
            if key in allowed and value is not None
        }
        for issue in issues[:20]
        if isinstance(issue, dict)
    ]


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
    *,
    browser_check: bool = False,
) -> dict:
    """Render one page in an isolated one-page deck."""
    try:
        page_data = load_yaml(entry.parent / path)
    except (OSError, UnicodeError, ValueError):
        return {
            'ok': False,
            'digest': '',
            'error': '页面尚未形成完整 YAML',
        }
    digest = hashlib.sha256(
        json.dumps(page_data, ensure_ascii=False, sort_keys=True).encode('utf-8')
    ).hexdigest()[:16]
    elements = page_data.get('elements') if isinstance(page_data, dict) else None
    if not isinstance(elements, list) or not elements:
        return {
            'ok': False,
            'digest': digest,
            'error': '页面正文尚未生成，不发布空白预览',
        }
    temporary = workspace / '.draft-preview-render' / f'{index + 1}-{digest}'
    shutil.rmtree(temporary, ignore_errors=True)
    preview_document = deepcopy(document)
    preview_document['pages'] = [path]
    trace = BenchmarkTrace.for_workspace(workspace, workspace.name)
    try:
        with trace.span(
            'structureValidation',
            scope='page',
            pageIndex=index,
            pagePath=path,
        ):
            structural_issues = validate_document(
                {
                    'document': preview_document,
                    'pages': [{'path': path, 'data': page_data}],
                },
                resource_root=entry.parent,
            )
        mini_entry = _write_preview_deck(
            temporary,
            entry,
            preview_document,
            path,
            page_data,
        )
        with trace.span(
            'browserRender',
            scope='page',
            pageIndex=index,
            pagePath=path,
        ):
            rendered = _render_pages(mini_entry)
        image = rendered / 'pages' / 'page-1.png' if rendered else None
        if image is None or not image.is_file():
            return {
                'ok': False,
                'digest': digest,
                'error': '浏览器预览未能生成',
                'issues': structural_issues,
            }
        with trace.span(
            'browserLayout',
            scope='page',
            pageIndex=index,
            pagePath=path,
        ):
            layout_issues, layouts = (
                final_layout_report(
                    mini_entry,
                    preview_document,
                    [{'path': path, 'data': page_data}],
                    rendered,
                )
                if browser_check
                else ([], {})
            )
        preview_directory = workspace / PREVIEW_DIRECTORY
        preview_directory.mkdir(parents=True, exist_ok=True)
        final_name = f'page-{index + 1}.png'
        target = preview_directory / final_name
        temporary_image = target.with_suffix('.tmp')
        temporary_image.write_bytes(image.read_bytes())
        temporary_image.replace(target)
        return {
            'ok': True,
            'digest': digest,
            'previewFile': final_name,
            'image': target,
            'pageData': page_data,
            'document': preview_document,
            'issues': [*structural_issues, *layout_issues],
            'layouts': layouts,
            'text': _page_text(page_data),
        }
    except Exception:
        return {
            'ok': False,
            'digest': digest,
            'error': '浏览器预览生成失败',
        }
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


class DraftPreviewPublisher:
    """Poll page files while the agent works and publish actual browser renders."""

    def __init__(
        self,
        workspace: Path,
        entry: Path,
        job_id: str,
        on_event=None,
        inspect_page=None,
        concurrency: int = 2,
    ):
        self.workspace = workspace
        self.entry = entry
        self.job_id = job_id
        self.on_event = on_event
        self.inspect_page = inspect_page
        self.concurrency = max(1, int(concurrency))
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._manifest_lock = asyncio.Lock()
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
            'reviewedPages': int(value.get('reviewedPages') or 0),
            'issuePages': int(value.get('issuePages') or 0),
            'totalPages': int(value.get('totalPages') or 0),
            'pages': [item for item in pages if isinstance(item, dict)]
            if isinstance(pages, list)
            else [],
            'updatedAt': value.get('updatedAt') or 0,
        }

    async def _emit(self, page: dict) -> None:
        if not self.on_event:
            return
        review_status = page.get('reviewStatus')
        issue_count = int(page.get('issueCount') or 0)
        if review_status == 'passed':
            message = f"第 {page['index'] + 1} 页已生成，并通过单页检视。"
            status = 'completed'
        elif review_status == 'issues':
            message = (
                f"第 {page['index'] + 1} 页已生成，检视发现 "
                f"{issue_count} 项问题。"
            )
            status = 'waiting'
        elif review_status == 'unavailable':
            message = f"第 {page['index'] + 1} 页已生成，实际预览可查看；单页 AI 检视暂不可用。"
            status = 'completed'
        else:
            message = f"第 {page['index'] + 1} 页已生成，可查看实际预览。"
            status = 'completed'
        try:
            result = self.on_event(
                {
                    'phase': 'pages',
                    'status': status,
                    'message': message,
                    'pagePath': page['path'],
                    'pageIndex': page['index'],
                    'title': page['title'],
                    'issueCount': issue_count,
                }
            )
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            # A cancelled or terminal job may reject late progress events.
            return

    def issue_pages(self) -> list[dict]:
        return [
            item
            for item in self._manifest.get('pages', [])
            if isinstance(item, dict)
            and item.get('status') == 'ready'
            and item.get('reviewStatus') == 'issues'
        ]

    def _refresh_counts(self) -> None:
        pages = self._manifest.get('pages', [])
        self._manifest.update(
            completedPages=sum(
                item.get('status') == 'ready' for item in pages
            ),
            reviewedPages=sum(
                item.get('reviewStatus') == 'passed' for item in pages
            ),
            issuePages=sum(
                item.get('reviewStatus') == 'issues' for item in pages
            ),
            totalPages=len(pages),
            updatedAt=time.time(),
        )

    async def check_page(self, index: int, path: str) -> dict:
        """Render, browser-check and visually inspect one page synchronously.

        The strict page gate uses this method before the next page is generated.
        It intentionally keeps the rendered image and unfiltered issues in the
        return value so a follow-up repair can use the same evidence.
        """
        try:
            document, paths, existing, _, _ = deck_status(
                self.entry,
                require_content=True,
            )
        except (OSError, UnicodeError, ValueError):
            return {'ok': False, 'error': '页面清单或页面数据无法读取'}
        if path not in paths or path not in existing:
            return {'ok': False, 'error': f'{path} 尚未形成可检查页面'}
        rendered = await asyncio.to_thread(
            _render_page,
            self.workspace,
            self.entry,
            document,
            path,
            index,
            browser_check=True,
        )
        previous = next(
            (
                item
                for item in self._manifest.get('pages', [])
                if isinstance(item, dict) and item.get('path') == path
            ),
            {},
        )
        item = {
            **previous,
            'path': path,
            'index': index,
            'title': (
                _page_titles(self.workspace, paths).get(path)
                or previous.get('title')
                or f'第 {index + 1} 页'
            ),
        }
        if not rendered.get('ok'):
            item.update(
                status='failed',
                reviewStatus='pending',
                error=rendered.get('error') or '页面渲染失败',
                issues=_safe_issues(rendered.get('issues') or []),
                issueCount=len(rendered.get('issues') or []),
            )
            self._replace_manifest_page(item)
            return {
                **rendered,
                'item': dict(item),
                'issues': list(rendered.get('issues') or []),
            }

        raw_issues = list(rendered.get('issues') or [])
        item.update(
            status='ready',
            previewFile=rendered['previewFile'],
            renderHash=rendered['digest'],
            sourceHash=hashlib.sha256(
                (self.entry.parent / path).read_bytes()
            ).hexdigest(),
            renderedAt=time.time(),
            text=rendered.get('text') or '',
            reviewStatus='reviewing',
            issueCount=len(raw_issues),
            issues=_safe_issues(raw_issues),
            reviewError=None,
            error=None,
        )
        self._replace_manifest_page(item)
        trace = BenchmarkTrace.for_workspace(
            self.workspace,
            self.workspace.name,
        )

        if self.inspect_page is None:
            report = {'status': 'unreviewed', 'issues': []}
        else:
            try:
                with trace.span(
                    'visionReview',
                    scope='page',
                    pageIndex=index,
                    pagePath=path,
                ):
                    report = await self.inspect_page(
                        path,
                        rendered['image'],
                        {
                            'document': rendered['document'],
                            'pages': [{
                                'path': path,
                                'data': rendered['pageData'],
                            }],
                        },
                        list(rendered.get('issues') or []),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report = {
                    'status': 'unavailable',
                    'issues': [],
                'reviewError': redact(str(exc))[:1200],
            }

        raw_issues = _dedupe_issues([
            *raw_issues,
            *(report.get('issues') or []),
        ])
        trace.record_issues(
            'pageGate',
            raw_issues,
            pagePath=path,
            pageIndex=index,
            reviewStatus=report.get('status'),
        )
        review_status = (
            report.get('status')
            if report.get('status') in {'passed', 'issues', 'unavailable', 'unreviewed'}
            else ('issues' if raw_issues else 'passed')
        )
        if raw_issues and review_status == 'passed':
            review_status = 'issues'
        item.update(
            reviewStatus=review_status,
            issueCount=len(raw_issues),
            issues=_safe_issues(raw_issues),
            reviewModel=str(report.get('model') or '')[:160],
            reviewError=(
                str(report.get('reviewError') or '')[:1200] or None
            ),
            reviewedAt=time.time(),
        )
        self._replace_manifest_page(item)
        await self._emit(item)
        return {
            **rendered,
            'item': dict(item),
            'issues': raw_issues,
            'report': report,
        }

    def _replace_manifest_page(self, item: dict) -> None:
        pages = self._manifest.setdefault('pages', [])
        for index, current in enumerate(pages):
            if isinstance(current, dict) and current.get('path') == item.get('path'):
                pages[index] = item
                break
        else:
            pages.append(item)
        pages.sort(key=lambda page: page.get('index', 0))
        self._refresh_counts()
        _write_manifest(self.workspace, self._manifest)

    async def scan(self, *, render_changed: bool = True) -> None:
        try:
            document, paths, existing, _, _ = deck_status(
                self.entry,
                require_content=True,
            )
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
                item.update(
                    status='pending',
                    previewFile=None,
                    renderHash=None,
                    reviewStatus='pending',
                    issueCount=0,
                    issues=[],
                )
                self._hashes.pop(path, None)
            else:
                target = self.entry.parent / path
                try:
                    digest = hashlib.sha256(target.read_bytes()).hexdigest()
                except OSError:
                    digest = ''
                preview = self.workspace / PREVIEW_DIRECTORY / f'page-{index + 1}.png'
                review_status = item.get('reviewStatus')
                review_complete = review_status in {
                    'passed',
                    'issues',
                    'unavailable',
                } or (
                    review_status == 'unreviewed'
                    and self.inspect_page is None
                )
                if (
                    digest
                    and digest == self._hashes.get(path)
                    and preview.is_file()
                    and review_complete
                ):
                    item.update(
                        status='ready',
                        previewFile=preview.name,
                        renderHash=item.get('renderHash') or digest[:16],
                        reviewStatus=item.get('reviewStatus') or 'pending',
                    )
                else:
                    item.update(
                        status='rendering',
                        previewFile=None,
                        renderHash=None,
                        reviewStatus='pending',
                        issueCount=0,
                        issues=[],
                        error=None,
                    )
                    changed.append((index, path))
            pages.append(item)
        self._manifest.update(pages=pages)
        self._refresh_counts()
        _write_manifest(self.workspace, self._manifest)

        semaphore = asyncio.Semaphore(self.concurrency)

        async def process(index: int, path: str) -> None:
            async with semaphore:
                rendered = await asyncio.to_thread(
                    _render_page,
                    self.workspace,
                    self.entry,
                    document,
                    path,
                    index,
                )
                async with self._manifest_lock:
                    item = next(
                        (
                            page
                            for page in self._manifest['pages']
                            if page.get('path') == path
                        ),
                        None,
                    )
                    if item is None:
                        return
                    if not rendered.get('ok'):
                        item.update(
                            status='failed',
                            reviewStatus='pending',
                            error=(
                                rendered.get('error')
                                or '浏览器预览生成失败'
                            ),
                            issues=_safe_issues(
                                rendered.get('issues') or []
                            ),
                        )
                        self._refresh_counts()
                        _write_manifest(self.workspace, self._manifest)
                        return
                    try:
                        self._hashes[path] = hashlib.sha256(
                            (self.entry.parent / path).read_bytes()
                        ).hexdigest()
                    except OSError:
                        self._hashes.pop(path, None)
                    item.update(
                        status='ready',
                        previewFile=rendered['previewFile'],
                        renderHash=rendered['digest'],
                        sourceHash=self._hashes.get(path),
                        renderedAt=time.time(),
                        text=rendered.get('text') or '',
                        reviewStatus='reviewing',
                        issueCount=len(rendered.get('issues') or []),
                        issues=_safe_issues(
                            rendered.get('issues') or []
                        ),
                        error=None,
                    )
                    self._refresh_counts()
                    _write_manifest(self.workspace, self._manifest)

                if self.inspect_page is not None:
                    try:
                        report = await self.inspect_page(
                            path,
                            rendered['image'],
                            {
                                'document': rendered['document'],
                                'pages': [
                                    {
                                        'path': path,
                                        'data': rendered['pageData'],
                                    }
                                ],
                            },
                            list(rendered.get('issues') or []),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        report = {
                            'status': 'unavailable',
                            'issues': list(
                                rendered.get('issues') or []
                            ),
                        }
                    issues = _dedupe_issues(
                        [
                            *(rendered.get('issues') or []),
                            *(report.get('issues') or []),
                        ]
                    )
                    review_status = (
                        report.get('status')
                        if report.get('status')
                        in {'passed', 'issues', 'unavailable'}
                        else ('issues' if issues else 'passed')
                    )
                    review_values = {
                        'reviewStatus': review_status,
                        'issueCount': len(issues),
                        'issues': _safe_issues(issues),
                        'reviewModel': str(
                            report.get('model') or ''
                        )[:160],
                        'reviewedAt': time.time(),
                    }
                else:
                    review_values = {
                        'reviewStatus': 'unreviewed',
                        'reviewedAt': time.time(),
                    }
                async with self._manifest_lock:
                    item = next(
                        (
                            page
                            for page in self._manifest['pages']
                            if page.get('path') == path
                        ),
                        None,
                    )
                    if item is None:
                        return
                    item.update(review_values)
                    self._refresh_counts()
                    _write_manifest(self.workspace, self._manifest)
                await self._emit(item)

        if render_changed and changed:
            await asyncio.gather(
                *(
                    process(index, path)
                    for index, path in changed
                )
            )

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                await self.scan()

    async def start(self, *, poll: bool = True) -> None:
        await self.scan(render_changed=poll)
        if poll and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None
        await self.scan()
