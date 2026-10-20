"""Request-budget contracts at the real AgentScope/OpenAI HTTP boundary."""
import json
import asyncio
import os
import unittest
from unittest.mock import patch

import httpx
from agentscope.message import Msg
from openai import AsyncOpenAI

from agent_service.agents import make_model, make_agent
from agent_service.job_execution import diagnose_error


class ModelContextTests(unittest.IsolatedAsyncioTestCase):
    def test_reference_budget_uses_model_input_room_and_hard_cap(self):
        from agent_service.model_context import reference_budget_from_context

        budget = reference_budget_from_context(
            {'contextTokens': 128000, 'outputTokens': 32000, 'safetyTokens': 8192,
             'source': 'model-api', 'counting': 'provider-tokenizer', 'model': 'local'},
            max_file_bytes=50 * 1024 * 1024,
            max_characters=2_000_000,
            characters_per_token=2,
        )
        self.assertEqual(budget['inputTokens'], 87808)
        self.assertEqual(budget['maxCharacters'], 175616)
        self.assertEqual(budget['maxFileBytes'], 50 * 1024 * 1024)
        self.assertEqual(budget['contextTokens'], 128000)

        capped = reference_budget_from_context(
            {'contextTokens': 262144, 'outputTokens': 32000, 'safetyTokens': 8192},
            max_characters=400000,
            characters_per_token=4,
        )
        self.assertEqual(capped['inputTokens'], 221952)
        self.assertEqual(capped['maxCharacters'], 400000)

    def test_reference_budget_rejects_incomplete_context(self):
        from agent_service.model_context import reference_budget_from_context

        with self.assertRaises(ValueError):
            reference_budget_from_context({'contextTokens': 128000})

    async def test_agents_reference_budget_uses_discovery_and_closes_model(self):
        from unittest.mock import AsyncMock
        from agent_service import agents

        model = type('BudgetModel', (), {})()
        model.discover_context = AsyncMock(return_value={
            'contextTokens': 262144,
            'outputTokens': 32000,
            'safetyTokens': 8192,
            'source': 'model-api',
            'counting': 'provider-tokenizer',
            'model': 'local',
        })
        model.client = type('Client', (), {})()
        model.client.close = AsyncMock()
        with patch.dict(os.environ, {
            'PPT_LLM_PROVIDER': 'openai',
            'PPT_LLM_MODEL': 'local',
            'OPENAI_API_KEY': 'test',
            'PPT_REFERENCE_MAX_CHARACTERS': '400000',
            'PPT_REFERENCE_CHARS_PER_TOKEN': '2',
        }), patch.object(agents, 'make_model', return_value=model):
            budget = await agents.reference_budget()
        self.assertEqual(budget['contextTokens'], 262144)
        self.assertEqual(budget['maxCharacters'], 400000)
        self.assertEqual(budget['source'], 'model-api')
        model.client.close.assert_awaited_once()

    async def response_counter_model(self, *, count=34388, available=True, context=None):
        model, _ = await self.model(PPT_LLM_CONTEXT_MODE='auto', PPT_LLM_CONTEXT_TOKENS='',
            PPT_LLM_TOKENIZER_URL='', PPT_LLM_EXTRA_BODY='{}', PPT_LLM_MAX_TOKENS='32000',
            PPT_LLM_CONTEXT_MARGIN='8192')
        await model.client.close()
        seen = []
        calls = 0
        def transport(req):
            nonlocal calls
            body = json.loads(req.content) if req.content else {}
            seen.append((req.url.path, body))
            if req.url.path == '/v1/models':
                entry = {'id': 'local'}
                if context: entry['context_length'] = context
                return httpx.Response(200, json={'data': [entry]})
            if req.url.path == '/v1/responses/input_tokens':
                calls += 1
                if not available or (calls > 1 and count == 'unavailable'):
                    return httpx.Response(503, json={'error':'PRIVATE unit-test-secret'})
                return httpx.Response(200, json={'object':'response.input_tokens',
                    'input_tokens':10 if calls == 1 else count})
            return httpx.Response(200, json={'id':'test','object':'chat.completion','created':1,
                'model':'local','choices':[{'index':0,'message':{'role':'assistant','content':'OK'},'finish_reason':'stop'}]})
        model.client = AsyncOpenAI(api_key='unit-test-secret',base_url='http://model.local/v1',
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        self.addAsyncCleanup(model.client.close)
        return model, seen

    async def test_server_counter_allows_large_utf8_prompt_that_fits_without_truncation(self):
        model, seen = await self.response_counter_model()
        original = '完整中文规则与标点：' * 7000
        await model(messages=[Msg(name='system',role='system',content=[{'type':'text','text':original}]),
                              Msg(name='user',role='user',content=[{'type':'text','text':'生成五页汇报'}])])
        self.assertEqual(model.context_size,128000)
        self.assertEqual(model.context_info['counting'],'responses-input-tokens')
        sent = next(body for path,body in seen if path.endswith('completions'))
        counted = [body for path,body in seen if path.endswith('input_tokens')][-1]
        self.assertEqual(counted['input'][0]['content'][0]['text'], sent['messages'][0]['content'][0]['text'])
        self.assertEqual(counted['input'][0]['content'][0]['text'], original)
        self.assertEqual(sent['max_completion_tokens'],32000)

    async def test_server_count_boundary_still_blocks_real_overflow(self):
        from agent_service.model_context import responses_name_overhead
        overhead = responses_name_overhead({'messages':[{'name':'user'}]})
        for total, allowed in [(92000,True),(92001,False)]:
            count = total - overhead
            with self.subTest(count=count):
                model, seen = await self.response_counter_model(count=count)
                error = None
                try: await model(messages=[Msg(name='user',role='user',content=[{'type':'text','text':'中文'}])])
                except ValueError as exc: error = exc
                self.assertEqual(any(p.endswith('completions') for p,_ in seen),allowed)
                if not allowed:
                    message,diagnostic,_ = diagnose_error(error)
                    self.assertEqual(diagnostic['budget']['inputTokens'],total)
                    self.assertEqual(diagnostic['budget']['counting'],'responses-input-tokens')
                    self.assertIn('回退窗口',message)
                    self.assertNotIn('。。',message)

    async def test_discovered_counter_failure_or_invalid_count_never_sends_completion(self):
        for count in ('unavailable', -1, True, '34388'):
            with self.subTest(count=count):
                model,seen = await self.response_counter_model(count=count)
                with self.assertRaises(ValueError) as caught:
                    await model(messages=[Msg(name='user',role='user',content=[{'type':'text','text':'PRIVATE'}])])
                message,diagnostic,_ = diagnose_error(caught.exception)
                self.assertEqual(diagnostic['code'],'model_context_unavailable')
                self.assertNotIn('PRIVATE',message)
                self.assertNotIn('unit-test-secret',message)
                self.assertFalse(any(p.endswith('completions') for p,_ in seen))

    async def test_unsupported_optional_counter_keeps_labelled_conservative_bound(self):
        model,seen = await self.response_counter_model(available=False)
        with self.assertRaises(ValueError) as caught:
            await model(messages=[Msg(name='user',role='user',content=[{'type':'text','text':'完整规则'*20000}])])
        message,diagnostic,_ = diagnose_error(caught.exception)
        self.assertIn('保守估算上界',message)
        self.assertEqual(diagnostic['budget']['counting'],'conservative-utf8')
        self.assertFalse(any(p.endswith('completions') for p,_ in seen))

    async def test_response_counter_retains_smaller_model_metadata_limit(self):
        model,seen = await self.response_counter_model(count=3201,context=4096)
        with self.assertRaises(ValueError):
            await model(messages=[Msg(name='user',role='user',content=[{'type':'text','text':'request'}])])
        self.assertEqual(model.context_size,4096)
        self.assertEqual(model.context_info['source'],'model-api')
        self.assertFalse(any(p.endswith('completions') for p,_ in seen))

    async def test_response_count_cache_tracks_full_messages_and_names(self):
        model,seen = await self.response_counter_model()
        messages = [Msg(name='user',role='user',content=[{'type':'text','text':'完整原文'}])]
        first = await model.count_tokens(messages)
        self.assertEqual(await model.count_tokens(messages),first)
        renamed = [Msg(name='longer-user-name',role='user',content=[{'type':'text','text':'完整原文'}])]
        self.assertGreater(await model.count_tokens(renamed),first)
        self.assertEqual(sum(p.endswith('input_tokens') for p,_ in seen),3)  # probe + two distinct requests

    async def test_response_counter_redirect_cannot_forward_prompt_or_credentials(self):
        model,_ = await self.response_counter_model()
        await model.discover_context()
        await model.client.close()
        seen = []
        def transport(req):
            seen.append(req.url.host)
            return httpx.Response(307,headers={'location':'https://other.local/input_tokens'})
        model.client = AsyncOpenAI(api_key='unit-test-secret',base_url='http://model.local/v1',
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport),follow_redirects=True))
        self.addAsyncCleanup(model.client.close)
        with self.assertRaises(ValueError):
            await model.count_tokens([Msg(name='user',role='user',content=[{'type':'text','text':'PRIVATE'}])])
        self.assertEqual(seen,['model.local'])

    async def test_response_counter_preserves_multimodal_messages_tools_and_tool_history(self):
        from agent_service.model_context import responses_count_payload
        payload = {'model':'any-model','messages':[
            {'role':'system','content':'完整规则'},
            {'role':'user','content':[{'type':'text','text':'解释图片'},
                {'type':'image_url','image_url':{'url':'data:image/png;base64,AAAA','detail':'high'}}]},
            {'role':'assistant','content':None,'tool_calls':[{'id':'call-1','type':'function',
                'function':{'name':'read','arguments':'{"path":"original"}'}}]},
            {'role':'tool','tool_call_id':'call-1','content':'完整原文'},
        ],'tools':[{'type':'function','function':{'name':'read','description':'只读',
                    'parameters':{'type':'object','properties':{'path':{'type':'string'}}}}}]}
        before = json.loads(json.dumps(payload))
        converted = responses_count_payload(payload)
        self.assertEqual(payload,before)
        self.assertEqual(converted['input'][0],{'type':'message','role':'system','content':'完整规则'})
        self.assertEqual(converted['input'][1]['content'],[
            {'type':'input_text','text':'解释图片'},
            {'type':'input_image','image_url':'data:image/png;base64,AAAA','detail':'high'}])
        self.assertEqual(converted['input'][2],{'type':'function_call','call_id':'call-1',
            'name':'read','arguments':'{"path":"original"}'})
        self.assertEqual(converted['input'][3],{'type':'function_call_output','call_id':'call-1','output':'完整原文'})
        self.assertEqual(converted['tools'],[{'type':'function',**payload['tools'][0]['function']}])

    async def test_response_conversion_rejects_unaccounted_content(self):
        from agent_service.model_context import responses_count_payload
        for message in [
            {'role':'user','content':[{'type':'input_audio','input_audio':{'data':'AAAA'}}]},
            {'role':'user','name':{'invalid':'named metadata'},'content':'test'},
        ]:
            with self.subTest(message=message),self.assertRaises(ValueError):
                responses_count_payload({'model':'any','messages':[message]})
        with self.assertRaises(ValueError):
            responses_count_payload({'model':'any','messages':[], 'chat_template':'private template'})

    async def test_pptd_request_uses_server_schema_with_source_read_alternative(self):
        from agent_service.pptd_generation import _ask
        from agent_service.kimi_rules import KimiRules
        model, seen = await self.model(server_limit=262144, PPT_LLM_CONTEXT_MODE='auto', PPT_LLM_CONTEXT_TOKENS='')
        result = await _ask(model,'repair',KimiRules(),{'request':'修复页面','_contextSources':{'source-1':'original'}})
        self.assertEqual(result,{'data':{}})
        sent = next(body for path,body in seen if path.endswith('completions'))
        contract = sent.get('response_format',{}).get('json_schema',{}).get('schema',{})
        options = contract.get('anyOf',[])
        self.assertTrue(any('data' in item.get('required',[]) for item in options))
        self.assertTrue(any('contextRequest' in item.get('required',[]) for item in options))

    async def test_concurrent_response_schemas_do_not_leak_across_requests(self):
        model, seen = await self.model(server_limit=4096, PPT_LLM_CONTEXT_MODE='auto', PPT_LLM_CONTEXT_TOKENS='')
        ready = asyncio.Event()
        async def call(key):
            schema={'type':'object','properties':{key:{'type':'string'}},'required':[key]}
            with model.json_response(schema):
                if key=='left': await ready.wait()
                else: ready.set()
                await model(messages=[Msg(name='user',role='user',content=[{'type':'text','text':key}])])
        await asyncio.gather(call('left'),call('right'))
        for path,body in seen:
            if path.endswith('completions'):
                key=body['messages'][-1]['content']
                if isinstance(key,list):key=key[0]['text']
                self.assertEqual(body['response_format']['json_schema']['schema']['required'],[key])
        await model(messages=[Msg(name='user',role='user',content=[{'type':'text','text':'plain'}])])
        self.assertNotIn('response_format',seen[-1][1])

    async def model(self, count=800, server_limit=1024, **settings):
        env = {'PPT_LLM_PROVIDER': 'openai', 'PPT_LLM_MODEL': 'local',
               'OPENAI_API_KEY': 'unit-test-secret', 'OPENAI_BASE_URL': 'http://model.local/v1',
               'PPT_LLM_STREAM': 'false', 'PPT_LLM_EXTRA_BODY': '{"chat_template_kwargs":{"enable_thinking":false}}',
               'PPT_LLM_MAX_TOKENS': '100', 'PPT_LLM_CONTEXT_TOKENS': '1024',
               'PPT_LLM_CONTEXT_MODE': 'configured',
               'PPT_LLM_CONTEXT_MARGIN': '25', 'PPT_LLM_TOKENIZER_URL': 'http://model.local/tokenize',
               **settings}
        with patch.dict(os.environ, env):
            model = make_model(output_mode='auto', observable_retries=True)
        self.addAsyncCleanup(model.client.close)
        seen = []
        def transport(req):
            payload = json.loads(req.content) if req.content else {}; seen.append((req.url.path, payload))
            if req.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [{'id': 'unrelated', 'max_model_len': 999999}, {'id': 'local', 'max_model_len': server_limit, 'owned_by': 'vllm'}]})
            if req.url.path == '/tokenize':
                if count == 'unavailable':
                    return httpx.Response(503, json={'error': 'PRIVATE SOURCE unit-test-secret'})
                return httpx.Response(200, json={'count': count, 'max_model_len': server_limit})
            return httpx.Response(200, json={'id': 'test', 'object': 'chat.completion', 'created': 1,
                'model': 'local', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '{"data":{}}'}, 'finish_reason': 'stop'}]})
        await model.client.close()
        model.client = AsyncOpenAI(api_key='unit-test-secret', base_url='http://model.local/v1',
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        self.addAsyncCleanup(model.client.close)
        return model, seen

    async def test_budget_boundary_reaches_generation_only_when_output_and_margin_fit(self):
        for count, allowed in [(899, True), (900, False)]:
            with self.subTest(count=count):
                model, seen = await self.model(count=count)
                error = None
                try:
                    await model(messages=[Msg(name='user', role='user', content=[{'type': 'text', 'text': 'PRIVATE SOURCE'}])])
                except ValueError as exc:
                    error = exc
                self.assertEqual(any(path == '/v1/chat/completions' for path, _ in seen), allowed)
                if not allowed:
                    self.assertIsNotNone(error)
                    message, diagnostic, _ = diagnose_error(error)
                    self.assertEqual(diagnostic['code'], 'model_context_limit')
                    self.assertNotIn('PRIVATE SOURCE', message)
                    self.assertEqual(diagnostic['budget']['inputTokens'], 900)

    async def test_smaller_server_window_takes_precedence(self):
        model, seen = await self.model(count=800, server_limit=900)
        with self.assertRaises(ValueError):
            await model(messages=[Msg(name='user', role='user', content=[{'type': 'text', 'text': 'data'}])])
        self.assertFalse(any(p.endswith('completions') for p, _ in seen))

    async def test_unavailable_tokenizer_fails_without_generation_or_secret_disclosure(self):
        model, seen = await self.model(count='unavailable')
        with self.assertRaises(ValueError) as caught:
            await model(messages=[Msg(name='user', role='user', content=[{'type': 'text', 'text': 'data'}])])
        message, diagnostic, _ = diagnose_error(caught.exception)
        self.assertEqual(diagnostic['code'], 'model_context_unavailable')
        self.assertNotIn('PRIVATE', message)
        self.assertNotIn('unit-test-secret', message)
        self.assertFalse(any(p.endswith('completions') for p, _ in seen))

    async def test_tokenizer_cannot_redirect_original_messages_to_another_origin(self):
        model, _ = await self.model()
        await model.client.close(); seen = []
        def transport(req):
            seen.append(req.url.host)
            if req.url.host == 'model.local':
                return httpx.Response(307,headers={'location':'http://other.local/tokenize'})
            return httpx.Response(200,json={'count':1,'max_model_len':1024})
        model.client = AsyncOpenAI(api_key='test',base_url='http://model.local/v1',
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport),follow_redirects=True))
        self.addAsyncCleanup(model.client.close)
        with self.assertRaises(ValueError):
            await model.count_tokens([Msg(name='user',role='user',content=[{'type':'text','text':'PRIVATE SOURCE'}])])
        self.assertEqual(seen,['model.local'])

    async def test_agent_checks_before_automatic_compression_and_keeps_full_system(self):
        model, seen = await self.model(count=930)
        agent = make_agent('BudgetTest', 'IMMUTABLE SYSTEM RULES', model)
        with self.assertRaises(ValueError):
            await agent.reply(Msg(name='user', role='user', content=[{'type': 'text', 'text': 'Required facts'}]))
        self.assertFalse(any(p.endswith('completions') for p, _ in seen))

    async def test_exact_tokenizer_sees_wire_messages_and_template_flags(self):
        model, seen = await self.model()
        await model(messages=[Msg(name='system', role='system', content=[{'type': 'text', 'text': 'All original rules'}]),
                              Msg(name='user', role='user', content=[{'type': 'text', 'text': 'Chinese 中文 request'}])])
        tokens = next(body for path, body in seen if path == '/tokenize')
        sent = next(body for path, body in seen if path.endswith('completions'))
        self.assertEqual(tokens['messages'], sent['messages'])
        self.assertEqual(tokens['chat_template_kwargs'], {'enable_thinking': False})

    async def test_changed_history_is_counted_again(self):
        model, seen = await self.model()
        messages = [Msg(name='user', role='user', content=[{'type': 'text', 'text': 'first'}])]
        await model.count_tokens(messages, [])
        await model.count_tokens(messages, [])
        await model.count_tokens([*messages, Msg(name='user', role='user', content=[{'type': 'text', 'text': 'repair'}])], [])
        self.assertEqual(sum(path == '/tokenize' for path, _ in seen), 2)

    async def test_auto_discovers_selected_model_limit_without_fixed_window(self):
        model, seen = await self.model(count=1800, server_limit=4096,
            PPT_LLM_CONTEXT_MODE='auto', PPT_LLM_CONTEXT_TOKENS='', PPT_LLM_TOKENIZER_URL='')
        await model(messages=[Msg(name='user', role='user', content=[{'type':'text','text':'payload'}])])
        self.assertEqual(model.context_size, 4096)
        self.assertEqual(model.context_info['source'], 'model-api')
        self.assertTrue(any(path == '/tokenize' for path, _ in seen))
        self.assertTrue(any(path.endswith('completions') for path, _ in seen))
        another, _ = await self.model(count=1800, server_limit=2048,
            PPT_LLM_CONTEXT_MODE='auto', PPT_LLM_CONTEXT_TOKENS='', PPT_LLM_TOKENIZER_URL='')
        await another.count_tokens([Msg(name='user', role='user', content=[{'type':'text','text':'payload'}])], [])
        self.assertEqual(another.context_size, 2048)

    async def test_cross_origin_tokenizer_and_invalid_window_are_rejected(self):
        for settings in ({'PPT_LLM_TOKENIZER_URL': 'http://unrelated.local/tokenize'},
                         {'PPT_LLM_CONTEXT_TOKENS': '0'}, {'PPT_LLM_CONTEXT_MARGIN': '-1'},
                         {'PPT_LLM_EXTRA_BODY':json.dumps({'tools':[{'type':'function','function':{'name':'oversized','description':'x'*20000}}]})}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                await self.model(**settings)

    async def test_small_discovered_window_adjusts_output_and_safety_on_the_wire(self):
        model, seen = await self.model(count=1000, server_limit=4096,
            PPT_LLM_CONTEXT_MODE='auto', PPT_LLM_CONTEXT_TOKENS='',
            PPT_LLM_CONTEXT_MARGIN='8192', PPT_LLM_MAX_TOKENS='32000')
        await model(messages=[Msg(name='user', role='user', content=[{'type':'text','text':'payload'}])])
        sent = next(body for path, body in seen if path.endswith('completions'))
        self.assertEqual(sent['max_completion_tokens'], 1024)
        self.assertEqual(model.context_info['safetyTokens'], 128)

    async def test_missing_metadata_is_labelled_and_output_cap_is_respected(self):
        model, _ = await self.model(PPT_LLM_CONTEXT_MODE='auto', PPT_LLM_TOKENIZER_URL='')
        from unittest.mock import AsyncMock, MagicMock
        getter = AsyncMock(return_value={'data':[{'id':'local'}]})
        with patch.object(model.client, 'with_options', return_value=MagicMock(get=getter)):
            info = await model.discover_context()
        self.assertEqual(info['source'], 'configured')
        self.assertIn('discoveryWarning', info)
        another, sent_requests = await self.model(PPT_LLM_CONTEXT_MODE='auto', PPT_LLM_CONTEXT_TOKENS='', PPT_LLM_TOKENIZER_URL='')
        getter = AsyncMock(return_value={'data':[{'id':'local','context_length':4096,'max_output_tokens':50}]})
        with patch.object(another.client, 'with_options', return_value=MagicMock(get=getter)):
            info = await another.discover_context()
        self.assertEqual(another.parameters.max_tokens, 50)
        self.assertEqual(info['contextTokens'], 4096)
        await another(messages=[Msg(name='user',role='user',content=[{'type':'text','text':'request'}])], max_completion_tokens=400)
        sent = next(body for path,body in sent_requests if path.endswith('completions'))
        self.assertEqual(sent['max_completion_tokens'],50)

    async def test_anthropic_discovers_and_counts_the_same_wire_system_and_messages(self):
        from anthropic import AsyncAnthropic
        import httpx2 as httpx
        with patch.dict(os.environ, {'PPT_LLM_PROVIDER':'anthropic', 'ANTHROPIC_API_KEY':'test',
                'ANTHROPIC_BASE_URL':'http://anthropic.local', 'PPT_LLM_MODEL':'local',
                'PPT_LLM_CONTEXT_MODE':'auto', 'PPT_LLM_CONTEXT_TOKENS':'',
                'PPT_LLM_CONTEXT_MARGIN':'8192', 'PPT_LLM_MAX_TOKENS':'32000', 'PPT_LLM_TOKENIZER_URL':''}):
            model = make_model(observable_retries=True)
        await model.client.close()
        seen = []
        def transport(req):
            payload = json.loads(req.content) if req.content else {}; seen.append((req.url.path, payload))
            if req.url.path == '/v1/models/local':
                return httpx.Response(200,json={'id':'local','type':'model','display_name':'Local',
                    'created_at':'2026-01-01T00:00:00Z','max_input_tokens':4096,'max_tokens':1000})
            if req.url.path == '/v1/messages/count_tokens':
                return httpx.Response(200,json={'input_tokens':1500})
            return httpx.Response(200,json={'id':'test','type':'message','model':'local','role':'assistant',
                'content':[{'type':'text','text':'ok'}], 'stop_reason':'end_turn','stop_sequence':None,
                'usage':{'input_tokens':1500,'output_tokens':1}})
        model.client = AsyncAnthropic(api_key='test',base_url='http://anthropic.local',
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        self.addAsyncCleanup(model.client.close)
        await model(messages=[Msg(name='system',role='system',content=[{'type':'text','text':'FULL SYSTEM'}]),
                              Msg(name='user',role='user',content=[{'type':'text','text':'user request'}])])
        self.assertEqual(model.context_size,4096)
        counted = next(body for path,body in seen if path.endswith('count_tokens'))
        sent = next(body for path,body in seen if path == '/v1/messages')
        self.assertEqual(sent['system'],counted['system'])
        self.assertEqual(sent['messages'],counted['messages'])
        self.assertEqual(sent['max_tokens'],1000)


if __name__ == '__main__':
    unittest.main()
