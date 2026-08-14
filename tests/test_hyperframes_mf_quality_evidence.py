from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = (
    ROOT / "third_party" / "hyperframes" / "windows-mf-quality-evidence.json"
)
SCRIPT_PATH = ROOT / "tools" / "generate_hyperframes_mf_quality_evidence.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def load_generator():
    spec = importlib.util.spec_from_file_location("mf_quality_evidence", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HyperframesMfQualityEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.generator = load_generator()

    def test_formal_source_and_generator_are_byte_bound(self) -> None:
        manifest = self.manifest
        generator = manifest["generator"]
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(
            manifest["evidence_id"], "hyperframes-h264-mf-quality-20260810-v2"
        )
        self.assertEqual(manifest["source"]["width"], 1080)
        self.assertEqual(manifest["source"]["height"], 1920)
        self.assertEqual(manifest["source"]["fps"], 30)
        self.assertEqual(manifest["source"]["frame_count"], 60)
        self.assertEqual(manifest["source"]["bytes"], 1080 * 1920 * 3 * 60)
        self.assertEqual(
            manifest["source"]["transport"],
            "generated_in_memory_and_streamed_via_pipe_0",
        )
        self.assertEqual(generator["bytes"], SCRIPT_PATH.stat().st_size)
        self.assertEqual(generator["sha256"], sha256(SCRIPT_PATH))
        first_frame = self.generator.render_frame(0)
        self.assertEqual(len(first_frame), 1080 * 1920 * 3)
        self.assertEqual(
            hashlib.sha256(first_frame).hexdigest().upper(),
            manifest["source"]["frame_sha256"][0],
        )

    def test_runtime_lock_and_audited_fonts_are_frozen_release_inputs(self) -> None:
        manifest = self.manifest
        lock = manifest["runtime"]["ffmpeg_lock"]
        lock_path = ROOT / lock["path"]
        self.assertEqual(lock["bytes"], lock_path.stat().st_size)
        self.assertEqual(lock["sha256"], sha256(lock_path))
        lock_payload = json.loads(lock_path.read_text(encoding="utf-8"))
        expected_runtime_files = [
            {
                "path": record["name"],
                "bytes": record["bytes"],
                "sha256": record["sha256"].upper(),
            }
            for record in lock_payload["runtime"]["files"]
        ]
        self.assertEqual(manifest["runtime"]["runtime_files"], expected_runtime_files)
        runtime_root = ROOT / "third_party" / "ffmpeg" / "runtime" / "win-x64"
        if runtime_root.is_dir():
            for record in manifest["runtime"]["runtime_files"]:
                path = runtime_root / record["path"]
                self.assertEqual(record["bytes"], path.stat().st_size)
                self.assertEqual(record["sha256"], sha256(path))
        font_paths = [record["path"] for record in manifest["font_audit"]["files"]]
        tracked = set(
            subprocess.check_output(
                ["git", "ls-files", "--", *font_paths],
                cwd=ROOT,
                text=True,
                encoding="utf-8",
            ).splitlines()
        )
        self.assertEqual(tracked, set(font_paths))
        self.assertFalse(manifest["font_audit"]["used_by_input_generator"])
        for record in manifest["font_audit"]["files"]:
            path = ROOT / record["path"]
            self.assertEqual(record["bytes"], path.stat().st_size)
            self.assertEqual(record["sha256"], sha256(path))

    def test_all_quality_commands_are_exact_token_arrays_and_mf_only(self) -> None:
        manifest = self.manifest
        self.assertEqual(
            manifest["decision"]["quality_tiers"],
            {"draft": 60, "standard": 72, "high": 80},
        )
        forbidden = {"libx264", "openh264", "rav1e", "-preset", "-crf"}
        for candidate in manifest["candidates"]:
            self.assertEqual(candidate["selected_tier"], candidate["tier"])
            tokens = candidate["encode"]["command_tokens"]
            self.assertIsInstance(tokens, list)
            self.assertEqual(tokens[0], "{ffmpeg}")
            self.assertIn("pipe:0", tokens)
            self.assertEqual(tokens[tokens.index("-video_size") + 1], "1080x1920")
            self.assertEqual(tokens[tokens.index("-c:v") + 1], "h264_mf")
            self.assertEqual(tokens[tokens.index("-rate_control") + 1], "quality")
            self.assertEqual(
                tokens[tokens.index("-quality") + 1], str(candidate["quality"])
            )
            self.assertEqual(tokens[tokens.index("-scenario") + 1], "archive")
            self.assertEqual(tokens[tokens.index("-hw_encoding") + 1], "0")
            self.assertEqual(tokens[tokens.index("-pix_fmt") + 1], "yuv420p")
            self.assertTrue(forbidden.isdisjoint(tokens))
            self.assertEqual(
                candidate["repeatability"]["status"],
                "byte_identical_on_same_runtime_and_os_identity",
            )
            self.assertEqual(
                candidate["output"]["sha256"],
                candidate["repeatability"]["repeat_sha256"],
            )

    def test_raw_probe_psnr_and_keyframes_are_complete_and_monotonic(self) -> None:
        sizes: list[int] = []
        psnr_values: list[float] = []
        for candidate in self.manifest["candidates"]:
            quality = candidate["quality"]
            probe = candidate["probe"]
            self.assertEqual(
                hashlib.sha256(probe["raw_stdout"].encode("utf-8"))
                .hexdigest()
                .upper(),
                probe["stdout_sha256"],
            )
            self.assertEqual(json.loads(probe["raw_stdout"]), probe["raw_json"])
            stream = probe["raw_json"]["streams"][0]
            self.assertEqual(stream["codec_name"], "h264")
            self.assertEqual(stream["profile"], "Constrained Baseline")
            self.assertEqual(stream["pix_fmt"], "yuv420p")
            self.assertEqual((stream["width"], stream["height"]), (1080, 1920))
            self.assertEqual(stream["r_frame_rate"], "30/1")
            self.assertEqual(stream["nb_frames"], "60")

            keyframes = candidate["keyframes"]
            self.assertEqual(json.loads(keyframes["raw_stdout"]), keyframes["raw_json"])
            self.assertEqual(keyframes["count"], 2)
            self.assertEqual(
                [
                    frame["best_effort_timestamp_time"]
                    for frame in keyframes["raw_json"]["frames"]
                ],
                ["0.000000", "1.000000"],
            )

            psnr = candidate["psnr"]
            self.assertEqual(psnr["stats_frame_count"], 60)
            self.assertEqual(len(psnr["raw_stats"].splitlines()), 60)
            self.assertEqual(
                hashlib.sha256(psnr["raw_stats"].encode("utf-8"))
                .hexdigest()
                .upper(),
                psnr["stats_sha256"],
            )
            self.assertIn("PSNR y:", psnr["raw_stderr"])
            sizes.append(candidate["output"]["bytes"])
            psnr_values.append(psnr["summary"]["average"])
            self.assertEqual(candidate["quality"], quality)
        self.assertEqual(sizes, sorted(sizes))
        self.assertEqual(psnr_values, sorted(psnr_values))
        self.assertEqual(len(set(sizes)), 3)
        self.assertEqual(len(set(psnr_values)), 3)

    def test_comparison_image_is_reproducible_from_bound_frame_and_outputs(self) -> None:
        comparison = self.manifest["comparison"]
        self.assertEqual(comparison["source_frame_index"], 30)
        self.assertEqual(
            comparison["source_frame_sha256"],
            self.manifest["source"]["frame_sha256"][30],
        )
        self.assertEqual(
            comparison["tile_layout"]["order"],
            ["reference", "quality_60", "quality_72", "quality_80"],
        )
        tokens = comparison["command_tokens"]
        self.assertEqual(tokens[0], "{ffmpeg}")
        self.assertEqual(tokens.count("pipe:0"), 1)
        self.assertIn("q60-draft.mp4", tokens)
        self.assertIn("q72-standard.mp4", tokens)
        self.assertIn("q80-high.mp4", tokens)
        self.assertEqual(comparison["output"]["path"].split(".")[-1], "png")
        self.assertEqual(
            comparison["output"]["sha256"],
            comparison["repeatability"]["repeat_sha256"],
        )

    def test_encoder_identity_is_scoped_without_cross_machine_claim(self) -> None:
        identity = self.manifest["environment"][
            "media_foundation_software_encoder_registration"
        ]
        self.assertEqual(identity["clsid"], "{6CA50344-051A-4DED-9779-A43305165E35}")
        self.assertEqual(identity["original_filename"].lower(), "mfh264enc.dll")
        self.assertEqual(identity["company_name"], "Microsoft Corporation")
        self.assertEqual(identity["signature_status"], "Valid")
        self.assertIn("not a direct per-process CLSID trace", identity["attestation_scope"])
        self.assertNotIn("source.rgb", {
            item["path"] for item in self.manifest["external_artifacts"]["files"]
        })


if __name__ == "__main__":
    unittest.main()
