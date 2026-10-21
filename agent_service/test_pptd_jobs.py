"""Lifecycle acceptance at the HTTP boundary, with isolated storage and fake providers."""
import asyncio
from copy import deepcopy
import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import httpx

service = importlib.import_module('agent_service.app')


def bundle():
    return {'document': {'version': 'v2', 'title': '测试源文档', 'size': [960, 540], 'pages': ['pages/a.page']}, 'pages': [{'path': 'pages/a.page', 'data': {'elements': [{'elementId': 'title', 'elementType': 'text', 'bounds': [40, 40, 880, 90], 'content': {'text': '保存原始内容'}, 'futureField': {'preserved': True}}]}}], 'generationMeta': {'research': {'warnings': []}}, 'checkpoint': {'stage': 'validated'}}


class ExecutionStateTests(unittest.TestCase):
    def test_export_waits_while_failed_layout_is_being_repaired(self):
        from agent_service.job_execution import record_event
        state = record_event(None, {'phase': 'export', 'status': 'started', 'message': '正在导出'})
        state = record_event(state, {'phase': 'validate', 'status': 'retrying', 'message': '正在修复排版'})
        nodes = {node['id']: node for node in state['nodes']}
        self.assertEqual(nodes['export']['status'], 'pending')
        self.assertEqual(nodes['validate']['status'], 'running')

    def test_render_failure_has_compact_message_and_located_issue_details(self):
        from agent_service.job_execution import diagnose_error
        from agent_service.pptd_exports import ValidationError
        issues = [{'page': 'pages/a.page', 'location': 'pages/a.page#title', 'code': 'text-overflow', 'message': '文字超出文本框' * 30} for _ in range(18)]
        inner = ValidationError(issues)
        try:
            raise ValueError('渲染检查仍未通过。' + str(inner)) from inner
        except ValueError as error:
            message, diagnostic, _ = diagnose_error(error, 'export')
        self.assertLess(len(message), 200)
        self.assertIn('18', message)
        self.assertEqual(diagnostic['issues'][0]['location'], 'pages/a.page#title')


class PptdJobsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ('projects', 'jobs', 'templates', 'artifacts', 'trash', 'requests', 'checkpoints'):
            (self.root / name).mkdir()
        self.patch = patch.object(service, 'ROOT', self.root)
        self.patch.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app), base_url='http://test/api/studio/')
        self.project = {'id': 'demo', 'title': '测试', 'workflow': 'pptd-v2', 'slides': [], 'imageSource': 'none'}
        self.health = patch.object(service.agents, 'health', return_value={'llmConfigured': True})
        self.health.start()
        self.visual = patch.object(service.visual_pipeline, 'review_deck', side_effect=self.review)
        self.visual.start()

    async def asyncTearDown(self):
        tasks = list(service.TASKS.values())
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        service.TASKS.clear()
        self.health.stop()
        self.visual.stop()
        self.patch.stop()
        await self.client.aclose()
        self.tmp.cleanup()

    async def wait(self, identifier):
        for _ in range(200):
            job = (await self.client.get('jobs/' + identifier)).json()
            if job['status'] not in ('queued', 'running'): return job
            await asyncio.sleep(.01)
        self.fail('job did not finish')

    async def generate(self, request, progress, **kwargs):
        value = bundle()
        if kwargs.get('save_checkpoint'): kwargs['save_checkpoint'](value['checkpoint'])
        return value

    async def export(self, store, project_id, folder, progress):
        await progress(95, '实际测试边界')
        folder.mkdir()
        (folder / 'presentation.pptx').write_bytes(b'test')
        from PIL import Image
        Image.new('RGB', (96, 54), 'white').save(folder / 'slide-1.png')
        (folder / 'export-report.json').write_text(json.dumps({'revision': store.read(project_id)['revision']}))
        return {'revision': store.read(project_id)['revision'], 'pageCount': 1, 'warnings': [], 'pptx': 'presentation.pptx', 'pdf': 'presentation.pdf', 'source': 'source.zip', 'report': 'export-report.json', 'previews': ['slide-1.png']}

    async def review(self, state, folder, **kwargs):
        return {'status': 'passed', 'coveredPages': state['document']['pages'], 'issues': [], 'model': 'fake-vision'}

    async def start(self):
        response = await self.client.post('jobs', json={'kind': 'outline', 'project': self.project, 'prompt': '做一页', 'allowResearch': False})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['id']

    async def test_cancelled_kimi_job_keeps_partial_deck_continue_checkpoint(self):
        identifier = 'partial-kimi'
        service.save(service.project_path(self.project['id']), {
            **self.project, 'phase': 'generating', 'activeJobId': identifier,
        })
        service.save(service.job_path(identifier), {
            'id': identifier,
            'projectId': self.project['id'],
            'kind': 'kimi-slides',
            'status': 'running',
            'stage': 5,
            'message': '正在生成',
            'recoverable': True,
        })
        workspace = service.kimi_agent._workspace(self.root, self.project['id'], identifier)
        entry = workspace / 'deck' / 'deck.pptd'
        (entry.parent / 'pages').mkdir(parents=True)
        entry.write_text(
            'version: v2\nsize: [960, 540]\n'
            'pages: [pages/one.page, pages/two.page, pages/three.page]\n',
            encoding='utf-8',
        )
        (entry.parent / 'pages' / 'one.page').write_text('elements: []\n', encoding='utf-8')

        response = await self.client.post(f'jobs/{identifier}/cancel')
        self.assertEqual(response.status_code, 200, response.text)
        job = response.json()
        self.assertEqual(job['status'], 'cancelled')
        self.assertEqual(job['checkpoint']['completedPages'], 1)
        self.assertEqual(job['checkpoint']['totalPages'], 3)
        self.assertIn('可继续任务', job['message'])

    async def test_page_review_routes_with_current_page_and_returns_report(self):
        async def review_action(kind, store, project_id, page_path, request, data_root):
            self.assertEqual(kind, 'page-review')
            self.assertEqual(project_id, self.project['id'])
            self.assertEqual(page_path, 'pages/a.page')
            return {
                'action': 'review',
                'pagePath': page_path,
                'revision': 0,
                'status': 'passed',
                'issues': [],
                'observations': [{'pagePath': page_path, 'evidence': '页面层级清晰'}],
                'model': 'fake-vision',
            }

        with patch.object(service.page_actions, 'run_page_action', side_effect=review_action):
            response = await self.client.post('jobs', json={
                'kind': 'page-review',
                'project': self.project,
                'selectedSlideId': 'pages/a.page',
            })
            self.assertEqual(response.status_code, 202, response.text)
            job = await self.wait(response.json()['id'])
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['result']['pageAction']['action'], 'review')
        self.assertEqual(job['result']['pageAction']['pagePath'], 'pages/a.page')

    async def test_page_repair_requires_a_selected_page(self):
        response = await self.client.post('jobs', json={
            'kind': 'page-repair',
            'project': self.project,
        })
        self.assertEqual(response.status_code, 202, response.text)
        job = await self.wait(response.json()['id'])
        self.assertEqual(job['status'], 'failed')
        self.assertIn('指定当前页面', job['error'])

    async def test_model_timeout_is_actionable_and_keeps_page_checkpoint(self):
        from openai import APITimeoutError
        async def timeout(request, progress, **kwargs):
            kwargs['save_checkpoint']({'stage': 'page', 'pages': [{'path': 'pages/a.page', 'data': {'elements': []}}],
                'plan': {'pages': [{'title': '已完成'}, {'title': '风险'}], 'document': {'pages': ['pages/a.page', 'pages/b.page']}}})
            error = APITimeoutError(request=httpx.Request('POST', 'https://provider.invalid'))
            error.generation_context = {'phase': 'pages', 'pagePath': 'pages/b.page', 'pageIndex': 1, 'title': '风险'}
            raise error
        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=timeout):
            identifier = await self.start()
            job = await self.wait(identifier)
        self.assertEqual(job['status'], 'failed')
        self.assertIn('超时', job['error'])
        self.assertIn('第 2 页', job['error'])
        self.assertEqual(job['diagnostic']['code'], 'model_timeout')
        self.assertEqual(job['checkpoint']['completedPages'], 1)
        self.assertTrue((self.root / 'checkpoints' / f'{identifier}.json').exists())
        self.assertNotIn('provider.invalid', json.dumps(job))

    async def test_public_execution_events_reach_job_api_without_raw_model_fields(self):
        async def generate(request, progress, **kwargs):
            emit = kwargs.get('on_event')
            if emit:
                emit({'phase': 'plan', 'status': 'started', 'message': '正在策划内容', 'reasoning': 'PRIVATE_MODEL_TEXT'})
                emit({'phase': 'plan', 'status': 'completed', 'message': '已完成叙事规划'})
                emit({'phase': 'pages', 'status': 'started', 'pageIndex': 0, 'pagePath': 'pages/a.page', 'title': '开场', 'message': '正在创作第 1 页'})
                emit({'phase': 'pages', 'status': 'retrying', 'pageIndex': 0, 'pagePath': 'pages/a.page', 'attempt': 2, 'message': '页面格式检查未通过，重试中'})
                emit({'phase': 'pages', 'status': 'completed', 'pageIndex': 0, 'pagePath': 'pages/a.page', 'message': '第 1 页已保存'})
            return await self.generate(request, progress, **kwargs)
        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=generate), patch.object(service.pptd_exports, 'export_document', side_effect=self.export):
            identifier = await self.start()
            job = await self.wait(identifier)
        self.assertEqual(job['status'], 'completed', job)
        execution = job.get('execution', {})
        nodes = {n['id']: n for n in execution.get('nodes', [])}
        self.assertEqual(nodes.get('plan', {}).get('status'), 'completed')
        self.assertEqual(nodes.get('export', {}).get('status'), 'completed')
        self.assertEqual(nodes.get('done', {}).get('status'), 'completed')
        self.assertTrue(any(e['status'] == 'retrying' for e in execution['events']))
        self.assertEqual(execution['pages'][0]['status'], 'completed')
        self.assertNotIn('PRIVATE_MODEL_TEXT', json.dumps(job))

    async def test_parallel_page_failure_marks_only_failed_page_and_retains_completed_pages(self):
        async def generate(request, progress, **kwargs):
            emit = kwargs['on_event']
            for index, path in enumerate(['pages/a.page', 'pages/b.page', 'pages/c.page']):
                emit({'phase': 'pages', 'status': 'started', 'pageIndex': index, 'pagePath': path, 'message': '正在创作'})
            emit({'phase': 'pages', 'status': 'completed', 'pageIndex': 0, 'pagePath': 'pages/a.page', 'message': '页面已保存'})
            error = ValueError('页面格式未通过校验')
            error.generation_context = {'phase': 'pages', 'pageIndex': 1, 'pagePath': 'pages/b.page'}
            raise error
        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=generate):
            identifier = await self.start()
            job = await self.wait(identifier)
        self.assertEqual([p['status'] for p in job['execution']['pages']], ['completed', 'failed', 'cancelled'])
        self.assertEqual(next(n for n in job['execution']['nodes'] if n['id'] == 'pages')['status'], 'failed')

    async def test_provider_auth_error_does_not_leak_request_or_credentials(self):
        from openai import AuthenticationError
        async def generate(*args, **kwargs):
            raise AuthenticationError('Authorization: Bearer SHOULD_NOT_APPEAR', response=httpx.Response(401, request=httpx.Request('POST', 'https://secret-provider.invalid')), body={'secret': 'SHOULD_NOT_APPEAR'})
        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=generate):
            identifier = await self.start()
            job = await self.wait(identifier)
        self.assertEqual(job['diagnostic']['code'], 'model_auth')
        self.assertIn('认证', job['error'])
        self.assertNotIn('SHOULD_NOT_APPEAR', json.dumps(job))
        self.assertNotIn('secret-provider', json.dumps(job))

    async def test_new_job_persists_authority_and_artifacts_without_browser(self):
        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=self.generate), patch.object(service.pptd_exports, 'export_document', side_effect=self.export):
            identifier = await self.start()
            job = await self.wait(identifier)
        self.assertEqual(job['status'], 'completed', job)
        reopened = (await self.client.get('projects/demo')).json()
        self.assertEqual(reopened['pptd']['revision'], 1)
        self.assertEqual(reopened['slides'], [])
        self.assertEqual(reopened['artifact']['revision'], 1)
        self.assertEqual(reopened['generationMeta']['validation']['status'], 'render-passed')
        self.assertEqual(reopened['generationMeta']['validation']['visualReview']['status'], 'passed')
        self.assertTrue((self.root / 'requests' / (identifier + '.json')).is_file())
        self.assertTrue((self.root / 'documents/demo/document.pptd').is_file())
        events = await self.client.get(f'jobs/{identifier}/events')
        self.assertIn('"status": "completed"', events.text)
        self.assertEqual((await self.client.get('projects')).json()[0]['slideCount'], 1)

    async def seed(self):
        await self.client.put('projects/demo', json=self.project)
        value = bundle()
        service.document_store().commit('demo', value['document'], value['pages'], '初稿')

    async def test_visual_failure_never_publishes_and_resume_runs_review_without_generation(self):
        async def unavailable(*args, **kwargs):
            error = ValueError('视觉模型不可用')
            error.code = 'vision_unavailable'
            error.generation_context = {'phase': 'visual'}
            raise error
        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=self.generate), \
             patch.object(service.pptd_exports, 'export_document', side_effect=self.export), \
             patch.object(service.visual_pipeline, 'review_deck', side_effect=unavailable):
            identifier = await self.start()
            job = await self.wait(identifier)
        self.assertEqual(job['status'], 'failed')
        self.assertIsNone((await self.client.get('projects/demo')).json()['artifact'])
        self.assertEqual((await self.client.get(f'artifacts/{identifier}/presentation.pptx')).status_code, 404)
        self.assertTrue(service.read(self.root/'checkpoints'/f'{identifier}.json')['_visualReview'])
        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=AssertionError('must not regenerate')), \
             patch.object(service.pptd_exports, 'export_document', side_effect=self.export):
            response = await self.client.post(f'jobs/{identifier}/resume')
            self.assertEqual(response.status_code, 202)
            resumed = await self.wait(response.json()['id'])
        self.assertEqual(resumed['status'], 'completed', resumed)
        self.assertEqual(resumed['result']['project']['generationMeta']['validation']['visualReview']['reviewedPages'], 1)

    async def test_export_only_job_skips_visual_review(self):
        await self.seed()
        reviews = []
        async def review(state, folder, **kwargs):
            reviews.append(state)
            issues = [] if len(reviews) == 2 else [{'pagePath': 'pages/a.page', 'elementIds': ['title'], 'code': 'alignment', 'message': '标题未对齐'}]
            return {'status': 'issues' if issues else 'passed', 'coveredPages': state['document']['pages'], 'issues': issues}
        with patch.object(service.pptd_exports, 'export_document', side_effect=self.export), \
             patch.object(service.visual_pipeline, 'review_deck', side_effect=review):
            response = await self.client.post('projects/demo/pptd/export')
            job = await self.wait(response.json()['id'])
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual(job['result']['artifact']['revision'], 1)
        self.assertEqual(len(reviews), 0)
        self.assertEqual((await self.client.get('projects/demo/pptd')).json()['pages'][0]['data']['elements'][0]['content']['text'], '保存原始内容')

    async def test_resume_export_completes_missing_planned_body_and_reviews_new_revision(self):
        incomplete = bundle()
        incomplete['pages'][0]['data']['pageType'] = 'content'
        other = deepcopy(incomplete['pages'][0])
        other['path'] = 'pages/b.page'
        other['data']['elements'].append({'elementId': 'body', 'elementType': 'text',
            'bounds': [40, 180, 880, 160], 'content': {'text': '已完成页面的正文保持原样'}})
        incomplete['pages'].append(other)
        incomplete['document']['pages'].append(other['path'])
        planned_text = '三层治理：决策层由公司分管领导牵头，负责季度评审；归口层由数字化办公室负责路线图与预算；执行层由业务人员和技术人员组成，按场景编组。协同机制包括双周例会、月度里程碑评审和季度业务复盘，持续跟踪问题与实施进展。'
        plan = {'document': deepcopy(incomplete['document']), 'pages': [
            {'title': '治理机制', 'readerTask': '说明角色与职责', 'content': planned_text, 'visualPlan': '三层结构'},
            {'title': '完整页', 'readerTask': '阅读正文', 'content': '已完成页面的正文保持原样', 'visualPlan': '正文'},
        ]}
        incomplete['checkpoint'] = {'stage': 'validated', 'plan': plan, 'pages': deepcopy(incomplete['pages'])}
        async def initial_generation(*args, **kwargs):
            kwargs['save_checkpoint'](incomplete['checkpoint'])
            return deepcopy(incomplete)
        async def fail_review(*args, **kwargs):
            error = ValueError('old review failed on missing body')
            error.generation_context = {'phase': 'visual'}
            raise error
        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=initial_generation), \
             patch.object(service.pptd_exports, 'export_document', side_effect=self.export), \
             patch.object(service.visual_pipeline, 'review_deck', side_effect=fail_review):
            identifier = await self.start()
            self.assertEqual((await self.wait(identifier))['status'], 'failed')

        async def complete_body(*args, **kwargs):
            cp = kwargs['checkpoint']
            self.assertEqual([i['pagePath'] for i in cp['externalIssues']], ['pages/a.page'])
            self.assertIsNone(cp.get('_visualReview'))
            result = deepcopy(incomplete)
            result['pages'][0]['data']['elements'].append({'elementId': 'recovered-body', 'elementType': 'text',
                'bounds': [40, 180, 880, 240], 'content': {'text': planned_text}})
            result['checkpoint']['pages'] = deepcopy(result['pages'])
            kwargs['save_checkpoint'](result['checkpoint'])
            return result
        async def review_complete(state, *args, **kwargs):
            # A fresh review must examine the newly committed source, not an
            # earlier visual candidate that still contains the blank page.
            self.assertEqual(state['revision'], 2)
            self.assertEqual(state['pages'][0]['data']['elements'][-1]['content']['text'], planned_text)
            return await self.review(state, *args, **kwargs)
        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=complete_body), \
             patch.object(service.pptd_exports, 'export_document', side_effect=self.export), \
             patch.object(service.visual_pipeline, 'review_deck', side_effect=review_complete):
            response = await self.client.post(f'jobs/{identifier}/resume')
            self.assertEqual(response.status_code, 202, response.text)
            resumed = await self.wait(response.json()['id'])
        self.assertEqual(resumed['status'], 'completed', resumed.get('error'))
        final = (await self.client.get('projects/demo/pptd')).json()
        self.assertEqual(final['pages'][1], other)
        self.assertEqual(final['pages'][0]['data']['elements'][:-1], incomplete['pages'][0]['data']['elements'])
        self.assertEqual(resumed['result']['artifact']['revision'], 2)

    async def test_export_preserves_generation_metadata_without_visual_review(self):
        await self.seed()
        original = service.read(self.root/'projects/demo.json')
        original['generationMeta'] = {'scenario':'tech-engineering', 'research':{'warnings':['原始研究记录']}, 'rules':['original-rule']}
        service.save(self.root/'projects/demo.json', original)
        with patch.object(service.pptd_exports, 'export_document', side_effect=self.export), \
             patch.object(service.visual_pipeline, 'review_deck', side_effect=AssertionError('export must not review')):
            response = await self.client.post('projects/demo/pptd/export')
            identifier = response.json()['id']
            completed = await self.wait(identifier)
        self.assertEqual(completed['status'], 'completed', completed)
        meta = (await self.client.get('projects/demo')).json()['generationMeta']
        for name in ('scenario','research','rules'):
            self.assertEqual(meta[name], original['generationMeta'][name])

    async def test_edits_conflicts_and_stale_autosave_cannot_overwrite_document(self):
        await self.seed()
        stale = (await self.client.get('projects/demo')).json()
        edit = {'expectedRevision': 1, 'page': 'pages/a.page', 'elementId': 'title', 'text': '真实修改'}
        result = await self.client.post('projects/demo/pptd/edit', json=edit)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual((await self.client.post('projects/demo/pptd/edit', json=edit)).status_code, 409)
        stale['title'] = '伪造覆盖'
        stale['pptd']['revision'] = 100
        stale['artifact'] = {'revision': 2, 'pptxUrl': 'https://bad.example/forged'}
        self.assertEqual((await self.client.put('projects/demo', json=stale)).status_code, 200)
        current = (await self.client.get('projects/demo')).json()
        self.assertEqual(current['title'], '测试源文档')
        self.assertEqual(current['pptd']['revision'], 2)
        self.assertIsNone(current['artifact'])
        source = (await self.client.get('projects/demo/pptd')).json()
        self.assertEqual(source['pages'][0]['data']['elements'][0]['content']['text'], '真实修改')
        self.assertEqual(source['pages'][0]['data']['elements'][0]['futureField'], {'preserved': True})
        self.assertEqual((await self.client.post('jobs', json={'kind': 'render', 'project': stale, 'expectedRevision': 1})).status_code, 409)

    async def test_failure_resume_reuses_committed_document_and_does_not_recall_model(self):
        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=self.generate), patch.object(service.pptd_exports, 'export_document', side_effect=ValueError('浏览器暂不可用')):
            identifier = await self.start()
            self.assertEqual((await self.wait(identifier))['status'], 'failed')
        project = (await self.client.get('projects/demo')).json()
        self.assertEqual(project['pptd']['revision'], 1)
        self.assertIsNone(project['artifact'])
        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=AssertionError('must resume export')), patch.object(service.pptd_exports, 'export_document', side_effect=self.export):
            response = await self.client.post(f'jobs/{identifier}/resume')
            self.assertEqual(response.status_code, 202, response.text)
            resumed = await self.wait(response.json()['id'])
        self.assertEqual(resumed['status'], 'completed', resumed)
        self.assertEqual(resumed['parentJobId'], identifier)
        self.assertEqual((await self.client.get('jobs/' + identifier)).json()['status'], 'failed')
        self.assertEqual((await self.client.get('projects/demo/pptd')).json()['revision'], 1)

    async def test_cancel_preserves_checkpoint_and_delete_prevents_resurrection(self):
        entered = asyncio.Event()
        async def slow(request, progress, **kwargs):
            kwargs['save_checkpoint']({'stage': 'page', 'pages': [{'path': 'saved'}]})
            entered.set()
            await asyncio.Future()
        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=slow):
            identifier = await self.start()
            await entered.wait()
            self.assertEqual((await self.client.put('projects/demo', json=self.project)).status_code, 409)
            await self.client.delete('projects/demo')
            self.assertEqual((await self.wait(identifier))['status'], 'cancelled')
        self.assertEqual((await self.client.post(f'jobs/{identifier}/resume')).status_code, 410)
        self.assertEqual((await self.client.get('projects/demo')).status_code, 404)
        self.assertTrue((self.root / 'checkpoints' / f'{identifier}.json').is_file())

    async def test_restart_marks_interrupted_and_later_edits_block_stale_resume(self):
        await self.seed()
        request = {'kind': 'render', 'project': self.project}
        service.save(self.root / 'requests/old.json', request)
        service.save(self.root / 'jobs/old.json', {'id': 'old', 'projectId': 'demo', 'status': 'running', 'baseRevision': 1})
        project = service.read(self.root / 'projects/demo.json'); project['activeJobId'] = 'old'
        service.save(self.root / 'projects/demo.json', project)
        with patch.object(service.telemetry, 'setup'), patch.object(service.telemetry, 'shutdown'):
            async with service.lifespan(service.app): pass
        self.assertEqual((await self.client.get('jobs/old')).json()['status'], 'interrupted')
        self.assertIsNone((await self.client.get('projects/demo')).json()['activeJobId'])
        await self.client.post('projects/demo/pptd/edit', json={'expectedRevision': 1, 'pageIndex': 0, 'elementId': 'title', 'text': '后续工作'})
        self.assertEqual((await self.client.post('jobs/old/resume')).status_code, 409)

    async def test_restore_creates_new_revision_and_deleted_documents_are_hidden(self):
        await self.seed()
        state = (await self.client.get('projects/demo/pptd')).json()
        version = state['versions'][0]['id']
        await self.client.post('projects/demo/pptd/edit', json={'expectedRevision': 1, 'pageIndex': 0, 'elementId': 'title', 'text': '修改'})
        response = await self.client.post(f'projects/demo/pptd/versions/{version}/restore', json={'expectedRevision': 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['revision'], 3)
        self.assertEqual(response.json()['pages'][0]['data']['elements'][0]['content']['text'], '保存原始内容')
        await self.client.delete('projects/demo')
        self.assertEqual((await self.client.get('projects/demo/pptd')).status_code, 410)
        self.assertEqual((await self.client.get('projects/demo/pptd/source.zip')).status_code, 410)

    async def test_resume_legacy_layout_checkpoint_keeps_saved_text_and_skips_generation(self):
        issue = {'page': 'pages/a.page', 'elementId': 'title', 'location': 'pages/a.page#title',
                 'code': 'text-overflow', 'message': 'Need 140px height'}
        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=self.generate), \
             patch.object(service.pptd_exports, 'export_document', side_effect=ValueError('renderer interrupted')):
            identifier = await self.start()
            self.assertEqual((await self.wait(identifier))['status'], 'failed')
        cp_path = self.root / 'checkpoints' / f'{identifier}.json'
        cp = service.read(cp_path)
        cp.update(externalIssues=[issue], _visualReview=None)
        service.save(cp_path, cp)
        async def export(store, project_id, folder, progress):
            if store.read(project_id)['pages'][0]['data']['elements'][0]['bounds'][3] < 140:
                raise service.pptd_exports.ValidationError([issue])
            return await self.export(store, project_id, folder, progress)
        async def repair(request, state, issues, **kwargs):
            from agent_service.visual_repair import apply_visual_patches
            return apply_visual_patches(state, [{'pagePath': 'pages/a.page', 'elementId': 'title',
                'op': 'set', 'path': '/bounds/3', 'value': 140}], issues)
        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=AssertionError('saved layout must not call generation')), \
             patch.object(service.pptd_exports, 'export_document', side_effect=export), \
             patch.object(service.visual_pipeline, 'repair_deck', side_effect=repair):
            response = await self.client.post(f'jobs/{identifier}/resume')
            self.assertEqual(response.status_code, 202, response.text)
            resumed = await self.wait(response.json()['id'])
        self.assertEqual(resumed['status'], 'completed', resumed)
        state = (await self.client.get('projects/demo/pptd')).json()
        self.assertEqual(state['revision'], 2)
        self.assertEqual(state['pages'][0]['data']['elements'][0]['content']['text'], '保存原始内容')

    async def test_external_render_error_repairs_and_exports_new_revision(self):
        issue = {'page': 'pages/a.page', 'location': 'pages/a.page#title', 'elementId': 'title', 'code': 'content-missing', 'severity': 'error', 'message': '不支持的富文本导致正文未完整渲染'}
        generated = []
        exported = []

        async def generate(request, progress, **kwargs):
            checkpoint = deepcopy(kwargs.get('checkpoint'))
            generated.append(checkpoint)
            value = bundle()
            value['checkpoint']['pages'] = deepcopy(value['pages'])
            if len(generated) == 2:
                self.assertEqual(checkpoint['externalIssues'], [issue])
                self.assertEqual(checkpoint['_committedRevision'], 1)
                self.assertIsNone(kwargs['existing'])
                value['pages'][0]['data']['elements'][0]['content']['text'] = '修复后的完整内容'
                value['checkpoint']['pages'] = deepcopy(value['pages'])
            kwargs['save_checkpoint'](value['checkpoint'])
            return value

        async def export(store, project_id, folder, progress):
            state = store.read(project_id)
            exported.append((state['revision'], state['pages'][0]['data']['elements'][0]['content']['text']))
            if len(exported) == 1:
                self.assertFalse(folder.exists())
                raise service.pptd_exports.ValidationError([issue])
            self.assertEqual(state['revision'], 2)
            self.assertEqual(exported[-1][1], '修复后的完整内容')
            return await self.export(store, project_id, folder, progress)

        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=generate), patch.object(service.pptd_exports, 'export_document', side_effect=export):
            identifier = await self.start()
            job = await self.wait(identifier)
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual(len(generated), 2)
        # One candidate render feeds validation/review, then the approved
        # revision is rendered once more for the final artifact.
        self.assertEqual([revision for revision, _ in exported], [1, 2, 2])
        current = (await self.client.get('projects/demo')).json()
        state = (await self.client.get('projects/demo/pptd')).json()
        self.assertEqual(current['artifact']['revision'], 2)
        self.assertEqual(current['generationMeta']['validation']['revision'], 2)
        self.assertEqual(len(state['versions']), 2)
        self.assertEqual(state['pages'][0]['data']['elements'][0]['content']['text'], '修复后的完整内容')
        checkpoint = service.read(self.root / 'checkpoints' / f'{identifier}.json')
        self.assertEqual(checkpoint['stage'], 'export')
        self.assertNotIn('externalIssues', checkpoint)
        self.assertEqual(service.read(self.root / 'artifacts' / identifier / 'metadata.json')['revision'], 2)

    async def test_resume_after_validated_repair_cancellation_commits_repaired_draft(self):
        """A validated draft has not yet replaced its previous committed revision."""
        entered = asyncio.Event()
        generation_calls = []
        fixed = bundle()
        fixed['pages'][0]['data']['elements'][0]['content']['text'] = '已经修复但等待提交的内容'
        fixed['checkpoint'] = {'stage': 'validated', 'pages': deepcopy(fixed['pages'])}
        issue = {'page': 'pages/a.page', 'location': 'pages/a.page#title', 'code': 'content-missing', 'message': '正文缺失需要修复'}

        async def generation_then_pause(request, progress, **kwargs):
            generation_calls.append(deepcopy(kwargs.get('checkpoint')))
            if len(generation_calls) == 1:
                value = bundle()
                kwargs['save_checkpoint'](value['checkpoint'])
                return value
            self.assertEqual(kwargs['checkpoint']['externalIssues'], [issue])
            # The generator clears the resolved external issues and persists a
            # validated checkpoint before returning it to run_job for commit.
            kwargs['save_checkpoint'](deepcopy(fixed['checkpoint']))
            entered.set()
            await asyncio.Future()

        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=generation_then_pause), patch.object(service.pptd_exports, 'export_document', side_effect=service.pptd_exports.ValidationError([issue])) as first_export:
            identifier = await self.start()
            await asyncio.wait_for(entered.wait(), timeout=3)
            task = service.TASKS[identifier]
            response = await self.client.post(f'jobs/{identifier}/cancel')
            self.assertEqual(response.status_code, 200, response.text)
            await asyncio.gather(task, return_exceptions=True)
            self.assertEqual((await self.wait(identifier))['status'], 'cancelled')
        first_export.assert_awaited_once()
        checkpoint = service.read(self.root / 'checkpoints' / f'{identifier}.json')
        self.assertEqual(checkpoint['stage'], 'validated')
        self.assertEqual(checkpoint['_committedRevision'], 1)
        self.assertNotIn('externalIssues', checkpoint)
        self.assertEqual(checkpoint['pages'][0]['data']['elements'][0]['content']['text'], '已经修复但等待提交的内容')
        current = (await self.client.get('projects/demo/pptd')).json()
        self.assertEqual(current['revision'], 1)
        self.assertEqual(current['pages'][0]['data']['elements'][0]['content']['text'], '保存原始内容')
        self.assertFalse((self.root / 'artifacts' / identifier / 'metadata.json').exists())

        async def resume_validated(request, progress, **kwargs):
            self.assertEqual(kwargs['checkpoint']['stage'], 'validated')
            self.assertEqual(kwargs['checkpoint']['_committedRevision'], 1)
            self.assertEqual(kwargs['checkpoint']['pages'], fixed['pages'])
            self.assertIsNone(kwargs['existing'])
            kwargs['save_checkpoint'](deepcopy(fixed['checkpoint']))
            return deepcopy(fixed)

        async def export_repaired(store, project_id, folder, progress):
            state = store.read(project_id)
            self.assertEqual(state['revision'], 2, '恢复时必须提交已校验修复稿，不能直接导出旧版本')
            self.assertEqual(state['pages'][0]['data']['elements'][0]['content']['text'], '已经修复但等待提交的内容')
            return await self.export(store, project_id, folder, progress)

        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=resume_validated) as resumed_generation, patch.object(service.pptd_exports, 'export_document', side_effect=export_repaired) as resumed_export:
            response = await self.client.post(f'jobs/{identifier}/resume')
            self.assertEqual(response.status_code, 202, response.text)
            resumed_id = response.json()['id']
            resumed = await self.wait(resumed_id)
        self.assertEqual(resumed['status'], 'completed', resumed)
        resumed_generation.assert_awaited_once()
        self.assertEqual(resumed_export.await_count, 2)
        self.assertEqual(resumed['parentJobId'], identifier)
        current = (await self.client.get('projects/demo')).json()
        self.assertEqual(current['artifact']['revision'], 2)
        self.assertEqual(current['generationMeta']['validation']['status'], 'render-passed')
        self.assertEqual((await self.client.get('jobs/' + identifier)).json()['status'], 'cancelled')

    async def test_render_migrates_legacy_native_incompatible_document(self):
        await self.seed()
        store = service.document_store()
        state = store.read('demo')
        pages = deepcopy(state['pages'])
        element = pages[0]['data']['elements'][0]
        element['content'] = {'fontFamily': 'Arial', 'text': '<p>第一行<br/>第二行 万亿元</p>'}
        element['opacity'] = 0.4
        store.commit('demo', state['document'], pages, '旧版文档', state['revision'])
        exported = []

        async def export(store, project_id, folder, progress):
            exported.append(store.read(project_id))
            return await self.export(store, project_id, folder, progress)

        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=AssertionError('render must not regenerate')), \
             patch.object(service.pptd_exports, 'export_document', side_effect=export):
            response = await self.client.post('jobs', json={'kind': 'render', 'project': self.project})
            self.assertEqual(response.status_code, 202, response.text)
            job = await self.wait(response.json()['id'])
        self.assertEqual(job['status'], 'completed', job)
        content = exported[-1]['pages'][0]['data']['elements'][0]['content']
        self.assertNotIn('<br', content['text'].lower())
        self.assertIn('第二行 万亿元', content['text'])
        self.assertEqual(content['fontFamily'], {'latin': 'Arial', 'ea': '微软雅黑'})
        self.assertNotIn('opacity', exported[-1]['pages'][0]['data']['elements'][0])
        self.assertEqual(exported[-1]['revision'], 3)

    async def test_render_autofits_measured_overflow_without_generation(self):
        await self.seed()
        store = service.document_store()
        state = store.read('demo')
        pages = deepcopy(state['pages'])
        element = pages[0]['data']['elements'][0]
        element['content'] = {'text': '<p><span style="font-size:40px">保存原始内容</span></p>'}
        store.commit('demo', state['document'], pages, '待导出文档', state['revision'])
        issue = {
            'page': 'pages/a.page', 'elementId': 'title', 'location': 'pages/a.page#title',
            'code': 'text-overflow', 'message': '文字超出文本框',
            'layout': {'availableSize': [880, 90], 'measuredTextSize': [880, 130], 'paddingSize': [0, 0]},
        }
        exported = []

        async def export(store, project_id, folder, progress):
            current = store.read(project_id)
            exported.append(current)
            if len(exported) == 1:
                raise service.pptd_exports.ValidationError(
                    [issue], layouts={'pages/a.page': {'elements': [issue['layout'] | {'elementId': 'title'}]}}
                )
            return await self.export(store, project_id, folder, progress)

        with patch.object(service.pptd_generation, 'generate_pptd', side_effect=AssertionError('render must not regenerate')), \
             patch.object(service.pptd_exports, 'export_document', side_effect=export):
            response = await self.client.post('jobs', json={'kind': 'render', 'project': self.project})
            self.assertEqual(response.status_code, 202, response.text)
            job = await self.wait(response.json()['id'])
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual([item['revision'] for item in exported], [2, 3])
        markup = exported[-1]['pages'][0]['data']['elements'][0]['content']['text']
        self.assertIn('保存原始内容', markup)
        size = float(markup.split('font-size:', 1)[1].split('px', 1)[0])
        self.assertLess(size, 40)


if __name__ == '__main__': unittest.main()
