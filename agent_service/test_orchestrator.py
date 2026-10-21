"""Unified execution routing contracts."""
import unittest

from agent_service.orchestrator import ExecutionRoute, resolve_route


class OrchestratorRoutingTests(unittest.TestCase):
    def route(self, **overrides):
        values = {
            'kind': 'outline',
            'workflow': 'pptd-v2',
            'has_document': False,
            'expert_enabled': True,
        }
        values.update(overrides)
        return resolve_route(**values)

    def test_page_actions_have_priority_over_generation_modes(self):
        for kind in ('page-repair', 'page-review', 'page-design'):
            with self.subTest(kind=kind):
                self.assertEqual(self.route(kind=kind), ExecutionRoute.PAGE_ACTION)

    def test_expert_route_requires_kimi_kind_and_enabled_flag(self):
        self.assertEqual(self.route(kind='kimi-slides'), ExecutionRoute.EXPERT)
        self.assertEqual(
            self.route(kind='kimi-slides', expert_enabled=False),
            ExecutionRoute.PPTD,
        )

    def test_legacy_route_requires_legacy_workflow_without_document(self):
        self.assertEqual(self.route(workflow='legacy'), ExecutionRoute.LEGACY)
        self.assertEqual(
            self.route(workflow='legacy', has_document=True),
            ExecutionRoute.PPTD,
        )

    def test_quick_generation_is_default_pptd_route(self):
        self.assertEqual(self.route(kind='render'), ExecutionRoute.PPTD)
