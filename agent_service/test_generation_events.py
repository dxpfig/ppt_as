"""Observable provider calls and bounded, provider-specific output controls."""
import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from agentscope.message import Msg
from agent_service import pptd_generation as g
from agent_service.kimi_rules import KimiRules
from agent_service.test_pptd_generation import bundle, request


class GenerationEvents(unittest.IsolatedAsyncioTestCase):
    async def test_format_retry_exposes_phase_page_attempt_without_output(self):
        events=[]
        answers=['<think>PRIVATE THOUGHT</think>{"data":{},}',json.dumps({"data":{"elements":[]}})]
        class Agent:
            async def reply(self,msg): return Msg(name='agent',role='assistant',content=[{'type':'text','text':answers.pop(0)}])
        with patch.object(g,'make_agent',return_value=Agent()):
            result=await g._ask(SimpleNamespace(model='MiniMax-M3'),'page',KimiRules(),{'pageIndex':10,'pagePath':'pages/11_risks.page','pagePlan':{'title':'风险与应对'}},on_event=events.append)
        self.assertEqual(result,{'data':{'elements':[]}})
        retried=[e for e in events if e['status']=='retrying']
        self.assertEqual(retried[0]['pageIndex'],10)
        self.assertEqual(retried[0]['phase'],'pages')
        self.assertEqual(retried[0]['attempt'],2)
        self.assertNotIn('PRIVATE',json.dumps(events))

    async def test_timeout_failure_keeps_cause_and_execution_location(self):
        events=[]
        class Agent:
            async def reply(self,msg): raise TimeoutError('request expired')
        with patch.object(g,'make_agent',return_value=Agent()):
            with self.assertRaises(g.GenerationModelError) as error:
                await g._ask(SimpleNamespace(model='MiniMax-M3'),'page',KimiRules(),{'pageIndex':10,'pagePath':'pages/11_risks.page','pagePlan':{'title':'风险与应对'}},on_event=events.append)
        exc=error.exception
        self.assertEqual(exc.generation_context['pagePath'],'pages/11_risks.page')
        self.assertIsInstance(exc.__cause__,TimeoutError)
        self.assertIs(exc.original_error,exc.__cause__)
        self.assertEqual(exc.failure_kind,'timeout')
        self.assertTrue(any(e['status']=='retrying' and '超时' in e['message'] for e in events))
        self.assertEqual(events[-1]['status'],'failed')

    async def test_waiting_event_and_async_callback(self):
        events=[]
        async def event(e): events.append(e)
        class Agent:
            async def reply(self,msg):
                await asyncio.sleep(.01)
                return Msg(name='agent',role='assistant',content=[{'type':'text','text':'{"data":{"elements":[]}}'}])
        with patch.object(g,'make_agent',return_value=Agent()),patch.object(g,'MODEL_WAIT_SECONDS',.002):
            await g._ask(SimpleNamespace(model='test'),'page',KimiRules(),{},on_event=event)
        self.assertTrue(any(e['status']=='waiting' for e in events))

    async def test_transient_provider_status_is_retried_before_failing_the_job(self):
        events = []
        calls = []

        class Error429(Exception):
            status_code = 529

        class Agent:
            async def reply(self, msg):
                calls.append(1)
                if len(calls) == 1:
                    raise Error429('overloaded')
                return Msg(
                    name='agent',
                    role='assistant',
                    content=[{'type': 'text', 'text': '{"data":{"elements":[]}}'}],
                )

        with patch.object(g, 'make_agent', side_effect=lambda *args: Agent()), \
             patch.object(g.asyncio, 'sleep', new=AsyncMock()) as sleep:
            result = await g._ask(
                SimpleNamespace(model='MiniMax-M3'),
                'page',
                KimiRules(),
                {},
                on_event=events.append,
            )
        self.assertEqual(result, {'data': {'elements': []}})
        self.assertEqual(len(calls), 2)
        sleep.assert_awaited_once_with(1)
        self.assertTrue(any(event.get('status') == 'retrying' and '529' in event['message'] for event in events))

    async def test_page_complete_only_after_structure_and_checkpoint(self):
        events=[];states=[]
        source=bundle();plan={'title':'Deck','narrative':'Explain','scenario':'education-training','inputType':'topic','document':source['document'],'pages':[{'title':'Hello','readerTask':'Understand','content':'Hello','visualPlan':'Type','imageQueries':[]}]}
        bad=json.loads(json.dumps(source['pages'][0]['data']));bad['elements'][0]['bounds'][3]=0
        answers=[plan,{'data':bad},{'data':source['pages'][0]['data']}]
        async def ask(*args,**kwargs):return answers.pop(0)
        async def close():pass
        async def observe(event):
            if event['status']=='completed' and event.get('pagePath'):
                self.assertTrue(states[-1]['pages'])
            events.append(event)
        with patch.object(g,'make_model',return_value=SimpleNamespace(client=SimpleNamespace(close=close))),patch.object(g,'_ask',side_effect=ask):
            await g.generate_pptd(request(),lambda *a:None,on_event=observe,save_checkpoint=states.append)
        retry=next(i for i,e in enumerate(events) if e['status']=='retrying')
        completed=next(i for i,e in enumerate(events) if e['status']=='completed' and e.get('pagePath'))
        self.assertLess(retry,completed)
        self.assertTrue(any(e['phase']=='pages' and e['status']=='completed' and 'pagePath' not in e for e in events))

    async def test_cancelling_waiting_model_does_not_leave_provider_task_running(self):
        cancelled=asyncio.Event();started=asyncio.Event()
        class Agent:
            async def reply(self,msg):
                started.set()
                try:await asyncio.Event().wait()
                finally:cancelled.set()
        with patch.object(g,'make_agent',return_value=Agent()):
            task=asyncio.create_task(g._ask(SimpleNamespace(model='test'),'page',KimiRules(),{}))
            await started.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(cancelled.is_set())

    async def test_request_deadline_cancels_stalled_stream_and_keeps_timeout_diagnostic(self):
        cancelled=[]
        class Agent:
            async def reply(self,msg):
                try:await asyncio.Event().wait()
                finally:cancelled.append(True)
        model=SimpleNamespace(model='local',pptd_request_timeout=.01)
        with patch.object(g,'make_agent',return_value=Agent()):
            with self.assertRaises(g.GenerationModelError) as error:
                await asyncio.wait_for(g._ask(model,'plan',KimiRules(),{}),timeout=.5)
        self.assertEqual(error.exception.failure_kind,'timeout')
        self.assertEqual(len(cancelled),2)

    async def test_stage_and_page_completion_events_preserve_saved_checkpoint(self):
        events=[];states=[]
        plan={'title':'Deck','narrative':'Explain','scenario':'education-training','inputType':'topic','document':bundle()['document'],'pages':[{'title':'Hello','readerTask':'Understand','content':'Hello','visualPlan':'Type','imageQueries':[]}]}
        async def ask(*args,**kwargs):return plan if args[1]=='plan' else {'data':bundle()['pages'][0]['data']}
        async def close():pass
        model=SimpleNamespace(client=SimpleNamespace(close=close))
        with patch.object(g,'make_model',return_value=model),patch.object(g,'_ask',side_effect=ask):
            await g.generate_pptd(request(),lambda *a:None,on_event=events.append,save_checkpoint=states.append)
        phases={e['phase'] for e in events if e['status']=='completed'}
        self.assertTrue({'context','plan','assets','pages','validate'}<=phases)
        self.assertTrue(any(e['status']=='completed' and e.get('pageIndex')==0 for e in events))
        self.assertEqual(len(states[-1]['pages']),1)


