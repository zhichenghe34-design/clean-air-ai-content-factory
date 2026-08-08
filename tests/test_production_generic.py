from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.motion_director import build_motion_plan, build_motion_project, derive_motion_segments
from core.production import (
    ProductionRunner,
    build_local_variants,
    estimate_narration_duration,
    review_script,
)


GENERIC_PACK = {
    "schema_version": 1,
    "id": "sales-workflow-v1",
    "version": 1,
    "sha256": "a" * 64,
    "generated_at": "2026-08-03T10:00:00+08:00",
    "source": "test",
    "snapshot": {
        "label": "销售工作流内容包",
        "industry": "企业服务销售",
        "goal": "帮助销售新人解释客户跟进流程",
        "audience": "销售新人",
        "platforms": ["抖音", "TikTok"],
        "content_purpose": "解释客户跟进方法",
        "tone": ["清晰", "克制"],
        "preferred_terms": ["以当前资料为准"],
        "avoided_terms": ["行业第一"],
        "evidence_requirements": ["来源", "时间", "对象", "适用范围"],
        "prohibited_claims": ["不得虚构客户证言"],
        "visual_direction": {
            "style": "precise kinetic editorial",
            "brand_name": "销售证据台",
            "keywords": ["路径", "节点", "复核"],
            "accent_color": "#3366FF",
        },
        "assumptions": ["具体企业资料尚待补充"],
        "risk_level": "medium",
    },
    "audit": {"status": "test"},
}

LEGACY_PACK = {
    "id": "legacy-clean-air-v2",
    "version": 2,
    "sha256": "b" * 64,
    "snapshot": {"label": "净界AI内容工厂"},
}

LEARNING_RULES = [{
    "rule_id": "rule-no-lowest-price",
    "scope": "project",
    "instruction": "以后不要出现“最低价”",
    "pack_id": "sales-workflow-v1",
    "source_event_ids": ["correction-1"],
}]

LEGACY_TERMS = ("甲醛", "除醛", "新房", "入住", "检测报告", "实验舱", "试验舱", "净界")


