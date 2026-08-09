from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from core.review_policy import (
    AGENT_TEST_IDENTITY,
    AGENT_TEST_REVIEW,
    CODEX_TEST_REVIEWER,
    HUMAN_IDENTITY,
    HUMAN_STAGE_REVIEW,
    approval_identity,
    approval_validation_line,
    classify_approval_record,
)
from tools.build_public_evidence import (
    public_sanitization_validation_line,
    record_public_sanitization,
    sanitize_research_text,
)
from tools.verify_public_evidence import (
    MPT_ENGINE_ARTIFACTS,
    MPT_ENGINE_IDENTITY,
    _engine_contract,
    _validate_mpt_evidence,
    _validate_mpt_review_contract,
    sha256,
    validate_srt,
)


class PublicEvidenceContractTests(unittest.TestCase):
    @staticmethod
    def _approval(reviewer: str, identity: dict[str, object]) -> dict[str, object]:
        return {
            "status": "approved",
            "reviewer": reviewer,
            **identity,
            "reviewed_at": "2026-08-09T12:00:00+08:00",
            "artifact_sha256": "a" * 64,
        }

    def test_public_evidence_accepts_explicit_human_formal_contract(self):
        approvals = {
            "research": self._approval("何sir", HUMAN_IDENTITY),
            "compliance": self._approval("何sir", HUMAN_IDENTITY),
        }
        for gate in approvals.values():
            mode, errors = classify_approval_record(gate, allow_legacy_human=False)
            self.assertEqual((mode, errors), (HUMAN_STAGE_REVIEW, []))
        line, errors = approval_validation_line(approvals, allow_legacy_human=False)
        self.assertEqual(errors, [])
        self.assertIn("用户本人操作", line)
        self.assertNotIn("测试代理", line)
        self.assertEqual(
            _validate_mpt_review_contract(
                {
                    "review_policy": {
                        "stage_review_mode": HUMAN_STAGE_REVIEW,
                        "final_human_acceptance_required": False,
                    },
                    "evidence_status": "human_stage_reviews_complete",
                },
                [HUMAN_STAGE_REVIEW, HUMAN_STAGE_REVIEW],
            ),
            [],
        )

    def test_public_evidence_accepts_explicit_agent_test_contract_without_human_claim(self):
        approvals = {
            "research": self._approval(CODEX_TEST_REVIEWER, AGENT_TEST_IDENTITY),
            "compliance": self._approval(CODEX_TEST_REVIEWER, AGENT_TEST_IDENTITY),
        }
        for gate in approvals.values():
            mode, errors = classify_approval_record(gate, allow_legacy_human=False)
            self.assertEqual((mode, errors), (AGENT_TEST_REVIEW, []))
        line, errors = approval_validation_line(approvals, allow_legacy_human=False)
        self.assertEqual(errors, [])
        self.assertIn("test_only", line)
        self.assertIn("未冒充用户本人签署", line)
        self.assertIn("最终成片验收另行记录", line)
        self.assertEqual(
            _validate_mpt_review_contract(
                {
                    "review_policy": {
                        "stage_review_mode": AGENT_TEST_REVIEW,
                        "final_human_acceptance_required": True,
                    },
                    "evidence_status": "test_only_pending_human_acceptance",
                },
                [AGENT_TEST_REVIEW, AGENT_TEST_REVIEW],
            ),
            [],
        )

    def test_public_evidence_rejects_forged_or_partial_identity_combinations(self):
        forged = [
            self._approval(CODEX_TEST_REVIEWER, HUMAN_IDENTITY),
            self._approval("何sir", AGENT_TEST_IDENTITY),
            {
                **self._approval("何sir", HUMAN_IDENTITY),
                "actor_type": "agent",
            },
            {
                key: value
                for key, value in self._approval("何sir", HUMAN_IDENTITY).items()
                if key != "human_approval_claimed"
            },
        ]
        for gate in forged:
            with self.subTest(gate=gate):
                mode, errors = classify_approval_record(gate, allow_legacy_human=False)
                self.assertIsNone(mode)
                self.assertTrue(errors)
        invalid_manifests = [
            ({"evidence_status": "human_stage_reviews_complete"}, [HUMAN_STAGE_REVIEW] * 2),
            (
                {
                    "review_policy": {
                        "stage_review_mode": AGENT_TEST_REVIEW,
                        "final_human_acceptance_required": True,
                    },
                    "evidence_status": "human_stage_reviews_complete",
                },
                [HUMAN_STAGE_REVIEW] * 2,
            ),
            (
                {
                    "review_policy": {
                        "stage_review_mode": HUMAN_STAGE_REVIEW,
                        "final_human_acceptance_required": False,
                    },
                    "evidence_status": "test_only_pending_human_acceptance",
                },
                [HUMAN_STAGE_REVIEW] * 2,
            ),
        ]
        for manifest, modes in invalid_manifests:
            with self.subTest(manifest=manifest, modes=modes):
                self.assertTrue(_validate_mpt_review_contract(manifest, modes))

    def test_human_review_rejects_obvious_automation_names_after_unicode_normalization(self):
        obvious_automation_names = (
            "Codex",
            "C.o.d.e.x",
            "C.o.d.e.x何",
            "AGENT",
            "何ＡＧＥＮＴ",
            "CodexAgent",
            "ＣｏｄｅｘＡｇｅｎｔ",
            "systembot",
            "testagent",
            "automationbot",
            "auto-mation",
            "s y s t e m",
            "ｔｅｓｔ 审核员",
            "M.O.C.K",
            "b-o-t",
            "c i",
            "审核代理",
            "自 动 化",
            "系 统",
            "测 试",
            "机 器 人",
        )
        for reviewer in obvious_automation_names:
            with self.subTest(reviewer=reviewer):
                with self.assertRaises(ValueError):
                    approval_identity(HUMAN_STAGE_REVIEW, reviewer)

                structured = self._approval(reviewer, HUMAN_IDENTITY)
                mode, errors = classify_approval_record(structured, allow_legacy_human=False)
                self.assertIsNone(mode)
                self.assertTrue(errors)

                legacy = {
                    "status": "approved",
                    "reviewer": reviewer,
                    "reviewed_at": "2026-07-18T12:00:00+08:00",
                    "artifact_sha256": "b" * 64,
                }
                mode, errors = classify_approval_record(legacy, allow_legacy_human=True)
                self.assertIsNone(mode)
                self.assertTrue(errors)

    def test_human_review_keeps_normal_names_and_agent_test_identity(self):
        for reviewer in ("何sir", "Contest Winner", "Systematic Liu", "Botan"):
            with self.subTest(reviewer=reviewer):
                identity = approval_identity(HUMAN_STAGE_REVIEW, reviewer)
                self.assertEqual(identity, {"reviewer": reviewer, **HUMAN_IDENTITY})
                self.assertEqual(
                    classify_approval_record(identity, allow_legacy_human=False),
                    (HUMAN_STAGE_REVIEW, []),
                )

        agent_identity = approval_identity(AGENT_TEST_REVIEW, CODEX_TEST_REVIEWER)
        self.assertEqual(agent_identity, {"reviewer": CODEX_TEST_REVIEWER, **AGENT_TEST_IDENTITY})
        self.assertEqual(
            classify_approval_record(agent_identity, allow_legacy_human=False),
            (AGENT_TEST_REVIEW, []),
        )

    def test_public_evidence_allows_identity_free_records_only_for_legacy_contract(self):
        legacy = {
            "status": "approved",
            "reviewer": "历史审批人",
            "reviewed_at": "2026-07-18T12:00:00+08:00",
            "artifact_sha256": "b" * 64,
        }
        self.assertEqual(
            classify_approval_record(legacy, allow_legacy_human=True),
            (HUMAN_STAGE_REVIEW, []),
        )
        mode, errors = classify_approval_record(legacy, allow_legacy_human=False)
        self.assertIsNone(mode)
        self.assertTrue(any("缺少结构化审查身份" in error for error in errors))

    def test_public_evidence_sanitizes_email_and_strict_mainland_phone(self):
        phone = "".join(("138", "0013", "8000"))
        sanitized, email_count, phone_count = sanitize_research_text(
            f"联系 test@example.com 或 {phone}；"
            "边界数字 9138001380001、138001380001 和非手机号 12800138000 保留。"
        )
        self.assertEqual((email_count, phone_count), (1, 1))
        self.assertIn("[REDACTED_EMAIL]", sanitized)
        self.assertIn("[REDACTED_PHONE]", sanitized)
        self.assertNotIn("test@example.com", sanitized)
        self.assertNotIn(f"{phone}；", sanitized)
        self.assertIn("9138001380001", sanitized)
        self.assertIn("138001380001", sanitized)
        self.assertIn("12800138000", sanitized)

    def test_public_sanitization_preserves_source_hash_and_records_public_copy(self):
        with tempfile.TemporaryDirectory() as folder_name:
            research_path = Path(folder_name) / "research.json"
            research_path.write_text('{"trace":"[REDACTED_PHONE]"}', encoding="utf-8")
            source_approval_hash = "a" * 64
            approvals: dict[str, object] = {
                "research": {"artifact_sha256": source_approval_hash},
            }

            record_public_sanitization(approvals, research_path, 2, 3)

            research_approval = approvals["research"]
            self.assertIsInstance(research_approval, dict)
            self.assertEqual(research_approval["artifact_sha256"], source_approval_hash)
            self.assertEqual(research_approval["source_artifact_sha256"], source_approval_hash)
            self.assertEqual(research_approval["public_artifact_sha256"], sha256(research_path))
            self.assertEqual(
                research_approval["public_sanitization"],
                {
                    "email_addresses_redacted": 2,
                    "mainland_phone_numbers_redacted": 3,
                    "scope": "source-page contact text only",
                },
            )
            validation_line = public_sanitization_validation_line(2, 3)
            self.assertIn("2 处来源页联系邮箱", validation_line)
            self.assertIn("3 处大陆手机号", validation_line)
            self.assertIn("[REDACTED_EMAIL]", validation_line)
            self.assertIn("[REDACTED_PHONE]", validation_line)

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
