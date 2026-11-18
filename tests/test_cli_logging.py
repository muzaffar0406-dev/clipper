import logging
import sys
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest import mock

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import watcher


class CliLoggingTests(unittest.TestCase):
    def test_watcher_log_path_target(self):
        self.assertEqual(watcher.APP_LOG_PATH.name, "clip-watcher.log")
        self.assertEqual(watcher.APP_LOG_PATH.resolve(), (PROJECT_DIR / "clip-watcher.log").resolve())

    def test_watcher_file_logger_rotation_policy(self):
        rotating_handlers = [h for h in watcher.file_logger.handlers if isinstance(h, RotatingFileHandler)]
        self.assertTrue(len(rotating_handlers) > 0, "Expected at least one RotatingFileHandler")
        for handler in rotating_handlers:
            self.assertEqual(handler.maxBytes, 1_000_000)
            self.assertEqual(handler.backupCount, 3)

    @mock.patch("builtins.print")
    def test_watcher_log_writes_to_file_handler(self, mock_print):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "test_watcher.log"
            test_handler = logging.FileHandler(log_path, encoding="utf-8")
            original_handlers = list(watcher.file_logger.handlers)
            for handler in original_handlers:
                watcher.file_logger.removeHandler(handler)
            watcher.file_logger.addHandler(test_handler)
            try:
                watcher.log("test standalone cli message 12345")
                test_handler.flush()
                content = log_path.read_text(encoding="utf-8")
                self.assertIn("test standalone cli message 12345", content)
            finally:
                watcher.file_logger.removeHandler(test_handler)
                test_handler.close()
                for handler in original_handlers:
                    watcher.file_logger.addHandler(handler)

    @mock.patch("builtins.print")
    def test_watcher_log_error_writes_to_file_handler(self, mock_print):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "test_error.log"
            test_handler = logging.FileHandler(log_path, encoding="utf-8")
            original_handlers = list(watcher.file_logger.handlers)
            for handler in original_handlers:
                watcher.file_logger.removeHandler(handler)
            watcher.file_logger.addHandler(test_handler)
            try:
                watcher.log_error("test cli error message 67890")
                test_handler.flush()
                content = log_path.read_text(encoding="utf-8")
                self.assertIn("ERROR: test cli error message 67890", content)
            finally:
                watcher.file_logger.removeHandler(test_handler)
                test_handler.close()
                for handler in original_handlers:
                    watcher.file_logger.addHandler(handler)

    @mock.patch("builtins.print")
    def test_watcher_log_survives_logging_exceptions(self, mock_print):
        """Logging failure (e.g., closed stream, disk full) must never interrupt processing."""
        class FailingHandler(logging.Handler):
            def emit(self, record):
                raise OSError(28, "No space left on device")

        original_handlers = list(watcher.file_logger.handlers)
        failing_handler = FailingHandler()
        failing_handler.handleError = lambda record: None
        for handler in original_handlers:
            watcher.file_logger.removeHandler(handler)
        watcher.file_logger.addHandler(failing_handler)
        try:
            # Should not raise any exception despite handler failure
            watcher.log("message during simulated stream failure")
            watcher.log_error("error during simulated stream failure")
        finally:
            watcher.file_logger.removeHandler(failing_handler)
            for handler in original_handlers:
                watcher.file_logger.addHandler(handler)