class GenericProductionTests(unittest.TestCase):
    def test_no_key_research_keeps_findings_empty_instead_of_fabricating_evidence(self):
        runner = ProductionRunner(provider=None, research_config={"enabled": True})
        with tempfile.TemporaryDirectory() as folder_name:
            research = runner.run_research_stage(
                Path(folder_name),
                {"topic": "如何跟进第一次接触的企业客户？", "audience": "销售新人"},
            )["research"]
        self.assertEqual(research["status"], "offline")
        self.assertEqual(research["findings"], [])
        self.assertEqual(research["sources"], [])

    def test_offline_variants_are_topic_bound_process_only_and_duration_safe(self):
        topic = "如何跟进第一次接触的企业客户？"
        variants = build_local_variants(topic, "销售新人", [], GENERIC_PACK, LEARNING_RULES)

        self.assertEqual(len(variants), 4)
        for item in variants:
            self.assertIn(topic, item["script"])
            self.assertEqual(item["source"], "local_process_only")
            self.assertFalse(any(term in item["script"] for term in LEGACY_TERMS))
            estimate = estimate_narration_duration(item["script"])
            self.assertGreaterEqual(estimate["estimated_seconds"], 35)
            self.assertLessEqual(estimate["estimated_seconds"], 75)

    def test_evidence_fallback_uses_strict_claim_not_freeform_review_summary(self):
        finding = {
            "finding_id": "finding-1",
            "claim": "公开流程材料列出需求确认、方案说明和后续复盘三个环节。",
            "review_summary": "净界甲醛实验舱内容不应被带入普通项目。",
            "strict_review_status": "proven_for_limited_use",
            "script_eligible": True,
            "evidence": [{"source_type": "company_public", "excerpt": "需求确认、方案说明和后续复盘"}],
        }
        variants = build_local_variants(
            "怎样讲清客户跟进流程？", "销售新人", [finding], GENERIC_PACK, LEARNING_RULES
        )

        for item in variants:
            self.assertIn(finding["claim"], item["script"])
            self.assertNotIn("甲醛", item["script"])
            self.assertEqual(item["evidence_finding_ids"], ["finding-1"])
            estimate = estimate_narration_duration(item["script"])
            self.assertGreaterEqual(estimate["estimated_seconds"], 35)
            self.assertLessEqual(estimate["estimated_seconds"], 75)

    def test_learning_rule_and_immutable_generic_safety_kernel_block(self):
        learned = review_script(
            "我们坚持最低价。", [], GENERIC_PACK, LEARNING_RULES
        )
        self.assertTrue(learned["blocked"])
        self.assertTrue(any(item.get("rule_id") == "rule-no-lowest-price" for item in learned["warnings"]))

        for script, warning_type in (
            ("这套方案只要99元。", "unsupported_numeric_claim"),
            ("所有客户一致好评。", "fabricated_testimonial_certification_ranking"),
            ("这项投资保证收益。", "financial_return_guarantee"),
            ("采用方案后保证胜诉。", "legal_outcome_guarantee"),
        ):
            result = review_script(script, [], GENERIC_PACK, [])
            self.assertTrue(result["blocked"], script)
            self.assertTrue(any(item["type"] == warning_type for item in result["warnings"]), script)

    def test_unapproved_qualitative_fact_is_blocked_and_exact_finding_can_support_it(self):
        claim = "咖啡豆来自埃塞俄比亚高海拔产区，因此风味更明亮"
        unsupported = review_script(f"{claim}。", [], GENERIC_PACK, [])
        self.assertTrue(unsupported["blocked"])
        self.assertTrue(any(item["type"] == "unsupported_qualitative_claim" for item in unsupported["warnings"]))

        finding = {
            "finding_id": "finding-origin-1",
            "claim": claim,
            "strict_review_status": "proven_for_limited_use",
            "script_eligible": True,
            "evidence": [{"source_type": "source_page", "excerpt": claim}],
        }
        supported = review_script(f"该来源页面称，{claim}。", [finding], GENERIC_PACK, [])
        self.assertFalse(supported["blocked"])

    def test_insight_and_motion_read_generic_pack_without_legacy_semantics(self):
        config = {
            "topic": "如何跟进第一次接触的企业客户？",
            "audience": "销售新人",
            "pattern_card_ids": ["03", "06"],
            "capability_pack": GENERIC_PACK,
            "learning_rules": LEARNING_RULES,
        }
        insight = ProductionRunner._build_insight(config, {"findings": [], "tool_trace": []})
        self.assertEqual(insight["evidence_requirements"], GENERIC_PACK["snapshot"]["evidence_requirements"])
        self.assertEqual(insight["tone"], ["清晰", "克制"])
        self.assertEqual(insight["selected_pattern_cards"], [])
        self.assertEqual(insight["official_references"], [])
        self.assertNotIn("GB/T 18883", json.dumps(insight, ensure_ascii=False))

        script = build_local_variants(config["topic"], config["audience"], [], GENERIC_PACK, LEARNING_RULES)[0]["script"]
        segments = derive_motion_segments(config["topic"], script, capability_pack=GENERIC_PACK)
        plan = build_motion_plan(config["topic"], config["audience"], segments, 52, capability_pack=GENERIC_PACK)
        self.assertEqual(plan["project"]["name"], "evidence-motion-output")
        self.assertEqual(plan["project"]["brand_name"], "销售证据台")
        self.assertTrue(all(scene["visual_type"] not in {"liquid-chamber", "report-scan", "compare"} for scene in plan["scenes"]))

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "motion"
            built = build_motion_project(output, plan, capability_pack=GENERIC_PACK)
            html = (output / "index.html").read_text(encoding="utf-8")
            self.assertEqual(built["project_name"], "evidence-motion-output")
            self.assertIn("销售证据台", html)
            self.assertFalse(any(term in html for term in LEGACY_TERMS))
            self.assertNotIn("clean-air-motion-output", html)

    def test_offline_stages_record_pack_and_learning_rule_identity(self):
        config = {
            "topic": "如何跟进第一次接触的企业客户？",
            "audience": "销售新人",
            "capability_pack": GENERIC_PACK,
            "learning_rules": LEARNING_RULES,
            "render_mode": "static",
        }

        def voice_adapter(folder: Path, script: str, production_input: dict) -> dict:
            (folder / "voice.wav").write_bytes(b"test-wave")
            return {"engine": "fake"}

        def render_adapter(folder: Path, plan: dict, production_input: dict) -> dict:
            (folder / "final.mp4").write_bytes(b"test-video")
            return {"ok": True, "mode": "fake", "duration_seconds": 52}

        runner = ProductionRunner(
            provider=None,
            research_config={"enabled": False},
            voice_adapter=voice_adapter,
            render_adapter=render_adapter,
        )
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            runner.run_research_stage(folder, config)
            content = runner.run_content_stage(folder, config, {"findings": []})
            self.assertFalse(content["review"]["blocked"])
            with (
                patch.object(ProductionRunner, "_normalize_voice_duration", return_value={"duration_seconds": 52}),
                patch.object(ProductionRunner, "_audio_duration", return_value=52),
            ):
                report = runner.run_render_stage(
                    folder,
                    config,
                    {"research": {"status": "approved", "findings": []}, "compliance": {"status": "approved"}},
                )
            self.assertEqual(report["capability_pack"], {
                "id": "sales-workflow-v1", "version": 1, "sha256": "a" * 64,
            })
            self.assertEqual(report["learning_rule_ids"], ["rule-no-lowest-price"])
            artifacts = "".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in folder.glob("*.json")
            )
            self.assertFalse(any(term in artifacts for term in LEGACY_TERMS))

    def test_legacy_pack_preserves_clean_air_six_condition_logic(self):
        insight = ProductionRunner._build_insight({
            "topic": "99%除醛率为什么必须看检测条件？",
            "audience": "新房家庭",
            "pattern_card_ids": [],
            "capability_pack": LEGACY_PACK,
        })
        self.assertEqual(insight["evidence_requirements"], [
            "剂量", "空间体积", "作用时间", "初始浓度", "检测方法", "报告来源",
        ])
        self.assertIn("GB/T 18883", json.dumps(insight, ensure_ascii=False))

        script = build_local_variants(
            "99%除醛率为什么必须看检测条件？", "新房家庭", None, LEGACY_PACK, []
        )[0]["script"]
        self.assertIn("甲醛", script)
        segments = derive_motion_segments("除醛数据怎样核验？", script, capability_pack=LEGACY_PACK)
        plan = build_motion_plan("除醛数据怎样核验？", "新房家庭", segments, 52, capability_pack=LEGACY_PACK)
        self.assertEqual(plan["project"]["name"], "clean-air-motion-output")
        self.assertIn("liquid-chamber", {scene["visual_type"] for scene in plan["scenes"]})


if __name__ == "__main__":
    unittest.main()
