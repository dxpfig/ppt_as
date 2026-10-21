#!/usr/bin/env python3
"""Dry-run-first cleanup for rebuildable runtime caches."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil


SAFE_CACHE_DIRS = (
    'studio/render-cache',
    'studio/review-cache',
    'studio/export-cache',
)
IGNORED_TMP_SUFFIXES = ('.tmp',)


def _within(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _old_enough(path: Path, cutoff: datetime) -> bool:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return False
    return modified < cutoff


def collect_targets(
    root: Path,
    *,
    days: int,
    include_candidates: bool = False,
) -> list[Path]:
    root = Path(root).resolve()
    if root == Path('/') or not _within(root.parent, root):
        raise ValueError('Refusing to clean the filesystem root')
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, days))
    directories = [root / relative for relative in SAFE_CACHE_DIRS]
    if include_candidates:
        directories.append(root / 'studio' / 'visual_candidates')
    targets = []
    for directory in directories:
        if not _within(root, directory) or not directory.is_dir():
            continue
        for child in directory.iterdir():
            if _old_enough(child, cutoff):
                targets.append(child)
    for path in root.rglob('*'):
        if (
            _within(root, path)
            and path.is_file()
            and path.name.endswith(IGNORED_TMP_SUFFIXES)
            and _old_enough(path, cutoff)
        ):
            targets.append(path)
    return sorted(set(targets), key=lambda item: str(item))


def clean(root: Path, targets: list[Path]) -> None:
    root = Path(root).resolve()
    for target in targets:
        if not _within(root, target):
            raise ValueError(f'Cleanup target escaped runtime root: {target}')
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, default=Path('runtime'))
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--include-candidates', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.days < 0:
        parser.error('--days must be non-negative')
    targets = collect_targets(
        args.runtime,
        days=args.days,
        include_candidates=args.include_candidates,
    )
    if args.apply:
        clean(args.runtime, targets)
    action = 'removed' if args.apply else 'would remove'
    for target in targets:
        print(f'{action}: {target}')
    print(f'{action} total: {len(targets)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
