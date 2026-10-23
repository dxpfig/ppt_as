"""Visual review boundary shared by quick, expert and page actions."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from . import visual_review


def _cache_disabled() -> bool:
    return os.getenv('PPT_REVIEW_CACHE', '1').strip().lower() in {
        '0',
        'false',
        'no',
        'off',
    }


def _sha(data) -> str:
    if not isinstance(data, (bytes, str)):
        data = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def _key(state, images: list[Path], scope: str, scenario: str, usage: str, extra=None) -> str:
    image_hashes = [
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in images
        if path.is_file()
    ]
    config = visual_review.vision_configuration()
    payload = {
        'version': os.getenv('PPT_REVIEW_CACHE_VERSION', '1'),
        'scope': scope,
        'state': state.get('document'),
        'pages': state.get('pages'),
        'images': image_hashes,
        'provider': config.provider,
        'model': config.model,
        'baseUrl': config.base_url or '',
        'scenario': scenario,
        'usage': usage,
        'extra': extra,
    }
    return _sha(payload)


def _read(cache_dir: Path | None, key: str) -> dict | None:
    if cache_dir is None or _cache_disabled():
        return None
    path = Path(cache_dir) / f'{key}.json'
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write(cache_dir: Path | None, key: str, report: dict) -> None:
    if (
        cache_dir is None
        or _cache_disabled()
        or report.get('status') not in ('passed', 'issues')
    ):
        return
    folder = Path(cache_dir)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f'{key}.json'
    temporary = folder / f'.{key}.{uuid4().hex}.tmp'
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    temporary.replace(target)


async def review_deck(
    state,
    artifact_dir,
    *,
    scenario='auto',
    usage='reading',
    on_event=None,
    previous_review=None,
    cache_dir=None,
    concurrency=2,
    layout_measurements=None,
    only_pages=None,
    design_contract=None,
    max_tokens=None,
):
    artifact_dir = Path(artifact_dir)
    images = [artifact_dir / f'slide-{index + 1}.png' for index in range(len(state.get('pages', [])))]
    if only_pages is not None and not isinstance(previous_review, dict):
        only_pages = None
    changed_pages = (
        [
            path for path in state.get('document', {}).get('pages', [])
            if path in set(only_pages)
        ]
        if only_pages is not None
        else None
    )
    key = _key(
        state,
        images,
        'incremental' if changed_pages is not None else 'deck',
        scenario,
        usage,
        {
            'changedPages': changed_pages,
            'previous': previous_review,
            'designContract': design_contract,
        },
    )
    cached = _read(cache_dir, key)
    if cached is not None:
        if on_event:
            result = on_event({
                'phase': 'visual',
                'status': 'completed',
                'scope': 'deck',
                'message': '未变化页面复用视觉审稿缓存',
                'covered': len(state.get('pages', [])),
                'total': len(state.get('pages', [])),
                'issueCount': len(cached.get('issues', [])),
            })
            if hasattr(result, '__await__'):
                await result
        return {**cached, 'cached': True}
    if changed_pages is not None:
        report = await _review_changed_pages(
            state,
            artifact_dir,
            changed_pages,
            scenario=scenario,
            usage=usage,
            on_event=on_event,
            previous_review=previous_review,
            concurrency=concurrency,
            layout_measurements=layout_measurements,
            design_contract=design_contract,
            max_tokens=max_tokens,
        )
    else:
        report = await visual_review.review_deck(
            state,
            artifact_dir,
            scenario=scenario,
            usage=usage,
            on_event=on_event,
            previous_review=previous_review,
            concurrency=concurrency,
            layout_measurements=layout_measurements,
            design_contract=design_contract,
            max_tokens=max_tokens,
        )
    _write(cache_dir, key, report)
    return report


async def _review_changed_pages(
    state,
    artifact_dir,
    changed_pages,
    *,
    scenario,
    usage,
    on_event,
    previous_review,
    concurrency,
    layout_measurements,
    design_contract=None,
    max_tokens=None,
):
    """Reuse prior verdicts for unchanged pages and inspect only changed ones."""
    paths = list(state.get('document', {}).get('pages', []))
    changed = set(changed_pages)
    previous_pages = {}
    if isinstance(previous_review, dict):
        for report in previous_review.get('pageReviews', []):
            for path in report.get('coveredPages', []):
                previous_pages[path] = report

    semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    async def review_one(path):
        if path not in changed and path in previous_pages:
            return path, previous_pages[path]
        index = paths.index(path)
        image_name = f'slide-{index + 1}.png'
        async with semaphore:
            last_error = None
            for attempt in range(2):
                try:
                    report = await review_page(
                        state,
                        artifact_dir,
                        path,
                        scenario=scenario,
                        usage=usage,
                        on_event=on_event,
                        image_name=image_name,
                        layout_measurements=layout_measurements,
                        design_contract=design_contract,
                        max_tokens=max_tokens,
                    )
                    return path, report
                except visual_review.VisualReviewError as error:
                    last_error = error
                    if error.code != 'visual_review_invalid' or attempt:
                        raise
            raise last_error

    results = await asyncio.gather(
        *(review_one(path) for path in paths),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result

    page_reviews = [report for _, report in results]
    issues = []
    seen = set()
    for report in page_reviews:
        for issue in report.get('issues', []):
            key = (
                issue.get('pagePath'),
                tuple(sorted(issue.get('elementIds') or [])),
                issue.get('code'),
            )
            if key not in seen:
                seen.add(key)
                issues.append(issue)
    observations = [
        observation
        for report in page_reviews
        for observation in report.get('observations', [])
    ]
    model = next(
        (
            report.get('model')
            for report in page_reviews
            if report.get('model')
        ),
        '',
    )
    return {
        'version': 1,
        'status': 'issues' if issues else 'passed',
        'coveredPages': paths,
        'observations': observations,
        'issues': issues,
        'pageReviews': page_reviews,
        'incrementalPages': list(changed_pages),
        'model': model,
    }


async def review_page(
    state,
    artifact_dir,
    page_path,
    *,
    scenario='auto',
    usage='reading',
    on_event=None,
    cache_dir=None,
    image_name='slide-1.png',
    layout_measurements=None,
    design_contract=None,
    max_tokens=None,
):
    artifact_dir = Path(artifact_dir)
    selected = next(
        page for page in state.get('pages', [])
        if page.get('path') == page_path
    )
    page_state = {
        'document': {
            **state.get('document', {}),
            'pages': [page_path],
        },
        'pages': [selected],
    }
    key = _key(
        page_state,
        [artifact_dir / image_name],
        'page',
        scenario,
        usage,
        {'pagePath': page_path, 'designContract': design_contract},
    )
    cached = _read(cache_dir, key)
    if cached is not None:
        return {**cached, 'cached': True}
    report = await visual_review.review_page(
        state,
        artifact_dir,
        page_path,
        scenario=scenario,
        usage=usage,
        on_event=on_event,
        image_name=image_name,
        layout_measurements=layout_measurements,
        design_contract=design_contract,
        max_tokens=max_tokens,
    )
    _write(cache_dir, key, report)
    return report
