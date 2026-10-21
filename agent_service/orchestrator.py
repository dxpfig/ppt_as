"""Single routing point for quick, expert, page, and legacy jobs."""
from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    INTERRUPTED = 'interrupted'


STAGES = (
    ('context', '读取材料'),
    ('plan', '内容策划'),
    ('assets', '准备素材'),
    ('pages', '逐页创作'),
    ('validate', '检查修复'),
    ('visual', 'AI 视觉审稿'),
    ('export', '渲染导出'),
    ('done', '完成交付'),
)

_TERMINAL = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}
_ALLOWED = {
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED},
    JobStatus.RUNNING: {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED},
    JobStatus.INTERRUPTED: {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED},
    JobStatus.FAILED: {JobStatus.RUNNING},
    JobStatus.CANCELLED: set(),
    JobStatus.COMPLETED: set(),
}


def transition_allowed(source: JobStatus | str, target: JobStatus | str) -> bool:
    source = JobStatus(source)
    target = JobStatus(target)
    return source == target or target in _ALLOWED[source]


class ExecutionRoute(StrEnum):
    PAGE_ACTION = 'page-action'
    EXPERT = 'expert'
    LEGACY = 'legacy'
    PPTD = 'pptd'


PAGE_ACTION_KINDS = frozenset({'page-repair', 'page-review', 'page-design'})


def resolve_route(
    *,
    kind: str,
    workflow: str,
    has_document: bool,
    expert_enabled: bool,
) -> ExecutionRoute:
    if kind in PAGE_ACTION_KINDS:
        return ExecutionRoute.PAGE_ACTION
    if kind == 'kimi-slides' and expert_enabled:
        return ExecutionRoute.EXPERT
    if workflow != 'pptd-v2' and not has_document:
        return ExecutionRoute.LEGACY
    return ExecutionRoute.PPTD
