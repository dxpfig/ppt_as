"""Provider configuration never exposes credentials."""
import os
import unittest
from unittest.mock import patch

from agent_service import provider_registry


class ProviderRegistryTests(unittest.TestCase):
    def test_snapshot_lists_model_vision_knowledge_and_renderer_without_secrets(self):
        with patch.dict(
            os.environ,
            {
                'PPT_LLM_PROVIDER': 'openai',
                'OPENAI_API_KEY': 'model-secret',
                'OPENAI_MODEL': 'text-model',
                'PPT_VISION_API_KEY': 'vision-secret',
                'PPT_VISION_MODEL': 'vision-model',
                'WEKNORA_BASE_URL': 'https://knowledge.test',
                'WEKNORA_API_KEY': 'knowledge-secret',
                'WEKNORA_TENANT_ID': 'tenant',
                'WEKNORA_KNOWLEDGE_BASE_IDS': 'one,two',
            },
            clear=False,
        ):
            snapshot = provider_registry.snapshot()

        self.assertEqual(
            {item['kind'] for item in snapshot['providers']},
            {'model', 'vision', 'knowledge', 'renderer'},
        )
        self.assertTrue(snapshot['tenantConfigured'])
        serialized = repr(snapshot)
        self.assertNotIn('model-secret', serialized)
        self.assertNotIn('vision-secret', serialized)
        self.assertNotIn('knowledge-secret', serialized)
