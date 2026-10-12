"""Missing visuals may be added only from verifiable original sources."""
import copy
import unittest
import json
from unittest.mock import patch
from types import SimpleNamespace

from agent_service.test_pptd_generation import bundle
from agent_service.test_visual_repair import issue


def plan():
    return {'document': {'pages': ['pages/one.page']}, 'pages': [{
        'content': '首次通过率目标60%。每月复盘。',
        'visualPlan': '右侧放置三个月趋势示意图，不提供实测月度数据。'}]}


def completion_answer():
    return {'additions': [
        {'pagePath': 'pages/one.page', 'sourceId': 'plan:0:diagram', 'element': {
            'elementType': 'line', 'bounds': [520, 220, 300, 160],
            'viewBox': [1, 1], 'points': '0,1 0.5,0.6 1,0',
            'border': {'color': '#123B2F', 'width': 3, 'style': 'solid'}}},
        {'pagePath': 'pages/one.page', 'sourceId': 'plan:0:schematic-note', 'element': {
            'elementType': 'text', 'bounds': [520, 410, 320, 28],
            'content': {'fontSize': 14, 'color': '#333333'}}}], 'operations': []}


class CompletionSafety(unittest.TestCase):
    def test_schematic_has_explicit_provenance_and_preserves_original_fields(self):
        from agent_service.visual_completion import apply_completion
        source = bundle(); original = copy.deepcopy(source)
        result = apply_completion(source, plan(), completion_answer(), [issue(code='missing-visual-content')])
        self.assertEqual(source, original)
        self.assertEqual(result['document'], source['document'])
        elements = result['pages'][0]['data']['elements']
        self.assertEqual(elements[0], source['pages'][0]['data']['elements'][0])
        self.assertIn('非实测数据', elements[-1]['content']['text'])

    def test_invented_text_numbers_unknown_source_and_unlabelled_schematic_are_rejected(self):
        from agent_service.visual_completion import apply_completion
        candidates = []
        answer = completion_answer(); answer['additions'][-1]['element']['content']['text'] = 'M1=10 M2=20 M3=30'; candidates.append(answer)
        answer = completion_answer(); answer['additions'][0]['sourceId'] = 'unknown'; candidates.append(answer)
        answer = completion_answer(); answer['additions'].pop(); candidates.append(answer)
        answer = completion_answer(); answer['additions'][0]['element']['elementId'] = 'title'; candidates.append(answer)
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                apply_completion(bundle(), plan(), candidate, [issue(code='missing-visual-content')])
        with self.assertRaises(ValueError):
            apply_completion(bundle(), {}, completion_answer(), [issue(code='missing-visual-content')])

    def test_missing_body_is_bound_to_exact_plan_segment_and_never_rewrites_existing(self):
        from agent_service.visual_completion import apply_completion
        answer = {'additions': [{'pagePath': 'pages/one.page', 'sourceId': 'plan:0:text:0',
            'element': {'elementType': 'text', 'bounds': [50, 200, 400, 50], 'content': {'fontSize': 18}}}],
            'operations': []}
        result = apply_completion(bundle(), plan(), answer, [issue(code='missing-content')])
        self.assertEqual(result['pages'][0]['data']['elements'][-1]['content']['text'], '首次通过率目标60%。')
        answer['operations'] = [{'pagePath': 'pages/one.page', 'elementId': 'title', 'op': 'set', 'path': '/content/text', 'value': 'lost'}]
        with self.assertRaises(ValueError):
            apply_completion(bundle(), plan(), answer, [issue(code='missing-content')])

    def test_small_schematic_label_is_enlarged_without_rewriting_source_text(self):
        from agent_service.visual_completion import apply_completion
        answer = completion_answer()
        answer['additions'][-1]['element']['content']['fontSize'] = 9
        answer['additions'][-1]['element']['bounds'][3] = 12
        result = apply_completion(bundle(), plan(), answer, [issue(code='missing-visual-content')])
        note = result['pages'][0]['data']['elements'][-1]
        self.assertEqual(note['content']['fontSize'], 12)
        self.assertGreaterEqual(note['bounds'][3], 18)
        self.assertEqual(note['content']['text'], '趋势示意（非实测数据）')

    def test_chart_addition_uses_exact_source_dataset_not_model_numbers(self):
        from agent_service.visual_completion import apply_completion
        source = bundle()
        original = {'elementId': 'known', 'elementType': 'chart', 'bounds': [30, 160, 300, 200],
                    'data': {'cols': ['月', '值'], 'rows': [['一月', 32], ['二月', 60]]},
                    'series': [{'type': 'line', 'encode': {'x': '月', 'y': '值'}}]}
        source['pages'][0]['data']['elements'].append(original)
        answer = {'additions': [{'pagePath': 'pages/one.page', 'sourceId': 'element:0:known',
            'element': {'elementType': 'chart', 'bounds': [520, 220, 300, 220]}}], 'operations': []}
        result = apply_completion(source, plan(), answer, [issue(code='missing-visual-content')])
        self.assertEqual(result['pages'][0]['data']['elements'][-1]['data'], original['data'])
        answer['additions'][0]['element']['data'] = {'cols': ['x','y'], 'rows': [[1,999]]}
        with self.assertRaises(ValueError):
            apply_completion(source, plan(), answer, [issue(code='missing-visual-content')])


class CompletionFeedback(unittest.IsolatedAsyncioTestCase):
    async def test_schema_retry_identifies_exact_constraints_without_echoing_input(self):
        from agentscope.message import Msg
        from agent_service import pptd_generation as generation
        from agent_service.visual_completion import CompletionResult
        from agent_service.kimi_rules import KimiRules
        calls = []
        class Agent:
            async def reply(self, message):
                payload = json.loads(message.get_text_content()); calls.append(payload)
                answer = completion_answer()
                if len(calls) == 1:
                    answer['additions'][1]['element']['content']['fontSize'] = 0
                    answer['additions'][1]['element']['content']['text'] = 'private-invalid-replacement'
                return Msg(name='completion', role='assistant', content=[{'type': 'text', 'text': json.dumps(answer)}])
        with patch.object(generation, 'make_agent', return_value=Agent()):
            await generation._ask(SimpleNamespace(), 'visual-completion', KimiRules(), {}, response_schema=CompletionResult)
        correction = calls[1]['formatCorrection']
        self.assertIn('additions.1.element.content.fontSize', correction)
        self.assertIn('greater_than_equal', correction)
        self.assertIn('extra_forbidden', correction)
        self.assertNotIn('private-invalid-replacement', correction)


if __name__ == '__main__':
    unittest.main()
