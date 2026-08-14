from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from core.capability_pack import local_capability_pack, local_topic_candidates
from core.motion_director import (
    CLEAN_AIR_EXPLAINER_TEMPLATE_FILE,
    MotionPlanError,
    NARRATION_PAIRS,
    build_motion_plan,
    build_motion_project,
    derive_motion_segments,
    split_narration_units,
    validate_motion_plan,
)
from core.production import ProductionRunner, build_local_variants
from core.provider import OpenAICompatibleProvider, ProviderError
from core.storyboard_agent import (
    StoryboardError,
    _verbatim_items,
    build_local_storyboard,
    storyboard_to_motion_segments,
    validate_storyboard,
)
from core.voice_contract import estimate_voice_scene_pacing


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
    def test_single_visual_phrase_never_splits_inside_participate_or_peaceful(self):
        for caption in (
            "请核对用户参与活动的具体条件。",
            "为客户提供平和稳定的沟通方式。",
            "请核对本地用户参与活动的具体条件。",
            "请核对企业服务平和稳定的沟通方式。",
        ):
            self.assertEqual(_verbatim_items(caption), [caption.rstrip("。")])
        self.assertEqual(
            _verbatim_items("请核对本地用户参与活动的具体条件、材料来源完整清晰。"),
            ["请核对本地用户参与活动的具体条件", "材料来源完整清晰"],
        )
        self.assertEqual(
            _verbatim_items("请核对企业服务平和稳定的沟通方式、售后条件真实完整。"),
            ["请核对企业服务平和稳定的沟通方式", "售后条件真实完整"],
        )

    def test_all_default_no_key_variants_build_complete_paced_paired_storyboards(self):
        goal = (
            "为除甲醛服务企业制作一条面向新房家庭的竖屏科普短视频，"
            "重点讲清检测条件、适用边界和可追溯证据。"
        )
        pack = local_capability_pack(goal)
        topic = local_topic_candidates(goal, pack, [])[0]["title"]
        variants = build_local_variants(
            topic,
            pack["snapshot"]["audience"],
            capability_pack=pack,
        )

        def assert_paired(text: str) -> None:
            stack: list[str] = []
            closing = set(NARRATION_PAIRS.values())
            for character in text:
                if stack and character == stack[-1]:
                    stack.pop()
                elif character in NARRATION_PAIRS:
                    stack.append(NARRATION_PAIRS[character])
                elif character in closing:
                    self.fail(f"未成对的右标点: {text}")
            self.assertFalse(stack, f"未成对的左标点: {text}")

        self.assertEqual([variant["id"] for variant in variants], ["A", "B", "C", "D"])
        for variant in variants:
            storyboard = build_local_storyboard(topic, variant["script"], pack)
            scenes = storyboard["scenes"]
            self.assertTrue(4 <= len(scenes) <= 8, variant["id"])
            self.assertEqual("".join(scene["caption"] for scene in scenes), variant["script"])
            for scene in scenes:
                pacing = estimate_voice_scene_pacing(scene["caption"])
                self.assertLessEqual(
                    pacing["spoken_characters_per_second"],
                    4.05,
                    f"{variant['id']}:{scene['id']}",
                )
                for text in (
                    scene["caption"],
                    scene["kicker"],
                    scene["title"],
                    scene["summary"],
                    *scene["items"],
                ):
                    assert_paired(text)
            fifth_visual = "".join(scenes[4]["items"])
            final_visual = "".join(scenes[-1]["items"])
            self.assertIn("报告来源和适用边界", fifth_visual, variant["id"])
            self.assertIn("结合房屋情况", final_visual, variant["id"])
            self.assertIn("请专业人员判断", final_visual, variant["id"])

    def test_eight_complete_sentences_never_merge_to_free_a_voice_scene(self):
        script = "".join([
            "第一步核对客户需求。",
            "第二步核对使用场景。",
            "第三步核对检测条件。",
            "第四步核对仪器位置。",
            "第五步核对持续时间。",
            "第六步核对报告来源。",
            "第七步核对适用边界。",
            "实验条件与真实房间不同，结论不能直接照搬；缺少来源和适用边界，也不能理解成入住保证。",
        ])

        with self.assertRaisesRegex(MotionPlanError, "禁止.*合并其他完整句"):
            derive_motion_segments(
                "条件和边界怎么判断",
                script,
                target_count=8,
                enforce_voice_pacing=True,
            )

    def test_single_clause_final_checklist_keeps_its_fifth_tail_item(self):
        script = (
            "先核对客户需求，再看适用场景。"
            "先记录材料来源，再看使用条件。"
            "先比较服务范围，再看交付周期。"
            "先回到原始资料，再逐项确认。"
            "核对材料，以及确认来源并且记录过程然后复查结果以及保存证据？"
        )

        storyboard = build_local_storyboard("资料怎么核对", script)
        final_scene = storyboard["scenes"][-1]

        self.assertEqual("".join(scene["caption"] for scene in storyboard["scenes"]), script)
        self.assertIn("保存证据", "".join(final_scene["items"]))
        self.assertLessEqual(len(final_scene["items"]), 4)

    def test_adjacent_single_item_sentences_keep_separate_voice_scenes(self):
        script = (
            "第一步核对客户需求。"
            "第二步核对使用场景。"
            "第三步核对材料来源，再看适用边界。"
            "第四步记录对象、时间和地点。"
            "第五步说明未知项，不能写成确定结论。"
            "最后保留资料、重新核对，再决定下一步。"
        )

        storyboard = build_local_storyboard("资料怎么核对", script)
        captions = [scene["caption"] for scene in storyboard["scenes"]]

        self.assertIn("第一步核对客户需求。", captions)
        self.assertIn("第二步核对使用场景。", captions)
        self.assertFalse(any(
            len(split_narration_units(
                caption,
                punctuation="。！？!?",
                keep_punctuation=True,
            )) > 1
            for caption in captions
        ))
        self.assertEqual("".join(captions), script)

    def test_approved_failed_script_keeps_source_attribution_leadin_with_its_claim(self):
        script = (
            "看到一条高比例除醛率宣传，先问一句：这个数字是在什么条件下得到的？"
            "央视网转载的上观新闻文章称："
            "甲醛检测盒只能看出室内甲醛浓度的大致范围。"
            "这项内容只适用于原来源的对象和范围，不能外推到所有产品或场景。"
            "判断除醛信息，还要核对剂量、空间体积、作用时间、初始浓度、检测方法和报告来源。"
            "实验条件与真实房间不同，结论不能直接照搬；证据不完整，也不能理解成入住保证。"
            "最后保留原始报告和核对记录，再结合真实房屋情况判断。"
        )

        storyboard = build_local_storyboard(
            "为什么除甲醛后数值反弹？适用边界不是所有人都会告诉你",
            script,
        )
        captions = [scene["caption"] for scene in storyboard["scenes"]]
        leadin_index = captions.index("央视网转载的上观新闻文章称：")

        self.assertEqual("".join(captions), script)
        self.assertEqual(storyboard["scenes"][leadin_index]["layout"], "explain_points")
        self.assertEqual(storyboard["scenes"][leadin_index + 1]["layout"], "explain_points")
        self.assertEqual(
            storyboard["scenes"][leadin_index + 1]["caption"],
            "甲醛检测盒只能看出室内甲醛浓度的大致范围。",
        )

    def test_arbitrary_colon_fragment_does_not_allow_adjacent_sparse_explanations(self):
        value = raw_storyboard()
        value["scenes"][1].update({
            "caption": "这篇文章说明：",
            "kicker": "这篇文章说明",
            "title": "这篇文章说明",
            "summary": "这篇文章说明",
            "layout": "explain_points",
            "items": ["这篇文章说明"],
            "focus_order": [0],
        })
        value["scenes"][2].update({
            "caption": "这项内容不能外推到所有场景。",
            "kicker": "这项内容不能外推到所有场景",
            "title": "这项内容不能外推到所有场景",
            "summary": "这项内容不能外推到所有场景",
            "layout": "explain_points",
            "items": ["这项内容不能外推到所有场景"],
            "focus_order": [0],
        })
        script = "".join(scene["caption"] for scene in value["scenes"])

        with self.assertRaisesRegex(StoryboardError, "重复同一信息结构"):
            validate_storyboard(value, script, source="DeepSeek")

    def test_local_storyboard_splits_a_dense_clause_for_the_faster_fixed_voice(self):
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

        self.assertEqual(len(storyboard["scenes"]), 8)
        self.assertEqual("".join(scene["caption"] for scene in storyboard["scenes"]), script)
        self.assertIn("实验条件与真实房间不同，结论不能直接照搬；", [scene["caption"] for scene in storyboard["scenes"]])
        self.assertIn("缺少来源和适用边界，也不能理解成入住保证。", [scene["caption"] for scene in storyboard["scenes"]])
        self.assertTrue(storyboard["mechanical_review"]["voice_scene_density_compatible"])

    def test_local_storyboard_does_not_remerge_two_complete_voice_sentences(self):
        goal = (
            "为除甲醛服务企业制作一条面向新房家庭的竖屏科普短视频，"
            "重点讲清检测条件、适用边界和可追溯证据。"
        )
        pack = local_capability_pack(goal)
        topic = local_topic_candidates(goal, pack, [])[0]["title"]
        script = build_local_variants(
            topic,
            pack["snapshot"]["audience"],
            capability_pack=pack,
        )[0]["script"]

        storyboard = build_local_storyboard(topic, script, pack)
        captions = [scene["caption"] for scene in storyboard["scenes"]]

        self.assertEqual(len(captions), 8)
        self.assertIn("实验条件与真实房间不同。", captions)
        self.assertIn("结论不能直接照搬；所谓入住保证，更不能这样理解。", captions)
        self.assertNotIn(
            "实验条件与真实房间不同。结论不能直接照搬；所谓入住保证，更不能这样理解。",
            captions,
        )
        self.assertEqual("".join(captions), script)

    def test_provider_storyboard_rejects_an_over_dense_caption_before_tts(self):
        value = raw_storyboard()
        dense_caption = "实验条件与真实房间不同，结论不能直接照搬；缺少来源和适用边界，也不能理解成入住保证。"
        value["scenes"][0].update({
            "caption": dense_caption,
            "kicker": "实验条件与真实房间不同",
            "title": "实验条件与真实房间不同",
            "summary": "结论不能直接照搬",
            "layout": "process_flow",
            "items": ["实验条件与真实房间不同", "结论不能直接照搬", "缺少来源和适用边界", "也不能理解成入住保证"],
            "focus_order": [0, 1, 2, 3],
        })
        script = "".join(scene["caption"] for scene in value["scenes"])

        with self.assertRaisesRegex(StoryboardError, "固定-2%大众播报预计"):
            validate_storyboard(value, script, source="DeepSeek")

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

        repeated_multi_item_explanation = raw_storyboard()
        repeated_multi_item_explanation["scenes"][0]["layout"] = "explain_points"
        repeated_multi_item_explanation["scenes"][1]["layout"] = "explain_points"
        with self.assertRaisesRegex(StoryboardError, "重复同一信息结构"):
            validate_storyboard(repeated_multi_item_explanation, SCRIPT, source="DeepSeek")

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
            "对于“甲醛检测数值低，就能安心入住吗？”，不能只凭一个低数值下结论。"
            "先分清气味线索和仪器读数，再核对室内甲醛证据。"
            "先核对检测时的门窗状态、仪器位置和持续时间。"
            "气味和体感只是线索，不能替代规范检测。"
            "再看剂量、空间体积、作用时间、初始浓度、检测方法、报告来源和适用边界。"
            "实验条件与真实房间不同。"
            "结论不能直接照搬；所谓入住保证，更不能这样理解。"
            "对上海装修后家庭，建议保留原始报告、持续有效通风；重要决定前，结合房屋情况，请专业人员判断。"
        )
        result = build_local_storyboard("检测条件", script)
        enumerated = next(
            scene for scene in result["scenes"] if "门窗状态" in scene["caption"]
        )
        self.assertEqual(
            enumerated["items"],
            ["先核对检测时的门窗状态", "仪器位置和持续时间"],
        )
        self.assertIn(enumerated["layout"], {"explain_points", "condition_map"})

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
