"""Single routing point for quick, expert, page, and legacy jobs."""
from __future__ import annotations

from enum import StrEnum


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
