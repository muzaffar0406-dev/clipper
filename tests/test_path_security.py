import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import gui


class ClipPathSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_clips_dir = gui.CLIPS_DIR
        gui.CLIPS_DIR = Path(self.temp_dir.name) / "clips"
        gui.CLIPS_DIR.mkdir()
        (gui.CLIPS_DIR / "safe.mp4").write_bytes(b"media")
        (gui.CLIPS_DIR / "safe.srt").write_text("subtitle")

    def tearDown(self):
        gui.CLIPS_DIR = self.original_clips_dir
        self.temp_dir.cleanup()

    def test_allows_regular_media_in_clips_directory(self):
        resolved = gui.resolve_clip_path("safe.mp4")
        self.assertEqual(resolved, (gui.CLIPS_DIR / "safe.mp4").resolve())

    def test_rejects_traversal_absolute_separators_and_wrong_extensions(self):
        for unsafe in (
            "../secret.mp4", "..\\secret.mp4", "nested/safe.mp4",
            "nested\\safe.mp4", "/tmp/secret.mp4", "safe.txt", "",
        ):
            with self.subTest(unsafe=unsafe):
                self.assertIsNone(gui.resolve_clip_path(unsafe))

    def test_rejects_symlinked_media_when_supported(self):
        outside = Path(self.temp_dir.name) / "outside.mp4"
        outside.write_bytes(b"outside")
        link = gui.CLIPS_DIR / "linked.mp4"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlink creation is unavailable on this host")
        self.assertIsNone(gui.resolve_clip_path("linked.mp4"))
