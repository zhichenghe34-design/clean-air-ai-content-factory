from __future__ import annotations

import hashlib
import json
import mimetypes
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from core.animation_registry import canonical_sha256
from core.motion_director import build_motion_plan, derive_motion_segments, validate_motion_plan
from core.production import estimate_narration_duration
from core.review_policy import (
    AGENT_TEST_IDENTITY,
    AGENT_TEST_REVIEW,
    AGENT_TEST_SCRIPT_EDIT_IDENTITY,
    BROWSER_SCRIPT_EDIT_LABELS,
    CODEX_TEST_REVIEWER,
    HUMAN_IDENTITY,
    HUMAN_STAGE_REVIEW,
    HUMAN_SCRIPT_EDIT_IDENTITY,
    MECHANICAL_IDENTITY,
    MECHANICAL_REVIEWER,
    MECHANICAL_STAGE_REVIEW,
    approval_identity,
    approval_validation_line,
    classify_approval_record,
    classify_script_edit_record,
    script_edit_validation_line,
)
from core.voice_contract import (
    DEFAULT_VOICE_CHUNK_MAX_CHARS,
    DEFAULT_VOICE_ENGINE,
    DEFAULT_VOICE_NAME,
    DEFAULT_VOICE_RATE,
    VOICE_SCENE_MAX_DELIVERED_CHARACTERS_PER_SECOND,
    VOICE_SCENE_PAUSE_SECONDS,
    VOICE_SEGMENT_CONTRACT_VERSION,
    voice_segments_digest,
)
from tools.build_public_evidence import (
    public_sanitization_validation_line,
    record_public_sanitization,
    sanitize_research_text,
)
from tools.verify_public_evidence import (
    MPT_ENGINE_ARTIFACTS,
    MPT_ENGINE_IDENTITY,
    MOTION_ENGINE_IDENTITY,
    MOTION_EVIDENCE_ARTIFACTS,
    _engine_contract,
    _validate_script_edit_contract,
    _validate_motion_evidence,
    _validate_mpt_evidence,
    _validate_mpt_review_contract,
    caption_binding_text,
    evidence_artifacts_for_contract,
    probe_video,
    sha256,
    validate_srt,
    verify,
)


