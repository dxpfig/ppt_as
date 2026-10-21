"""Golden corpus contract required before architecture refactoring."""
import hashlib
from pathlib import Path
import json
import unittest


class GoldenCorpusTests(unittest.TestCase):
    def test_corpus_covers_modes_failures_templates_and_page_actions(self):
        corpus_path = Path(__file__).resolve().parents[1] / 'tests' / 'golden' / 'corpus.json'
        corpus = json.loads(corpus_path.read_text(encoding='utf-8'))
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
        fixtures = corpus.get('fixtures')
        self.assertIsInstance(fixtures, dict)
        self.assertGreaterEqual(len(fixtures), 6)
        for identifier, fixture in fixtures.items():
            fixture_path = corpus_path.parent / fixture['path']
            self.assertTrue(fixture_path.is_file(), identifier)
            self.assertEqual(
                hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
                fixture['sha256'],
                identifier,
            )
        for item in scenarios:
            self.assertIsInstance(item.get('pages'), int)
            self.assertGreater(item['pages'], 0)
            self.assertTrue(item.get('assertions'))
            self.assertTrue(item.get('fixtures'))
            self.assertTrue(set(item['fixtures']) <= set(fixtures))


if __name__ == '__main__':
    unittest.main()
