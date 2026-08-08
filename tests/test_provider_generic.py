from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.capability_pack import normalize_capability_pack
from core.provider import (
    CAPABILITY_SNAPSHOT_FIELDS,
    OpenAICompatibleProvider,
    ProviderError,
)
from core.strict_audit import strict_audit_research
from core.web_agent import EXACT_EVIDENCE_RULES, WebResearchAgent, normalize_research_result
from core.web_tools import TrustedWebToolRegistry


def provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        {"base_url": "https://api.deepseek.com", "model": "test-model"},
        "test-only-key",
    )


def capability_snapshot() -> dict:
    return {
        "label": "本地餐饮门店内容包",
        "industry": "本地餐饮",
        "goal": "用短视频解释门店菜品与到店流程",
        "audience": "附近有就餐需求的消费者",
        "platforms": ["抖音", "TikTok"],
        "content_purpose": "知识介绍与到店前决策辅助",
        "tone": ["清楚", "克制", "生活化"],
        "preferred_terms": ["以门店公开资料为准"],
        "avoided_terms": ["全城第一"],
        "evidence_requirements": ["价格必须使用当前公开菜单并记录日期"],
        "prohibited_claims": ["虚构顾客证言", "未经证实的销量排名"],
        "visual_direction": ["真实环境", "信息卡片"],
        "assumptions": ["具体门店与菜单尚待用户提供"],
        "risk_level": "medium",
    }


def candidates() -> list[dict]:
    return [
        {"id": "topic-1", "title": "第一次到店怎样更快看懂菜单？", "reason": "从决策流程切入。", "audience": "首次到店顾客"},
        {"id": "topic-2", "title": "一道招牌菜该核对哪些公开信息？", "reason": "从证据边界切入。", "audience": "重视信息透明的顾客"},
        {"id": "topic-3", "title": "拍门店短视频怎样避免虚构证言？", "reason": "从可信表达切入。", "audience": "门店运营人员"},
    ]


