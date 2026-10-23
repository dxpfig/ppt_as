"""Atomic persistence and legal transitions for durable generation jobs."""
from __future__ import annotations

import json
from pathlib import Path
import re
import time
from urllib.parse import quote
from uuid import uuid4

from .contracts import JobRecord
from .job_execution import checkpoint_execution, record_event, stage_metrics
from .kimi_workspace import workspace as kimi_workspace_path
from .orchestrator import transition_allowed


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f'.{uuid4().hex}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


class JobStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.directory = self.root / 'jobs'
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _identifier(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', value):
            raise ValueError('Invalid job id')
        return value

    def path(self, identifier: str) -> Path:
        return self.directory / f'{self._identifier(identifier)}.json'

    def read(self, identifier: str) -> dict:
        path = self.path(identifier)
        if not path.is_file():
            raise FileNotFoundError(path)
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('Job state must be a JSON object')
        value['stageMetrics'] = stage_metrics(value.get('execution'))
        plan = self._plan_preview(identifier, value)
        if plan:
            value['planPreview'] = plan
        else:
            value.pop('planPreview', None)
        draft = self._draft_preview(identifier, value)
        if draft:
            value['draftPreview'] = draft
        else:
            value.pop('draftPreview', None)
        return value

    def _plan_preview(self, identifier: str, job: dict) -> dict | None:
        """Expose the accepted plan while the editable deck is still building."""
        if (
            job.get('kind') != 'kimi-slides'
            or job.get('status') not in ('queued', 'running', 'interrupted')
        ):
            return None
        project_id = job.get('projectId')
        if (
            not isinstance(project_id, str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', project_id)
        ):
            return None
        from .kimi_workspace import workspace as kimi_workspace_path

        path = (
            kimi_workspace_path(
                self.root,
                project_id,
                self._identifier(identifier),
            )
            / 'deck'
            / 'plan.json'
        )
        try:
            plan = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        items = plan.get('pages') if isinstance(plan, dict) else None
        if not isinstance(items, list) or not items:
            return None
        pages = []
        for index, item in enumerate(items[:80]):
            if not isinstance(item, dict):
                item = {}
            source_sections = item.get('sourceSections')
            source_subsections = item.get('sourceSubsections')
            pages.append(
                {
                    'path': str(item.get('path') or f'pages/{index + 1}.page')[:240],
                    'index': index,
                    'title': str(item.get('title') or f'第 {index + 1} 页')[:120],
                    'readerTask': str(item.get('readerTask') or '')[:180],
                    'content': str(item.get('content') or '')[:420],
                    'visualPlan': str(item.get('visualPlan') or '')[:240],
                    'sourceSections': [
                        str(value)[:160]
                        for value in (
                            source_sections
                            if isinstance(source_sections, list)
                            else []
                        )
                        if isinstance(value, str)
                    ][:12],
                    'sourceSubsections': [
                        str(value)[:160]
                        for value in (
                            source_subsections
                            if isinstance(source_subsections, list)
                            else []
                        )
                        if isinstance(value, str)
                    ][:20],
                }
            )
        return {
            'status': 'ready',
            'title': str(
                plan.get('deckTitle')
                or plan.get('title')
                or '内容策划'
            )[:160],
            'subtitle': str(plan.get('subtitle') or '')[:240],
            'totalPages': len(pages),
            'updatedAt': path.stat().st_mtime,
            'pages': pages,
        }

    def _draft_preview(self, identifier: str, job: dict) -> dict | None:
        if job.get('status') not in ('queued', 'running', 'interrupted'):
            return None
        manifest_path = (
            kimi_workspace_path(self.root, job.get('projectId', ''), identifier)
            / '.draft-preview.json'
        )
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                manifest = None
            if isinstance(manifest, dict):
                pages = []
                for item in manifest.get('pages') or []:
                    if not isinstance(item, dict):
                        continue
                    preview_file = item.get('previewFile')
                    if (
                        preview_file is not None
                        and not re.fullmatch(r'page-[1-9][0-9]*\.png', str(preview_file))
                    ):
                        preview_file = None
                    try:
                        page_index = max(0, int(item.get('index') or 0))
                    except (TypeError, ValueError):
                        page_index = 0
                    review_status = item.get('reviewStatus')
                    if review_status not in (
                        'pending',
                        'reviewing',
                        'passed',
                        'issues',
                        'unavailable',
                        'unreviewed',
                    ):
                        review_status = 'pending'
                    issues = []
                    for issue in item.get('issues') or []:
                        if not isinstance(issue, dict):
                            continue
                        public = {
                            key: issue[key]
                            for key in (
                                'location',
                                'elementId',
                                'severity',
                                'code',
                                'message',
                            )
                            if issue.get(key) is not None
                        }
                        for key in ('location', 'elementId', 'severity', 'code', 'message'):
                            if key in public:
                                public[key] = str(public[key])[:600]
                        if public:
                            issues.append(public)
                        if len(issues) >= 8:
                            break
                    try:
                        issue_count = max(0, int(item.get('issueCount') or 0))
                    except (TypeError, ValueError):
                        issue_count = len(issues)
                    pages.append(
                        {
                            'path': str(item.get('path') or ''),
                            'index': page_index,
                            'title': str(item.get('title') or '未命名页面')[:120],
                            'status': item.get('status')
                            if item.get('status') in (
                                'pending', 'rendering', 'ready', 'failed'
                            )
                            else 'pending',
                            'previewUrl': (
                                f'/api/studio/jobs/{quote(identifier)}/draft-previews/'
                                f'{quote(str(preview_file))}?v='
                                f'{quote(str(item.get("renderHash") or "0"))}'
                                if preview_file
                                else None
                            ),
                            'text': str(item.get('text') or '')[:6000],
                            'reviewStatus': review_status,
                            'issueCount': issue_count,
                            'issues': issues,
                            'reviewModel': str(item.get('reviewModel') or '')[:160],
                            'error': str(item.get('error'))[:300]
                            if item.get('error')
                            else None,
                        }
                    )
                if pages:
                    return {
                        'status': 'draft',
                        'completedPages': sum(
                            page['status'] == 'ready' for page in pages
                        ),
                        'reviewedPages': sum(
                            page['reviewStatus'] == 'passed' for page in pages
                        ),
                        'issuePages': sum(
                            page['reviewStatus'] == 'issues' for page in pages
                        ),
                        'totalPages': len(pages),
                        'latestIndex': max(
                            (
                                page['index']
                                for page in pages
                                if page['status'] == 'ready'
                            ),
                            default=-1,
                        ),
                        'pages': pages,
                    }
        checkpoint_path = self.root / 'checkpoints' / f'{self._identifier(identifier)}.json'
        if not checkpoint_path.is_file():
            return None
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        plan = checkpoint.get('plan') or {}
        planned = plan.get('pages') if isinstance(plan, dict) else None
        if not isinstance(planned, list) or not planned:
            return None
        completed = {
            item.get('path')
            for item in checkpoint.get('pages') or []
            if isinstance(item, dict) and isinstance(item.get('data'), dict)
        }
        paths = (plan.get('document') or {}).get('pages') or []
        pages = []
        for index, item in enumerate(planned[:60]):
            if not isinstance(item, dict):
                item = {}
            path = item.get('path') or (
                paths[index] if index < len(paths) and isinstance(paths[index], str) else ''
            )
            pages.append(
                {
                    'path': path,
                    'index': index,
                    'title': str(item.get('title') or f'第 {index + 1} 页')[:120],
                    'status': 'ready' if path in completed else 'pending',
                }
            )
        return {
            'status': 'draft',
            'completedPages': sum(page['status'] == 'ready' for page in pages),
            'totalPages': len(pages),
            'pages': pages,
        }

    def save(self, identifier: str, job: dict) -> dict:
        if not isinstance(job, dict):
            raise ValueError('Job state must be a JSON object')
        job = JobRecord.model_validate(job).model_dump()
        path = self.path(identifier)
        _write_json(path, job)
        return job

    def update(self, identifier: str, **changes) -> dict:
        job = self.read(identifier)
        if job.get('status') == 'cancelled':
            return job
        next_status = changes.get('status')
        if next_status is not None and next_status != job.get('status'):
            if not transition_allowed(job['status'], next_status):
                raise ValueError(
                    f"非法任务状态迁移：{job['status']} → {next_status}"
                )
        now = time.time()
        job.update(changes, updatedAt=now)
        if {'message', 'stage', 'status'}.intersection(changes):
            job['events'] = [
                *job.get('events', []),
                {
                    'timestamp': now,
                    'stage': job.get('stage', 0),
                    'message': job.get('message', ''),
                    'status': job.get('status'),
                },
            ][-200:]
        return self.save(identifier, job)


class JobRuntime:
    """Shared progress, event and checkpoint contract for every job route."""

    def __init__(
        self,
        store: JobStore,
        identifier: str,
        project_id: str,
        assert_current,
        checkpoint_path: Path,
    ):
        self.store = store
        self.identifier = identifier
        self.project_id = project_id
        self.assert_current = assert_current
        self.checkpoint_path = Path(checkpoint_path)

    def progress(self, stage: int, message: str) -> dict:
        self.assert_current()
        return self.store.update(
            self.identifier,
            status='running',
            stage=stage,
            message=message,
        )

    def event(self, event: dict) -> dict:
        self.assert_current()
        current = self.store.read(self.identifier)
        execution = record_event(current.get('execution'), event)
        stage = current.get('stage', 0)
        if event.get('phase') == 'visual':
            covered = event.get('covered', 0)
            total = event.get('total', 1)
            if isinstance(covered, int) and isinstance(total, int) and total > 0:
                stage = max(stage, 85 + min(12, int(12 * covered / total)))
        changes = {
            'stage': stage,
            'execution': execution,
            'message': (
                execution['events'][-1]['message']
                if execution.get('events')
                else current.get('message', '')
            ),
        }
        covered = event.get('covered')
        total = event.get('total')
        if (
            event.get('phase') == 'pages'
            and isinstance(covered, int)
            and isinstance(total, int)
            and total > 0
        ):
            changes['checkpoint'] = {
                'stage': 'pages',
                'completedPages': covered,
                'totalPages': total,
                'summary': f'已保留 {covered}/{total} 页',
            }
        return self.store.update(self.identifier, **changes)

    def save_checkpoint(self, value: dict) -> dict:
        self.assert_current()
        previous = {}
        if self.checkpoint_path.is_file():
            try:
                loaded = json.loads(self.checkpoint_path.read_text(encoding='utf-8'))
                previous = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                previous = {}
        value = {
            **{key: item for key, item in previous.items() if key.startswith('_')},
            **value,
        }
        _write_json(self.checkpoint_path, value)
        completed = len(value.get('pages', []))
        total = len((value.get('plan') or {}).get('pages', []))
        summary = f'已保存 {completed}/{total} 页' if total else '已保存生成检查点'
        stage = value.get('stage')
        position = {
            'context': 5,
            'plan': 10,
            'assets': 15,
            'edit': 35,
            'validated': 60,
            'export': 65,
        }.get(stage, 20 + int(40 * completed / max(1, total)))
        current = self.store.read(self.identifier)
        self.store.update(
            self.identifier,
            stage=max(current.get('stage', 0), position),
            checkpoint={
                'stage': stage,
                'completedPages': completed,
                'totalPages': total,
                'summary': summary,
            },
            recoverable=True,
            execution=checkpoint_execution(current.get('execution'), value),
        )
        return value
