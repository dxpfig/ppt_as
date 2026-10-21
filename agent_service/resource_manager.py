"""Independent concurrency pools for model, render and delivery work."""
from __future__ import annotations

import asyncio
import os


def _limit(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{name} 必须是正整数。') from exc
    if value < 1:
        raise ValueError(f'{name} 必须是正整数。')
    return value


MODEL_LIMIT = asyncio.Semaphore(_limit('PPT_MODEL_TASK_CONCURRENCY', 2))
RENDER_LIMIT = asyncio.Semaphore(_limit('PPT_RENDER_CONCURRENCY', 1))
EXPORT_LIMIT = asyncio.Semaphore(_limit('PPT_EXPORT_CONCURRENCY', 1))
