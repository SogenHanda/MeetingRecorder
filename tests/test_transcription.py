import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave


SCRIPT = Path(__file__).resolve().parents[1] / "Sources/MeetingRecorder/Resources/process_session.py"
spec = importlib.util.spec_from_file_location("meeting_processing", SCRIPT)
processing = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = processing
spec.loader.exec_module(processing)


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.session = Path(self.temporary.name)
        self.manifest = {
            "title": "検証", "startedAt": "2026-10-08T00:00:00Z", "chunks": [
                {"source": "microphone", "relativePath": "first.wav", "startedAt": "2026-10-08T00:00:01Z"},
                {"source": "microphone", "relativePath": "second.wav", "startedAt": "2026-10-08T00:00:03Z"},
            ],
        }
        for name in ("first.wav", "second.wav"):
            with wave.open(str(self.session / name), "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                audio.writeframes(b"\x10\x00" * 16000)
        (self.session / "session.json").write_text(json.dumps(self.manifest))

    def tearDown(self):
        self.temporary.cleanup()

    def test_audio_keeps_initial_offset_and_gap(self):
        sources = processing.build_audio_sources(self.session, self.manifest, "microphone")
        with wave.open(str(sources["microphone"]), "rb") as audio:
            self.assertEqual(audio.getnframes(), 4 * 16000)
            self.assertEqual(audio.readframes(16000), bytes(32000))
            self.assertNotEqual(audio.readframes(16000), bytes(32000))
            self.assertEqual(audio.readframes(16000), bytes(32000))

    def test_missing_audio_is_reported(self):
        (self.session / "second.wav").unlink()
        with self.assertRaisesRegex(RuntimeError, "元音声が見つかりません"):
            processing.build_audio_sources(self.session, self.manifest, "microphone")

    def test_echo_dedup_keeps_distinct_and_repeated_speech(self):
        text = "メディアアートについて検討していきます"
        items = [
            processing.SpeechSegment(0, 5, text, audio_source="microphone"),
            processing.SpeechSegment(0.1, 5.1, text, audio_source="system"),
            processing.SpeechSegment(0.2, 5.2, "それとは違う意見があります", audio_source="system"),
            processing.SpeechSegment(8, 13, text, audio_source="system"),
        ]
        merged, duplicates = processing.merge_sources(items)
        self.assertEqual(duplicates, 1)
        self.assertEqual(len(merged), 3)
        self.assertIn(items[2], merged)
        self.assertIn(items[3], merged)

    def test_forced_transcription_archives_old_outputs(self):
        original = "以前の全文ログ"
        (self.session / "transcript.md").write_text(original)
        (self.session / "transcript.json").write_text("[]")
        (self.session / "minutes.md").write_text("以前の議事録")
        args = ["process_session.py", "--session", str(self.session), "--force-transcription", "--transcribe-only"]
        with patch.object(sys, "argv", args), patch.dict(os.environ, {"PYANNOTE_TOKEN": ""}), \
             patch.object(processing, "load_whisper_model", return_value=object()), \
             patch.object(processing, "transcribe", return_value=([processing.SpeechSegment(1, 2, "新しい認識", audio_source="microphone")], "ja")):
            self.assertEqual(processing.main(), 0)
        history = list((self.session / "history").iterdir())
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0] / "transcript.md").read_text(), original)
        self.assertEqual((history[0] / "minutes.md").read_text(), "以前の議事録")
        self.assertIn("新しい認識", (self.session / "transcript.md").read_text())
        metadata = json.loads((self.session / "transcription.json").read_text())
        self.assertEqual(metadata["model"], "large-v3")
        self.assertEqual(metadata["audio_source"], "separate")
        self.assertTrue((self.session / "transcript.sources.json").is_file())

    def test_failed_transcription_preserves_old_outputs(self):
        original = "以前の全文ログ"
        (self.session / "transcript.md").write_text(original)
        args = ["process_session.py", "--session", str(self.session), "--force-transcription", "--transcribe-only"]
        with patch.object(sys, "argv", args), patch.object(processing, "load_whisper_model", side_effect=RuntimeError("model failure")):
            with self.assertRaisesRegex(RuntimeError, "model failure"):
                processing.main()
        self.assertEqual((self.session / "transcript.md").read_text(), original)
        self.assertFalse((self.session / "history").exists())


if __name__ == "__main__":
    unittest.main()
