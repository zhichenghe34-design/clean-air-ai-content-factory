from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "core" / "sapi_tts.ps1"


@unittest.skipUnless(os.name == "nt", "Windows SAPI contract")
class SapiTtsContractTests(unittest.TestCase):
    @staticmethod
    def _powershell() -> Path:
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        executable = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if not executable.is_file():
            raise unittest.SkipTest("Windows PowerShell is unavailable")
        return executable

    def _probe_zh_cn_voice(self) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [
                str(self._powershell()),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-ProbeOnly",
                "-Language",
                "zh-CN",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            self.assertIn("Required offline SAPI voice is not installed: zh-CN", result.stderr)
        return result

    def _require_zh_cn_voice(self) -> subprocess.CompletedProcess[str]:
        result = self._probe_zh_cn_voice()
        if result.returncode != 0:
            raise unittest.SkipTest("This Windows host has no zh-CN SAPI voice; packaged preflight fails closed")
        return result

    def test_generates_nonempty_chinese_wave_with_explicit_zh_cn_voice(self) -> None:
        self._require_zh_cn_voice()
        with tempfile.TemporaryDirectory(prefix="shiyi-sapi-contract-") as temp:
            root = Path(temp)
            text_file = root / "script.txt"
            output_file = root / "voice.wav"
            text_file.write_text("开窗通风不能替代专业检测。", encoding="utf-8")
            result = subprocess.run(
                [
                    str(self._powershell()),
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(SCRIPT),
                    "-TextFile",
                    str(text_file),
                    "-OutputFile",
                    str(output_file),
                    "-Language",
                    "zh-CN",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("CULTURE=zh-CN", result.stdout)
            self.assertTrue(output_file.is_file())
            self.assertGreater(output_file.stat().st_size, 44)
            with wave.open(str(output_file), "rb") as audio:
                self.assertGreater(audio.getframerate(), 0)
                self.assertGreater(audio.getnframes(), audio.getframerate() // 2)
                self.assertIn(audio.getnchannels(), {1, 2})
                self.assertEqual(audio.getsampwidth(), 2)

    def test_probe_only_confirms_zh_cn_without_creating_audio(self) -> None:
        result = self._probe_zh_cn_voice()
        if result.returncode == 0:
            self.assertIn("CULTURE=zh-CN", result.stdout)
            self.assertIn("SAPI_VOICE=", result.stdout)
        else:
            self.assertNotIn("SAPI_VOICE=", result.stdout)

    def test_missing_language_fails_closed_without_wave(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shiyi-sapi-contract-") as temp:
            root = Path(temp)
            text_file = root / "script.txt"
            output_file = root / "voice.wav"
            text_file.write_text("测试", encoding="utf-8")
            result = subprocess.run(
                [
                    str(self._powershell()),
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(SCRIPT),
                    "-TextFile",
                    str(text_file),
                    "-OutputFile",
                    str(output_file),
                    "-Language",
                    "qaa-ZZ",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Required offline SAPI voice is not installed", result.stderr)
            self.assertFalse(output_file.exists())

    def test_empty_text_fails_before_creating_wave(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shiyi-sapi-contract-") as temp:
            root = Path(temp)
            text_file = root / "script.txt"
            output_file = root / "voice.wav"
            text_file.write_text("  \n", encoding="utf-8")
            result = subprocess.run(
                [
                    str(self._powershell()),
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(SCRIPT),
                    "-TextFile",
                    str(text_file),
                    "-OutputFile",
                    str(output_file),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SAPI input text must not be empty", result.stderr)
            self.assertFalse(output_file.exists())


if __name__ == "__main__":
    unittest.main()
