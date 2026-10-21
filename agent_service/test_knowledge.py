"""Company knowledge-base connector contracts."""
import os
import unittest
from unittest.mock import AsyncMock, patch

from agent_service import knowledge


class KnowledgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_preserves_document_and_chunk_provenance(self):
        payload = {
            'data': [{
                'knowledge_title': '公司技术规范',
                'knowledge_id': 'doc-1',
                'chunk_id': 'chunk-3',
                'knowledge_base_id': 'kb-1',
                'page': 7,
                'content': '<p>系统必须支持离线部署。</p>',
            }],
        }
        with patch.dict(os.environ, {
            'WEKNORA_BASE_URL': 'http://knowledge.test',
            'WEKNORA_API_KEY': 'test-key',
        }, clear=False), patch.object(knowledge, 'api_json', AsyncMock(return_value=payload)) as api:
            result = await knowledge.search('离线部署', ['kb-1'])
        self.assertEqual(result['status'], 'available')
        source = result['sources'][0]
        self.assertEqual(source['provider'], 'weknora')
        self.assertEqual(source['documentId'], 'doc-1')
        self.assertEqual(source['chunkId'], 'chunk-3')
        self.assertEqual(source['knowledgeBaseId'], 'kb-1')
        self.assertEqual(source['page'], 7)
        self.assertEqual(source['text'], '系统必须支持离线部署。')
        self.assertEqual(api.call_args.kwargs['json']['knowledge_base_ids'], ['kb-1'])

    async def test_unconfigured_knowledge_returns_explicit_state(self):
        with patch.dict(os.environ, {'WEKNORA_BASE_URL': '', 'WEKNORA_API_KEY': ''}, clear=False):
            result = await knowledge.search('测试')
        self.assertEqual(result, {'status': 'unconfigured', 'sources': [], 'warnings': []})

    async def test_missing_tenant_is_reported_by_provider_error(self):
        with patch.dict(os.environ, {
            'WEKNORA_BASE_URL': 'http://knowledge.test',
            'WEKNORA_API_KEY': 'test-key',
        }, clear=False), patch.object(
            knowledge,
            'knowledge_bases',
            AsyncMock(side_effect=RuntimeError('TENANT_REQUIRED')),
        ):
            with self.assertRaises(knowledge.KnowledgeError):
                await knowledge.search('测试')