class OutputMode(unittest.IsolatedAsyncioTestCase):
    async def model(self,hostname,output='auto',model_name='MiniMax-M3',settings=None):
        from agent_service.agents import make_model
        environment={'OPENAI_BASE_URL':hostname,'PPT_LLM_CONTEXT_MODE':'off',**{key:'' for key in ('PPT_LLM_STREAM','PPT_LLM_TIMEOUT_SECONDS','PPT_LLM_MAX_TOKENS','PPT_LLM_EXTRA_BODY','PPT_LLM_CONTEXT_TOKENS','PPT_LLM_CONTEXT_MARGIN','PPT_LLM_TOKENIZER_URL')},**(settings or {})}
        with patch('agent_service.agents.configuration',return_value=('openai','test-key',model_name)),patch.dict(os.environ,environment):
            model=make_model(output_mode=output,observable_retries=True)
        self.addAsyncCleanup(model.client.close)
        return model

    async def test_official_m3_auto_controls_thinking_and_streams(self):
        model=await self.model('https://api.minimaxi.com/v1')
        self.assertEqual(model.extra_body,{'thinking':{'type':'disabled'},'reasoning_split':True})
        self.assertTrue(model.stream)
        self.assertEqual(model.max_retries,0)

    async def test_direct_profile_reaches_actual_openai_http_request_body(self):
        import httpx
        from openai import AsyncOpenAI
        seen=[]
        def transport(request):
            seen.append(json.loads(request.content))
            packet={"id":"test-stream","object":"chat.completion.chunk","model":"MiniMax-M3","created":1,"choices":[{"index":0,"delta":{"role":"assistant","content":"{\"data\":{\"elements\":[]}}"},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":8,"total_tokens":13}}
            content="data: "+json.dumps(packet)+"\n\ndata: [DONE]\n\n"
            return httpx.Response(200,headers={"content-type":"text/event-stream"},text=content)
        model=await self.model('https://api.minimaxi.com/v1')
        await model.client.close()
        model.client=AsyncOpenAI(api_key='unit-test-only',base_url='https://api.minimaxi.com/v1',http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        self.addAsyncCleanup(model.client.close)
        stream=await model(messages=[Msg(name='user',role='user',content=[{'type':'text','text':'Return JSON'}])])
        chunks=[chunk async for chunk in stream]
        self.assertTrue(chunks)
        self.assertEqual(seen[0]['thinking'],{'type':'disabled'})
        self.assertTrue(seen[0]['reasoning_split'])
        self.assertTrue(seen[0]['stream'])
        self.assertEqual(seen[0]['stream_options'],{'include_usage':True})
        self.assertEqual(seen[0]['max_completion_tokens'],16000)
        self.assertEqual(seen[0]['model'],'MiniMax-M3')

    async def test_explicit_adaptive_and_other_provider_are_preserved(self):
        model=await self.model('https://api.minimax.io/v1','adaptive')
        self.assertEqual(model.extra_body,{'thinking':{'type':'adaptive'},'reasoning_split':True})
        other=await self.model('https://gateway.example/v1')
        self.assertIsNone(other.extra_body)
        other_model=await self.model('https://api.minimax.io/v1',model_name='another-model')
        self.assertIsNone(other_model.extra_body)

    async def test_unverified_direct_mode_is_rejected(self):
        with self.assertRaises(ValueError): await self.model('https://gateway.example/v1','direct')

    async def test_local_model_profile_reaches_http_and_returns_complete_json(self):
        import httpx
        from openai import AsyncOpenAI
        seen=[]
        def transport(request):
            body=json.loads(request.content);seen.append((body,request.extensions.get('timeout')))
            packet={'id':'local','object':'chat.completion.chunk','model':'Qwen3.8-27B','created':1,'choices':[{'index':0,'delta':{'role':'assistant','content':'{"data":{"elements":[]}}'},'finish_reason':'stop'}]}
            if body.get('stream'):
                return httpx.Response(200,headers={'content-type':'text/event-stream'},text='data: '+json.dumps(packet)+'\n\ndata: [DONE]\n\n')
            return httpx.Response(200,json={'id':'local','object':'chat.completion','model':'Qwen3.8-27B','created':1,'choices':[{'index':0,'message':{'role':'assistant','content':'{"data":{"elements":[]}}'},'finish_reason':'stop'}]})
        model=await self.model('http://localhost:8000/v1',model_name='Qwen3.8-27B',settings={
            'PPT_LLM_STREAM':'true','PPT_LLM_TIMEOUT_SECONDS':'600','PPT_LLM_MAX_TOKENS':'32000',
            'PPT_LLM_EXTRA_BODY':'{"chat_template_kwargs":{"enable_thinking":false}}',
        })
        timeout=model.client.timeout
        await model.client.close()
        model.client=AsyncOpenAI(api_key='unit-test-only',base_url='http://localhost:8000/v1',timeout=timeout,http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        self.addAsyncCleanup(model.client.close)
        result=await g._ask(model,'page',KimiRules(),{})
        self.assertEqual(result,{'data':{'elements':[]}})
        body,timeout=seen[0]
        self.assertTrue(body['stream'])
        self.assertEqual(body['chat_template_kwargs'],{'enable_thinking':False})
        self.assertEqual(body['max_completion_tokens'],32000)
        self.assertEqual(timeout['read'],600)
        self.assertEqual(model.pptd_request_timeout,600)

    async def test_invalid_local_model_settings_fail_before_any_request(self):
        for settings in ({'PPT_LLM_TIMEOUT_SECONDS':'NaN'},{'PPT_LLM_MAX_TOKENS':'0'},
                         {'PPT_LLM_STREAM':'maybe'},{'PPT_LLM_EXTRA_BODY':'[]'},
                         {'PPT_LLM_EXTRA_BODY':'{"stream":false}'}):
            with self.subTest(settings=settings),self.assertRaises(ValueError):
                await self.model('http://localhost:8000/v1',model_name='Qwen3.8-27B',settings=settings)



class VisualExecutionEvents(unittest.TestCase):
    def test_visual_subtasks_preserve_coverage_without_finishing_or_mutating_saved_pages(self):
        from agent_service.job_execution import record_event
        state = record_event(None, {'phase':'pages','status':'completed','pagePath':'p/1.page','pageIndex':0,'message':'已保存'})
        saved = state['pages'].copy()
        state = record_event(state, {'phase':'visual','status':'started','scope':'deck','attempt':1,'covered':0,'total':2,'message':'AI 正在检查整稿'})
        state = record_event(state, {'phase':'visual','status':'completed','scope':'deck','attempt':1,'issueCount':1,'message':'整稿检查完成','reasoning':'PRIVATE'})
        self.assertEqual(next(n for n in state['nodes'] if n['id']=='visual')['status'], 'running')
        state = record_event(state, {'phase':'visual','status':'completed','scope':'page','pagePath':'p/1.page','pageIndex':0,'attempt':1,'covered':1,'total':2,'issueCount':1,'elementId':'title','message':'发现标题裁切','rawResponse':'PRIVATE'})
        self.assertEqual(state['pages'], saved)
        self.assertEqual(state['visualPages'][0]['issueCount'], 1)
        self.assertEqual(state['visual']['covered'], 1)
        self.assertEqual(state['visual']['total'], 2)
        self.assertEqual(state['visual']['status'], 'running')
        self.assertEqual(state['events'][-1]['elementId'], 'title')
        self.assertNotIn('PRIVATE', json.dumps(state))
        self.assertEqual(next(n for n in state['nodes'] if n['id']=='visual')['status'], 'running')
        state = record_event(state, {'phase':'visual','status':'completed','covered':2,'total':2,'issueCount':0,'message':'AI 视觉审稿通过'})
        self.assertEqual(next(n for n in state['nodes'] if n['id']=='visual')['status'], 'completed')

    def test_new_review_round_resets_prior_coverage_and_resumed_old_execution_gains_visual_phase(self):
        from agent_service.job_execution import record_event, initial_execution
        state = initial_execution()
        state['nodes'] = [n for n in state['nodes'] if n['id'] != 'visual']
        state = record_event(state, {'phase':'visual','status':'completed','scope':'page','attempt':1,'pagePath':'p/1.page','pageIndex':0,'covered':1,'total':1,'message':'检查完成'})
        state = record_event(state, {'phase':'visual','status':'started','scope':'deck','attempt':2,'covered':0,'total':1,'message':'AI 开始复审'})
        self.assertEqual(state['visualPages'], [])
        self.assertEqual(state['visual']['covered'], 0)
        self.assertEqual(state['visual']['attempt'], 2)
        self.assertEqual(state['phase'], 'visual')

    def test_visual_error_diagnostics_are_specific_and_redact_independent_vision_key(self):
        from agent_service.job_execution import diagnose_error
        for code in ('vision_unavailable','visual_review_invalid','visual_review_exhausted','visual_content_changed'):
            error = RuntimeError('vision-secret')
            error.code = code
            error.generation_context = {'phase':'visual','pageIndex':0,'attempt':2}
            error.issues = [{'pagePath':'p/1.page','pageIndex':0,'elementIds':['title','body'],'code':'overlap','message':'vision-secret'}]
            with patch.dict(os.environ, {'PPT_VISION_API_KEY':'vision-secret'}):
                message, diagnostic, _ = diagnose_error(error)
            self.assertEqual(diagnostic['code'], code)
            self.assertEqual(diagnostic['phase'], 'visual')
            self.assertEqual(diagnostic['issues'][0]['pageIndex'], 0)
            self.assertEqual(diagnostic['issues'][0]['page'], 'p/1.page')
            self.assertEqual(diagnostic['issues'][0]['location'], 'p/1.page')
            self.assertEqual(diagnostic['issues'][0]['elementId'], 'title, body')
            self.assertNotIn('vision-secret', json.dumps(diagnostic))
            self.assertNotIn('人工', message)

    def test_rejected_visual_repair_is_not_reported_as_remaining_layout_errors(self):
        from agent_service.job_execution import diagnose_error
        from agent_service.visual_repair import VisualRepairError
        issues = [{'pagePath': 'pages/11_organization.page', 'pageIndex': 10,
                   'elementIds': [], 'code': 'missing-body-content', 'message': '正文缺失'}]
        repair_error = VisualRepairError('repair rejected', issues, ValueError('protected field'))
        wrapped = RuntimeError('job failed')
        wrapped.__cause__ = repair_error
        for error in (repair_error, wrapped):
            with self.subTest(wrapped=error is wrapped):
                message, diagnostic, _ = diagnose_error(error, 'context')
                self.assertEqual(diagnostic['code'], 'visual_repair_failed')
                self.assertEqual(diagnostic['phase'], 'visual')
                self.assertIn('安全校验', message)
                self.assertNotIn('自动修复后仍有', message)
                self.assertNotIn('protected field', message)
                self.assertEqual(diagnostic['issues'][0]['code'], 'missing-body-content')
                self.assertEqual(diagnostic['issues'][0]['pageIndex'], 10)

    def test_routes_without_a_checkpoint_do_not_promise_a_resume(self):
        from agent_service.job_execution import diagnose_error
        error = ValueError('最终排版检查未通过')
        message, _, _ = diagnose_error(error, 'validate', resumable=False)
        self.assertIn('可重新尝试该任务', message)
        self.assertNotIn('检查点', message)
        resumable, _, _ = diagnose_error(error, 'validate')
        self.assertIn('已保留检查点', resumable)

    def test_model_failure_during_content_recovery_takes_precedence_over_missing_body(self):
        from agent_service.job_execution import diagnose_error
        issues = [{'code': 'PlannedContentMissing', 'pagePath': 'pages/11.page',
                   'pageIndex': 10, 'location': 'pages/11.page.elements', 'message': '计划正文缺失'}]
        context = {'phase': 'validate', 'pagePath': 'pages/11.page', 'pageIndex': 10, 'attempt': 3}
        cases = [('format', 'model_output_invalid'), ('output-limit', 'model_output_limit'),
                 ('provider', 'model_provider_error')]
        for failure_kind, expected_code in cases:
            with self.subTest(failure_kind=failure_kind):
                cause = json.JSONDecodeError('Expecting delimiter', '{"PRIVATE RAW MODEL OUTPUT"', 2) if failure_kind == 'format' else ValueError('PRIVATE RAW MODEL OUTPUT')
                model_error = g.GenerationModelError('PRIVATE RAW MODEL OUTPUT', context, cause, failure_kind)
                error = g.GenerationIncomplete('PRIVATE RAW MODEL OUTPUT', {}, issues)
                error.__cause__ = model_error
                message, diagnostic, _ = diagnose_error(error, 'pages')
                self.assertEqual(diagnostic['code'], expected_code)
                self.assertEqual(diagnostic['phase'], 'validate')
                self.assertEqual(diagnostic['pageIndex'], 10)
                self.assertEqual(diagnostic['issues'][0]['location'], 'pages/11.page.elements')
                self.assertNotIn('自动修复后仍有', message)
                self.assertNotIn('PRIVATE RAW MODEL OUTPUT', message + json.dumps(diagnostic))

    def test_content_integrity_failure_reports_preserved_page_without_raw_response(self):
        from agent_service.job_execution import diagnose_error
        issues = [{'code': 'PlannedContentMissing', 'pagePath': 'pages/15.page',
                   'pageIndex': 14, 'location': 'pages/15.page.elements', 'message': '计划正文缺失'}]
        error = g.GenerationIncomplete('PRIVATE REJECTED CONTENT', {}, issues)
        error.__cause__ = ValueError('PRIVATE REJECTED CONTENT')
        message, diagnostic, _ = diagnose_error(error, 'validate')
        self.assertEqual(diagnostic['code'], 'content_recovery_failed')
        self.assertIn('原规划正文恢复未通过完整性检查', message)
        self.assertIn('现有页面已保留', message)
        self.assertEqual(diagnostic['issues'][0]['location'], 'pages/15.page.elements')
        self.assertNotIn('自动修复后仍有', message)
        self.assertNotIn('PRIVATE REJECTED CONTENT', message + json.dumps(diagnostic))


if __name__=='__main__':unittest.main()
