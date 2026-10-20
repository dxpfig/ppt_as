import unittest

from agent_service.app import editable_fallback_ready


class EditableFallbackTests(unittest.TestCase):
    def checkpoint(self, codes):
        return {
            'plan': {'document': {'pages': ['pages/one.page', 'pages/two.page']}},
            'pages': [{'path': 'pages/one.page'}, {'path': 'pages/two.page'}],
            'externalIssues': [{'code': code, 'location': 'pages/one.page'} for code in codes],
        }

    def test_complete_deck_with_layout_and_native_issues_can_stay_editable(self):
        self.assertTrue(
            editable_fallback_ready(
                self.checkpoint(['text-overflow', 'font-unresolved', 'native-unsupported'])
            )
        )

    def test_missing_pages_do_not_use_the_editable_fallback(self):
        checkpoint = self.checkpoint(['text-overflow'])
        checkpoint['pages'].pop()
        self.assertFalse(editable_fallback_ready(checkpoint))

    def test_content_or_integrity_issues_still_fail(self):
        for code in ('missing-body-content', 'InvalidTable', 'visual_integrity'):
            with self.subTest(code=code):
                self.assertFalse(editable_fallback_ready(self.checkpoint([code])))


if __name__ == '__main__':
    unittest.main()
