"""Shared model, context, review, repair and export boundaries."""
import unittest
from unittest.mock import AsyncMock, patch

from agent_service import context_service, model_gateway


class ServiceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_gateway_delegates_health_and_model_creation(self):
        sentinel = object()
        with patch('agent_service.model_gateway.agents.health', return_value={'llmConfigured': True}), patch(
            'agent_service.model_gateway.agents.make_model', return_value=sentinel
        ):
            self.assertTrue(model_gateway.health()['llmConfigured'])
            self.assertIs(model_gateway.make_model(), sentinel)

    async def test_context_service_delegates_research_and_template_loading(self):
        with patch.object(context_service, 'research_context', AsyncMock(return_value={'status': 'available'})) as research, patch.object(
            context_service.template_reference, 'load', return_value={'summary': {'pages': 1}}
        ) as template:
            self.assertEqual(await context_service.research(object()), {'status': 'available'})
            self.assertEqual(context_service.load_template(None, 'template-1'), {'summary': {'pages': 1}})
        research.assert_awaited_once()
        template.assert_called_once_with(None, 'template-1')
