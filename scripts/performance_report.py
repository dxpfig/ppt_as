#!/usr/bin/env python3
"""Summarize recorded PPT job durations without reading prompts or user data."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics


def percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1))
    return round(ordered[position], 3)


def summarize(values: list[float]) -> dict:
    return {
        'count': len(values),
        'min': round(min(values), 3) if values else None,
        'p50': round(statistics.median(values), 3) if values else None,
        'p95': percentile(values, .95),
        'max': round(max(values), 3) if values else None,
    }


def collect(data_dir: Path, limit: int) -> dict:
    jobs = []
    for path in sorted((data_dir / 'jobs').glob('*.json'), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]:
        try:
            job = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        created = job.get('createdAt')
        updated = job.get('updatedAt') or created
        if not isinstance(created, (int, float)) or not isinstance(updated, (int, float)):
            continue
        duration = max(0.0, float(updated) - float(created))
        nodes = {}
        for node in (job.get('execution') or {}).get('nodes') or []:
            started, ended = node.get('startedAt'), node.get('endedAt')
            if isinstance(started, (int, float)) and isinstance(ended, (int, float)):
                nodes[node.get('id')] = max(0.0, float(ended) - float(started))
        jobs.append({
            'id': job.get('id'),
            'kind': job.get('kind') or 'unknown',
            'status': job.get('status') or 'unknown',
            'durationSeconds': round(duration, 3),
            'nodes': {key: round(value, 3) for key, value in nodes.items()},
            'eventCount': len(job.get('events') or []),
        })
    by_kind = defaultdict(list)
    by_status = defaultdict(list)
    node_values = defaultdict(list)
    for job in jobs:
        by_kind[job['kind']].append(job['durationSeconds'])
        by_status[job['status']].append(job['durationSeconds'])
        for node, seconds in job['nodes'].items():
            node_values[node].append(seconds)
    return {
        'version': 1,
        'source': str((data_dir / 'jobs').resolve()),
        'jobsAnalyzed': len(jobs),
        'durationSeconds': summarize([job['durationSeconds'] for job in jobs]),
        'byKind': {kind: summarize(values) for kind, values in sorted(by_kind.items())},
        'byStatus': {status: summarize(values) for status, values in sorted(by_status.items())},
        'byNode': {node: summarize(values) for node, values in sorted(node_values.items())},
        'jobs': jobs,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=Path('runtime/studio'))
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--summary-only', action='store_true')
    args = parser.parse_args()
    if not (args.data_dir / 'jobs').is_dir():
        print(f'任务目录不存在：{args.data_dir / "jobs"}')
        return 2
    report = collect(args.data_dir, max(1, args.limit))
    if args.summary_only:
        report = {key: value for key, value in report.items() if key not in ('source', 'jobs')}
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
