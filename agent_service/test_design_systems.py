"""Preset design systems are a readable, deduplicated library."""
import unittest

from agent_service import design_systems


class DesignSystemTests(unittest.TestCase):
    def test_catalog_is_unique_and_categorised(self):
        items = design_systems.catalog()
        self.assertGreaterEqual(len(items), 30)
        identifiers = [item['id'] for item in items]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        order = [(item['category'], item['id']) for item in items]
        self.assertEqual(order, sorted(order))
        for item in items:
            self.assertTrue(item['category'])
            self.assertGreater(item['characters'], 0)
            self.assertRegex(item['id'], r'^[A-Za-z0-9][A-Za-z0-9 _-]*$')

    def test_every_preset_loads_with_its_own_text(self):
        for item in design_systems.catalog():
            with self.subTest(identifier=item['id']):
                spec = design_systems.load(item['id'])
                self.assertEqual(spec['id'], item['id'])
                self.assertTrue(spec['text'].strip().startswith('#'))
                self.assertGreater(len(spec['text']), 400)

    def test_unknown_preset_is_rejected(self):
        with self.assertRaises(design_systems.DesignSystemError):
            design_systems.load('../../etc/passwd')
        with self.assertRaises(design_systems.DesignSystemError):
            design_systems.load('not-a-style')


if __name__ == '__main__':
    unittest.main()
