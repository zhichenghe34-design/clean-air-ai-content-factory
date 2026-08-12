from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from core.animation_registry import (
    AnimationRegistry,
    AnimationRegistryError,
    DEFAULT_PACK_PATH,
    canonical_sha256,
    validate_animation_pack,
)
from core.motion_director import (
    CLEAN_AIR_EXPLAINER_TEMPLATE_FILE,
    CINEMATIC_TEMPLATE_FILE,
    CINEMATIC_VISUAL_FILE,
    FONT_BOLD_FILE,
    FONT_REGULAR_FILE,
    MotionPlanError,
    TEMPLATE_FILE,
    build_motion_plan,
    build_motion_project,
    derive_motion_segments,
    validate_motion_plan,
)


def _segments(count: int = 6) -> list[dict[str, str]]:
    captions = [
        "先明确用户真正需要解决的问题。",
        "把流程拆成可检查的执行步骤。",
        "时间与阶段必须按顺序记录。",
        "每条结论都要核验资料来源。",
        "比较方案时说明各自适用边界。",
        "最后给出下一步行动原则。",
        "未知风险保持未知，不用动画替代证据。",
        "收束所有依据并形成明确判断。",
    ]
    return [
        {"kicker": f"要点{i}", "title": f"可信动画{i}", "caption": captions[i - 1]}
        for i in range(1, count + 1)
    ]


