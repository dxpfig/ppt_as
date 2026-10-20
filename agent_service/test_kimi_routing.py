import os
import unittest
from unittest.mock import patch

from agent_service.app import agentic_kimi_enabled


class KimiRoutingTests(unittest.TestCase):
    def test_native_agentic_pipeline_is_default(self):
        with patch.dict(os.environ, {'PPT_KIMI_AGENTIC': ''}):
            self.assertTrue(agentic_kimi_enabled())

    def test_coordinated_pipeline_is_explicit_opt_out(self):
        for value in ('1', 'true', 'TRUE', 'yes'):
            with self.subTest(value=value), patch.dict(
                os.environ, {'PPT_KIMI_AGENTIC': value}
            ):
                self.assertTrue(agentic_kimi_enabled())
        for value in ('0', 'false', 'no'):
            with self.subTest(value=value), patch.dict(
                os.environ, {'PPT_KIMI_AGENTIC': value}
            ):
                self.assertFalse(agentic_kimi_enabled())


if __name__ == '__main__':
    unittest.main()
