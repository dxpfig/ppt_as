"""Side-channel timing for repeatable single-page benchmark runs.

This recorder never participates in generation decisions. It only persists
measured spans so the benchmark runner can distinguish template loading,
planning, page generation, browser checks, visual review and repairs.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time
from typing import Iterator
from uuid import uuid4


_FILE_LOCK = threading.Lock()


def _enabled() -> bool:
    return os.getenv('PPT_BENCHMARK_TRACE', '1').strip().lower() in {
        '1',
        'true',
        'yes',
        'on',
    }


class BenchmarkTrace:
    """Append-only measurements for one benchmark job."""

    def __init__(self, data_root: Path, job_id: str):
        self.enabled = _enabled()
        self.data_root = Path(data_root).resolve()
        self.job_id = str(job_id)
        self.path = (
            self.data_root / 'benchmark-traces' / f'{self.job_id}.json'
            if self.enabled
            else None
        )
        self._lock = threading.Lock()
        self._state = {
            'version': 1,
            'jobId': self.job_id,
            'startedAt': time.time(),
            'finishedAt': None,
            'status': 'running',
            'spans': [],
            'issueSets': [],
        }
        self._write()

    @classmethod
    def for_workspace(
        cls,
        workspace: Path,
        job_id: str,
    ) -> 'BenchmarkTrace':
        workspace = Path(workspace).resolve()
        data_root = (
            workspace.parents[2]
            if len(workspace.parents) >= 3
            else workspace
        )
        return cls(data_root, job_id)

    def _write(self) -> None:
        if self.path is None:
            return
        with _FILE_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_file():
                try:
                    existing = json.loads(
                        self.path.read_text(encoding='utf-8')
                    )
                except (OSError, ValueError):
                    existing = {}
                if isinstance(existing, dict):
                    known_spans = {
                        item.get('spanId')
                        for item in existing.get('spans') or []
                        if isinstance(item, dict)
                    }
                    known_issues = {
                        item.get('issueSetId')
                        for item in existing.get('issueSets') or []
                        if isinstance(item, dict)
                    }
                    existing['spans'] = [
                        *(existing.get('spans') or []),
                        *[
                            item
                            for item in self._state.get('spans') or []
                            if item.get('spanId') not in known_spans
                        ],
                    ]
                    existing['issueSets'] = [
                        *(existing.get('issueSets') or []),
                        *[
                            item
                            for item in self._state.get('issueSets') or []
                            if item.get('issueSetId') not in known_issues
                        ],
                    ]
                    if self._state.get('finishedAt') is not None:
                        existing['finishedAt'] = self._state['finishedAt']
                        existing['status'] = self._state['status']
                    self._state = existing
            temporary = self.path.with_name(
                f'.{self.path.name}.{uuid4().hex}.tmp'
            )
            temporary.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8',
            )
            temporary.replace(self.path)

    @contextmanager
    def span(self, stage: str, **details) -> Iterator[None]:
        started = time.time()
        status = 'completed'
        try:
            yield
        except BaseException:
            status = 'failed'
            raise
        finally:
            if self.enabled:
                ended = time.time()
                with self._lock:
                    self._state['spans'].append(
                        {
                            'spanId': uuid4().hex,
                            'stage': stage,
                            'status': status,
                            'startedAt': started,
                            'endedAt': ended,
                            'durationMs': round(max(0.0, ended - started) * 1000, 3),
                            **{
                                key: value
                                for key, value in details.items()
                                if value is not None
                            },
                        }
                    )
                    self._write()

    def record_ms(self, stage: str, duration_ms: float, **details) -> None:
        if not self.enabled:
            return
        ended = time.time()
        with self._lock:
            self._state['spans'].append(
                {
                    'spanId': uuid4().hex,
                    'stage': stage,
                    'status': 'completed',
                    'startedAt': ended - max(0.0, duration_ms) / 1000,
                    'endedAt': ended,
                    'durationMs': round(max(0.0, duration_ms), 3),
                    **{
                        key: value
                        for key, value in details.items()
                        if value is not None
                    },
                }
            )
            self._write()

    def record_issues(
        self,
        stage: str,
        issues: list[dict],
        **details,
    ) -> None:
        if not self.enabled:
            return
        public = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            public.append(
                {
                    key: issue.get(key)
                    for key in (
                        'code',
                        'page',
                        'pagePath',
                        'elementId',
                        'location',
                        'severity',
                        'message',
                    )
                    if issue.get(key) is not None
                }
            )
        with self._lock:
            self._state['issueSets'].append(
                {
                    'issueSetId': uuid4().hex,
                    'stage': stage,
                    'recordedAt': time.time(),
                    'issueCount': len(public),
                    'issues': public,
                    **{
                        key: value
                        for key, value in details.items()
                        if value is not None
                    },
                }
            )
            self._write()

    def finish(self, status: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._state['status'] = status
            self._state['finishedAt'] = time.time()
            self._write()