def write_pcm_wave(path: Path, duration_seconds: float = 52.0, *, amplitude: int = 64) -> None:
    frame_count = round(48000 * duration_seconds)
    positive = int(amplitude).to_bytes(2, "little", signed=True)
    negative = (-int(amplitude)).to_bytes(2, "little", signed=True)
    sample_pair = positive + negative
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        chunk_frames = 48000
        remaining = frame_count
        while remaining:
            current = min(chunk_frames, remaining)
            payload = sample_pair * (current // 2)
            if current % 2:
                payload += positive
            audio.writeframesraw(payload)
            remaining -= current
        audio.writeframes(b"")


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

    def test_probe_video_rejects_non_finite_ffprobe_duration(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            ffprobe = folder / "ffprobe.exe"
            video = folder / "final.mp4"
            ffprobe.write_bytes(b"probe")
            video.write_bytes(b"video")
            payload = json.dumps(
                {
                    "format": {"duration": "NaN"},
                    "streams": [
                        {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920},
                        {"codec_type": "audio", "codec_name": "aac"},
                    ],
                }
            )

            with mock.patch(
                "tools.verify_public_evidence.subprocess.check_output",
                return_value=payload,
            ):
                with self.assertRaisesRegex(RuntimeError, "无效成片时长"):
                    probe_video(video, ffprobe_path=ffprobe)

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

    def test_public_evidence_accepts_mechanical_internal_candidate_contract(self):
        approvals = {
            "research": self._approval(MECHANICAL_REVIEWER, MECHANICAL_IDENTITY),
            "compliance": self._approval(MECHANICAL_REVIEWER, MECHANICAL_IDENTITY),
        }
        for gate in approvals.values():
            mode, errors = classify_approval_record(gate, allow_legacy_human=False)
            self.assertEqual((mode, errors), (MECHANICAL_STAGE_REVIEW, []))
        line, errors = approval_validation_line(approvals, allow_legacy_human=False)
        self.assertEqual(errors, [])
        self.assertIn("human_approval_claimed=false", line)
        self.assertIn("内部候选", line)
        self.assertEqual(
            _validate_mpt_review_contract(
                {
                    "review_policy": {
                        "stage_review_mode": MECHANICAL_STAGE_REVIEW,
                        "final_human_acceptance_required": True,
                    },
                    "evidence_status": "mechanically_reviewed_internal_candidate_pending_human_release",
                },
                [MECHANICAL_STAGE_REVIEW, MECHANICAL_STAGE_REVIEW],
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

    def test_script_edit_identity_is_bound_to_review_policy_without_human_impersonation(self):
        agent_record = {
            **BROWSER_SCRIPT_EDIT_LABELS,
            "script": "代理测试通过浏览器修改的脚本。",
            "edited_at": "2026-08-10T12:00:00+08:00",
            "editor_identity": {
                "editor": CODEX_TEST_REVIEWER,
                **AGENT_TEST_SCRIPT_EDIT_IDENTITY,
            },
        }
        self.assertEqual(
            classify_script_edit_record(agent_record, allow_legacy_human=False),
            (AGENT_TEST_REVIEW, []),
        )
        line, errors = script_edit_validation_line(
            agent_record, allow_legacy_human=False
        )
        self.assertEqual(errors, [])
        self.assertIn("human_edit_claimed=false", line)
        self.assertIn("未冒充人工精修", line)
        agent_manifest = {
            "review_policy": {
                "stage_review_mode": AGENT_TEST_REVIEW,
                "final_human_acceptance_required": True,
            }
        }
        self.assertEqual(
            _validate_script_edit_contract(agent_manifest, agent_record, "motion_v0.3"),
            [],
        )

        human_record = {
            **BROWSER_SCRIPT_EDIT_LABELS,
            "script": "用户通过浏览器修改的脚本。",
            "edited_at": "2026-08-10T12:00:00+08:00",
            "editor_identity": {"editor": "何sir", **HUMAN_SCRIPT_EDIT_IDENTITY},
        }
        self.assertTrue(
            _validate_script_edit_contract(agent_manifest, human_record, "motion_v0.3")
        )

        contradictory_current_record = {
            "id": "human-edited",
            "hook_type": "人工精修",
            "selected_by": "human_editor",
            "script": "实际由代理改写却标成人工的旧记录。",
            "edited_at": "2026-08-10T12:00:00+08:00",
        }
        current_errors = _validate_script_edit_contract(
            agent_manifest, contradictory_current_record, "motion_v0.3"
        )
        self.assertTrue(any("缺少结构化编辑身份" in error for error in current_errors))
        self.assertEqual(
            classify_script_edit_record(
                contradictory_current_record, allow_legacy_human=True
            ),
            (HUMAN_STAGE_REVIEW, []),
        )

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
            self.assertEqual(
                validate_srt(captions, 5.5, "第一句，第二句。", contract="motion_v0.3"),
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

        motion = base | MOTION_EVIDENCE_ARTIFACTS
        contract, errors = _engine_contract(motion, motion)
        self.assertEqual((contract, errors), ("motion_v0.3", []))
        self.assertEqual(
            evidence_artifacts_for_contract(contract),
            MOTION_EVIDENCE_ARTIFACTS,
        )

        contract, errors = _engine_contract(base | {"engine_report.json"}, mpt)
        self.assertEqual(contract, "legacy_v2")
        self.assertTrue(errors)

        contract, errors = _engine_contract(base | {"visual-qc.json"}, motion)
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

    def test_motion_report_binds_engine_plan_captions_and_visual_qc(self):
        approved_script = (
            "先明确客户真正关心的问题，再逐项核对来源、对象、时间和适用边界。"
            "没有完整证据时，不写未经批准的数字、功效、保证、证言或排名。"
            "最后列出已证实内容、仍缺材料和下一步核验动作，让每一句都能复查。"
        )
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            voice_path = folder / "voice.wav"
            write_pcm_wave(voice_path)
            final_video = folder / "final.mp4"
            final_video.write_bytes(b"motion-video")
            contact_sheet = folder / "contact-sheet.png"
            contact_sheet.write_bytes(b"\x89PNG\r\n\x1a\n" + b"contact-sheet")
            visual_qc = {
                "schema_version": 1,
                "status": "passed",
                "sample_count": 12,
                "blocking_reasons": [],
                "review_reasons": [],
                "checks": {"test_color_bars": {"status": "passed"}},
                "frames": [{"index": index} for index in range(12)],
                "video": {
                    "name": "final.mp4",
                    "size": final_video.stat().st_size,
                    "sha256": sha256(final_video).upper(),
                },
            }
            visual_path = folder / "visual-qc.json"
            visual_path.write_text(json.dumps(visual_qc), encoding="utf-8")
            plan = build_motion_plan(
                "如何核验本地服务信息？",
                "潜在客户",
                derive_motion_segments("如何核验本地服务信息？", approved_script),
                52.0,
            )
            binding = caption_binding_text(approved_script)
            voice_scenes = []
            voice_cursor = 0.0
            spoken_segment_duration = (52.0 - VOICE_SCENE_PAUSE_SECONDS * (len(plan["scenes"]) - 1)) / len(plan["scenes"])
            for index, scene in enumerate(plan["scenes"], start=1):
                spoken = int(estimate_narration_duration(scene["caption"])["spoken_characters"])
                pause_after = VOICE_SCENE_PAUSE_SECONDS if index < len(plan["scenes"]) else 0.0
                spoken_end = voice_cursor + spoken_segment_duration
                voice_end = spoken_end + pause_after
                scene["start"] = round(voice_cursor, 3)
                scene["end"] = round(voice_end, 3)
                voice_scenes.append({
                    "id": scene["id"],
                    "index": index,
                    "caption": scene["caption"],
                    "text_sha256": hashlib.sha256(scene["caption"].encode("utf-8")).hexdigest().upper(),
                    "spoken_characters": spoken,
                    "start_seconds": scene["start"],
                    "spoken_end_seconds": round(spoken_end, 3),
                    "end_seconds": scene["end"],
                    "spoken_duration_seconds": round(spoken_segment_duration, 3),
                    "pause_after_seconds": pause_after,
                    "spoken_characters_per_second": round(spoken / spoken_segment_duration, 3),
                })
                voice_cursor = voice_end
            (folder / "motion_plan.json").write_text(
                json.dumps(plan, ensure_ascii=False), encoding="utf-8"
            )
            for selection, scene in zip(plan["selection_receipt"]["selections"], plan["scenes"]):
                selection["duration_seconds"] = round(float(scene["end"]) - float(scene["start"]), 3)
            plan["selection_receipt"]["sha256"] = ""
            plan["selection_receipt"]["sha256"] = canonical_sha256({
                key: value for key, value in plan["selection_receipt"].items() if key != "sha256"
            })
            (folder / "motion_plan.json").write_text(
                json.dumps(plan, ensure_ascii=False), encoding="utf-8"
            )
            validation = validate_motion_plan(plan)
            total_spoken = int(estimate_narration_duration(approved_script)["spoken_characters"])
            report = {
                "status": "complete",
                "production_mode": "motion",
                "voice": {
                    "schema_version": 3,
                    "engine": DEFAULT_VOICE_ENGINE,
                    "requested_engine": DEFAULT_VOICE_ENGINE,
                    "voice": DEFAULT_VOICE_NAME,
                    "voice_rate": DEFAULT_VOICE_RATE,
                    "voice_selection_exposed": False,
                    "voice_chunk_max_chars": DEFAULT_VOICE_CHUNK_MAX_CHARS,
                    "segment_contract_version": VOICE_SEGMENT_CONTRACT_VERSION,
                    "segment_aligned": True,
                    "segment_count": len(voice_scenes),
                    "segments_sha256": voice_segments_digest(voice_scenes),
                    "scene_segments": voice_scenes,
                    "fallback": False,
                    "natural_voice": True,
                    "quality_eligible": True,
                    "tempo_adjusted": False,
                    "duration_source": "scene_voice_segments",
                    "duration_seconds": 52.0,
                    "pacing_status": "passed",
                    "spoken_characters": total_spoken,
                    "spoken_characters_per_second": round(total_spoken / 52.0, 3),
                    "maximum_spoken_characters_per_second": VOICE_SCENE_MAX_DELIVERED_CHARACTERS_PER_SECOND,
                    "script_sha256": hashlib.sha256(approved_script.encode("utf-8")).hexdigest().upper(),
                    "voice_sha256": sha256(voice_path).upper(),
                },
                "production_engine": {
                    **MOTION_ENGINE_IDENTITY,
                    "browser_strategy": "trusted_system_edge",
                    "browser_version": "151.0.1000.1",
                    "browser_minimum_major": 151,
                },
                "render": {
                    "mode": "animated_hyperframes",
                    "production_mode": "motion",
                    "duration_seconds": 52.0,
                    "runtime_source": "packaged",
                    "runtime_version": MOTION_ENGINE_IDENTITY["version"],
                    "renderer": MOTION_ENGINE_IDENTITY["renderer"],
                    "codec_strategy": MOTION_ENGINE_IDENTITY["codec_strategy"],
                    "patch_id": MOTION_ENGINE_IDENTITY["patch_id"],
                    "patch_version": MOTION_ENGINE_IDENTITY["patch_version"],
                    "patched_cli_sha256": MOTION_ENGINE_IDENTITY["patched_cli_sha256"],
                    "browser_strategy": "trusted_system_edge",
                    "browser_version": "151.0.1000.1",
                    "browser_minimum_major": 151,
                    "video_codec": "h264",
                    "audio_codec": "aac",
                    "width": 1080,
                    "height": 1920,
                    "motion_validation": validation,
                    "caption_validation": {
                        "status": "passed",
                        "cue_count": len(plan["scenes"]),
                        "text_sha256": hashlib.sha256(binding.encode("utf-8")).hexdigest().upper(),
                    },
                    "visual_qc": {
                        "status": "passed",
                        "sample_count": 12,
                        "blocking_reasons": [],
                        "review_reasons": [],
                        "report_sha256": sha256(visual_path).upper(),
                        "contact_sheet_sha256": sha256(contact_sheet).upper(),
                    },
                },
            }
            self.assertEqual(_validate_motion_evidence(folder, report, approved_script), [])

            write_pcm_wave(voice_path, amplitude=0)
            report["voice"]["voice_sha256"] = sha256(voice_path).upper()
            silent_voice_errors = _validate_motion_evidence(folder, report, approved_script)
            self.assertTrue(
                any("silent_or_near_silent_voice_wav" in error for error in silent_voice_errors)
            )
            write_pcm_wave(voice_path)
            report["voice"]["voice_sha256"] = sha256(voice_path).upper()

            def srt_time(seconds: float) -> str:
                milliseconds = round(seconds * 1000)
                hours, remainder = divmod(milliseconds, 3_600_000)
                minutes, remainder = divmod(remainder, 60_000)
                whole_seconds, milliseconds = divmod(remainder, 1000)
                return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"

            caption_lines: list[str] = []
            for index, scene in enumerate(plan["scenes"], start=1):
                caption_lines.extend([
                    str(index),
                    f"{srt_time(scene['start'])} --> {srt_time(scene['end'])}",
                    scene["caption"],
                    "",
                ])
            (folder / "captions.srt").write_text("\n".join(caption_lines), encoding="utf-8")
            json_payloads = {
                "research.json": {"findings": []},
                "insight.json": {},
                "script_variants.json": {"variants": [{"script": approved_script}]},
                "approved_script.json": {"script": approved_script},
                "review.json": {"status": "needs_human", "blocked": False},
                "run_report.json": report,
            }
            for name, payload in json_payloads.items():
                (folder / name).write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
            approvals = {
                "research": {
                    **self._approval("何sir", HUMAN_IDENTITY),
                    "artifact_sha256": sha256(folder / "research.json"),
                },
                "compliance": {
                    **self._approval("何sir", HUMAN_IDENTITY),
                    "artifact_sha256": sha256(folder / "review.json"),
                    "script_sha256": sha256(folder / "approved_script.json"),
                },
            }
            (folder / "approvals.json").write_text(
                json.dumps(approvals, ensure_ascii=False), encoding="utf-8"
            )
            approval_line, approval_errors = approval_validation_line(
                approvals, allow_legacy_human=False
            )
            self.assertEqual(approval_errors, [])
            (folder / "VALIDATION.md").write_text(approval_line + "\n", encoding="utf-8")
            expected_files = {
                "research.json", "insight.json", "script_variants.json",
                "approved_script.json", "review.json", "voice.wav", "captions.srt",
                "motion_plan.json", "final.mp4", "run_report.json", "approvals.json",
                "VALIDATION.md", "contact-sheet.png", "visual-qc.json",
            }
            manifest = {
                "schema_version": 2,
                "stage": "render",
                "status": "complete",
                "budget": {"limit": 7, "attempted": 0},
                "approval_hashes": {"research": sha256(folder / "research.json")},
                "review_policy": {
                    "stage_review_mode": HUMAN_STAGE_REVIEW,
                    "final_human_acceptance_required": False,
                },
                "evidence_status": "human_stage_reviews_complete",
                "artifacts": [
                    {
                        "name": name,
                        "size": (folder / name).stat().st_size,
                        "sha256": sha256(folder / name),
                        "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
                    }
                    for name in sorted(expected_files)
                ],
            }
            (folder / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
            with mock.patch(
                "tools.verify_public_evidence.probe_video",
                return_value={
                    "duration_seconds": 52.0,
                    "video_codec": "h264",
                    "audio_codec": "aac",
                    "width": 1080,
                    "height": 1920,
                },
            ):
                errors, media = verify(folder)
            self.assertEqual(errors, [])
            self.assertEqual(media["evidence_contract"], "motion_v0.3")

            with mock.patch(
                "tools.verify_public_evidence.probe_video",
                return_value={
                    "duration_seconds": float("nan"),
                    "video_codec": "h264",
                    "audio_codec": "aac",
                    "width": 1080,
                    "height": 1920,
                },
            ):
                non_finite_media_errors, _ = verify(folder)
            self.assertTrue(any("成片时长不在" in error for error in non_finite_media_errors))

            report["render"]["runtime_source"] = "development_repo"
            runtime_errors = _validate_motion_evidence(folder, report, approved_script)
            self.assertTrue(any("正式便携包" in error for error in runtime_errors))
            report["render"]["runtime_source"] = "packaged"

            report["render"]["duration_seconds"] = float("nan")
            nan_duration_errors = _validate_motion_evidence(folder, report, approved_script)
            self.assertTrue(any("时长不一致" in error for error in nan_duration_errors))
            report["render"]["duration_seconds"] = 52.0

            report["voice"]["voice_rate"] = "-15%"
            voice_errors = _validate_motion_evidence(folder, report, approved_script)
            self.assertTrue(any("invalid_voice_rate" in error for error in voice_errors))
            report["voice"]["voice_rate"] = DEFAULT_VOICE_RATE

            report["voice"]["scene_segments"][0]["start_seconds"] = 0.5
            binding_errors = _validate_motion_evidence(folder, report, approved_script)
            self.assertTrue(any("motion_plan_scene_binding_mismatch" in error for error in binding_errors))
            report["voice"]["scene_segments"][0]["start_seconds"] = plan["scenes"][0]["start"]

            report["render"]["browser_version"] = "150.0.999.1"
            browser_errors = _validate_motion_evidence(folder, report, approved_script)
            self.assertTrue(any("受信系统Edge" in error for error in browser_errors))
            report["render"]["browser_version"] = "151.0.1000.1"

            report["production_engine"] = {
                **report["production_engine"],
                "version": "latest",
            }
            self.assertTrue(_validate_motion_evidence(folder, report, approved_script))


if __name__ == "__main__":
    unittest.main()
