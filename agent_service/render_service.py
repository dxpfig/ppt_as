"""Shared page rendering and located browser evidence."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pymupdf

from . import pptd_exports


@dataclass
class RenderedPage:
    state: dict
    source_root: Path
    image: bytes
    issues: list[dict]
    layout: dict


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def render_cache_key(state: dict, source_dir: Path, page_path: str) -> str:
    page = next((item for item in state.get('pages', []) if item.get('path') == page_path), None)
    media = []
    media_dir = source_dir / 'media'
    if media_dir.is_dir():
        media = [
            {
                'path': path.relative_to(source_dir).as_posix(),
                'sha256': _file_hash(path),
            }
            for path in sorted(media_dir.rglob('*'))
            if path.is_file()
        ]
    payload = {
        'version': os.getenv('PPT_RENDER_CACHE_VERSION', '1'),
        'pagePath': page_path,
        'page': page,
        'document': {
            'version': state.get('document', {}).get('version'),
            'size': state.get('document', {}).get('size'),
            'theme': state.get('document', {}).get('theme'),
        },
        'media': media,
        'chromium': os.getenv('PPTD_CHROMIUM', ''),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()


def _cache_root(source) -> Path | None:
    if os.getenv('PPT_RENDER_CACHE', '1').strip().lower() in ('0', 'false', 'no'):
        return None
    root = getattr(source, 'root', None)
    return Path(root) / 'render-cache' if root is not None else None


def issue_page(issue: dict) -> str:
    path = issue.get("pagePath") or issue.get("page")
    if isinstance(path, str) and path:
        return path
    location = issue.get("location")
    return location.split("#", 1)[0] if isinstance(location, str) else ""


def page_issues(issues: list[dict], page_path: str) -> list[dict]:
    selected = []
    for issue in issues:
        if issue_page(issue) != page_path:
            continue
        identifiers = issue.get("elementIds")
        if not isinstance(identifiers, list):
            identifiers = [issue["elementId"]] if issue.get("elementId") else []
        selected.append({
            **issue,
            "pagePath": page_path,
            "elementIds": identifiers,
        })
    return selected


async def render_page(source, project_id: str, folder: Path, page_path: str) -> RenderedPage:
    """Render one page with the same browser pipeline used by final export."""
    folder = Path(folder).resolve()
    folder.mkdir(parents=True)
    source_dir = folder / "source"
    browser_dir = folder / "browser"
    browser_dir.mkdir()
    state = source.snapshot(project_id, source_dir)
    paths = state["document"].get("pages", [])
    if page_path not in paths:
        raise ValueError("当前页面不属于这个演示。")
    page_index = paths.index(page_path)
    cache_root = _cache_root(source)
    cache_dir = cache_root / render_cache_key(state, source_dir, page_path) if cache_root else None
    if cache_dir is not None:
        metadata_path = cache_dir / 'metadata.json'
        image_path = cache_dir / 'page.png'
        if metadata_path.is_file() and image_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
            image = image_path.read_bytes()
            (folder / 'slide-1.png').write_bytes(image)
            return RenderedPage(
                state=state,
                source_root=source_dir,
                image=image,
                issues=list(metadata.get('issues') or []),
                layout=dict(metadata.get('layout') or {}),
            )
    env = os.environ.copy()
    browser = pptd_exports.chromium_path()
    if browser:
        env["PPTD_CHROMIUM"] = browser
    issues: list[dict] = []
    previews: dict[str, bytes] = {}
    layouts: dict[str, dict] = {}
    entry = source_dir / "document.pptd"
    try:
        async with pptd_exports._snapshot_renderer(entry, state["revision"]) as url:
            await pptd_exports._validated_process(
                browser_dir,
                "node",
                pptd_exports.WEB_DIR / "export_pdf.cjs",
                url,
                browser_dir,
                env=env,
                timeout=360,
            )
    except pptd_exports.ValidationError as error:
        issues = list(error.issues)
        previews = dict(error.previews or {})
        layouts = dict(error.layouts or {})

    image = previews.get(page_path)
    if image is None:
        report_path = browser_dir / "pdf-pages.json"
        if not report_path.is_file():
            raise ValueError("页面渲染没有返回可审阅的结果。")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        item = next((page for page in report.get("pages", []) if page.get("index") == page_index), None)
        if not item:
            raise ValueError("页面渲染结果缺少当前页。")
        with pymupdf.open(browser_dir / item["file"]) as pdf:
            image = pdf[0].get_pixmap(matrix=pymupdf.Matrix(1.2, 1.2), alpha=False).tobytes("png")
        layouts[page_path] = {
            "canvasSize": state["document"]["size"],
            "elements": item.get("textLayout", []),
        }

    (folder / "slide-1.png").write_bytes(image)
    result = RenderedPage(
        state=state,
        source_root=source_dir,
        image=image,
        issues=page_issues(issues, page_path),
        layout=layouts.get(page_path, {}),
    )
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        token = uuid4().hex
        metadata_temporary = cache_dir / f'.metadata.{token}.tmp'
        image_temporary = cache_dir / f'.page-{token}.png.tmp'
        metadata_temporary.write_text(
            json.dumps(
                {'issues': result.issues, 'layout': result.layout},
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding='utf-8',
        )
        image_temporary.write_bytes(image)
        image_temporary.replace(cache_dir / 'page.png')
        metadata_temporary.replace(cache_dir / 'metadata.json')
    return result
