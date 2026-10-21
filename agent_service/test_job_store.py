"""Durable job persistence and transition tests."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_service.job_execution import initial_execution, record_event, stage_metrics
from agent_service.job_store import JobRuntime, JobStore
from agent_service.job_stream import events


class JobStoreTests(unittest.TestCase):
    def test_update_is_atomic_and_records_bounded_status_events(self):
        with TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary))
            store.save(
                'job',
                {
                    'id': 'job',
                    'projectId': 'project',
                    'status': 'queued',
                    'stage': 0,
                    'message': '排队',
                },
            )

            updated = store.update('job', status='running', stage=5, message='开始')

            self.assertEqual(updated['status'], 'running')
            self.assertEqual(store.read('job')['events'][-1]['stage'], 5)
            self.assertFalse(list(store.directory.glob('*.tmp')))

    def test_terminal_job_cannot_return_to_running(self):
        with TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary))
            store.save(
                'job',
                {
                    'id': 'job',
                    'projectId': 'project',
                    'status': 'completed',
                    'stage': 100,
                },
            )

            with self.assertRaises(ValueError):
                store.update('job', status='running')

    def test_invalid_job_contract_is_rejected_before_publication(self):
        with TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary))
            with self.assertRaises(ValueError):
                store.save(
                    'job',
                    {
                        'id': 'job',
                        'projectId': 'project',
                        'status': 'unknown',
                        'stage': 0,
                    },
                )

    def test_cancelled_job_is_immutable(self):
        with TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary))
            store.save(
                'job',
                {
                    'id': 'job',
                    'projectId': 'project',
                    'status': 'cancelled',
                    'stage': 5,
                    'message': '停止',
                },
            )

            unchanged = store.update('job', status='failed', message='late failure')

            self.assertEqual(unchanged['status'], 'cancelled')
            self.assertEqual(unchanged['message'], '停止')

    def test_runtime_shares_progress_event_and_checkpoint_contract(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = JobStore(root)
            store.save(
                'job',
                {
                    'id': 'job',
                    'projectId': 'project',
                    'status': 'queued',
                    'stage': 0,
                    'message': '排队',
                    'execution': initial_execution(),
                },
            )
            checks = []
            runtime = JobRuntime(
                store,
                'job',
                'project',
                lambda: checks.append(True),
                root / 'checkpoints' / 'job.json',
            )

            runtime.progress(5, '读取材料')
            updated = runtime.event(
                {'phase': 'pages', 'status': 'started', 'message': '开始', 'covered': 1, 'total': 2}
            )
            checkpoint = runtime.save_checkpoint(
                {'stage': 'pages', 'pages': [{'path': 'pages/a.page'}], 'plan': {'pages': [{}, {}]}}
            )

            self.assertEqual(updated['checkpoint']['completedPages'], 1)
            self.assertEqual(checkpoint['pages'][0]['path'], 'pages/a.page')
            self.assertEqual(store.read('job')['checkpoint']['completedPages'], 1)
            self.assertGreaterEqual(store.read('job')['stage'], 40)
            self.assertEqual(len(checks), 3)

    def test_stage_metrics_only_report_measured_nodes(self):
        execution = record_event(
            initial_execution(),
            {'phase': 'plan', 'status': 'started', 'message': '开始'},
            now=100,
        )
        execution = record_event(
            execution,
            {'phase': 'plan', 'status': 'completed', 'message': '完成'},
            now=103.5,
        )

        self.assertEqual(stage_metrics(execution)['plan'], {'status': 'completed', 'seconds': 3.5})
        self.assertNotIn('pages', stage_metrics(execution))


class JobStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_emits_snapshot_and_stops_on_terminal_state(self):
        with TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary))
            store.save(
                'job',
                {
                    'id': 'job',
                    'projectId': 'project',
                    'status': 'running',
                    'stage': 5,
                    'message': '运行',
                },
            )
            disconnected = [False, False, True]

            async def is_disconnected():
                return disconnected.pop(0)

            stream = events(store, 'job', is_disconnected, poll_interval=0)
            first = await anext(stream)
            store.update('job', status='completed', stage=100, message='完成')
            second = await anext(stream)

            self.assertIn('"status": "running"', first)
            self.assertIn('"status": "completed"', second)
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