class AnimationRegistryTests(unittest.TestCase):
    def test_legacy_natural_script_uses_short_actionable_scene_titles(self):
        script = (
            "闻不到气味，不代表甲醛达标。"
            "第一步先看它讨论的对象和使用场景。"
            "第二步核对剂量、空间体积和作用时间。"
            "保存完整检测报告并持续通风，请专业人员判断。"
            "记住，感觉只能提醒你，证据才能支持结论。"
        )
        segments = derive_motion_segments(
            "闻不到气味，不代表甲醛达标",
            script,
            target_count=5,
            capability_pack={"id": "legacy-clean-air-v2"},
        )
        titles = [segment["title"] for segment in segments]
        self.assertIn("先看对象与场景", titles)
        self.assertIn("报告 × 通风 × 判断", titles)
        self.assertEqual("感觉 ≠ 结论", titles[-1])

    def test_clean_air_explainer_keeps_complete_sentences_and_binds_each_diagram(self):
        sentences = [
            "甲醛检测仪数值低，就能直接安心入住吗？先别只看这一刻的数字。",
            "测量位置、门窗状态和测量时间不同，读数可能出现变化。",
            "一次读数反映的是当时结果，不能代替持续观察，也不能证明污染来源已经消失。",
            "先记录检测条件，再在相同条件下复测，观察结果是否稳定。",
            "涉及具体产品时，要核对检测材料、适用空间和使用条件。",
            "把数字、条件和来源放在一起看，再决定下一步。",
        ]
        segments = derive_motion_segments(
            "低读数能否直接安心入住？",
            "".join(sentences),
            target_count=7,
            capability_pack={"id": "legacy-clean-air-v2"},
        )
        self.assertEqual([item["caption"] for item in segments], sentences)
        self.assertEqual(
            [item["visual_content"]["kind"] for item in segments],
            [
                "low-reading",
                "measurement-conditions",
                "single-reading",
                "retest-process",
                "product-conditions",
                "final-checklist",
            ],
        )
        self.assertEqual(
            segments[1]["visual_content"]["items"],
            ["测量位置", "门窗状态", "测量时间"],
        )

    def test_bundled_pack_is_hash_bound_and_families_are_explicit(self):
        registry = AnimationRegistry.load()
        summary = registry.summary()
        self.assertGreaterEqual(summary["block_count"], 36)
        self.assertGreaterEqual(summary["renderer_family_count"], 12)
        families = {block["renderer_family"] for block in registry.pack["blocks"]}
        self.assertEqual(families, {family["id"] for family in registry.pack["renderer_families"]})
        self.assertEqual(summary["renderer"], "hyperframes-waapi-v1")
        family_counts = {
            family_id: sum(block["renderer_family"] == family_id for block in registry.pack["blocks"])
            for family_id in families
        }
        self.assertTrue(all(count >= 3 for count in family_counts.values()))

    def test_template_implements_every_family_and_every_registered_variant(self):
        registry = AnimationRegistry.load()
        template = TEMPLATE_FILE.read_text(encoding="utf-8")
        variant_source = template.split("const FAMILY_VARIANTS = ", 1)[1].split(";", 1)[0]
        template_variants = json.loads(variant_source)
        registered_variants: dict[str, list[str]] = {}
        for block in registry.pack["blocks"]:
            registered_variants.setdefault(block["renderer_family"], []).append(block["id"])
        self.assertEqual(
            {family: set(block_ids) for family, block_ids in template_variants.items()},
            {family: set(block_ids) for family, block_ids in registered_variants.items()},
        )
        builders = template.split("const FAMILY_BUILDERS = {", 1)[1].split("function visualMarkup", 1)[0]
        animators = template.split("const FAMILY_ANIMATORS = {", 1)[1].split("function animateScene", 1)[0]
        for family in registered_variants:
            with self.subTest(family=family):
                self.assertIn(f'"{family}":', builders)
                self.assertIn(f'"{family}":', animators)
                self.assertEqual(len(template_variants[family]), len(set(template_variants[family])))
                self.assertGreaterEqual(len(template_variants[family]), 3)
        for family, blocks in registered_variants.items():
            labels = {registry.get(block_id)["label"] for block_id in blocks}
            motions = {registry.get(block_id)["motion"]["primary"] for block_id in blocks}
            self.assertEqual(len(labels), len(blocks), family)
            self.assertEqual(len(motions), len(blocks), family)
        self.assertIn('data-variant="${type}"', template)

    def test_new_offline_blocks_are_selectable_through_the_whitelist(self):
        registry = AnimationRegistry.load()
        cases = (
            ("risk-gauge", ["metric", "risk", "boundary"], "generic"),
            ("evidence-pyramid", ["evidence", "source", "boundary"], "legacy_clean_air"),
            ("source-network", ["source", "evidence", "document"], "generic"),
            ("claim-guard", ["boundary", "evidence", "source"], "legacy_clean_air"),
        )
        for block_id, tags, pack_mode in cases:
            with self.subTest(block_id=block_id):
                selected, matched = registry.select(
                    semantic_tags=tags,
                    pack_mode=pack_mode,
                    duration_seconds=7.0,
                    preferred_ids=[block_id],
                )
                self.assertEqual(selected["id"], block_id)
                self.assertEqual(matched, sorted(tags))

    def test_pack_rejects_unknown_fields_and_hash_tampering(self):
        raw = json.loads(DEFAULT_PACK_PATH.read_text(encoding="utf-8"))
        with_unknown = copy.deepcopy(raw)
        with_unknown["download_url"] = "not-allowed"
        with self.assertRaises(AnimationRegistryError):
            validate_animation_pack(with_unknown)
        tampered = copy.deepcopy(raw)
        tampered["blocks"][0]["label"] = "被篡改"
        with self.assertRaisesRegex(AnimationRegistryError, "哈希不匹配"):
            validate_animation_pack(tampered)

    def test_semantic_selection_is_whitelisted_and_avoids_adjacent_repeat(self):
        registry = AnimationRegistry.load()
        first, matched = registry.select(
            semantic_tags=["metric", "evidence"],
            pack_mode="generic",
            duration_seconds=7.0,
            preferred_ids=["stat-ring", "signal-grid"],
        )
        second, _ = registry.select(
            semantic_tags=["metric", "evidence"],
            pack_mode="generic",
            duration_seconds=7.0,
            previous_block_id=first["id"],
            preferred_ids=[first["id"], "signal-grid", "magnifier"],
        )
        self.assertTrue(matched)
        self.assertNotEqual(first["id"], second["id"])
        self.assertIn(second["id"], {block["id"] for block in registry.pack["blocks"]})

    def test_motion_plan_records_and_validates_selection_receipt(self):
        plan = build_motion_plan("企业服务怎样建立信任？", "企业客户", _segments(), 48.0)
        report = validate_motion_plan(plan)
        self.assertEqual(report["registry_sha256"], plan["animation_registry"]["sha256"])
        self.assertEqual(plan["animation_pack_mode"], "generic")
        self.assertEqual(plan["selection_receipt"]["animation_pack_mode"], "generic")
        self.assertEqual(len(plan["selection_receipt"]["selections"]), len(plan["scenes"]))
        self.assertTrue(all(
            left["visual_type"] != right["visual_type"]
            for left, right in zip(plan["scenes"], plan["scenes"][1:])
        ))
        tampered = copy.deepcopy(plan)
        tampered["selection_receipt"]["selections"][0]["renderer_family"] = "forged-family"
        with self.assertRaises(MotionPlanError):
            validate_motion_plan(tampered)

    def test_real_build_keeps_generic_variant_branches_reachable_and_legacy_compatible(self):
        generic_plan = build_motion_plan("企业服务怎样建立信任？", "企业客户", _segments(7), 45.0)
        selected = {scene["visual_type"] for scene in generic_plan["scenes"]}
        self.assertTrue({"timeline-pulse", "source-stack", "option-compare"}.issubset(selected))
        legacy_plan = build_motion_plan(
            "除醛数据怎样核验？",
            "新房家庭",
            _segments(7),
            45.0,
            capability_pack={"id": "legacy-clean-air-v2"},
        )

        with tempfile.TemporaryDirectory() as folder:
            output_root = Path(folder)
            generic_output = output_root / "generic"
            legacy_output = output_root / "legacy"
            build_motion_project(generic_output, generic_plan)
            legacy_built = build_motion_project(
                legacy_output,
                legacy_plan,
                capability_pack={"id": "legacy-clean-air-v2"},
            )
            generic_html = (generic_output / "index.html").read_text(encoding="utf-8")
            legacy_html = (legacy_output / "index.html").read_text(encoding="utf-8")
            legacy_asset_exists = (legacy_output / "assets" / CINEMATIC_VISUAL_FILE.name).is_file()
            generic_asset_exists = (generic_output / "assets" / CINEMATIC_VISUAL_FILE.name).exists()
            legacy_motion = json.loads((legacy_output / "index.motion.json").read_text(encoding="utf-8"))

        self.assertIn(
            'if (type === "timeline-pulse") return `<div class="timeline-board"',
            generic_html,
        )
        self.assertIn(
            'if (type === "source-stack") return `<div class="doc-stack"',
            generic_html,
        )
        self.assertIn(
            'const labels = type === "compare" ? ["已有依据","实际场景"] : ["方案 A","方案 B"]',
            generic_html,
        )
        for legacy_id, generic_id in (
            ("clock-wave", "timeline-pulse"),
            ("report-scan", "source-stack"),
            ("compare", "option-compare"),
        ):
            self.assertNotIn(
                f'(type === "{legacy_id}" || type === "{generic_id}")',
                generic_html,
            )
        self.assertIn('"visual_type": "timeline-pulse"', generic_html)
        self.assertIn('"visual_type": "source-stack"', generic_html)
        self.assertIn('"visual_type": "option-compare"', generic_html)
        self.assertIn('<i>时</i><span>净界AI内容工厂</span>', legacy_html)
        self.assertIn('"visual_type": "liquid-chamber"', legacy_html)
        self.assertIn('"visual_type": "orbit-summary"', legacy_html)
        self.assertEqual(legacy_built["template"], CLEAN_AIR_EXPLAINER_TEMPLATE_FILE.name)
        self.assertIsNone(legacy_built["visual_asset_sha256"])
        self.assertFalse(legacy_asset_exists)
        self.assertFalse(generic_asset_exists)
        self.assertIn(
            {"kind": "keepsMoving", "withinSelector": "#scenes", "maxStaticSec": 2.5},
            legacy_motion["assertions"],
        )
        self.assertIn(
            {"kind": "staysInFrame", "selector": "#scene-01 .visual"},
            legacy_motion["assertions"],
        )
        self.assertNotIn("clean-air-device-neutral-v1.png", legacy_html)
        self.assertNotIn("第一项", legacy_html)
        self.assertNotIn("已有依据", legacy_html)
        self.assertNotIn("scene-wipe", legacy_html)
        self.assertNotIn("translateX(", legacy_html)
        self.assertNotIn("data-layout-allow-occlusion", legacy_html)
        self.assertIn('finite(node.querySelector(".semantic-progress i")', legacy_html)
        self.assertIn('const visual = scene.visual_content;', legacy_html)
        self.assertIn('const unorderedTargets = Array.from', legacy_html)
        self.assertIn('scene.visual_content.focus_order', legacy_html)
        self.assertNotIn('检测仪当下读数', legacy_html)
        self.assertNotIn('<strong>安心入住</strong>', legacy_html)
        self.assertIn('scene.visual_content.summary', legacy_html)

    def test_generic_plan_rejects_legacy_only_block_even_with_rehashed_receipt(self):
        registry = AnimationRegistry.load()
        plan = build_motion_plan("企业服务怎样建立信任？", "企业客户", _segments(), 48.0)
        tampered = copy.deepcopy(plan)
        scene = tampered["scenes"][2]
        block = registry.get("liquid-chamber")
        scene.update({
            "visual_type": block["id"],
            "renderer_family": block["renderer_family"],
            "semantic_tags": ["space", "metric", "boundary"],
            "primary_motion": block["motion"]["primary"],
            "secondary_motion": block["motion"]["secondary"],
            "transition": block["motion"]["transition"],
        })
        selection = tampered["selection_receipt"]["selections"][2]
        selection.update({
            "block_id": block["id"],
            "renderer_family": block["renderer_family"],
            "semantic_tags": list(scene["semantic_tags"]),
            "matched_tags": sorted(set(scene["semantic_tags"]).intersection(block["semantic_tags"])),
        })
        receipt = tampered["selection_receipt"]
        receipt["sha256"] = canonical_sha256({key: value for key, value in receipt.items() if key != "sha256"})
        with self.assertRaisesRegex(MotionPlanError, "animation_pack_mode"):
            validate_motion_plan(tampered)

    def test_receipt_rejects_forged_or_missing_matched_tags_after_rehash(self):
        plan = build_motion_plan("企业服务怎样建立信任？", "企业客户", _segments(), 48.0)
        for mutation in ("forged", "empty", "missing"):
            with self.subTest(mutation=mutation):
                tampered = copy.deepcopy(plan)
                selection = tampered["selection_receipt"]["selections"][0]
                if mutation == "forged":
                    selection["matched_tags"] = ["risk"]
                elif mutation == "empty":
                    selection["matched_tags"] = []
                else:
                    selection.pop("matched_tags")
                receipt = tampered["selection_receipt"]
                receipt["sha256"] = canonical_sha256({key: value for key, value in receipt.items() if key != "sha256"})
                with self.assertRaises(MotionPlanError):
                    validate_motion_plan(tampered)

    def test_legacy_plan_reserves_orbit_summary_for_the_final_scene(self):
        plan = build_motion_plan(
            "除醛数据怎样核验？",
            "新房家庭",
            _segments(7),
            52.0,
            capability_pack={"id": "legacy-clean-air-v2"},
        )
        self.assertEqual(plan["scenes"][-1]["visual_type"], "orbit-summary")
        self.assertEqual(plan["animation_pack_mode"], "legacy_clean_air")
        self.assertEqual(plan["selection_receipt"]["animation_pack_mode"], "legacy_clean_air")
        self.assertNotIn("orbit-summary", [scene["visual_type"] for scene in plan["scenes"][:-1]])
        self.assertIn("liquid-chamber", [scene["visual_type"] for scene in plan["scenes"][:-1]])
        validate_motion_plan(plan)

    def test_natural_legacy_seven_scene_plan_reserves_space_block_without_collision(self):
        titles = [
            "装修后先别急着下结论",
            "一个数字不等于安全结论",
            "先看报告的采样条件",
            "把检测流程拆开核对",
            "比较工具的适用边界",
            "结论不能代替专业检测",
            "给家庭一份行动清单",
        ]
        captions = [
            "装修完成后，气味和体感只能作为线索，不能直接说明室内空气安全。",
            "检测数字要结合房间空间、采样时间和方法理解，单个数字不能代表安全。",
            "查看报告时先核对采样点、采样时长、环境条件和资料来源。",
            "按准备、采样、分析和记录的顺序逐项检查，不跳过关键步骤。",
            "不同工具适合不同场景，比较时要说明精度、范围和使用限制。",
            "内容只能帮助整理判断框架，不能代替具备资质的专业检测。",
            "保存完整报告、保持合理通风，并在重要决策前咨询专业人员。",
        ]
        segments = [
            {"kicker": f"核验步骤{i}", "title": title, "caption": caption}
            for i, (title, caption) in enumerate(zip(titles, captions), start=1)
        ]
        plan = build_motion_plan(
            "装修后怎样判断室内空气风险？",
            "新房家庭",
            segments,
            45.0,
            capability_pack={"id": "legacy-clean-air-v2"},
        )
        self.assertEqual(plan["scenes"][2]["visual_type"], "liquid-chamber")
        self.assertNotEqual(plan["scenes"][1]["visual_type"], "liquid-chamber")
        self.assertTrue(all(item["matched_tags"] for item in plan["selection_receipt"]["selections"]))
        validate_motion_plan(plan)

    def test_uneven_eight_scene_script_keeps_every_scene_within_registry_duration(self):
        segments = _segments(8)
        segments[0]["caption"] = "短句。"
        segments[1]["caption"] = "这是一段明显更长的解释，用来验证极端不均匀文本权重不会生成低于可信最小时长的镜头。"
        plan = build_motion_plan("不均匀脚本测试", "目标受众", segments, 45.0)
        registry = AnimationRegistry.load()
        for scene in plan["scenes"]:
            block = registry.get(scene["visual_type"])
            actual = scene["end"] - scene["start"]
            self.assertGreaterEqual(actual, block["duration_seconds"]["minimum"])
            self.assertLessEqual(actual, block["duration_seconds"]["maximum"])

    def test_template_is_offline_finite_waapi_and_project_bundles_noto_fonts(self):
        template = TEMPLATE_FILE.read_text(encoding="utf-8")
        for forbidden in (
            "http://", "https://", "cdn.jsdelivr", "gsap", "Microsoft YaHei",
            "Math.random", "requestAnimationFrame", "setTimeout", "iterations: Infinity",
            "iterations: -1", "repeat: -1", "Date.now", "performance.now", "fetch(",
            "XMLHttpRequest", "WebSocket", "EventSource",
        ):
            self.assertNotIn(forbidden, template)
        self.assertIn("element.animate", template)
        self.assertEqual(template.count(".animate("), 1)
        self.assertIn('iterations: 1', template)
        self.assertIn("data-no-timeline", template)
        self.assertIn("data-layout-allow-overflow", template)
        self.assertIn(
            '[{transform:"translateY(150px)"},{transform:"translateY(0)"}]',
            template,
        )
        self.assertNotIn(
            '[{transform:"translateY(150px)",opacity:0}',
            template,
        )
        self.assertIn('url("assets/NotoSansSC-Regular.ttf")', template)
        self.assertTrue(FONT_REGULAR_FILE.is_file())
        self.assertTrue(FONT_BOLD_FILE.is_file())

        plan = build_motion_plan("离线动画如何交付？", "企业客户", _segments(4), 45.0)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "motion"
            built = build_motion_project(output, plan)
            self.assertEqual(set(built["font_sha256"]), {FONT_REGULAR_FILE.name, FONT_BOLD_FILE.name})
            self.assertTrue((output / "assets" / FONT_REGULAR_FILE.name).is_file())
            self.assertTrue((output / "assets" / FONT_BOLD_FILE.name).is_file())


if __name__ == "__main__":
    unittest.main()
