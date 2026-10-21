"""Visual review boundary shared by quick, expert and page actions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from . import visual_review


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
    if cache_dir is None:
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
    if cache_dir is None or report.get('status') not in ('passed', 'issues'):
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
):
    artifact_dir = Path(artifact_dir)
    images = [artifact_dir / f'slide-{index + 1}.png' for index in range(len(state.get('pages', [])))]
    key = _key(state, images, 'deck', scenario, usage, previous_review)
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
    report = await visual_review.review_deck(
        state,
        artifact_dir,
        scenario=scenario,
        usage=usage,
        on_event=on_event,
        previous_review=previous_review,
        concurrency=concurrency,
    )
    _write(cache_dir, key, report)
    return report


async def review_page(
    state,
    artifact_dir,
    page_path,
    *,
    scenario='auto',
    usage='reading',
    on_event=None,
    cache_dir=None,
):
    artifact_dir = Path(artifact_dir)
    key = _key(state, [artifact_dir / 'slide-1.png'], 'page', scenario, usage, page_path)
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
    )
    _write(cache_dir, key, report)
    return report
