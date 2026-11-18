import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import watcher


class ClipGenerationTests(unittest.TestCase):
    def test_command_preserves_existing_vertical_encoding_settings(self):
        command = watcher.build_ffmpeg_command("input.ts", 12.5, 30, "clip.mp4")
        self.assertEqual(command[:4], ["ffmpeg", "-y", "-loglevel", "error"])
        self.assertIn("crop='trunc(ih*9/16/2)*2':ih,scale=1080:1920,setsar=1", command)
        self.assertIn("libx264", command)
        self.assertIn("aac", command)
        self.assertLess(command.index("-ss"), command.index("-i"))

    def test_fallback_command_uses_decoding_seek(self):
        command = watcher.build_ffmpeg_command("input.ts", 12.5, 30, "clip.mp4", accurate_seek=True)
        self.assertGreater(command.index("-ss"), command.index("-i"))

    def test_ffmpeg_failure_is_reported_without_claiming_success(self):
        with patch.object(watcher.subprocess, "run", return_value=SimpleNamespace(
            returncode=1, stderr="bad input", stdout=""
        )), patch.object(watcher, "log_error") as log_error:
            self.assertFalse(watcher.run_ffmpeg_cut("input.ts", 0, 30, "clip.mp4"))
        log_error.assert_called_once()

    def test_ffmpeg_timeout_is_reported_without_claiming_success(self):
        with patch.object(watcher.subprocess, "run", side_effect=watcher.subprocess.TimeoutExpired(
            cmd="ffmpeg", timeout=1
        )), patch.object(watcher, "log_error") as log_error:
            self.assertFalse(watcher.run_ffmpeg_cut("input.ts", 0, 30, "clip.mp4"))
        log_error.assert_called_once()

    def test_output_validation_requires_nontrivial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "clip.mp4"
            output.write_bytes(b"x" * 10_001)
            self.assertTrue(watcher.is_valid_clip_output(output))
            output.write_bytes(b"x")
            self.assertFalse(watcher.is_valid_clip_output(output))
