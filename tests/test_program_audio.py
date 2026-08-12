from __future__ import annotations

import hashlib
import math
import tempfile
import unittest
import wave
from pathlib import Path

from core.program_audio import build_default_program_audio, synthesize_builtin_bgm


REPO_ROOT = Path(__file__).resolve().parents[1]
FORMAL_FFMPEG = REPO_ROOT / "third_party" / "ffmpeg" / "runtime" / "win-x64" / "ffmpeg.exe"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _write_narration(path: Path, *, duration_seconds: float = 1.5) -> None:
    sample_rate = 48_000
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        frames = bytearray()
        for frame in range(round(duration_seconds * sample_rate)):
            t = frame / sample_rate
            value = 0.14 * math.sin(2 * math.pi * 220 * t)
            frames.extend(round(value * 32767).to_bytes(2, "little", signed=True))
        audio.writeframes(frames)


class ProgramAudioTests(unittest.TestCase):
    def test_builtin_bgm_is_byte_deterministic_for_the_same_variant(self):
        with tempfile.TemporaryDirectory() as folder_name:
            root = Path(folder_name)
            first = synthesize_builtin_bgm(root / "first.wav", duration_seconds=1.25, variant=2)
            second = synthesize_builtin_bgm(root / "second.wav", duration_seconds=1.25, variant=2)
            self.assertEqual(_sha256(first), _sha256(second))
            with wave.open(str(first), "rb") as audio:
                self.assertEqual(audio.getnchannels(), 2)
                self.assertEqual(audio.getframerate(), 48_000)
                self.assertEqual(audio.getsampwidth(), 2)

    @unittest.skipUnless(FORMAL_FFMPEG.is_file(), "formal FFmpeg runtime is unavailable")
    def test_program_audio_uses_fixed_narration_and_no_external_music_asset(self):
        with tempfile.TemporaryDirectory() as folder_name:
            root = Path(folder_name)
            narration = root / "voice.wav"
            _write_narration(narration)
            report = build_default_program_audio(
                narration,
                root / "bgm.wav",
                root / "program_audio.wav",
                ffmpeg_path=FORMAL_FFMPEG,
                duration_seconds=1.5,
                script_sha256="A" * 64,
            )
            self.assertEqual(report["narration"], "fixed_edge_tts_zh-CN-YunxiNeural")
            self.assertFalse(report["voice_selection_exposed"])
            self.assertEqual(report["background_music"], "deterministic_builtin_synthesis")
            self.assertFalse(report["background_music_external_asset"])
            self.assertAlmostEqual(report["requested_duration_seconds"], 1.5, places=3)
            self.assertAlmostEqual(report["duration_seconds"], 1.5, places=3)
            self.assertEqual(report["program_audio_sha256"], _sha256(root / "program_audio.wav"))
            with wave.open(str(root / "program_audio.wav"), "rb") as audio:
                self.assertEqual(audio.getnchannels(), 2)
                self.assertEqual(audio.getframerate(), 48_000)
                self.assertEqual(audio.getnframes(), 72_000)

    def test_invalid_script_identity_fails_before_writing_outputs(self):
        with tempfile.TemporaryDirectory() as folder_name:
            root = Path(folder_name)
            narration = root / "voice.wav"
            _write_narration(narration)
            with self.assertRaisesRegex(ValueError, "script_sha256"):
                build_default_program_audio(
                    narration,
                    root / "bgm.wav",
                    root / "program_audio.wav",
                    ffmpeg_path=FORMAL_FFMPEG,
                    duration_seconds=1.5,
                    script_sha256="invalid",
                )
            self.assertFalse((root / "bgm.wav").exists())
            self.assertFalse((root / "program_audio.wav").exists())


if __name__ == "__main__":
    unittest.main()
