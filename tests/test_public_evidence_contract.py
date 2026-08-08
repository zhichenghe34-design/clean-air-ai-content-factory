from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.verify_public_evidence import (
    MPT_ENGINE_ARTIFACTS,
    MPT_ENGINE_IDENTITY,
    _engine_contract,
    _validate_mpt_evidence,
    sha256,
    validate_srt,
)


class PublicEvidenceContractTests(unittest.TestCase):
    def test_legacy_v2_keeps_frozen_timing_contract(self):
        with tempfile.TemporaryDirectory() as folder_name:
            captions = Path(folder_name) / "captions.srt"
            captions.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n第一句\n\n"
                "2\n00:00:01,040 --> 00:00:05,000\n第二句\n",
                encoding="utf-8",
            )
            self.assertEqual(validate_srt(captions, 5.1, contract="legacy_v2"), [])

            captions.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n第一句\n\n"
                "2\n00:00:02,500 --> 00:00:04,000\n第二句\n",
                encoding="utf-8",
            )
            errors = validate_srt(captions, 5.5, contract="legacy_v2")
            self.assertTrue(any("不连续" in error for error in errors))
            self.assertTrue(any("终点" in error for error in errors))

    def test_mpt_contract_binds_full_text_and_allows_at_most_two_second_gaps(self):
        with tempfile.TemporaryDirectory() as folder_name:
            captions = Path(folder_name) / "captions.srt"
            captions.write_text(
                "1\n00:00:00,500 --> 00:00:01,000\n第一句，\n\n"
                "2\n00:00:02,500 --> 00:00:04,000\n第二句。\n",
                encoding="utf-8",
            )
            self.assertEqual(
                validate_srt(captions, 5.5, "第一句，第二句。", contract="mpt_v0.3"),
                [],
            )
            mismatch = validate_srt(captions, 5.5, "完全不同的批准稿", contract="mpt_v0.3")
            self.assertTrue(any("正文" in error for error in mismatch))

            too_wide = validate_srt(captions, 6.001, "第一句，第二句。", contract="mpt_v0.3")
            self.assertTrue(any("超过 2.000s" in error for error in too_wide))

    def test_mpt_contract_rejects_overlap(self):
        with tempfile.TemporaryDirectory() as folder_name:
            captions = Path(folder_name) / "captions.srt"
            captions.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\n第一句\n\n"
                "2\n00:00:01,998 --> 00:00:04,000\n第二句\n",
                encoding="utf-8",
            )
            errors = validate_srt(captions, 4.0, "第一句第二句", contract="mpt_v0.3")
            self.assertTrue(any("重叠" in error for error in errors))

    def test_engine_evidence_pair_selects_contract_and_rejects_partial_pair(self):
        base = {"manifest.json", "final.mp4"}
        contract, errors = _engine_contract(base, base)
        self.assertEqual((contract, errors), ("legacy_v2", []))

        mpt = base | MPT_ENGINE_ARTIFACTS
        contract, errors = _engine_contract(mpt, mpt)
        self.assertEqual((contract, errors), ("mpt_v0.3", []))

        contract, errors = _engine_contract(base | {"engine_report.json"}, mpt)
        self.assertEqual(contract, "legacy_v2")
        self.assertTrue(errors)

    def test_mpt_report_binds_identity_script_sources_and_artifact_hashes(self):
        approved_script = "这是经过人工批准的脚本。"
        task_id = "11111111-1111-4111-8111-111111111111"
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            (folder / "voice.wav").write_bytes(b"voice")
            (folder / "final.mp4").write_bytes(b"video")
            (folder / "captions.srt").write_text(
                f"1\n00:00:00,000 --> 00:00:05,000\n{approved_script}\n",
                encoding="utf-8",
            )
            (folder / "material_sources.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "task_id": task_id,
                    "sources": [{"provider": "local", "local_file": "fixture.mp4"}],
                }),
                encoding="utf-8",
            )
            artifact_names = ("final.mp4", "captions.srt", "material_sources.json")
            report = {
                "schema_version": 1,
                "status": "complete",
                "engine": dict(MPT_ENGINE_IDENTITY),
                "task_id": task_id,
                "script_sha256": hashlib.sha256(approved_script.encode("utf-8")).hexdigest().upper(),
                "artifacts": [
                    {
                        "relative_path": name,
                        "size": (folder / name).stat().st_size,
                        "sha256": sha256(folder / name).upper(),
                    }
                    for name in artifact_names
                ],
                "control_layer_validation": {
                    "status": "passed",
                    "canonical_voice_sha256": sha256(folder / "voice.wav").upper(),
                    "final_video_sha256": sha256(folder / "final.mp4").upper(),
                    "captions_sha256": sha256(folder / "captions.srt").upper(),
                },
            }
            self.assertEqual(_validate_mpt_evidence(folder, report, approved_script), [])
            report["engine"] = {**MPT_ENGINE_IDENTITY, "version": "latest"}
            self.assertTrue(_validate_mpt_evidence(folder, report, approved_script))


if __name__ == "__main__":
    unittest.main()
