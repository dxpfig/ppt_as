"""Bounded page execution with explicit page lifecycle state."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum


class PageWorkStatus(StrEnum):
    PLANNED = 'planned'
    GENERATED = 'generated'
    STRUCTURALLY_VALID = 'structurally_valid'
    RENDERED = 'rendered'
    REVIEWED = 'reviewed'
    REPAIRED = 'repaired'
    APPROVED = 'approved'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


@dataclass
class PageWorkState:
    path: str
    index: int
    status: PageWorkStatus = PageWorkStatus.PLANNED
    attempts: int = 0
    message: str = ''


class PageWorkQueue:
    def __init__(self, paths: list[str], concurrency: int):
        self.states = {
            path: PageWorkState(path=path, index=index)
            for index, path in enumerate(paths)
        }
        self.semaphore = asyncio.Semaphore(max(1, concurrency))

    @asynccontextmanager
    async def guard(self, path: str):
        state = self.states[path]
        async with self.semaphore:
            state.status = PageWorkStatus.GENERATED
            state.attempts += 1
            try:
                yield state
            except asyncio.CancelledError:
                state.status = PageWorkStatus.CANCELLED
                raise
            except Exception as exc:
                state.status = PageWorkStatus.FAILED
                state.message = str(exc)[:500]
                raise

    def mark(self, path: str, status: PageWorkStatus, message: str = '') -> None:
        state = self.states[path]
        state.status = status
        if message:
            state.message = message[:500]

    def snapshot(self) -> list[dict]:
        return [
            {
                'path': state.path,
                'index': state.index,
                'status': state.status.value,
                'attempts': state.attempts,
                'message': state.message,
            }
            for state in sorted(self.states.values(), key=lambda item: item.index)
        ]
