"""Page queue lifecycle and bounded concurrency."""
import asyncio
import unittest

from agent_service.page_work_queue import PageWorkQueue, PageWorkStatus


class PageWorkQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_guard_tracks_status_attempts_and_bounded_concurrency(self):
        queue = PageWorkQueue(['a', 'b', 'c'], 2)
        active = 0
        maximum = 0

        async def work(path):
            nonlocal active, maximum
            async with queue.guard(path):
                active += 1
                maximum = max(maximum, active)
                try:
                    await asyncio.sleep(.02)
                finally:
                    active -= 1

        await asyncio.gather(*(work(path) for path in ('a', 'b', 'c')))
        self.assertEqual(maximum, 2)
        self.assertTrue(all(item['status'] == PageWorkStatus.GENERATED for item in queue.snapshot()))
        self.assertTrue(all(item['attempts'] == 1 for item in queue.snapshot()))

    async def test_guard_records_failure_and_cancellation(self):
        queue = PageWorkQueue(['a', 'b'], 2)
        with self.assertRaises(ValueError):
            async with queue.guard('a'):
                raise ValueError('bad page')
        entered = asyncio.Event()

        async def cancel_me():
            async with queue.guard('b'):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(cancel_me())
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        states = {item['path']: item for item in queue.snapshot()}
        self.assertEqual(states['a']['status'], PageWorkStatus.FAILED)
        self.assertEqual(states['a']['message'], 'bad page')
        self.assertEqual(states['b']['status'], PageWorkStatus.CANCELLED)
