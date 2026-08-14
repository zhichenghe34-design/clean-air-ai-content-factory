from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout
from pathlib import Path

from tools.apply_moviepy_windows_mf_patch import (
    DISTRIBUTION_VERSION,
    LICENSE_SHA256,
    METADATA_SHA256,
    MODULE_REPORTED_VERSION,
    MODULE_VERSION_SHA256,
    PATCHED_RECORD_BYTES,
    PATCHED_RECORD_SHA256,
    PATCHED_WRITER_BYTES,
    PATCHED_WRITER_SHA256,
    PATCH_ID,
    PATCH_VERSION,
    UPSTREAM_RECORD_BYTES,
    UPSTREAM_RECORD_SHA256,
    UPSTREAM_WRITER_BYTES,
    UPSTREAM_WRITER_SHA256,
    PatchContractError,
    _patched_record_payload,
    _patched_writer_payload,
    _record_line,
    main as patch_main,
)


ROOT = Path(__file__).resolve().parents[1]
PATCHER = ROOT / "tools" / "apply_moviepy_windows_mf_patch.py"
PATCH_MANIFEST = ROOT / "third_party" / "python_runtime" / "moviepy-windows-mf-patch.json"
UPSTREAM_FIXTURE = ROOT / "tests" / "fixtures" / "moviepy-2.2.1-upstream-files.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _inflate_upstream_runtime(runtime_root: Path) -> dict[str, bytes]:
    fixture = _load_json(UPSTREAM_FIXTURE)
    payloads: dict[str, bytes] = {}
    for relative, entry in fixture["files"].items():
        payload = zlib.decompress(base64.b64decode(entry["zlib_base64"]))
        if len(payload) != entry["bytes"]:
            raise AssertionError(f"fixture byte count mismatch: {relative}")
        if hashlib.sha256(payload).hexdigest().upper() != entry["sha256"]:
            raise AssertionError(f"fixture SHA-256 mismatch: {relative}")
        destination = runtime_root / "Lib" / "site-packages" / Path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        payloads[relative] = payload
    return payloads


