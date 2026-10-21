"""Shared execution policies for quick and expert PPT generation."""
from __future__ import annotations

from dataclasses import dataclass, replace
import os


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是 {minimum}–{maximum} 的整数。") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须是 {minimum}–{maximum} 的整数。")
    return value


def _integer_with_fallback(
    name: str,
    fallback_name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        value = os.getenv(fallback_name)
    try:
        parsed = int(value) if value is not None and value.strip() else default
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是 {minimum}–{maximum} 的整数。") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} 必须是 {minimum}–{maximum} 的整数。")
    return parsed


@dataclass(frozen=True)
class ExecutionPolicy:
    mode: str
    page_concurrency: int
    page_batch_size: int
    max_batch_pages: int
    max_iterations: int
    planning_iterations: int
    planning_attempts: int
    repair_rounds: int
    layout_page_attempts: int
    layout_page_batch: int
    visual_repair_page_batch: int
    visual_review_concurrency: int
    layout_autofit_budget: int
    render_repair_budget: int
    visual_review: str
    export_after_review: bool


def adapt_to_model(policy: ExecutionPolicy, model_context: dict | None) -> ExecutionPolicy:
    """Reduce concurrency for smaller context windows while respecting the user cap."""
    try:
        context_tokens = int((model_context or {}).get('contextTokens') or 0)
    except (TypeError, ValueError):
        context_tokens = 0
    if context_tokens and context_tokens < 32_000:
        limit = 1
    elif context_tokens and context_tokens < 128_000:
        limit = 2
    elif context_tokens:
        limit = 4
    else:
        return policy
    return replace(policy, page_concurrency=min(policy.page_concurrency, limit))


def quick_policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        mode='quick',
        page_concurrency=_integer('PPTD_PAGE_CONCURRENCY', 2, 1, 8),
        page_batch_size=1,
        max_batch_pages=1,
        max_iterations=1,
        planning_iterations=1,
        planning_attempts=1,
        repair_rounds=_integer('PPTD_REPAIR_BUDGET', 8, 2, 30),
        layout_page_attempts=0,
        layout_page_batch=1,
        visual_repair_page_batch=_integer('PPTD_VISUAL_REPAIR_PAGE_BATCH', 4, 1, 20),
        visual_review_concurrency=_integer('PPTD_VISUAL_REVIEW_CONCURRENCY', 2, 1, 6),
        layout_autofit_budget=_integer('PPTD_LAYOUT_AUTOFIT_BUDGET', 2, 0, 10),
        render_repair_budget=_integer('PPTD_RENDER_REPAIR_BUDGET', 4, 1, 20),
        visual_review='risk-and-final',
        export_after_review=True,
    )


def expert_policy() -> ExecutionPolicy:
    max_iterations = _integer('PPT_KIMI_MAX_ITERS', 40, 4, 200)
    return ExecutionPolicy(
        mode='expert',
        page_concurrency=1,
        page_batch_size=_integer('PPT_KIMI_BATCH_PAGES', 2, 1, 5),
        max_batch_pages=5,
        max_iterations=max_iterations,
        planning_iterations=min(
            max_iterations,
            _integer('PPT_KIMI_PLAN_ITERS', 24, 4, 200),
        ),
        planning_attempts=_integer('PPT_KIMI_PLAN_ATTEMPTS', 3, 1, 10),
        repair_rounds=_integer('PPT_KIMI_REPAIR_ROUNDS', 2, 0, 10),
        layout_page_attempts=_integer_with_fallback(
            'PPT_KIMI_LAYOUT_PAGE_ATTEMPTS',
            'PPT_KIMI_LAYOUT_REPAIR_ROUNDS',
            3,
            1,
            10,
        ),
        layout_page_batch=_integer('PPT_KIMI_LAYOUT_PAGE_BATCH', 4, 1, 20),
        visual_repair_page_batch=_integer('PPTD_VISUAL_REPAIR_PAGE_BATCH', 4, 1, 20),
        visual_review_concurrency=_integer('PPTD_VISUAL_REVIEW_CONCURRENCY', 2, 1, 6),
        layout_autofit_budget=_integer('PPTD_LAYOUT_AUTOFIT_BUDGET', 2, 0, 10),
        render_repair_budget=_integer('PPTD_RENDER_REPAIR_BUDGET', 4, 1, 20),
        visual_review='full',
        export_after_review=False,
    )
