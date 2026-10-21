"""Shared concurrency pools remain independent."""
import unittest

from agent_service import pptd_exports, rendering
from agent_service.resource_manager import EXPORT_LIMIT, MODEL_LIMIT, RENDER_LIMIT
from agent_service import app


class ResourceManagerTests(unittest.TestCase):
    def test_model_render_and_export_pools_are_distinct(self):
        self.assertIs(app.LLM_LIMIT, MODEL_LIMIT)
        self.assertIs(rendering.RENDER_LOCK, RENDER_LIMIT)
        self.assertIs(pptd_exports.EXPORT_LOCK, EXPORT_LIMIT)
        self.assertIsNot(MODEL_LIMIT, RENDER_LIMIT)
        self.assertIsNot(RENDER_LIMIT, EXPORT_LIMIT)
