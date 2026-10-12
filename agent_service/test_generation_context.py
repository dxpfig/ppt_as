import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from agentscope.message import Msg

from agent_service.generation_context import page_context, read_context, prepare_materials, split_text


class GenerationContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_failure_keeps_budget_and_does_not_retry_tokenizer_timeout(self):
        from agent_service import pptd_generation as g
        from agent_service.kimi_rules import KimiRules
        from agent_service.model_context import ContextBudgetError, ContextCountError
        from agent_service.job_execution import diagnose_error
        budget = {'inputTokens':950,'outputTokens':100,'safetyTokens':25,'contextLimit':1024,'inputLimit':899}
        for error in (ContextBudgetError(budget), ContextCountError()):
            error.__cause__ = TimeoutError('PRIVATE TOKENIZER DETAILS')
            calls = []
            class Failing:
                async def reply(self, message):
                    calls.append(1)
                    raise error
            with patch.object(g,'make_agent',return_value=Failing()), self.assertRaises(g.GenerationModelError) as caught:
                await g._ask(SimpleNamespace(model='local'),'page',KimiRules(),{'pageIndex':3})
            message, diagnostic, _ = diagnose_error(caught.exception)
            self.assertEqual(len(calls),1)
            self.assertEqual(diagnostic['pageIndex'],3)
            self.assertNotIn('PRIVATE',message)
            if isinstance(error,ContextBudgetError): self.assertEqual(diagnostic['budget'],budget)

    async def test_generation_prepares_long_material_before_plan_and_saves_original(self):
        from agent_service import pptd_generation as g
        from agent_service.test_pptd_generation import request, bundle
        source = bundle(); original = '准确收入 12 万。' * 4000; states = []; roles = []
        plan = {'title':'Deck','narrative':'Explain','scenario':'education-training','inputType':'document',
                'document':source['document'],'pages':[{'title':'Hello','readerTask':'Explain','content':'Hello','visualPlan':'Type'}]}
        async def ask(model, role, rules, data, **kwargs):
            roles.append(role)
            if role == 'source': return {'excerpts':[data['source']['text'][:50]]}
            self.assertIn('materials',data)
            if role == 'plan': return plan
            return {'data':source['pages'][0]['data']}
        async def close(): pass
        with patch.object(g,'make_model',return_value=SimpleNamespace(client=SimpleNamespace(close=close))), patch.object(g,'_ask',side_effect=ask):
            await g.generate_pptd(request(references=original),lambda *a:None,save_checkpoint=states.append)
        self.assertEqual(roles[0],'source')
        self.assertEqual(next(iter(states[-1]['materialSources'].values())),original)
        self.assertEqual(len(states[-1]['pages']),1)

    async def test_original_source_can_be_requested_before_final_json(self):
        from agent_service import pptd_generation as g
        from agent_service.kimi_rules import KimiRules
        received = []
        class Reader:
            async def reply(self, message):
                data = json.loads(message.get_text_content()); received.append(data)
                text = '{"contextRequest":{"id":"source-1","offset":0,"length":20}}' if len(received) == 1 else '{"data":{"elements":[]}}'
                return Msg(name='assistant', role='assistant', content=[{'type':'text','text':text}])
        with patch.object(g, 'make_agent', return_value=Reader()):
            result = await g._ask(SimpleNamespace(model='local'), 'page', KimiRules(),
                {'request': 'Exact user requirement', '_contextSources': {'source-1':'原文利润 12.3%。', 'unrequested':'Never send this'}})
        self.assertEqual(result, {'data': {'elements': []}})
        self.assertEqual(received[1]['contextReads'][0]['text'], '原文利润 12.3%。')
        self.assertEqual(received[1]['request'], 'Exact user requirement')
        self.assertNotIn('Never send this', json.dumps(received))

    async def test_format_retry_replays_complete_request_without_accumulating_bad_reply(self):
        from agent_service import pptd_generation as g
        from agent_service.kimi_rules import KimiRules
        received = []
        class Writer:
            async def reply(self, message):
                received.append(message.get_text_content())
                return Msg(name='assistant', role='assistant', content=[{'type':'text', 'text':
                    'INVALID RAW OUTPUT' if len(received) == 1 else '{"data":{}}'}])
        with patch.object(g, 'make_agent', side_effect=lambda *args: Writer()):
            await g._ask(SimpleNamespace(model='local'), 'page', KimiRules(), {'request': 'MUST KEEP FACT 123'})
        retry = json.loads(received[1])
        self.assertEqual(retry['request'], 'MUST KEEP FACT 123')
        self.assertNotIn('INVALID RAW OUTPUT', received[1])

    def deck(self):
        paths = [f'pages/{i}.page' for i in range(50)]
        plan = {'document': {'pages': paths}, 'pages': [
            {'title': f'Page {i}', 'readerTask': 'Explain', 'content': f'Immutable facts {i}', 'visualPlan': 'Cards'}
            for i in range(50)]}
        complete = {path: {'path': path, 'data': {'elements': [{'text': f'Body {i}', 'geometry': 'x' * 2000}]}}
                    for i, path in enumerate(paths)}
        return plan, complete

    async def test_window_is_bounded_ordered_and_original_pages_remain_readable(self):
        plan, complete = self.deck()
        before = copy.deepcopy(complete)
        # Deliberately use reverse completion order, including future pages.
        data = page_context({'request': 'Original requirement'}, plan, dict(reversed(list(complete.items()))), 40)
        self.assertEqual([p['path'] for p in data['previousPages']], ['pages/38.page', 'pages/39.page'])
        self.assertEqual(data['plan'], plan)
        self.assertEqual(data['request'], 'Original requirement')
        self.assertEqual(len(data['pageCatalog']), 40)
        entry = next(p for p in data['pageCatalog'] if p['path'] == 'pages/0.page')
        read = read_context(data['_contextSources'], {'id': entry['sourceId'], 'offset': 0, 'length': 12000})
        self.assertEqual(json.loads(read['text']), complete['pages/0.page'])
        self.assertEqual(complete, before)
        sent = {k: v for k, v in data.items() if k != '_contextSources'}
        self.assertLess(len(json.dumps(sent)), len(json.dumps(complete)) // 3)

    async def test_reads_cannot_escape_registry_or_accept_unbounded_slices(self):
        for query in ({'id': '../.env', 'offset': 0, 'length': 10},
                      {'id': 'safe', 'offset': -1, 'length': 10},
                      {'id': 'safe', 'offset': 0, 'length': 999999},
                      {'id': 'safe', 'offset': 100, 'length': 1}):
            with self.subTest(query=query), self.assertRaises(ValueError):
                read_context({'safe': 'source'}, query)

    async def test_material_chunks_are_lossless_and_resume_keeps_successful_extractions(self):
        original = ''.join(f'事实{i}：利润 12.3%，禁止修改单位。\n' for i in range(3000))
        self.assertEqual(''.join(split_text(original)), original)
        context = {'request': '汇总财务数据', 'designSystem': 'Exact design', 'scenario': 'management-report',
                   'references': original, 'researchResults': {'sources': [{'text': original, 'url': 'source://original'}]}}
        before = copy.deepcopy(context); state = {}; saved = []; seen = []
        async def save(): saved.append(copy.deepcopy(state))
        async def interrupted(role, data, **kwargs):
            seen.append(data['source']['id'])
            if len(seen) == 2: raise TimeoutError()
            return {'excerpts': [data['source']['text'][:100]]}
        with self.assertRaises(TimeoutError):
            await prepare_materials(context, state, interrupted, save)
        self.assertEqual(len(state['materialDigests']), 1)
        processed = seen[0]; resumed = []
        async def finish(role, data, **kwargs):
            resumed.append(data['source']['id'])
            return {'excerpts': [data['source']['text'][:100]]}
        result = await prepare_materials(context, state, finish, save)
        self.assertNotIn(processed, resumed)
        self.assertEqual(context, before)
        self.assertEqual(result['request'], context['request'])
        self.assertEqual(result['designSystem'], 'Exact design')
        self.assertEqual(len(result['materials']), 1)  # Duplicate reference source shares one digest.
        entry = result['materials'][0]
        self.assertEqual(result['_contextSources'][entry['sourceId']], original)
        self.assertLess(len(json.dumps({k:v for k,v in result.items() if k != '_contextSources'})), len(original))

    async def test_invented_excerpts_are_not_saved_or_used(self):
        context = {'request': '分析', 'references': '确切收入 10 万。' * 4000}
        state = {}
        async def ask(*a, **k): return {'excerpts': ['确切收入 999 万。']}
        async def save(): pass
        with self.assertRaises(ValueError):
            await prepare_materials(context, state, ask, save)
        self.assertEqual(state.get('materialDigests'), {})

    async def test_authoritative_document_is_not_replaced_with_excerpt_references(self):
        context = {'request':'修改颜色','existingDocument':{'pages':[{'text':'原文' * 13000}]}}
        before = copy.deepcopy(context)
        async def ask(*a, **k): self.fail('Existing document must remain verbatim')
        async def save(): pass
        result = await prepare_materials(context,{},ask,save)
        self.assertEqual(result,before)


if __name__ == '__main__': unittest.main()
