from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from core.motion_runtime_contract import (
    H264_CODEC_STRATEGY,
    H264_MF_QUALITY_BY_TIER,
    HYPERFRAMES_PATCHED_CLI_SHA256,
    HYPERFRAMES_PATCH_ID,
    HYPERFRAMES_PATCH_VERSION,
    HYPERFRAMES_UPSTREAM_CLI_SHA256,
    h264_mf_quality_for_tier,
    h264_mf_quality_from_crf,
    h264_mf_video_args,
)
from tools.apply_hyperframes_windows_mf_patch import (
    PATCHED_CLI_SHA256,
    PATCH_ID,
    PATCH_VERSION,
    UPSTREAM_CLI_SHA256,
    PatchContractError,
    _patched_text,
    main as patch_main,
)


ROOT = Path(__file__).resolve().parents[1]
PATCHER = ROOT / "tools" / "apply_hyperframes_windows_mf_patch.py"
PATCH_MANIFEST = ROOT / "third_party" / "hyperframes" / "windows-mf-patch.json"
QUALITY_EVIDENCE = (
    ROOT / "third_party" / "hyperframes" / "windows-mf-quality-evidence.json"
)
RUNTIME_EVIDENCE = (
    ROOT / "third_party" / "hyperframes" / "windows-mf-runtime-evidence.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class HyperFramesWindowsMfPatchTests(unittest.TestCase):
    def test_manifest_binds_exact_source_patch_and_patcher(self):
        manifest = _load_json(PATCH_MANIFEST)
        modified = manifest["modified_files"]
        self.assertEqual(manifest["patch_id"], PATCH_ID)
        self.assertEqual(manifest["patch_version"], PATCH_VERSION)
        self.assertEqual(manifest["patcher"]["sha256"], _sha256(PATCHER))
        self.assertEqual(len(modified), 1)
        self.assertEqual(modified[0]["path"], "dist/cli.js")
        self.assertEqual(modified[0]["upstream_sha256"], UPSTREAM_CLI_SHA256)
        self.assertEqual(modified[0]["patched_sha256"], PATCHED_CLI_SHA256)
        self.assertEqual(
            manifest["formal_packaging_contract"]["report_fields"]["patched_cli_sha256"],
            PATCHED_CLI_SHA256,
        )
        self.assertEqual(manifest["formal_packaging_contract"]["mpt_video_codec"], "h264_mf")
        self.assertEqual(HYPERFRAMES_PATCH_ID, PATCH_ID)
        self.assertEqual(HYPERFRAMES_PATCH_VERSION, PATCH_VERSION)
        self.assertEqual(HYPERFRAMES_UPSTREAM_CLI_SHA256, UPSTREAM_CLI_SHA256)
        self.assertEqual(HYPERFRAMES_PATCHED_CLI_SHA256, PATCHED_CLI_SHA256)

    def test_empirical_quality_tiers_are_frozen_in_code_and_evidence(self):
        quality = _load_json(QUALITY_EVIDENCE)
        self.assertEqual(H264_CODEC_STRATEGY, "h264_mf")
        self.assertEqual(H264_MF_QUALITY_BY_TIER, {"draft": 60, "standard": 72, "high": 80})
        self.assertEqual(quality["decision"]["quality_tiers"], H264_MF_QUALITY_BY_TIER)
        self.assertEqual(
            {item["selected_tier"]: item["quality"] for item in quality["candidates"]},
            H264_MF_QUALITY_BY_TIER,
        )
        self.assertEqual(h264_mf_quality_from_crf(15), 80)
        self.assertEqual(h264_mf_quality_from_crf(18), 72)
        self.assertEqual(h264_mf_quality_from_crf(28), 60)
        self.assertEqual(h264_mf_quality_for_tier("standard"), 72)
        with self.assertRaises(ValueError):
            h264_mf_quality_from_crf(52)
        with self.assertRaises(ValueError):
            h264_mf_quality_for_tier("unknown")

    def test_quality_and_cbr_command_contracts_have_no_x264_controls(self):
        quality_args = h264_mf_video_args(crf_equivalent=18, gop_size=30)
        self.assertEqual(
            quality_args,
            [
                "-c:v",
                "h264_mf",
                "-rate_control",
                "quality",
                "-quality",
                "72",
                "-scenario",
                "archive",
                "-hw_encoding",
                "0",
                "-g",
                "30",
                "-keyint_min",
                "30",
                "-force_key_frames",
                "expr:eq(mod(n,30),0)",
                "-flags",
                "+cgop",
                "-bf",
                "0",
                "-pix_fmt",
                "yuv420p",
            ],
        )
        cbr_args = h264_mf_video_args(video_bitrate="2M")
        self.assertIn("cbr", cbr_args)
        self.assertIn("2M", cbr_args)
        self.assertNotIn("-quality", cbr_args)
        joined = " ".join(quality_args + cbr_args).lower()
        for forbidden in ("libx264", "openh264", "rav1e", "-crf", "-preset", "x264-params"):
            self.assertNotIn(forbidden, joined)

    def test_patch_is_deterministic_and_patched_bundle_h264_paths_are_clean(self):
        cli_path = ROOT / "node_modules" / "hyperframes" / "dist" / "cli.js"
        if not cli_path.is_file():
            self.skipTest("npm dependency tree is not installed")
        payload = cli_path.read_bytes()
        source_sha = hashlib.sha256(payload).hexdigest().upper()
        if source_sha == UPSTREAM_CLI_SHA256:
            text = _patched_text(payload.decode("utf-8"))
            patched_payload = text.encode("utf-8")
        elif source_sha == PATCHED_CLI_SHA256:
            text = payload.decode("utf-8")
            patched_payload = payload
        else:
            self.fail(f"unexpected installed HyperFrames bundle SHA-256: {source_sha}")
        self.assertEqual(hashlib.sha256(patched_payload).hexdigest().upper(), PATCHED_CLI_SHA256)

        lowered = text.lower()
        for forbidden in ("libx264", "openh264", "rav1e", "-x264-params"):
            self.assertNotIn(forbidden, lowered)
        self.assertNotIn('"-preset"', text)
        self.assertTrue(
            text.startswith(
                "#!/usr/bin/env node\n"
                "// Apache-2.0 modification notice: Shanghai Shiyi Brand Management Co., Ltd. "
                "modified this file via shiyi-hyperframes-windows-mf v1.2.0"
            )
        )
        self.assertIn(
            "async function fetchGoogleFont(familyName, options, fontText) {\n  return [];\n}",
            text,
        )
        self.assertIn(
            "async function fetchExternalStylesheetCss(href) {\n  return null;\n}",
            text,
        )

        crf_offsets = []
        cursor = 0
        while (offset := text.find('"-crf"', cursor)) >= 0:
            crf_offsets.append(offset)
            cursor = offset + 1
        self.assertEqual(len(crf_offsets), 5)
        self.assertTrue(
            all("libvpx" in text[max(0, offset - 320) : offset + 120] for offset in crf_offsets)
        )

        profile_offsets = []
        cursor = 0
        while (offset := text.find('"-profile:v"', cursor)) >= 0:
            profile_offsets.append(offset)
            cursor = offset + 1
        self.assertEqual(len(profile_offsets), 3)
        self.assertTrue(
            all("prores" in text[max(0, offset - 320) : offset + 120] for offset in profile_offsets)
        )

    def test_patcher_rejects_an_unrecognized_bundle_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            package_root = Path(temporary)
            (package_root / "dist").mkdir()
            (package_root / "package.json").write_text(
                json.dumps({"name": "hyperframes", "version": "0.7.86"}),
                encoding="utf-8",
            )
            cli_path = package_root / "dist" / "cli.js"
            cli_path.write_text("unknown bundle", encoding="utf-8")
            original = cli_path.read_bytes()
            with self.assertRaises(PatchContractError):
                patch_main(["--package-root", str(package_root)])
            self.assertEqual(cli_path.read_bytes(), original)

    def test_runtime_evidence_is_bound_to_current_fixture_and_lgpl_runtime(self):
        evidence = _load_json(RUNTIME_EVIDENCE)
        fixture = ROOT / evidence["render_fixture"]["path"]
        self.assertEqual(_sha256(fixture), evidence["render_fixture"]["sha256"])
        self.assertEqual(evidence["offline_guard"]["external_request_count"], 0)
        self.assertEqual(evidence["strict_check"]["external_request_count"], 0)
        self.assertFalse(evidence["strict_check"]["meta"]["updateAvailable"])
        self.assertTrue(evidence["strict_check"]["meta"]["offline"])
        self.assertEqual({item["capture_path"] for item in evidence["renders"]}, {"streaming", "disk"})
        for render in evidence["renders"]:
            self.assertEqual(render["external_request_count"], 0)
            self.assertEqual(render["probe"]["codec"], "h264")
            self.assertEqual(render["probe"]["pixel_format"], "yuv420p")

    @unittest.skipUnless(os.name == "nt", "h264_mf is a Windows-only encoder")
    def test_real_lgpl_h264_mf_aac_canary_when_runtime_is_available(self):
        runtime_dir = ROOT / "third_party" / "ffmpeg" / "runtime" / "win-x64"
        ffmpeg = Path(os.environ.get("SHIYI_TEST_FFMPEG", runtime_dir / "ffmpeg.exe"))
        ffprobe = Path(os.environ.get("SHIYI_TEST_FFPROBE", runtime_dir / "ffprobe.exe"))
        if not ffmpeg.is_file() or not ffprobe.is_file():
            self.skipTest("reviewed LGPL FFmpeg runtime is not present")

        version = subprocess.run(
            [str(ffmpeg), "-version"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout.lower()
        self.assertIn("--enable-mediafoundation", version)
        self.assertIn("--enable-encoder=h264_mf", version)
        self.assertNotIn("--enable-gpl", version)
        for forbidden in ("libx264", "openh264", "rav1e"):
            self.assertNotIn(forbidden, version)

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            width = height = 128
            header = f"P6\n{width} {height}\n255\n".encode("ascii")
            pixels = bytes(
                component
                for y in range(height)
                for x in range(width)
                for component in (x * 2 % 256, y * 2 % 256, (x + y) % 256)
            )
            for frame in range(15):
                (work / f"frame-{frame:03d}.ppm").write_bytes(header + pixels)
            audio_path = work / "audio.wav"
            with wave.open(str(audio_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(48000)
                wav.writeframes(b"\0\0" * 24000)

            output = work / "canary.mp4"
            command = [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-framerate",
                "30",
                "-i",
                str(work / "frame-%03d.ppm"),
                "-i",
                str(audio_path),
                *h264_mf_video_args(crf_equivalent=18, gop_size=15),
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-shortest",
                "-movflags",
                "+faststart",
                str(output),
            ]
            joined = " ".join(command).lower()
            for forbidden in ("libx264", "-crf", "-preset", "x264-params", "-profile:v"):
                self.assertNotIn(forbidden, joined)
            subprocess.run(command, check=True, capture_output=True)
            probe = subprocess.run(
                [
                    str(ffprobe),
                    "-v",
                    "error",
                    "-show_streams",
                    "-of",
                    "json",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            streams = json.loads(probe.stdout)["streams"]
            video = next(stream for stream in streams if stream["codec_type"] == "video")
            audio = next(stream for stream in streams if stream["codec_type"] == "audio")
            self.assertEqual(video["codec_name"], "h264")
            self.assertEqual(video["pix_fmt"], "yuv420p")
            self.assertEqual(video["width"], width)
            self.assertEqual(video["height"], height)
            self.assertEqual(audio["codec_name"], "aac")


if __name__ == "__main__":
    unittest.main()