class ProviderGenericTests(unittest.TestCase):
    def test_bootstrap_identifies_domain_and_returns_raw_pack_with_three_candidates(self):
        subject = provider()
        observed = {}

        def fake_chat(system, user_data, *, stage="provider", count_budget=True):
            observed.update(system=system, user_data=user_data, stage=stage, count_budget=count_budget)
            return {"capability_pack": capability_snapshot(), "candidates": candidates()}

        subject._chat_json = fake_chat  # type: ignore[method-assign]
        goal = "为一家本地餐饮门店制作可信的知识短视频"
        result = subject.bootstrap_project(
            goal,
            ["不要再做探店合集"],
            [{"instruction": "不要使用低价促销定位"}],
        )

        self.assertEqual(set(result["capability_pack"]), set(CAPABILITY_SNAPSHOT_FIELDS))
        normalized_pack = normalize_capability_pack(result["capability_pack"], goal, "deepseek")
        self.assertEqual(normalized_pack["snapshot"]["industry"], "本地餐饮")
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual([item["id"] for item in result["candidates"]], ["topic-1", "topic-2", "topic-3"])
        self.assertEqual(observed["stage"], "project_bootstrap")
        self.assertIn("现场识别行业", observed["system"])
        self.assertIn("所有关于企业", observed["system"])
        self.assertIn("不得虚构企业资料", observed["system"])
        self.assertEqual(observed["user_data"]["memory_rules"][0]["instruction"], "不要使用低价促销定位")

    def test_bootstrap_rejects_incomplete_pack_or_wrong_candidate_count(self):
        subject = provider()
        incomplete = capability_snapshot()
        incomplete.pop("risk_level")
        subject._chat_json = lambda *args, **kwargs: {  # type: ignore[method-assign]
            "capability_pack": incomplete,
            "candidates": candidates(),
        }
        with self.assertRaises(ProviderError):
            subject.bootstrap_project("制作本地餐饮知识短视频")

        subject._chat_json = lambda *args, **kwargs: {  # type: ignore[method-assign]
            "capability_pack": capability_snapshot(),
            "candidates": candidates()[:2],
        }
        with self.assertRaises(ProviderError):
            subject.bootstrap_project("制作本地餐饮知识短视频")

    def test_capability_review_can_only_narrow_or_reject(self):
        subject = provider()
        observed = {}

        def fake_chat(system, user_data, *, stage="provider", count_budget=True):
            observed.update(system=system, user_data=user_data, stage=stage)
            return {
                "status": "needs_revision",
                "issues": ["具体门店资料尚未提供"],
                "safe_scope": ["只讨论核验流程"],
                "candidate_verdicts": [
                    {
                        "candidate_id": item["id"],
                        "verdict": "usable_limited" if index == 0 else "needs_evidence",
                        "reasons": ["不得预设门店事实"],
                        "safe_scope": "仅作为待研究选题",
                    }
                    for index, item in enumerate(candidates())
                ],
            }

        subject._chat_json = fake_chat  # type: ignore[method-assign]
        result = subject.adversarial_review_capability_pack(capability_snapshot(), candidates())
        self.assertEqual(result["status"], "needs_revision")
        self.assertEqual(observed["stage"], "capability_pack_adversarial_review")
        self.assertIn("所有内容都是假的", observed["system"])
        self.assertIn("只能否决或缩小", observed["system"])
        self.assertNotIn("approved", {row["verdict"] for row in result["candidate_verdicts"]})

    def test_old_suggest_topics_call_still_works_and_new_context_is_forwarded(self):
        subject = provider()
        calls = []

        def fake_chat(system, user_data, *, stage="provider", count_budget=True):
            calls.append((system, user_data, stage))
            return {"candidates": [{key: value for key, value in item.items() if key != "id"} for item in candidates()]}

        subject._chat_json = fake_chat  # type: ignore[method-assign]
        self.assertEqual(len(subject.suggest_topics("制作本地餐饮知识短视频", [])), 3)
        subject.suggest_topics(
            "制作本地餐饮知识短视频",
            [],
            capability_pack={"id": "dynamic-food-v1", "snapshot": capability_snapshot()},
            memory_rules=[{"instruction": "不做低价促销"}],
        )
        self.assertEqual(calls[1][1]["capability_pack"]["id"], "dynamic-food-v1")
        self.assertEqual(calls[1][1]["memory_rules"][0]["instruction"], "不做低价促销")
        self.assertIn("不得默认为除甲醛", calls[0][0])

    def test_script_chain_reads_pack_and_learning_rules_with_generic_hard_blocks(self):
        subject = provider()
        observed = {}
        variants = [
            {"id": f"v{index}", "hook_type": "问题", "script": "这是一段保守的流程说明。", "reason": "测试"}
            for index in range(1, 5)
        ]

        def fake_chat(system, user_data, *, stage="provider", count_budget=True):
            observed[stage] = (system, user_data)
            if stage == "script_generation":
                return {"variants": variants}
            if stage == "compliance_review":
                return {
                    "status": "pass_with_human_review",
                    "risks": [],
                    "suggested_script": user_data["script"],
                    "human_confirmation_required": True,
                }
            return {"script": "修订后的保守流程说明。", "changes": ["删除未证实主张"]}

        subject._chat_json = fake_chat  # type: ignore[method-assign]
        production_input = {
            "capability_pack": {"id": "dynamic-food-v1", "snapshot": capability_snapshot()},
            "learning_rules": [{"instruction": "不要使用低价促销定位"}],
        }
        subject.generate_content_scripts(production_input, {"approved_findings": []})
        subject.review_content_script("待审脚本", {"blocked": False}, production_input)
        subject.repair_content_script("待修脚本", {"blocked": True}, {}, production_input)

        for stage in ("script_generation", "compliance_review", "script_repair"):
            system, payload = observed[stage]
            self.assertEqual(payload["production_input"], production_input)
            self.assertIn("capability_pack", system)
        self.assertIn("价格", observed["script_generation"][0])
        self.assertIn("投资收益", observed["compliance_review"][0])
        self.assertIn("胜诉保证", observed["script_repair"][0])


