"""Delivery export boundary with content-addressed artifact caching."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from . import pptd_exports
from .pptd_exports import ValidationError, layout_only_issues, renderer_available


_LOCKS: dict[tuple[int, str], tuple[asyncio.Lock, int]] = {}


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _cache_key(source, project_id: str, allow_vector: bool) -> str:
    state = source.read(project_id)
    media = []
    current = getattr(source, '_current', None)
    if callable(current):
        source_dir = current(project_id)
        media_dir = source_dir / 'media'
        if media_dir.is_dir():
            media = [
                {
                    'path': path.relative_to(source_dir).as_posix(),
                    'sha256': _hash_file(path),
                }
                for path in sorted(media_dir.rglob('*'))
                if path.is_file()
            ]
    payload = {
        'version': os.getenv('PPT_EXPORT_CACHE_VERSION', '1'),
        'document': state['document'],
        'pages': state['pages'],
        'media': media,
        'vector': allow_vector,
        'chromium': os.getenv('PPTD_CHROMIUM', ''),
        'renderer': os.getenv('PPT_RENDERER_IMAGE', ''),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()


@asynccontextmanager
async def _cache_guard(key: str):
    identity = (id(asyncio.get_running_loop()), key)
    lock, users = _LOCKS.get(identity, (asyncio.Lock(), 0))
    _LOCKS[identity] = (lock, users + 1)
    try:
        async with lock:
            yield
    finally:
        lock, users = _LOCKS[identity]
        if users == 1:
            _LOCKS.pop(identity, None)
        else:
            _LOCKS[identity] = (lock, users - 1)


def _cached(cache_dir: Path) -> tuple[Path, dict] | None:
    metadata_path = cache_dir / 'metadata.json'
    artifact = cache_dir / 'artifact'
    if not metadata_path.is_file() or not artifact.is_dir():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return (artifact, metadata) if isinstance(metadata, dict) else None


def _publish_cache(temporary: Path, cache_dir: Path) -> None:
    """Publish a complete cache entry with one atomic directory rename."""
    try:
        os.replace(temporary, cache_dir)
    except OSError as exc:
        # A different process may have won the same content-addressed publish.
        if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
            raise


def _copy_cached(artifact: Path, folder: Path) -> None:
    try:
        shutil.copytree(artifact, folder)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


async def export_document(store, project_id, folder, progress=None, *, allow_vector=True):
    folder = Path(folder).resolve()
    if folder.exists():
        raise ValueError('Artifact output must be a new directory')
    cache_root = getattr(store, 'root', None)
    if cache_root is None or os.getenv('PPT_EXPORT_CACHE', '1').strip().lower() in ('0', 'false', 'no'):
        return await pptd_exports.export_document(
            store, project_id, folder, progress, allow_vector=allow_vector
        )
    key = _cache_key(store, project_id, allow_vector)
    cache_dir = Path(cache_root) / 'export-cache' / key
    async with _cache_guard(key):
        cached = _cached(cache_dir)
        if cached is not None:
            if progress:
                result = progress(92, '未变化文档复用已有交付缓存')
                if hasattr(result, '__await__'):
                    await result
            _copy_cached(cached[0], folder)
            return cached[1]

        # An incomplete entry can only come from an older/corrupt cache. It is
        # rebuildable and must never be treated as a successful export.
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_dir.with_name(f'.{key}.{uuid4().hex}.tmp')
        temporary.mkdir()
        try:
            metadata = await pptd_exports.export_document(
                store,
                project_id,
                temporary / 'artifact',
                progress,
                allow_vector=allow_vector,
            )
            metadata_path = temporary / 'metadata.json'
            with metadata_path.open('w', encoding='utf-8') as handle:
                handle.write(json.dumps(metadata, ensure_ascii=False, sort_keys=True) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            _publish_cache(temporary, cache_dir)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

        cached = _cached(cache_dir)
        if cached is None:
            raise ValueError('交付缓存发布不完整，请重试导出。')
        _copy_cached(cached[0], folder)
        return cached[1]


__all__ = [
    'ValidationError',
    'export_document',
    'layout_only_issues',
    'renderer_available',
]
