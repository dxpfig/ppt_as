"""Performance merge-gate comparison tests."""
import unittest

from agent_service.performance import compare_reports


class PerformanceComparisonTests(unittest.TestCase):
    def test_detects_stage_p95_regression(self):
        baseline = {
            'durationSeconds': {'p95': 100},
            'byNode': {'validate': {'p95': 40}},
            'milestones': {'firstPageSeconds': {'p95': 20}},
        }
        candidate = {
            'durationSeconds': {'p95': 90},
            'byNode': {'validate': {'p95': 50}},
            'milestones': {'firstPageSeconds': {'p95': 15}},
        }
        result = compare_reports(baseline, candidate, max_regression=0.2)
        self.assertFalse(result['passed'])
        self.assertEqual(
            [(item['group'], item['metric']) for item in result['regressions']],
            [('byNode', 'validate')],
        )
        self.assertEqual(len(result['improvements']), 2)

    def test_missing_metrics_are_not_treated_as_regressions(self):
        result = compare_reports(
            {'byNode': {'visual': {'p95': 10}}},
            {'byNode': {}},
            max_regression=0,
        )
        self.assertTrue(result['passed'])