class MoviePyWindowsMfPatchTests(unittest.TestCase):
    def test_manifest_binds_exact_distribution_module_license_and_patcher(self):
        manifest = _load_json(PATCH_MANIFEST)
        self.assertEqual(manifest["patch_id"], PATCH_ID)
        self.assertEqual(manifest["patch_version"], PATCH_VERSION)
        self.assertEqual(manifest["component"]["distribution_version"], DISTRIBUTION_VERSION)
        self.assertEqual(manifest["component"]["module_reported_version"], MODULE_REPORTED_VERSION)
        self.assertEqual(manifest["component"]["license"], "MIT")
        self.assertEqual(manifest["patcher"]["sha256"], _sha256(PATCHER))
        self.assertEqual(manifest["identity_files"]["METADATA"]["sha256"], METADATA_SHA256)
        self.assertEqual(manifest["identity_files"]["module_version"]["sha256"], MODULE_VERSION_SHA256)
        self.assertEqual(manifest["identity_files"]["license"]["sha256"], LICENSE_SHA256)

        modified = {entry["role"]: entry for entry in manifest["modified_files"]}
        self.assertEqual(modified["writer"]["upstream_sha256"], UPSTREAM_WRITER_SHA256)
        self.assertEqual(modified["writer"]["patched_sha256"], PATCHED_WRITER_SHA256)
        self.assertEqual(modified["writer"]["upstream_bytes"], UPSTREAM_WRITER_BYTES)
        self.assertEqual(modified["writer"]["patched_bytes"], PATCHED_WRITER_BYTES)
        self.assertEqual(modified["record"]["upstream_sha256"], UPSTREAM_RECORD_SHA256)
        self.assertEqual(modified["record"]["patched_sha256"], PATCHED_RECORD_SHA256)
        self.assertEqual(modified["record"]["upstream_bytes"], UPSTREAM_RECORD_BYTES)
        self.assertEqual(modified["record"]["patched_bytes"], PATCHED_RECORD_BYTES)

    def test_frozen_fixture_and_deterministic_patch_match_all_exact_identities(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime_root = Path(temporary) / "python"
            payloads = _inflate_upstream_runtime(runtime_root)
            writer = payloads["moviepy/video/io/ffmpeg_writer.py"]
            record = payloads["moviepy-2.2.1.dist-info/RECORD"]
            self.assertEqual(hashlib.sha256(writer).hexdigest().upper(), UPSTREAM_WRITER_SHA256)
            self.assertEqual(hashlib.sha256(record).hexdigest().upper(), UPSTREAM_RECORD_SHA256)

            patched_writer = _patched_writer_payload(writer)
            patched_record = _patched_record_payload(record, patched_writer)
            self.assertEqual(len(patched_writer), PATCHED_WRITER_BYTES)
            self.assertEqual(len(patched_record), PATCHED_RECORD_BYTES)
            self.assertEqual(
                hashlib.sha256(patched_writer).hexdigest().upper(), PATCHED_WRITER_SHA256
            )
            self.assertEqual(
                hashlib.sha256(patched_record).hexdigest().upper(), PATCHED_RECORD_SHA256
            )
            self.assertEqual(
                _record_line(patched_writer),
                b"moviepy/video/io/ffmpeg_writer.py,"
                b"sha256=3-ds2K7RUbmYgd0B-ivB4EDQeI7DZKjG7xQCDyAJ2Lk,12362\n",
            )

    def test_apply_is_atomic_idempotent_and_checkable(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime_root = Path(temporary) / "python"
            _inflate_upstream_runtime(runtime_root)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(patch_main(["--python-runtime", str(runtime_root)]), 0)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "patched")

            writer = runtime_root / "Lib/site-packages/moviepy/video/io/ffmpeg_writer.py"
            record = runtime_root / "Lib/site-packages/moviepy-2.2.1.dist-info/RECORD"
            self.assertEqual(_sha256(writer), PATCHED_WRITER_SHA256)
            self.assertEqual(_sha256(record), PATCHED_RECORD_SHA256)
            text = writer.read_text(encoding="utf-8")
            self.assertIn("MIT modification notice", text)
            self.assertIn(f"via {PATCH_ID} v{PATCH_VERSION}", text)
            self.assertIn('if codec != "h264_mf":\n            cmd.extend(["-preset", preset])', text)
            for pair in (
                ('"-rate_control"', '"quality"'),
                ('"-quality"', '"72"'),
                ('"-scenario"', '"archive"'),
                ('"-hw_encoding"', '"0"'),
                ('"-bf"', '"0"'),
                ('"-pix_fmt"', '"yuv420p"'),
            ):
                self.assertLess(text.index(pair[0]), text.index(pair[1], text.index(pair[0])))

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    patch_main(["--python-runtime", str(runtime_root), "--check"]), 0
                )
            self.assertEqual(json.loads(stdout.getvalue())["status"], "already_patched")

            before = (writer.read_bytes(), record.read_bytes())
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(patch_main(["--python-runtime", str(runtime_root)]), 0)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "already_patched")
            self.assertEqual((writer.read_bytes(), record.read_bytes()), before)

    def test_check_rejects_unpatched_runtime_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime_root = Path(temporary) / "python"
            _inflate_upstream_runtime(runtime_root)
            writer = runtime_root / "Lib/site-packages/moviepy/video/io/ffmpeg_writer.py"
            record = runtime_root / "Lib/site-packages/moviepy-2.2.1.dist-info/RECORD"
            before = (writer.read_bytes(), record.read_bytes())
            with self.assertRaises(PatchContractError):
                patch_main(["--python-runtime", str(runtime_root), "--check"])
            self.assertEqual((writer.read_bytes(), record.read_bytes()), before)

    def test_unknown_or_mixed_state_is_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime_root = Path(temporary) / "python"
            payloads = _inflate_upstream_runtime(runtime_root)
            writer = runtime_root / "Lib/site-packages/moviepy/video/io/ffmpeg_writer.py"
            record = runtime_root / "Lib/site-packages/moviepy-2.2.1.dist-info/RECORD"

            writer.write_bytes(_patched_writer_payload(payloads["moviepy/video/io/ffmpeg_writer.py"]))
            mixed_before = (writer.read_bytes(), record.read_bytes())
            with self.assertRaises(PatchContractError):
                patch_main(["--python-runtime", str(runtime_root)])
            self.assertEqual((writer.read_bytes(), record.read_bytes()), mixed_before)

            writer.write_bytes(b"unknown writer")
            unknown_before = (writer.read_bytes(), record.read_bytes())
            with self.assertRaises(PatchContractError):
                patch_main(["--python-runtime", str(runtime_root)])
            self.assertEqual((writer.read_bytes(), record.read_bytes()), unknown_before)

    @unittest.skipUnless(os.name == "nt", "h264_mf is a Windows-only encoder")
    def test_real_colorclip_with_tracked_lgpl_ffmpeg(self):
        ffmpeg_root = ROOT / "third_party" / "ffmpeg" / "runtime" / "win-x64"
        ffmpeg = Path(os.environ.get("SHIYI_TEST_FFMPEG", ffmpeg_root / "ffmpeg.exe"))
        ffprobe = Path(os.environ.get("SHIYI_TEST_FFPROBE", ffmpeg_root / "ffprobe.exe"))
        runtime_override = os.environ.get("SHIYI_TEST_PYTHON_RUNTIME", "").strip()
        source_runtime = (
            Path(runtime_override)
            if runtime_override
            else ROOT.parents[1]
            / "08_产出与验收"
            / "v0.3发布闭环"
            / "20260810-motion-primary-candidate-94311a8"
            / "runtime"
            / "python"
        )
        python = source_runtime / "python.exe"
        source_site_packages = source_runtime / "Lib" / "site-packages"
        if not ffmpeg.is_file() or not ffprobe.is_file():
            self.skipTest("tracked LGPL FFmpeg runtime is not present")
        if not python.is_file() or not (source_site_packages / "moviepy").is_dir():
            self.skipTest("reviewed portable Python runtime is not present")

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            runtime_root = work / "python"
            staged_site_packages = runtime_root / "Lib" / "site-packages"
            shutil.copytree(source_site_packages / "moviepy", staged_site_packages / "moviepy")
            shutil.copytree(
                source_site_packages / "moviepy-2.2.1.dist-info",
                staged_site_packages / "moviepy-2.2.1.dist-info",
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(patch_main(["--python-runtime", str(runtime_root)]), 0)
                self.assertEqual(
                    patch_main(["--python-runtime", str(runtime_root), "--check"]), 0
                )

            output = work / "moviepy-h264-mf-colorclip.mp4"
            environment = os.environ.copy()
            environment.update(
                {
                    "FFMPEG_BINARY": str(ffmpeg),
                    "IMAGEIO_FFMPEG_EXE": str(ffmpeg),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "SHIYI_STAGED_SITE_PACKAGES": str(staged_site_packages),
                    "SHIYI_COLORCLIP_OUTPUT": str(output),
                }
            )
            script = (
                "import hashlib, json, os, sys\n"
                "sys.path.insert(0, os.environ['SHIYI_STAGED_SITE_PACKAGES'])\n"
                "import numpy as np\n"
                "from moviepy import AudioClip, ColorClip\n"
                "from moviepy.video.io import ffmpeg_writer\n"
                "audio = AudioClip(lambda t: 0.02 * np.sin(2 * np.pi * 440 * t), duration=0.5, fps=48000)\n"
                "clip = ColorClip(size=(128, 128), color=(24, 132, 196), duration=0.5).with_audio(audio)\n"
                "try:\n"
                "    clip.write_videofile(os.environ['SHIYI_COLORCLIP_OUTPUT'], fps=10, codec='h264_mf', audio_codec='aac', audio_bitrate='64k', logger=None)\n"
                "finally:\n"
                "    clip.close()\n"
                "    audio.close()\n"
                "payload = open(ffmpeg_writer.__file__, 'rb').read()\n"
                "print(json.dumps({'writer': ffmpeg_writer.__file__, 'sha256': hashlib.sha256(payload).hexdigest().upper()}))\n"
            )
            completed = subprocess.run(
                [str(python), "-c", script],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=60,
            )
            runtime_result = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertEqual(runtime_result["sha256"], PATCHED_WRITER_SHA256)
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)

            probe = subprocess.run(
                [
                    str(ffprobe),
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type,codec_name,pix_fmt,width,height",
                    "-of",
                    "json",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            streams = json.loads(probe.stdout)["streams"]
            video = next(stream for stream in streams if stream["codec_type"] == "video")
            audio = next(stream for stream in streams if stream["codec_type"] == "audio")
            self.assertEqual(video["codec_name"], "h264")
            self.assertEqual(video["pix_fmt"], "yuv420p")
            self.assertEqual(video["width"], 128)
            self.assertEqual(video["height"], 128)
            self.assertEqual(audio["codec_name"], "aac")


if __name__ == "__main__":
    unittest.main()
