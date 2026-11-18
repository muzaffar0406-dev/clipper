import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import gui


class DashboardStartupTests(unittest.TestCase):
    def test_startup_banner_is_cp1252_safe(self):
        banner = "\n".join(gui.startup_messages("http://localhost:5000"))
        banner.encode("cp1252")
        self.assertNotIn("🎬", banner)
        self.assertIn("clip-watcher Dashboard is running.", banner)

    def test_dashboard_host_is_loopback_only(self):
        self.assertEqual(gui.DASHBOARD_HOST, "127.0.0.1")

    def test_append_log_writes_to_a_utf8_file_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "dashboard.log"
            test_handler = logging.FileHandler(log_path, encoding="utf-8")
            original_handlers = list(gui.file_logger.handlers)
            original_history = list(gui.log_history)
            for handler in original_handlers:
                gui.file_logger.removeHandler(handler)
            gui.file_logger.addHandler(test_handler)
            try:
                gui.append_log("dashboard persistence test")
                test_handler.flush()
                self.assertIn("dashboard persistence test", log_path.read_text(encoding="utf-8"))
            finally:
                gui.file_logger.removeHandler(test_handler)
                test_handler.close()
                for handler in original_handlers:
                    gui.file_logger.addHandler(handler)
                gui.log_history[:] = original_history

    @mock.patch("gui.ThreadedHTTPServer", side_effect=OSError("Address already in use"))
    @mock.patch("builtins.print")
    def test_main_exits_with_code_1_on_port_conflict(self, mock_print, mock_server):
        with self.assertRaises(SystemExit) as cm:
            gui.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertTrue(any("could not bind" in str(arg) for call in mock_print.call_args_list for arg in call[0]))
