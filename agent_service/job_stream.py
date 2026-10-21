"""Server-sent event stream for durable generation jobs."""
from __future__ import annotations

import asyncio
import json

from .job_store import JobStore


TERMINAL_JOB_STATUSES = frozenset(
    {'completed', 'failed', 'cancelled', 'interrupted'}
)


async def events(
    store: JobStore,
    identifier: str,
    is_disconnected,
    *,
    poll_interval: float = 0.5,
):
    """Yield complete job snapshots and heartbeats until the job is terminal."""
    previous = None
    while not await is_disconnected():
        job = store.read(identifier)
        encoded = json.dumps(job, ensure_ascii=False)
        if encoded != previous:
            yield 'data: ' + encoded + '\n\n'
            previous = encoded
        else:
            yield ': heartbeat\n\n'
        if job['status'] in TERMINAL_JOB_STATUSES:
            break
        await asyncio.sleep(poll_interval)
