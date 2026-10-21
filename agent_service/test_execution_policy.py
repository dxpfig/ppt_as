"""Execution policy defaults and environment validation."""
import os
import unittest
from unittest.mock import patch

from agent_service.execution_policy import expert_policy, quick_policy


class ExecutionPolicyTests(unittest.TestCase):
    def test_quick_policy_controls_page_concurrency(self):
        with patch.dict(os.environ, {'PPTD_PAGE_CONCURRENCY': '4'}, clear=False):
            policy = quick_policy()
        self.assertEqual(policy.mode, 'quick')
        self.assertEqual(policy.page_concurrency, 4)
        self.assertEqual(policy.visual_review, 'risk-and-final')
        self.assertTrue(policy.export_after_review)

    def test_expert_policy_bounds_batch_and_planning_iterations(self):
        with patch.dict(
            os.environ,
            {
                'PPT_KIMI_MAX_ITERS': '12',
                'PPT_KIMI_PLAN_ITERS': '24',
                'PPT_KIMI_BATCH_PAGES': '3',
            },
            clear=False,
        ):
            policy = expert_policy()
        self.assertEqual(policy.mode, 'expert')
        self.assertEqual(policy.page_batch_size, 3)
        self.assertEqual(policy.max_iterations, 12)
        self.assertEqual(policy.planning_iterations, 12)
        self.assertEqual(policy.visual_review, 'full')
        self.assertFalse(policy.export_after_review)

    def test_invalid_policy_value_fails_explicitly(self):
        with patch.dict(os.environ, {'PPT_KIMI_BATCH_PAGES': '99'}, clear=False):
            with self.assertRaises(ValueError):
                expert_policy()
