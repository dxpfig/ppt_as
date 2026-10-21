"""Golden corpus contract required before architecture refactoring."""
from pathlib import Path
import json
import unittest


class GoldenCorpusTests(unittest.TestCase):
    def test_corpus_covers_modes_failures_templates_and_page_actions(self):
        path = Path(__file__).resolve().parents[1] / 'tests' / 'golden' / 'corpus.json'
        corpus = json.loads(path.read_text(encoding='utf-8'))
        scenarios = corpus.get('scenarios')
        self.assertIsInstance(scenarios, list)
        self.assertGreaterEqual(len(scenarios), 10)
        identifiers = [item.get('id') for item in scenarios]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertEqual({item.get('mode') for item in scenarios}, {'quick', 'expert'})
        self.assertTrue(any(item.get('template') for item in scenarios))
        self.assertTrue(any(item.get('fault') for item in scenarios))
        self.assertTrue(any(item.get('offline') for item in scenarios))
        self.assertTrue(any(item.get('id') == 'page-actions' for item in scenarios))
        for item in scenarios:
            self.assertIsInstance(item.get('pages'), int)
            self.assertGreater(item['pages'], 0)
            self.assertTrue(item.get('assertions'))


if __name__ == '__main__':
    unittest.main()
