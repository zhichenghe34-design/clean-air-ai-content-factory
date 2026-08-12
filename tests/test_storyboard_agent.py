from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from core.motion_director import (
    CLEAN_AIR_EXPLAINER_TEMPLATE_FILE,
    MotionPlanError,
    build_motion_plan,
    build_motion_project,
    validate_motion_plan,
)
from core.production import ProductionRunner, build_local_variants
from core.provider import OpenAICompatibleProvider, ProviderError
from core.storyboard_agent import (
    StoryboardError,
    build_local_storyboard,
    storyboard_to_motion_segments,
    validate_storyboard,
)


CAPTIONS = [
    "检测仪显示数值较低，并不等于所有条件都已满足。",
    "先核对门窗状态、检测位置和检测时长。",
    "再在相同条件下复测，保留完整记录。",
    "最后结合材料来源和适用边界，再决定下一步。",
]
SCRIPT = "".join(CAPTIONS)
PACED_SCRIPT = build_local_variants("检测结果怎么判断", "家庭用户", [])[0]["script"]


def raw_storyboard() -> dict:
    return {
        "schema_version": 1,
        "content_mode": "educational",
        "narrative_arc": "反驳误区，再解释条件，最后给出核对步骤",
        "scenes": [
            {
                "caption": CAPTIONS[0],
                "kicker": "数值较低",
                "title": "检测仪显示数值较低",
                "summary": "所有条件都已满足",
                "layout": "claim_contrast",
                "items": ["检测仪显示数值较低", "所有条件都已满足"],
                "focus_order": [0, 1],
            },
            {
                "caption": CAPTIONS[1],
                "kicker": "先核对",
                "title": "门窗状态",
                "summary": "检测位置和检测时长",
                "layout": "condition_map",
                "items": ["门窗状态", "检测位置", "检测时长"],
                "focus_order": [0, 1, 2],
            },
            {
                "caption": CAPTIONS[2],
                "kicker": "再",
                "title": "相同条件下复测",
                "summary": "保留完整记录",
                "layout": "explain_points",
                "items": ["相同条件下复测", "保留完整记录"],
                "focus_order": [0, 1],
            },
            {
                "caption": CAPTIONS[3],
                "kicker": "最后",
                "title": "材料来源",
                "summary": "适用边界",
                "layout": "final_checklist",
                "items": ["材料来源", "适用边界", "决定下一步"],
                "focus_order": [0, 1, 2],
            },
        ],
    }


