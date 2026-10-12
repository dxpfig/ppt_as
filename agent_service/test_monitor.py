"""The Studio monitor check parses configuration and reports honestly."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
SPEC = importlib.util.spec_from_file_location('ppt_monitor', SCRIPTS / 'monitor.py')
monitor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(monitor)


class MonitorTests(unittest.TestCase):
    def test_configuration_reads_the_project_env(self):
        with patch.object(monitor, 'dotenv', return_value={'AGENTSCOPE_STUDIO_URL': 'http://192.168.0.10:3001'}):
            config = monitor.configuration()
        self.assertTrue(config['configured'])
        self.assertEqual((config['host'], config['port']), ('192.168.0.10', 3001))
        self.assertEqual(config['base'], 'http://192.168.0.10:3001')

    def test_missing_or_invalid_configuration_disables_the_check(self):
        for value in ({}, {'AGENTSCOPE_STUDIO_URL': ''}, {'AGENTSCOPE_STUDIO_URL': 'http://:3001'}):
            with self.subTest(value=value):
                with patch.object(monitor, 'dotenv', return_value=value):
                    self.assertFalse(monitor.configuration()['configured'])

    def test_default_port_when_the_url_omits_one(self):
        with patch.object(monitor, 'dotenv', return_value={'AGENTSCOPE_STUDIO_URL': 'http://127.0.0.1'}):
            self.assertEqual(monitor.configuration()['port'], 80)

    def test_summary_covers_every_state(self):
        unconfigured = {'configured': False, 'running': False, 'host': None, 'port': None, 'detail': ''}
        self.assertIn('未配置', monitor.summary(unconfigured))
        down = {'configured': True, 'running': False, 'host': '127.0.0.1', 'port': 3001,
                'detail': '未运行', 'managed': False}
        self.assertIn('未运行', monitor.summary(down))
        self.assertIn('npx', monitor.summary(down).lower())
        up = {'configured': True, 'running': True, 'host': '127.0.0.1', 'port': 3001,
              'detail': 'HTTP 就绪', 'managed': True}
        self.assertIn('运行中', monitor.summary(up))
        self.assertIn('本脚本管理', monitor.summary(up))

    def test_status_never_raises_and_reports_the_configured_endpoint(self):
        state = monitor.status()
        self.assertIn('configured', state)
        self.assertIn('running', state)
        if state['configured']:
            self.assertIsInstance(state['port'], int)


if __name__ == '__main__':
    unittest.main()
