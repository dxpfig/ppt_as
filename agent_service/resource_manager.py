"""Independent concurrency pools for model, render and delivery work."""
from __future__ import annotations

import asyncio
import os
import time


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


class ModelPressure:
    """Pause new model work briefly after an upstream rate-limit response."""

    def __init__(self, cooldown: float = 2.0):
        self.cooldown = cooldown
        self.blocked_until = 0.0
        self.strikes = 0

    def rate_limited(self) -> float:
        self.strikes = min(4, self.strikes + 1)
        delay = min(20.0, self.cooldown * (2 ** (self.strikes - 1)))
        self.blocked_until = max(self.blocked_until, time.monotonic() + delay)
        return delay

    def succeeded(self) -> None:
        self.strikes = 0

    def remaining(self) -> float:
        return max(0.0, self.blocked_until - time.monotonic())

    async def wait(self) -> None:
        delay = self.remaining()
        if delay > 0:
            await asyncio.sleep(delay)


MODEL_PRESSURE = ModelPressure()
