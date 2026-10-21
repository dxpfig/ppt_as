"""Visual publication gate tests exercise real image encoding and provider wire bodies."""
import asyncio
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import httpx2
from PIL import Image


def state():
    return {'revision': 7, 'document': {'title': 'Demo', 'size': [960, 540], 'pages': ['pages/a.page', 'pages/b.page']}, 'pages': [
        {'path': 'pages/a.page', 'data': {'elements': [{'elementId': 'title-a', 'elementType': 'text', 'bounds': [30, 30, 800, 80], 'content': {'text': 'Hello A', 'fontSize': 32}}]}},
        {'path': 'pages/b.page', 'data': {'elements': [{'elementId': 'title-b', 'elementType': 'text', 'bounds': [30, 30, 800, 80], 'content': {'text': 'Hello B', 'fontSize': 32}}]}},
    ]}


def verdict(paths, issues=None):
    return {'status': 'issues' if issues else 'passed', 'coveredPages': paths, 'observations': [{'pagePath': p, 'evidence': '深色标题位于左上角，背景简洁，文字可辨。'} for p in paths], 'issues': issues or []}


class Availability(unittest.TestCase):
    def test_visual_gate_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('agent_service.visual_review'), 'The automatic visual review gate has not been implemented')


class ConfigurationTests(unittest.TestCase):
    def test_same_model_vision_inherits_request_controls_but_other_models_do_not(self):
        from agent_service.visual_review import vision_extra_body, VisionConfig
        env = {'PPT_LLM_PROVIDER': 'openai', 'PPT_LLM_MODEL': 'local', 'OPENAI_BASE_URL': 'http://local/v1',
               'PPT_LLM_EXTRA_BODY': '{"chat_template_kwargs":{"enable_thinking":false}}'}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(vision_extra_body(VisionConfig('openai', 'local', 'key', 'http://local/v1/')),
                             {'chat_template_kwargs': {'enable_thinking': False}})
            self.assertEqual(vision_extra_body(VisionConfig('openai', 'different', 'key', 'http://local/v1')), {})
            self.assertEqual(vision_extra_body(VisionConfig('openai', 'local', 'key', 'http://other/v1')), {})

    def test_vision_controls_cannot_override_images_or_model(self):
        from agent_service.visual_review import vision_extra_body, VisionConfig, VisualReviewError
        for value in ('{"messages":[]}', '{"model":"other"}', '[]', 'bad json'):
            with self.subTest(value=value), patch.dict(os.environ, {'PPT_VISION_EXTRA_BODY': value}, clear=True):
                with self.assertRaises(VisualReviewError):
                    vision_extra_body(VisionConfig('openai', 'vision', 'key'))

    def test_explicit_vision_provider_never_uses_other_provider_credentials(self):
        from agent_service.visual_review import vision_configuration, VisualReviewError
        with patch.dict(os.environ, {'PPT_LLM_PROVIDER': 'anthropic', 'PPT_LLM_MODEL': 'text-only', 'ANTHROPIC_API_KEY': 'anthro', 'PPT_VISION_PROVIDER': 'openai', 'PPT_VISION_MODEL': 'vision'}, clear=True):
            with self.assertRaises(VisualReviewError): vision_configuration()
        with patch.dict(os.environ, {'PPT_LLM_PROVIDER': 'openai', 'PPT_LLM_MODEL': 'vision-capable', 'OPENAI_API_KEY': 'key', 'OPENAI_BASE_URL': 'http://provider.test/v1'}, clear=True):
            config = vision_configuration()
            self.assertEqual((config.provider, config.model, config.base_url), ('openai', 'vision-capable', 'http://provider.test/v1'))


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def test_passed_vision_capability_is_cached_per_endpoint(self):
        from agent_service.visual_review import (
            VisionClient,
            VisionConfig,
            _CAPABILITY_CACHE,
            _CAPABILITY_LOCKS,
        )

        _CAPABILITY_CACHE.clear()
        _CAPABILITY_LOCKS.clear()
        with TemporaryDirectory() as folder, patch(
            'agent_service.visual_review.AsyncOpenAI'
        ), patch('agent_service.visual_review.secrets.choice', return_value='2'):
            reviewer = VisionClient(
                VisionConfig('openai', 'vision', 'key', 'http://provider.test/v1')
            )
            reviewer.ask = AsyncMock(
                return_value={
                    'digits': '222222',
                    'color': 'red',
                    'shape': 'circle',
                }
            )
            first = await reviewer.verify_vision(folder)
            second = await reviewer.verify_vision(folder)

        self.assertEqual(first['method'], 'random-pixel-challenge')
        self.assertEqual(second['method'], 'random-pixel-challenge')
        self.assertTrue(second['cached'])
        reviewer.ask.assert_awaited_once()

    async def test_openai_wire_contains_actual_image_bytes_and_no_hidden_retries(self):
        from agent_service.visual_review import VisionClient, VisionConfig
        seen = []
        async def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={'id': 'vision-1', 'object': 'chat.completion', 'created': 1, 'model': 'vision', 'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{"digits":"7284"}'}}]})
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key='secret-not-in-prompts', base_url='http://provider.test/v1', max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with TemporaryDirectory() as folder:
            image = Path(folder) / 'one.png'; Image.new('RGB', (64, 32), 'red').save(image)
            with patch('agent_service.visual_review.AsyncOpenAI', return_value=client), patch.dict(os.environ, {
                'PPT_LLM_PROVIDER': 'openai', 'PPT_LLM_MODEL': 'vision', 'OPENAI_BASE_URL': 'http://provider.test/v1',
                'PPT_LLM_EXTRA_BODY': '{"chat_template_kwargs":{"enable_thinking":false}}'}, clear=True):
                async with VisionClient(VisionConfig('openai', 'vision', 'key', 'http://provider.test/v1')) as reviewer:
                    result = await reviewer.ask('System', 'Read pixels.', [image])
            self.assertEqual(result, {'digits': '7284'})
            content = seen[0]['messages'][1]['content']
            self.assertEqual(content[1]['type'], 'image_url')
            self.assertEqual(base64.b64decode(content[1]['image_url']['url'].split(',', 1)[1]), image.read_bytes())
            self.assertEqual(content[1]['image_url']['detail'], 'high')
            self.assertEqual(seen[0]['chat_template_kwargs'], {'enable_thinking': False})
            self.assertNotIn('secret-not-in-prompts', json.dumps(seen))
            self.assertTrue(client.is_closed())

    async def test_anthropic_wire_contains_image_source_and_checks_refusal(self):
        from agent_service.visual_review import VisionClient, VisionConfig, VisualReviewError
        seen = []
        async def handler(request):
            seen.append(json.loads(request.content))
            return httpx2.Response(200, json={'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': 'vision', 'content': [{'type': 'text', 'text': 'I cannot view images.'}], 'stop_reason': 'end_turn', 'stop_sequence': None, 'usage': {'input_tokens': 10, 'output_tokens': 10}})
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key='secret', base_url='http://provider.test', max_retries=0, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
        with TemporaryDirectory() as folder:
            image = Path(folder) / 'one.png'; Image.new('RGB', (64, 32), 'red').save(image)
            with patch('agent_service.visual_review.AsyncAnthropic', return_value=client):
                async with VisionClient(VisionConfig('anthropic', 'vision', 'key', 'http://provider.test')) as reviewer:
                    with self.assertRaises(VisualReviewError): await reviewer.ask('System', 'Read pixels.', [image])
            block = seen[0]['messages'][0]['content'][1]
            self.assertEqual(block['type'], 'image')
            self.assertEqual(base64.b64decode(block['source']['data']), image.read_bytes())
            self.assertTrue(client.is_closed())


    async def test_random_pixel_challenge_rejects_text_only_model(self):
        from agent_service.visual_review import VisionClient, VisionConfig, VisualReviewError
        from openai import AsyncOpenAI
        bodies = []
        async def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={'id': 'vision-1', 'object': 'chat.completion', 'created': 1, 'model': 'vision', 'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{"digits":"000000","color":"red","shape":"circle"}'}}]})
        client = AsyncOpenAI(api_key='key', base_url='http://provider.test/v1', max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with TemporaryDirectory() as folder, patch('agent_service.visual_review.AsyncOpenAI', return_value=client), patch('agent_service.visual_review.secrets.choice', return_value='2'):
            async with VisionClient(VisionConfig('openai', 'vision', 'key')) as reviewer:
                with self.assertRaises(VisualReviewError) as raised:
                    await reviewer.verify_vision(folder)
            self.assertEqual(raised.exception.code, 'vision_unavailable')
            self.assertNotIn('222222', bodies[0]['messages'][1]['content'][0]['text'])
            self.assertTrue((Path(folder)/'vision-capability.png').exists())

class SerializationPreservationTests(unittest.TestCase):
    def test_string_whitespace_cannot_change_status_ids_or_evidence(self):
        from agent_service.visual_review import _validate_serialization, VisualReviewError
        cases = [
            ('{"status":"pa ssed","evidence":"标题"测试"可见"}', {'status': 'passed', 'evidence': '标题"测试"可见'}),
            ('{"elementIds":["t itle"],"evidence":"标题"测试"可见"}', {'elementIds': ['title'], 'evidence': '标题"测试"可见'}),
            ('{"evidence":"A B"}', {'evidence': 'AB'}),
            ('{"evidence":"A\\nB"}', {'evidence': 'AB'}),
            ('{"evidence":"A\\u0020B"}', {'evidence': 'AB'}),
        ]
        for original, corrected in cases:
            with self.subTest(original=original), self.assertRaises(VisualReviewError):
                _validate_serialization(original, corrected)

    def test_only_structural_whitespace_and_string_escaping_may_change(self):
        from agent_service.visual_review import _validate_serialization
        original = '{ \n "status" : "passed", "evidence" : "标题"A B"可见\n保留 换行", "pageIndex": 9 }'
        corrected = {'status': 'passed', 'evidence': '标题"A B"可见\n保留 换行', 'pageIndex': 9}
        self.assertEqual(_validate_serialization(original, corrected), corrected)
        self.assertEqual(_validate_serialization('{"evidence":"A\\u0020B"}', {'evidence': 'A B'}), {'evidence': 'A B'})

    def test_whitespace_inside_an_unquoted_number_is_not_structural(self):
        from agent_service.visual_review import _validate_serialization, VisualReviewError
        with self.assertRaises(VisualReviewError): _validate_serialization('{"pageIndex": 1 2}', {'pageIndex': 12})


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory(); self.folder = Path(self.tmp.name)
        for n, color in ((1, 'red'), (2, 'blue')): Image.new('RGB', (960, 540), color).save(self.folder / f'slide-{n}.png')
        self.responses = []; self.calls = []; self.closed = False
    async def asyncTearDown(self): self.tmp.cleanup()
    async def run_review(self, responses, **kwargs):
        from agent_service import visual_review as module
        from agent_service.visual_review import VisionConfig
        owner = self
        class Client:
            requests = 0
            async def verify_vision(self, folder): return {'status': 'passed', 'method': 'test-transport'}
            async def __aenter__(self): return self
            async def __aexit__(self, *args): owner.closed = True
            async def ask(self, system, prompt, images):
                owner.calls.append({'prompt': json.loads(prompt) if prompt.startswith('{') else prompt, 'images': images, 'system': system})
                result = responses.pop(0)
                if isinstance(result, BaseException): raise result
                return copy.deepcopy(result)
        events = []
        with patch.object(module, 'vision_configuration', return_value=VisionConfig('openai', 'vision', 'key', 'http://provider.test/v1')), patch.object(module, 'VisionClient', return_value=Client()):
            result = await module.review_deck(state(), self.folder, on_event=events.append, concurrency=1, **kwargs)
        return result, events
    async def test_every_page_has_high_resolution_image_in_addition_to_contact_sheet(self):
        result, events = await self.run_review([verdict(['pages/a.page', 'pages/b.page']), verdict(['pages/a.page']), verdict(['pages/b.page'])])
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['coveredPages'], ['pages/a.page', 'pages/b.page'])
        self.assertEqual(result['revision'], 7)
        self.assertEqual(len(result['images']), 2)
        self.assertTrue(all(len(item['sha256']) == 64 for item in result['images']))
        self.assertTrue(self.calls[0]['images'][0].name.startswith('contact-sheet-'))
        self.assertEqual([self.calls[1]['images'][0].name, self.calls[2]['images'][0].name], ['slide-1.png', 'slide-2.png'])
        self.assertTrue(self.closed)
        self.assertEqual(json.loads((self.folder/'visual-review.json').read_text())['status'], 'passed')
        self.assertTrue(events)

    async def test_page_reviews_use_bounded_concurrency_and_preserve_page_order(self):
        from agent_service import visual_review as module
        from agent_service.visual_review import VisionConfig
        active = 0
        maximum = 0

        class Client:
            requests = 0
            async def verify_vision(self, _folder): return {'status': 'passed'}
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return None
            async def ask(self, _system, prompt, _images):
                nonlocal active, maximum
                payload = json.loads(prompt)
                paths = [item['pagePath'] for item in payload['pages']]
                if payload['scope'] == 'deck':
                    return verdict(paths)
                active += 1
                maximum = max(maximum, active)
                try:
                    await asyncio.sleep(0.05)
                    return verdict(paths)
                finally:
                    active -= 1

        with patch.object(
            module,
            'vision_configuration',
            return_value=VisionConfig('openai', 'vision', 'key'),
        ), patch.object(module, 'VisionClient', return_value=Client()):
            result = await module.review_deck(state(), self.folder, concurrency=2)
        self.assertEqual(maximum, 2)
        self.assertEqual(result['coveredPages'], ['pages/a.page', 'pages/b.page'])
        self.assertEqual(
            [review['coveredPages'][0] for review in result['pageReviews']],
            ['pages/a.page', 'pages/b.page'],
        )
    async def test_partial_coverage_and_unknown_element_ids_never_pass(self):
        from agent_service.visual_review import VisualReviewError
        with self.assertRaises(VisualReviewError):
            await self.run_review([verdict(['pages/a.page']), verdict(['pages/a.page'])])
        self.assertFalse((self.folder/'visual-review.json').exists() and json.loads((self.folder/'visual-review.json').read_text())['status'] == 'passed')
        self.assertTrue(self.closed)
        self.calls.clear(); self.closed = False
        issue = {'pagePath': 'pages/a.page', 'pageIndex': 0, 'elementIds': ['invented'], 'severity': 'error', 'code': 'TextOverlap', 'message': '遮挡', 'evidence': '标题被遮挡', 'repair': '移动标题'}
        with self.assertRaises(VisualReviewError):
            await self.run_review([verdict(['pages/a.page','pages/b.page'], [issue]), verdict(['pages/a.page','pages/b.page'], [issue])])
    async def test_warnings_block_publication_and_have_precise_visual_evidence(self):
        issue = {'pagePath': 'pages/b.page', 'pageIndex': 1, 'elementIds': ['title-b'], 'severity': 'warning', 'code': 'PoorContrast', 'message': '标题对比度不足', 'evidence': '深蓝色背景上的蓝色标题难以辨识', 'repair': '提高标题与背景亮度差，保持原文'}
        result, _ = await self.run_review([verdict(['pages/a.page','pages/b.page']), verdict(['pages/a.page']), verdict(['pages/b.page'], [issue])])
        self.assertEqual(result['status'], 'issues'); self.assertEqual(result['issues'], [issue])
    async def test_empty_observations_and_unseen_image_fail_closed(self):
        from agent_service.visual_review import VisualReviewError
        bad = verdict(['pages/a.page', 'pages/b.page']); bad['observations'] = []
        with self.assertRaises(VisualReviewError): await self.run_review([bad, bad])
        (self.folder/'slide-2.png').unlink(); self.calls.clear()
        with self.assertRaises(VisualReviewError): await self.run_review([])
        self.assertEqual(self.calls, [])
    async def test_high_resolution_page_can_reject_wrong_contact_sheet_claim(self):
        issue = {'pagePath': 'pages/a.page', 'pageIndex': 0, 'elementIds': ['title-a'], 'severity': 'error', 'code': 'DarkBand', 'message': '标题被深色带遮挡', 'evidence': '总览中标题区域看似深色横带', 'repair': '去除深色带'}
        page = verdict(['pages/a.page']); page['deckFindingDecisions'] = [{'findingId': 'deck-0', 'decision': 'reject', 'evidence': '高清图标题背景实际为白色，标题清晰可见，无深色带。'}]
        result, _ = await self.run_review([verdict(['pages/a.page','pages/b.page'], [issue]), page, verdict(['pages/b.page'])])
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['issues'], [])
        self.assertEqual(result['deckReview']['issues'], [issue], 'The original claim remains auditable')
        self.assertEqual(self.calls[1]['prompt']['deckFindingsForPage'][0]['findingId'], 'deck-0')

    async def test_confirmed_deck_issue_cannot_disappear_when_page_omits_duplicate(self):
        issue = {'pagePath': 'pages/a.page', 'pageIndex': 0, 'elementIds': ['title-a'], 'severity': 'error', 'code': 'InvisibleTitle', 'message': '标题不可见', 'evidence': '白色背景上的白色标题不可见', 'repair': '把标题改为深色'}
        page = verdict(['pages/a.page']); page['status'] = 'issues'; page['deckFindingDecisions'] = [{'findingId': 'deck-0', 'decision': 'confirm', 'evidence': '高清渲染图上标题位置完全空白，源文本为白色。'}]
        result, _ = await self.run_review([verdict(['pages/a.page','pages/b.page'], [issue]), page, verdict(['pages/b.page'])])
        self.assertEqual(result['status'], 'issues')
        self.assertEqual(len(result['issues']), 1)
        self.assertEqual(result['issues'][0]['code'], 'InvisibleTitle')
        self.assertEqual(result['issues'][0]['evidence'], page['deckFindingDecisions'][0]['evidence'])

    async def test_unassessed_deck_finding_never_silently_disappears(self):
        from agent_service.visual_review import VisualReviewError
        issue = {'pagePath': 'pages/a.page', 'pageIndex': 0, 'elementIds': ['title-a'], 'severity': 'error', 'code': 'InvisibleTitle', 'message': '标题不可见', 'evidence': '白色背景上的白色标题不可见', 'repair': '把标题改为深色'}
        with self.assertRaises(VisualReviewError):
            await self.run_review([verdict(['pages/a.page','pages/b.page'], [issue]), verdict(['pages/a.page']), verdict(['pages/a.page'])])

    async def test_previous_review_context_reaches_all_pages_without_skipping_images(self):
        old = {'issues': [{'pagePath': 'pages/a.page', 'code': 'InvisibleTitle', 'message': '标题不可见'}], 'pageReviews': []}
        result, _ = await self.run_review([verdict(['pages/a.page','pages/b.page']), verdict(['pages/a.page']), verdict(['pages/b.page'])], previous_review=old)
        self.assertEqual(result['coveredPages'], ['pages/a.page','pages/b.page'])
        self.assertEqual(self.calls[1]['prompt']['previousReview']['issues'][0]['code'], 'InvisibleTitle')
        self.assertEqual(self.calls[2]['prompt']['previousReview']['issues'], [])

    async def test_history_cannot_reintroduce_old_deck_finding_identifiers(self):
        old = {'issues': [], 'pageReviews': [{'coveredPages': ['pages/a.page'], 'deckFindingDecisions': [{'findingId': 'deck-0', 'decision': 'reject', 'evidence': '上一轮深色背景假设不符合实际白色背景。'}]}]}
        await self.run_review([verdict(['pages/a.page','pages/b.page']), verdict(['pages/a.page']), verdict(['pages/b.page'])], previous_review=old)
        page_prompt = self.calls[1]['prompt']
        self.assertEqual(page_prompt['requiredDeckFindingIds'], [])
        self.assertNotIn('findingId', json.dumps(page_prompt['previousReview']))
        self.assertEqual(page_prompt['previousReview']['pageReviews'][0]['deckFindingDecisions'][0]['decision'], 'reject')

    async def test_review_output_contract_only_includes_current_page_decisions(self):
        issue = {'pagePath': 'pages/a.page', 'pageIndex': 0, 'elementIds': ['title-a'], 'severity': 'error', 'code': 'InvisibleTitle', 'message': '标题不可见', 'evidence': '总览中白色标题无法辨认', 'repair': '提高标题对比度'}
        page = verdict(['pages/a.page'])
        page['deckFindingDecisions'] = [{'findingId': 'deck-0', 'decision': 'reject', 'evidence': '高清图中标题清晰且与浅色背景对比充分。'}]
        old = {'issues': [], 'pageReviews': [{'coveredPages': ['pages/a.page'], 'deckFindingDecisions': [{'findingId': 'deck-old', 'decision': 'confirm', 'evidence': '这是上一轮的历史检查结论。'}]}]}
        await self.run_review([verdict(['pages/a.page','pages/b.page'], [issue]), page, verdict(['pages/b.page'])], previous_review=old)
        # The transport example must not ask a deck or unrelated page to invent
        # decisions. Only the page with a current finding receives a decision ID.
        for index in (0, 2):
            self.assertIn('"deckFindingDecisions":[]', self.calls[index]['system'])
            self.assertNotIn('"findingId":"deck-0"', self.calls[index]['system'])
        self.assertIn('"findingId":"deck-0"', self.calls[1]['system'])
        self.assertNotIn('pageReviews', self.calls[0]['prompt']['previousReview'])
        self.assertNotIn('deck-old', json.dumps(self.calls, default=str))

    async def test_json_syntax_failure_uses_one_lossless_small_serialization_call(self):
        from agent_service.visual_review import _strict_json, VisualReviewError
        original = verdict(['pages/a.page','pages/b.page'])
        original['observations'][0]['evidence'] = '标题"试点阶段"位于左上方，正文清楚。'
        malformed = json.dumps(original, ensure_ascii=False, separators=(',', ':')).replace('\\"', '"')
        try:
            _strict_json(malformed)
        except VisualReviewError as error:
            syntax_error = error
        else:
            self.fail('Unescaped quotes must never be accepted')
        result, _ = await self.run_review([syntax_error, original, verdict(['pages/a.page']), verdict(['pages/b.page'])])
        self.assertEqual(result['status'], 'passed')
        correction = self.calls[1]['prompt']
        self.assertIn('<broken_report>', correction)
        self.assertIn('转义', correction)
        self.assertLess(len(self.calls[1]['system']), 1200, 'Serialization must not repeat the full Kimi review instruction set')
        self.assertEqual(len(self.calls), 4)

    async def test_serialization_cannot_drop_a_finding_or_change_status(self):
        from agent_service.visual_review import _strict_json, VisualReviewError
        issue = {'pagePath': 'pages/a.page', 'pageIndex': 0, 'elementIds': ['title-a'], 'severity': 'error', 'code': 'InvisibleTitle', 'message': '标题不可见', 'evidence': '白色背景上的"标题"不可见', 'repair': '把标题改为深色'}
        original = verdict(['pages/a.page','pages/b.page'], [issue])
        malformed = json.dumps(original, ensure_ascii=False, separators=(',', ':')).replace('\\"', '"')
        try: _strict_json(malformed)
        except VisualReviewError as error: syntax_error = error
        with self.assertRaises(VisualReviewError):
            await self.run_review([syntax_error, verdict(['pages/a.page','pages/b.page'])])
        self.assertEqual(len(self.calls), 2, 'A failed formatter must not start more blind requests')
        self.assertEqual(json.loads((self.folder/'visual-review.json').read_text())['status'], 'failed')

    async def test_report_drops_unexpected_private_observation_fields(self):
        responses = [verdict(['pages/a.page','pages/b.page']), verdict(['pages/a.page']), verdict(['pages/b.page'])]
        for response in responses:
            for observation in response['observations']:
                observation.update(reasoning='private trace', privateThoughts={'internal': True})
        result, _ = await self.run_review(responses)
        for part in [result['deckReview'], *result['pageReviews']]:
            self.assertTrue(all(set(item) == {'pagePath', 'evidence'} for item in part['observations']))
        persisted = (self.folder/'visual-review.json').read_text()
        self.assertNotIn('private trace', persisted)
        self.assertNotIn('privateThoughts', persisted)

    async def test_oversized_observation_evidence_is_not_accepted(self):
        from agent_service.visual_review import VisualReviewError
        bad = verdict(['pages/a.page','pages/b.page']); bad['observations'][0]['evidence'] = '可见证据' * 1100
        with self.assertRaises(VisualReviewError): await self.run_review([bad, bad, verdict(['pages/a.page']), verdict(['pages/b.page'])])

    async def test_waiting_events_and_cancellation_cover_actual_pending_provider_call(self):
        from agent_service import visual_review as module
        from agent_service.visual_review import VisionConfig
        entered, cancelled, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
        owner = self
        class Client:
            requests = 0
            async def verify_vision(self, folder): return {'status': 'passed'}
            async def __aenter__(self): return self
            async def __aexit__(self, *_): owner.closed = True
            async def ask(self, *_):
                entered.set()
                try: await asyncio.Event().wait()
                finally: cancelled.set()
        events = []
        def event(value):
            events.append(value)
            if value['status'] == 'waiting': waiting.set()
        with patch.object(module, 'vision_configuration', return_value=VisionConfig('openai', 'vision', 'key')), patch.object(module, 'VisionClient', return_value=Client()), patch.object(module, 'VISUAL_WAIT_SECONDS', 0.01, create=True):
            task = asyncio.create_task(module.review_deck(state(), self.folder, on_event=event))
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                await asyncio.wait_for(waiting.wait(), timeout=0.3)
                self.assertTrue(any(item['status'] == 'waiting' and item['scope'] == 'deck' for item in events))
                self.assertTrue(all(set(item) <= {'phase', 'status', 'scope', 'pageIndex', 'pagePath', 'message', 'covered', 'total', 'issueCount'} for item in events))
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task, timeout=1)
            await asyncio.wait_for(cancelled.wait(), timeout=0.2)
        self.assertTrue(self.closed)
        self.assertEqual(json.loads((self.folder/'visual-review.json').read_text())['status'], 'cancelled')

    async def test_nonstring_coverage_is_a_bounded_invalid_review_not_type_error(self):
        from agent_service.visual_review import VisualReviewError
        bad = verdict(['pages/a.page','pages/b.page']); bad['coveredPages'] = [{'page': 'pages/a.page'}, 'pages/b.page']
        with self.assertRaises(VisualReviewError): await self.run_review([bad, bad])
        self.assertTrue(self.closed)

    async def test_small_font_is_only_a_hint_until_ai_confirms_visible_harm(self):
        from agent_service import visual_review as module
        source = state(); source['pages'][0]['data']['elements'][0]['content']['fontSize'] = 9
        with patch(__name__ + '.state', return_value=source):
            result, _ = await self.run_review([verdict(['pages/a.page','pages/b.page']), verdict(['pages/a.page']), verdict(['pages/b.page'])])
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(self.calls[1]['prompt']['sourceHints'][0]['elementId'], 'title-a')
        self.assertEqual(self.calls[1]['prompt']['sourceHints'][0]['kind'], 'small-font')

    async def test_provider_failure_and_cancellation_leave_recoverable_report(self):
        from agent_service.visual_review import VisualReviewError
        with self.assertRaises(VisualReviewError):
            await self.run_review([VisualReviewError('service down', code='vision_unavailable')])
        report = json.loads((self.folder/'visual-review.json').read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['failure']['code'], 'vision_unavailable')
        self.assertTrue(self.closed)
        self.closed = False
        with self.assertRaises(asyncio.CancelledError):
            await self.run_review([asyncio.CancelledError()])
        self.assertEqual(json.loads((self.folder/'visual-review.json').read_text())['status'], 'cancelled')
        self.assertTrue(self.closed)

    async def test_contradictory_pass_status_is_rejected(self):
        from agent_service.visual_review import VisualReviewError
        bad = verdict(['pages/a.page','pages/b.page']); bad['status'] = 'issues'
        with self.assertRaises(VisualReviewError): await self.run_review([bad, bad])


if __name__ == '__main__': unittest.main()
