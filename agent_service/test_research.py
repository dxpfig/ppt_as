import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from agent_service.research import research_context


def request(**kwargs):
    return SimpleNamespace(prompt=kwargs.get('prompt', '主题'), references=kwargs.get('references', ''), allowResearch=kwargs.get('allowResearch', False), project=SimpleNamespace(title='演示', references=''))


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_prompt_links_are_read_even_when_expansion_is_disabled(self):
        text = '资料正文' * 6000
        with patch('agent_service.research.fetch_bytes', return_value=('<html><meta charset="utf-8"><title>资料</title><p>'+text+'</p></html>').encode()) as fetch:
            result = await research_context(request(prompt='阅读 https://example.org/report'))
        fetch.assert_awaited_once()
        self.assertIn(text, result['sources'][0]['text'])
        self.assertEqual(result['sources'][0]['url'], 'https://example.org/report')

    async def test_provider_absence_is_explicit_without_invented_citations(self):
        with patch.dict(os.environ, {}, clear=True):
            result = await research_context(request(allowResearch=True))
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['sources'], [])
        self.assertTrue(result['warnings'])

    async def test_pdf_text_is_extracted_instead_of_treated_as_html(self):
        import pymupdf
        with pymupdf.open() as pdf:
            page = pdf.new_page(); page.insert_text((30, 30), 'Source evidence')
            raw = pdf.tobytes()
        with patch('agent_service.research.fetch_bytes', return_value=raw):
            result = await research_context(request(references='https://example.org/a.pdf'))
        self.assertIn('Source evidence', result['sources'][1]['text'])

    async def test_failed_link_remains_unverified(self):
        with patch('agent_service.research.fetch_bytes', side_effect=ValueError('not public')):
            result = await research_context(request(prompt='https://example.org/private'))
        self.assertEqual(result['sources'], [])
        self.assertIn('不将该链接视作已核实内容', result['warnings'][0])

if __name__ == '__main__': unittest.main()