class SourceTrustTests(unittest.TestCase):
    @staticmethod
    def trace(url: str, text: str) -> list[dict]:
        return [{
            "tool": "extract_url",
            "result": {"ok": True, "url": url, "final_url": url, "text": text},
        }]

    @staticmethod
    def research(url: str, source_type: str, claim: str) -> dict:
        return {
            "status": "complete",
            "sources": [{
                "url": url,
                "title": "页面",
                "publisher": "模型声称的发布者",
                "source_type": source_type,
                "retrieved_at": "2026-08-03T10:00:00+08:00",
            }],
            "findings": [{
                "claim": claim,
                "source_urls": [url],
                "evidence": [{
                    "url": url,
                    "excerpt": claim,
                    "source_type": source_type,
                    "retrieved_at": "2026-08-03T10:00:00+08:00",
                }],
                "limitations": ["仅限页面原文范围"],
                "binding_method": "exact_tool_excerpt",
            }],
            "evidence_gaps": [],
        }

    def test_model_cannot_promote_ordinary_page_but_exact_text_can_be_page_statement(self):
        url = "https://example.com/report"
        claim = "页面只说明一个待核验流程。"
        trace = self.trace(url, claim)
        normalized = normalize_research_result(self.research(url, "government", claim), trace, allow_legacy_exact=False)
        self.assertEqual(normalized["sources"][0]["source_type"], "source_page")
        self.assertEqual(normalized["sources"][0]["publisher"], "example.com")
        self.assertEqual(normalized["findings"][0]["evidence"][0]["source_type"], "source_page")
        self.assertTrue(normalized["findings"][0]["claim"].startswith("该来源页面称："))
        self.assertFalse(normalized["findings"][0]["independent_fact_supported"])
        audited = strict_audit_research(normalized, trace)
        self.assertEqual(audited["strict_audit"]["passed_count"], 1)
        self.assertEqual(
            audited["strict_audit"]["findings"][0]["evidence_scope"],
            "source_page_statement_only",
        )
        self.assertFalse(audited["strict_audit"]["findings"][0]["independent_fact_supported"])

        forged = self.research(url, "government", claim)
        forged["findings"][0]["script_eligible"] = True
        audited_directly = strict_audit_research(forged, trace)
        self.assertEqual(audited_directly["strict_audit"]["passed_count"], 0)
        self.assertIn(
            "ordinary_page_is_attributed",
            audited_directly["strict_audit"]["findings"][0]["rejection_reasons"],
        )
        self.assertEqual(audited_directly["findings"][0]["evidence"][0]["source_type"], "source_page")

    def test_extracted_gov_cn_url_is_classified_locally(self):
        url = "https://example.gov.cn/policy"
        claim = "页面说明该政策适用于公开列明的范围。"
        trace = self.trace(url, claim)
        normalized = normalize_research_result(self.research(url, "media_original", claim), trace, allow_legacy_exact=False)
        self.assertEqual(normalized["findings"][0]["evidence"][0]["source_type"], "government")
        audited = strict_audit_research(normalized, trace)
        self.assertEqual(audited["strict_audit"]["passed_count"], 1)

    def test_education_research_domain_has_a_higher_local_category(self):
        url = "https://lab.example.edu.cn/research"
        claim = "研究页面列出样本范围和观察方法。"
        trace = self.trace(url, claim)
        normalized = normalize_research_result(
            self.research(url, "government", claim), trace, allow_legacy_exact=False
        )
        self.assertEqual(normalized["sources"][0]["source_type"], "education_research")
        self.assertEqual(normalized["findings"][0]["evidence"][0]["source_type"], "education_research")
        audited = strict_audit_research(normalized, trace)
        self.assertEqual(audited["strict_audit"]["passed_count"], 1)
        self.assertIsNone(audited["strict_audit"]["findings"][0]["independent_fact_supported"])

    def test_ordinary_page_cannot_support_guarantees_or_unbounded_numeric_efficacy(self):
        url = "https://company.example/product"
        for claim, failed_check in (
            ("本产品保证永久有效。", "no_absolute_or_medical_overreach"),
            ("本产品转化率提升30%。", "numeric_efficacy_has_boundaries"),
        ):
            with self.subTest(claim=claim):
                trace = self.trace(url, claim)
                normalized = normalize_research_result(
                    self.research(url, "government", claim), trace, allow_legacy_exact=False
                )
                audited = strict_audit_research(normalized, trace)
                self.assertEqual(audited["strict_audit"]["passed_count"], 0)
                self.assertIn(failed_check, audited["strict_audit"]["findings"][0]["rejection_reasons"])

        bounded = "该公司页面称：在20家门店测试30天，转化率提升30%。"
        trace = self.trace(url, bounded)
        normalized = normalize_research_result(
            self.research(url, "government", bounded), trace, allow_legacy_exact=False
        )
        audited = strict_audit_research(normalized, trace)
        self.assertEqual(audited["strict_audit"]["passed_count"], 1)
        self.assertFalse(audited["strict_audit"]["findings"][0]["independent_fact_supported"])

    def test_dynamic_pack_never_receives_legacy_clean_air_findings(self):
        rule = EXACT_EVIDENCE_RULES[1]
        url = "https://www.samr.gov.cn/law"

        def extract(value, output_dir):
            return {
                "status": "complete",
                "source": {"final_url": value, "title": "中华人民共和国广告法"},
                "content": {"text": f"第十一条 {rule['excerpt']} 第十二条", "text_chars": len(rule["excerpt"]) + 12},
                "attempts": [{"route": "fake", "status": "complete"}],
                "warnings": [],
            }

        class ExhaustedProvider:
            api_key = "mock-only"

            @staticmethod
            def chat_with_tools(messages, tools, tool_choice="auto"):
                raise ProviderError("API调用预算已耗尽（7/7）")

        def run(pack_id: str) -> dict:
            with tempfile.TemporaryDirectory() as folder:
                registry = TrustedWebToolRegistry(
                    Path(folder),
                    extractor=extract,
                    seed_urls=[url],
                )
                return WebResearchAgent(ExhaustedProvider(), registry).run(
                    "如何核验公开数据",
                    "普通消费者",
                    [url],
                    capability_pack={"id": pack_id, "snapshot": capability_snapshot()},
                )

        dynamic = run("dynamic-generic-v1")
        legacy = run("legacy-clean-air-v2")
        self.assertEqual(dynamic["findings"], [])
        self.assertEqual(dynamic["strict_audit"]["passed_count"], 0)
        self.assertEqual(legacy["strict_audit"]["passed_count"], 1)
        self.assertEqual(legacy["findings"][0]["evidence"][0]["source_type"], "government_law")


if __name__ == "__main__":
    unittest.main()
