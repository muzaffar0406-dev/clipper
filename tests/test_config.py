import sys
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from app_config import ConfigurationError, GIB, load_retention_settings


class RetentionConfigurationTests(unittest.TestCase):
    def test_defaults_are_conservative_and_enabled(self):
        settings = load_retention_settings({})
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.retention_hours, 168.0)
        self.assertEqual(settings.max_recording_bytes, 20 * GIB)

    def test_valid_environment_values_are_loaded(self):
        settings = load_retention_settings({
            "CLIP_WATCHER_CLEANUP_ENABLED": "false",
            "CLIP_WATCHER_RECORDING_RETENTION_HOURS": "12",
            "CLIP_WATCHER_MAX_RECORDING_GB": "1.5",
        })
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.retention_hours, 12.0)
        self.assertEqual(settings.max_recording_bytes, int(1.5 * GIB))

    def test_invalid_values_are_rejected(self):
        with self.assertRaises(ConfigurationError):
            load_retention_settings({"CLIP_WATCHER_MAX_RECORDING_GB": "0"})
        with self.assertRaises(ConfigurationError):
            load_retention_settings({"CLIP_WATCHER_CLEANUP_ENABLED": "perhaps"})
