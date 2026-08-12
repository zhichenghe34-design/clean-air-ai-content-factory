from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from core.orchestrator import JobStore, PUBLIC_ARTIFACTS
from core.production import (
    KNOWN_TEST_MATERIAL_SHA256,
    ProductionRunner,
    VISUAL_QC_SAMPLE_COUNT,
    VideoVisualQualityBlocked,
    _bind_visual_qc_to_engine_report,
    analyze_visual_motion_pairs,
    analyze_visual_qc_frames,
    verify_video_visuals,
)
from core.provider import BudgetLedger
from core.review_policy import build_review_policy


def normal_frames() -> list[Image.Image]:
    frames: list[Image.Image] = []
    for index in range(VISUAL_QC_SAMPLE_COUNT):
        image = Image.new("RGB", (360, 640), (20 + index * 3, 48 + index * 4, 76 + index * 5))
        draw = ImageDraw.Draw(image)
        left = 15 + index * 25
        draw.ellipse((left, 90 + index * 8, left + 150, 240 + index * 8), fill=(230, 232, 215))
        draw.rectangle((40, 350 - index * 5, 320, 390 + index * 5), fill=(45, 120, 100))
        frames.append(image)
    return frames


def active_motion_pairs() -> list[tuple[Image.Image, Image.Image]]:
    pairs: list[tuple[Image.Image, Image.Image]] = []
    for index in range(VISUAL_QC_SAMPLE_COUNT):
        left = Image.new("RGB", (360, 640), (16, 34, 48))
        right = left.copy()
        ImageDraw.Draw(left).ellipse((30, 170, 180, 320), fill=(80, 205, 245))
        ImageDraw.Draw(right).ellipse((125, 105, 275, 255), fill=(80, 205, 245))
        pairs.append((left, right))
    return pairs


class VisualQualityGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.timestamps = [float(index * 4 + 2) for index in range(VISUAL_QC_SAMPLE_COUNT)]

    def test_rgb_cmy_vertical_test_bars_are_blocked(self):
        colors = [
            (255, 0, 0),
            (0, 255, 0),
            (255, 255, 0),
            (0, 0, 255),
            (255, 0, 255),
            (0, 255, 255),
        ]
        image = Image.new("RGB", (600, 900))
        draw = ImageDraw.Draw(image)
        for index, color in enumerate(colors):
            draw.rectangle((index * 100, 0, (index + 1) * 100, 900), fill=color)
        report = analyze_visual_qc_frames([image.copy() for _ in range(12)], self.timestamps)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("test_pattern_color_bars", report["blocking_reasons"])
        self.assertEqual(report["checks"]["test_color_bars"]["detected_frame_count"], 12)

    def test_known_testsrc2_material_sha_is_always_blocked(self):
        digest = next(iter(KNOWN_TEST_MATERIAL_SHA256))
        report = analyze_visual_qc_frames(
            normal_frames(), self.timestamps, material_hashes=[digest]
        )
        self.assertEqual(report["status"], "blocked")
        self.assertIn("known_test_fixture_material", report["blocking_reasons"])

    def test_normal_moving_frames_pass(self):
        report = analyze_visual_qc_frames(normal_frames(), self.timestamps)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["checks"]["test_color_bars"]["detected_frame_count"], 0)

    def test_extreme_repetition_requires_visual_review(self):
        image = Image.new("RGB", (360, 640), (35, 45, 55))
        report = analyze_visual_qc_frames([image.copy() for _ in range(12)], self.timestamps)
        self.assertEqual(report["status"], "needs_visual_review")
        self.assertIn("extreme_visual_repetition", report["review_reasons"])

    def test_motion_density_rejects_static_cards(self):
        image = Image.new("RGB", (360, 640), (35, 45, 55))
        report = analyze_visual_motion_pairs(
            [(image.copy(), image.copy()) for _ in range(VISUAL_QC_SAMPLE_COUNT)],
            self.timestamps,
        )
        self.assertEqual(report["status"], "needs_visual_review")
        self.assertEqual(report["active_pair_count"], 0)
        self.assertEqual(report["longest_inactive_run"], VISUAL_QC_SAMPLE_COUNT)

    def test_motion_density_accepts_sustained_internal_motion(self):
        report = analyze_visual_motion_pairs(active_motion_pairs(), self.timestamps)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["active_pair_count"], VISUAL_QC_SAMPLE_COUNT)

    def test_semantic_motion_profile_allows_reading_holds_but_not_static_cards(self):
        moving = active_motion_pairs()
        static = Image.new("RGB", (360, 640), (35, 45, 55))
        reading_hold_pairs = [
            pair if index in {1, 4, 7, 10} else (static.copy(), static.copy())
            for index, pair in enumerate(moving)
        ]
        report = analyze_visual_motion_pairs(
            reading_hold_pairs,
            self.timestamps,
            motion_profile="semantic_motion_graphics",
        )
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["active_pair_count"], 4)
        all_static = analyze_visual_motion_pairs(
            [(static.copy(), static.copy()) for _ in range(VISUAL_QC_SAMPLE_COUNT)],
            self.timestamps,
            motion_profile="semantic_motion_graphics",
        )
        self.assertEqual(all_static["status"], "needs_visual_review")

    def test_verifier_writes_contact_sheet_and_json(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            video = folder / "final.mp4"
            video.write_bytes(b"video-fixture")
            with (
                patch(
                    "core.production._extract_visual_qc_frames",
                    return_value=(normal_frames(), self.timestamps),
                ),
                patch(
                    "core.production._extract_visual_motion_pairs",
                    return_value=(active_motion_pairs(), self.timestamps),
                ),
            ):
                report = verify_video_visuals(video, output_dir=folder)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["checks"]["motion_density"]["status"], "passed")
            self.assertTrue((folder / "contact-sheet.png").is_file())
            on_disk = json.loads((folder / "visual-qc.json").read_text(encoding="utf-8"))
            self.assertEqual(on_disk["sample_count"], 12)
            self.assertEqual(on_disk["video"]["name"], "final.mp4")

    def test_visual_qc_artifacts_are_eligible_for_run_manifest(self):
        self.assertIn("contact-sheet.png", PUBLIC_ARTIFACTS)
        self.assertIn("visual-qc.json", PUBLIC_ARTIFACTS)
        with tempfile.TemporaryDirectory() as folder_name:
            root = Path(folder_name)
            staging = root / "staging"
            staging.mkdir()
            (staging / "contact-sheet.png").write_bytes(b"png")
            (staging / "visual-qc.json").write_text('{"status":"passed"}', encoding="utf-8")
            store = JobStore(root / "runtime")
            manifest = store._build_manifest(
                {
                    "id": "visual-qc-job",
                    "production_input": {},
                    "capability_pack": {},
                    "learning_rule_ids": [],
                    "approvals": {"research": {}, "compliance": {}},
                    "review_policy": build_review_policy(),
                },
                {
                    "run_id": "visual-qc-run",
                    "stage": "render",
                    "started_at": "2026-08-09T00:00:00+08:00",
                },
                staging,
                SimpleNamespace(budget=BudgetLedger(7)),
            )
            names = {item["name"] for item in manifest["artifacts"]}
            self.assertEqual(names, {"contact-sheet.png", "visual-qc.json"})

    def test_engine_artifact_contract_stays_exact_and_review_status_is_nested(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            (folder / "contact-sheet.png").write_bytes(b"png")
            (folder / "visual-qc.json").write_text(
                '{"status":"needs_visual_review"}', encoding="utf-8"
            )
            engine_artifacts = [
                {"relative_path": name}
                for name in ("final.mp4", "captions.srt", "material_sources.json")
            ]
            (folder / "engine_report.json").write_text(
                json.dumps({
                    "artifacts": engine_artifacts,
                    "control_layer_validation": {"status": "passed"},
                }),
                encoding="utf-8",
            )
            _bind_visual_qc_to_engine_report(folder, {
                "status": "needs_visual_review",
                "sample_count": 12,
                "blocking_reasons": [],
                "review_reasons": ["extreme_visual_repetition"],
            })
            report = json.loads((folder / "engine_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["artifacts"], engine_artifacts)
            self.assertEqual(report["control_layer_validation"]["status"], "passed")
            self.assertEqual(
                report["control_layer_validation"]["visual_qc"]["status"],
                "needs_visual_review",
            )

    def test_production_runner_never_completes_needs_visual_review(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            script = (
                "先确认问题、对象和使用场景，再核对来源、时间与适用范围。"
                "没有完整证据时，不给出数字、效果保证或绝对结论。"
                "最后列出已确认内容、仍缺材料和下一步核验动作，让每一句都可以复查。"
            )
            (folder / "approved_script.json").write_text(
                json.dumps({"script": script}, ensure_ascii=False), encoding="utf-8"
            )
            (folder / "review.json").write_text(
                '{"status":"passed","blocked":false}', encoding="utf-8"
            )

            def voice_adapter(output: Path, _script: str, _config: dict) -> dict:
                (output / "voice.wav").write_bytes(b"RIFF" + b"\0" * 64)
                return {"engine": "fake"}

            def render_adapter(output: Path, _plan: dict, _config: dict) -> dict:
                (output / "final.mp4").write_bytes(b"fake-video")
                return {"mode": "fake", "duration_seconds": 52.0}

            def needs_review_adapter(_video: Path, *, output_dir: Path, **_kwargs) -> dict:
                (output_dir / "contact-sheet.png").write_bytes(b"contact")
                payload = {
                    "status": "needs_visual_review",
                    "sample_count": 12,
                    "blocking_reasons": [],
                    "review_reasons": ["extreme_visual_repetition"],
                }
                (output_dir / "visual-qc.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                return payload

            runner = ProductionRunner(
                voice_adapter=voice_adapter,
                render_adapter=render_adapter,
                visual_qc_adapter=needs_review_adapter,
            )
            with patch.object(ProductionRunner, "_audio_duration", return_value=52.0):
                with self.assertRaises(VideoVisualQualityBlocked) as raised:
                    runner.run_render_stage(
                        folder,
                        {"topic": "如何核验室内空气信息？", "audience": "普通家庭"},
                        {
                            "research": {"status": "approved"},
                            "compliance": {"status": "approved"},
                        },
                    )
            self.assertIn("等待视觉复核", str(raised.exception))
            self.assertFalse((folder / "run_report.json").exists())
            self.assertTrue((folder / "visual-qc.json").is_file())
            self.assertTrue((folder / "contact-sheet.png").is_file())


if __name__ == "__main__":
    unittest.main()