class StoryboardMechanicalReviewTests(unittest.TestCase):
    def test_local_storyboard_keeps_a_quoted_question_as_one_visual_phrase(self):
        script = (
            "对于“甲醛检测数值低，就能安心入住吗？”，不能只凭一个低数值下结论。"
            "先分清气味线索和仪器读数，再核对室内甲醛证据。"
            "先核对检测时的门窗状态、仪器位置和持续时间。"
            "气味和体感只是线索，不能替代规范检测。"
            "再看剂量、空间体积、作用时间、初始浓度、检测方法和报告来源。"
            "实验条件与真实房间不同，结论不能直接照搬；缺少来源和适用边界，也不能理解成入住保证。"
            "对新房家庭，建议保留报告、持续通风，重要决定前结合房屋情况请专业人员判断。"
        )

        storyboard = build_local_storyboard("甲醛检测数值低，就能安心入住吗？", script)
        first = storyboard["scenes"][0]

        self.assertEqual(first["title"], "甲醛检测数值低，就能安心入住吗？")
        self.assertEqual(first["kicker"], "甲醛检测数值低")
        self.assertEqual(
            first["items"],
            ["甲醛检测数值低，就能安心入住吗？", "不能只凭一个低数值下结论"],
        )
        self.assertNotIn("对于“甲醛检测数值低", first.values())
        self.assertNotEqual(first["layout"], "claim_contrast")
        self.assertNotEqual(storyboard["scenes"][3]["layout"], "claim_contrast")
        motion_scene = storyboard_to_motion_segments(storyboard)[0]
        self.assertEqual(motion_scene["title"], first["title"])
        self.assertEqual(motion_scene["visual_content"]["headline"], first["title"])
        self.assertEqual(motion_scene["visual_content"]["items"], first["items"])

    def test_local_storyboard_preserves_a_cross_industry_quoted_question(self):
        script = (
            "对于“新品价格低，就一定适合所有顾客吗？”，不能只凭价格下结论。"
            "先核对目标顾客和实际需求，再比较产品条件。"
            "先记录使用场景、预算范围和售后条件。"
            "价格只是线索，不能替代完整比较。"
            "再看材料、规格、使用周期、服务范围和信息来源。"
            "展示条件与实际使用不同，结论不能直接照搬；缺少来源和适用边界，也不能理解成购买保证。"
            "对普通顾客，建议保留资料、逐项核对，重要决定前结合实际情况判断。"
        )

        first = build_local_storyboard("新品价格怎么判断", script)["scenes"][0]

        self.assertEqual(first["title"], "新品价格低，就一定适合所有顾客吗？")
        self.assertEqual(first["summary"], "不能只凭价格下结论")

    def test_long_conclusion_after_quoted_question_remains_in_one_caption(self):
        first_sentence = (
            "对于“新品价格低，就一定适合所有顾客吗？”，不能只凭价格下结论，"
            "因为还要核对售后服务、交付周期和真实适用场景。"
        )
        script = first_sentence + (
            "先核对目标顾客和实际需求。再比较产品条件和服务范围。"
            "价格只是线索，不能替代完整比较。再查看材料规格和信息来源。"
            "展示条件与实际使用不同，结论不能直接照搬。"
            "对普通顾客，建议保留资料、逐项核对，重要决定前结合实际情况判断。"
        )

        storyboard = build_local_storyboard("新品价格怎么判断", script)

        self.assertEqual(storyboard["scenes"][0]["caption"], first_sentence)
        self.assertTrue(all(scene["caption"].count("“") == scene["caption"].count("”") for scene in storyboard["scenes"]))

    def test_provider_storyboard_rejects_an_unclosed_quoted_heading(self):
        value = raw_storyboard()
        value["scenes"][0].update({
            "caption": "对于“新品价格低，就一定适合所有顾客吗？”，不能只凭价格下结论。",
            "kicker": "新品价格低",
            "title": "对于“新品价格低",
            "summary": "不能只凭价格下结论",
            "items": ["新品价格低，就一定适合所有顾客吗？", "不能只凭价格下结论"],
        })
        script = "".join(scene["caption"] for scene in value["scenes"])

        with self.assertRaisesRegex(StoryboardError, "引号或括号不成对"):
            validate_storyboard(value, script, source="DeepSeek")

    def test_valid_provider_storyboard_is_normalized_and_bound_to_script(self):
        provider_value = raw_storyboard()
        provider_value.pop("narrative_arc")
        for scene in provider_value["scenes"]:
            scene.pop("kicker")
            scene.pop("title")
            scene.pop("summary")
        result = validate_storyboard(provider_value, SCRIPT, source="DeepSeek", model="deepseek")
        self.assertEqual(result["mechanical_review"]["status"], "passed")
        self.assertTrue(result["mechanical_review"]["script_fully_covered"])
        self.assertEqual(result["source"], "DeepSeek")
        self.assertEqual(result["narrative_arc"], "按旁白顺序完整展示")
        self.assertEqual("".join(item["caption"] for item in result["scenes"]), SCRIPT)
        self.assertTrue(all(scene["title"] in scene["caption"] for scene in result["scenes"]))

    def test_visual_words_must_come_verbatim_from_the_current_caption(self):
        value = raw_storyboard()
        value["scenes"][1]["items"][0] = "专业机构检测"
        with self.assertRaisesRegex(StoryboardError, "逐字短语"):
            validate_storyboard(value, SCRIPT, source="DeepSeek")

    def test_cards_may_drop_punctuation_but_not_change_words(self):
        value = raw_storyboard()
        value["scenes"][1]["caption"] = "先核对“门窗状态”、检测位置和检测时长。"
        value["scenes"][1]["items"] = ["门窗状态", "检测位置", "检测时长"]
        punctuated_script = "".join(item["caption"] for item in value["scenes"])
        result = validate_storyboard(value, punctuated_script, source="DeepSeek")
        self.assertEqual(result["scenes"][1]["items"][0], "门窗状态")

    def test_missing_narration_unknown_layout_and_css_are_rejected(self):
        missing = raw_storyboard()
        missing["scenes"][2]["caption"] = "保留完整记录。"
        missing["scenes"][2]["kicker"] = "保留"
        missing["scenes"][2]["title"] = "完整记录"
        missing["scenes"][2]["summary"] = "保留完整记录"
        missing["scenes"][2]["items"] = ["保留完整记录"]
        missing["scenes"][2]["focus_order"] = [0]
        with self.assertRaisesRegex(StoryboardError, "完整覆盖"):
            validate_storyboard(missing, SCRIPT, source="DeepSeek")

        unknown = raw_storyboard()
        unknown["scenes"][1]["layout"] = "freeform_canvas"
        with self.assertRaisesRegex(StoryboardError, "白名单"):
            validate_storyboard(unknown, SCRIPT, source="DeepSeek")

        coded = raw_storyboard()
        coded["scenes"][0]["css"] = "position:absolute"
        with self.assertRaisesRegex(StoryboardError, "未授权字段"):
            validate_storyboard(coded, SCRIPT, source="DeepSeek")

    def test_adjacent_layout_repeat_and_generic_placeholder_are_rejected(self):
        repeated = raw_storyboard()
        repeated["scenes"][1]["layout"] = "claim_contrast"
        with self.assertRaisesRegex(StoryboardError, "重复同一信息结构"):
            validate_storyboard(repeated, SCRIPT, source="DeepSeek")

        placeholder = raw_storyboard()
        placeholder["scenes"][1]["caption"] = "先核对第一项、检测位置和检测时长。"
        placeholder["scenes"][1]["title"] = "检测位置"
        placeholder["scenes"][1]["items"][0] = "第一项"
        placeholder_script = "".join(item["caption"] for item in placeholder["scenes"])
        with self.assertRaisesRegex(StoryboardError, "通用占位词"):
            validate_storyboard(placeholder, placeholder_script, source="DeepSeek")

    def test_sparse_multi_slot_layout_is_rejected_before_render(self):
        value = raw_storyboard()
        value["scenes"][2]["layout"] = "process_flow"
        with self.assertRaisesRegex(StoryboardError, "需要3到4项信息"):
            validate_storyboard(value, SCRIPT, source="DeepSeek")

        value = raw_storyboard()
        value["scenes"][2]["layout"] = "boundary_list"
        with self.assertRaisesRegex(StoryboardError, "需要3到5项信息"):
            validate_storyboard(value, SCRIPT, source="DeepSeek")

    def test_visible_heading_and_summary_must_also_match_the_narration(self):
        value = raw_storyboard()
        value["scenes"][0]["title"] = "专业结论"
        with self.assertRaisesRegex(StoryboardError, "title不是当前旁白的逐字短语"):
            validate_storyboard(value, SCRIPT, source="DeepSeek")

    def test_local_fallback_is_also_mechanically_valid(self):
        result = build_local_storyboard("检测结果怎么判断", SCRIPT)
        self.assertEqual(result["source"], "local_deterministic_storyboard")
        self.assertEqual(result["scenes"][-1]["layout"], "final_checklist")
        segments = storyboard_to_motion_segments(result)
        self.assertEqual("".join(item["caption"] for item in segments), SCRIPT)
        self.assertTrue(all(item["visual_content"]["items"] for item in segments))

    def test_local_fallback_keeps_complete_list_phrases(self):
        result = build_local_storyboard("安全表达", PACED_SCRIPT)
        visible_items = [item for scene in result["scenes"] for item in scene["items"]]
        self.assertNotIn("功效", visible_items)
        self.assertNotIn("价格", visible_items)
        self.assertNotIn("”", visible_items)
        self.assertTrue(any("功效、价格、业绩数字" in item for item in visible_items))

    def test_local_fallback_expands_existing_enumerations_instead_of_one_empty_card(self):
        script = (
            "对于“甲醛检测仪数值低为什么不能立刻安心入住”，不能只凭一个低数值下结论。"
            "先分清气味线索和仪器读数，再核对室内甲醛证据。"
            "先核对检测时的门窗状态、仪器位置和持续时间。"
            "气味和体感只是线索，不能替代规范检测。"
            "再看剂量、空间体积、作用时间、初始浓度、检测方法和报告来源。"
            "实验条件与真实房间不同，结论不能直接照搬；缺少来源和适用边界，也不能理解成入住保证。"
            "对上海装修后家庭，建议保留报告、持续通风，重要决定前结合房屋情况请专业人员判断。"
        )
        result = build_local_storyboard("检测条件", script)
        enumerated = next(
            scene for scene in result["scenes"] if "门窗状态" in scene["caption"]
        )
        self.assertEqual(
            enumerated["items"],
            ["先核对检测时的门窗状态", "仪器位置", "持续时间"],
        )
        self.assertNotEqual(enumerated["layout"], "explain_points")

    def test_dynamic_pack_executes_agent_storyboard_with_guarded_renderer(self):
        storyboard = validate_storyboard(raw_storyboard(), SCRIPT, source="DeepSeek", model="deepseek-test")
        segments = storyboard_to_motion_segments(storyboard)
        capability_pack = {"id": "dynamic-clean-air-pack", "version": "1"}
        plan = build_motion_plan(
            "检测结果怎么判断",
            "家庭用户",
            segments,
            36.0,
            capability_pack=capability_pack,
        )
        self.assertFalse(plan["project"]["legacy"])
        self.assertTrue(plan["project"]["agent_directed"])
        self.assertEqual(plan["scenes"][0]["visual_content"]["items"], raw_storyboard()["scenes"][0]["items"])
        validate_motion_plan(plan)
        with tempfile.TemporaryDirectory() as folder_name:
            project_dir = Path(folder_name) / "motion"
            built = build_motion_project(
                project_dir,
                plan,
                capability_pack=capability_pack,
            )
            html = (project_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn("height: 660px", html)
            self.assertIn("font-size: 58px", html)
            self.assertIn("overflow-wrap: anywhere", html)
            self.assertIn(".point-row b { color: #748d0f", html)
        self.assertEqual(built["template"], CLEAN_AIR_EXPLAINER_TEMPLATE_FILE.name)

        tampered = deepcopy(plan)
        tampered["scenes"][0]["visual_content"]["items"][0] = "旁白没有说过的结论"
        with self.assertRaisesRegex(MotionPlanError, "逐字绑定当前旁白"):
            validate_motion_plan(tampered)

    def test_motion_plan_uses_exact_scene_audio_boundaries_instead_of_text_weights(self):
        storyboard = validate_storyboard(raw_storyboard(), SCRIPT, source="DeepSeek")
        segments = storyboard_to_motion_segments(storyboard)
        bounds = [(0.0, 6.0), (6.0, 15.0), (15.0, 25.0), (25.0, 36.0)]
        timed_segments = [
            {**segment, "start": start, "end": end}
            for segment, (start, end) in zip(segments, bounds)
        ]
        plan = build_motion_plan(
            "检测结果怎么判断", "家庭用户", timed_segments, 36.0,
            capability_pack={"id": "dynamic-clean-air-pack", "version": "1"},
        )
        self.assertEqual(
            [(scene["start"], scene["end"]) for scene in plan["scenes"]], bounds
        )
        self.assertEqual(
            [row["duration_seconds"] for row in plan["selection_receipt"]["selections"]],
            [6.0, 9.0, 10.0, 11.0],
        )


class StoryboardProductionIntegrationTests(unittest.TestCase):
    class Provider:
        api_key = "configured"
        model = "deepseek-test"

        def generate_motion_storyboard(self, *_args):
            return raw_storyboard()

    class UnsafeProvider(Provider):
        def generate_motion_storyboard(self, *_args):
            value = raw_storyboard()
            value["scenes"][0]["html"] = "<script>unsafe()</script>"
            return value

    class RepairingProvider(Provider):
        def __init__(self):
            self.feedback = []

        def generate_motion_storyboard(self, *_args, mechanical_feedback=""):
            self.feedback.append(mechanical_feedback)
            if not mechanical_feedback:
                value = raw_storyboard()
                value["scenes"][0]["items"] = ["检测仪显示数值较低，并不等于所有条件都已满足。检测仪显示数值较低"]
                value["scenes"][0]["focus_order"] = [0]
                return value
            return raw_storyboard()

    class ContentProvider(Provider):
        def generate_content_scripts(self, *_args):
            return [
                {"id": f"candidate-{index}", "hook_type": "事实拆解", "script": PACED_SCRIPT}
                for index in range(4)
            ]

        def review_content_script(self, *_args, **_kwargs):
            return {
                "status": "pass_with_human_review",
                "risks": [],
                "suggested_script": "",
                "human_confirmation_required": True,
            }

        def generate_motion_storyboard(self, script, *_args):
            return build_local_storyboard("检测结果怎么判断", script)

    class LocalScriptAgentDirectorProvider(ContentProvider):
        def generate_content_scripts(self, *_args):
            raise ProviderError("脚本接口没有返回variants数组")

    def test_runner_accepts_valid_deepseek_director_output(self):
        runner = ProductionRunner(provider=self.Provider())
        storyboard, report = runner._generate_motion_storyboard(
            {"topic": "检测结果怎么判断"}, {}, SCRIPT, prefer_provider=True
        )
        self.assertEqual(report["source"], "DeepSeek")
        self.assertFalse(report["fallback_used"])
        self.assertEqual(storyboard["source"], "DeepSeek")

    def test_runner_falls_back_when_deepseek_director_output_fails_review(self):
        runner = ProductionRunner(provider=self.UnsafeProvider())
        storyboard, report = runner._generate_motion_storyboard(
            {"topic": "检测结果怎么判断"}, {}, SCRIPT, prefer_provider=True
        )
        self.assertTrue(report["fallback_used"])
        self.assertEqual(report["fallback_reason"], "provider_output_failed_mechanical_storyboard_review")
        self.assertEqual(storyboard["source"], "local_deterministic_storyboard")
        self.assertNotIn("html", json.dumps(storyboard, ensure_ascii=False))

    def test_runner_returns_mechanical_feedback_for_one_bounded_agent_repair(self):
        provider = self.RepairingProvider()
        runner = ProductionRunner(provider=provider)
        storyboard, report = runner._generate_motion_storyboard(
            {"topic": "检测结果怎么判断"}, {}, SCRIPT, prefer_provider=True
        )
        self.assertEqual(storyboard["source"], "DeepSeek")
        self.assertTrue(report["mechanical_retry_used"])
        self.assertEqual(len(provider.feedback), 2)
        self.assertIn("场景1.items无效", provider.feedback[1])

    def test_content_stage_automatically_persists_reviewed_agent_storyboard(self):
        runner = ProductionRunner(provider=self.ContentProvider())
        passed_review = {"status": "passed", "blocked": False, "warnings": []}
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            (folder / "research.json").write_text(
                json.dumps({"findings": [], "tool_trace": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            with mock.patch("core.production.review_script", return_value=deepcopy(passed_review)):
                result = runner.run_content_stage(
                    folder,
                    {"topic": "检测结果怎么判断", "audience": "家庭用户", "production_mode": "motion"},
                    {"findings": []},
                )
            stored = json.loads((folder / "motion_storyboard.json").read_text(encoding="utf-8"))
            provider_report = json.loads((folder / "script_variants.json").read_text(encoding="utf-8"))["provider"]
        self.assertEqual(result["approved_script"]["script"], PACED_SCRIPT)
        self.assertEqual(stored["source"], "DeepSeek")
        self.assertEqual(stored["mechanical_review"]["status"], "passed")
        self.assertEqual(provider_report["motion_storyboard"]["source"], "DeepSeek")

    def test_local_script_fallback_does_not_disable_deepseek_director(self):
        runner = ProductionRunner(provider=self.LocalScriptAgentDirectorProvider())
        passed_review = {"status": "passed", "blocked": False, "warnings": []}
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            (folder / "research.json").write_text(
                json.dumps({"findings": [], "tool_trace": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            with mock.patch("core.production.review_script", return_value=deepcopy(passed_review)), mock.patch(
                "core.production.build_local_variants",
                return_value=[
                    {"id": f"local-{index}", "hook_type": "本地安全", "script": PACED_SCRIPT, "reason": "fallback"}
                    for index in range(4)
                ],
            ):
                runner.run_content_stage(
                    folder,
                    {"topic": "检测结果怎么判断", "audience": "家庭用户", "production_mode": "motion"},
                    {"findings": []},
                )
            stored = json.loads((folder / "motion_storyboard.json").read_text(encoding="utf-8"))
            provider_report = json.loads((folder / "script_variants.json").read_text(encoding="utf-8"))["provider"]
        self.assertEqual(provider_report["source"], "local_deterministic")
        self.assertTrue(provider_report["fallback_used"])
        self.assertEqual(stored["source"], "DeepSeek")
        self.assertEqual(provider_report["motion_storyboard"]["source"], "DeepSeek")

    def test_render_revalidates_stored_storyboard_against_exact_script(self):
        stored = validate_storyboard(raw_storyboard(), SCRIPT, source="DeepSeek")
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            (folder / "motion_storyboard.json").write_text(
                json.dumps(stored, ensure_ascii=False), encoding="utf-8"
            )
            segments = ProductionRunner._segments(
                {
                    "topic": "检测结果怎么判断",
                    "motion_scenes": [
                        {"caption": "legacy override", "title": "legacy"}
                        for _ in range(4)
                    ],
                },
                SCRIPT,
                folder=folder,
            )
            self.assertEqual(len(segments), 4)
            self.assertEqual(segments[0]["caption"], CAPTIONS[0])
            with self.assertRaisesRegex(StoryboardError, "脚本哈希已失效"):
                ProductionRunner._segments(
                    {"topic": "检测结果怎么判断"}, SCRIPT + "新增旁白", folder=folder
                )

    def test_provider_prompt_exposes_only_structured_director_contract(self):
        provider = OpenAICompatibleProvider(
            {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-test"},
            "not-used",
        )
        captured: dict = {}

        def fake_chat(system, payload, *, stage):
            captured.update({"system": system, "payload": payload, "stage": stage})
            return deepcopy(raw_storyboard())

        provider._chat_json = fake_chat
        result = provider.generate_motion_storyboard(SCRIPT, {"topic": "测试"}, {})
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(captured["stage"], "motion_storyboard")
        self.assertIn("不写代码", captured["system"])
        self.assertIn("不输出坐标", captured["system"])
        self.assertIn("layout只能", captured["system"])
        self.assertIn("禁止另写", captured["system"])


if __name__ == "__main__":
    unittest.main()
