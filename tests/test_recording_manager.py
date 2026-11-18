import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from app_config import RetentionSettings
from recording_manager import cleanup_recordings


class RecordingCleanupTests(unittest.TestCase):
    def write_recording(self, directory, name, age_seconds, size=32):
        path = directory / name
        path.write_bytes(b"x" * size)
        timestamp = self.now - age_seconds
        os.utime(path, (timestamp, timestamp))
        return path

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.rec = self.root / "rec"
        self.clips = self.root / "clips"
        self.rec.mkdir()
        self.clips.mkdir()
        self.now = 2_000_000.0

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_preserves_active_recent_and_non_recording_files(self):
        active = self.write_recording(self.rec, "active.ts", 20_000)
        recent = self.write_recording(self.rec, "recent.ts", 60)
        old = self.write_recording(self.rec, "old.ts", 20_000)
        note = self.rec / "notes.txt"
        note.write_text("keep")
        clip = self.clips / "keep.mp4"
        clip.write_bytes(b"clip")

        result = cleanup_recordings(
            self.rec,
            RetentionSettings(retention_hours=1, max_recording_bytes=10_000),
            active_recording=active,
            now=self.now,
        )

        self.assertFalse(old.exists())
        self.assertTrue(active.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(note.exists())
        self.assertTrue(clip.exists())
        self.assertEqual(result.deleted, [old])
        self.assertEqual(result.skipped_active, [active])

    def test_enforces_size_limit_oldest_first_after_safety_checks(self):
        oldest = self.write_recording(self.rec, "oldest.ts", 3_000, size=60)
        newer = self.write_recording(self.rec, "newer.ts", 2_000, size=60)
        settings = RetentionSettings(retention_hours=100, max_recording_bytes=70)

        result = cleanup_recordings(self.rec, settings, now=self.now)

        self.assertFalse(oldest.exists())
        self.assertTrue(newer.exists())
        self.assertEqual(result.deleted, [oldest])

    def test_dry_run_reports_without_deleting(self):
        old = self.write_recording(self.rec, "old.ts", 20_000)
        result = cleanup_recordings(
            self.rec,
            RetentionSettings(retention_hours=1, max_recording_bytes=10_000),
            now=self.now,
            dry_run=True,
        )
        self.assertTrue(old.exists())
        self.assertEqual(result.deleted, [])
