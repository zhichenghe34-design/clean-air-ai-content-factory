from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from core.capability_pack import normalize_capability_pack
from core.provider import (
    BudgetLedger,
    CAPABILITY_SNAPSHOT_FIELDS,
    OpenAICompatibleProvider,
    ProviderError,
    bootstrap_capability_schema_diagnostics,
    normalize_bootstrap_capability_snapshot,
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
        goal = "为一家本地餐饮门店制作可信的知识短视频"

        def fake_chat(system, user_data, *, stage="provider", count_budget=True):
            observed.update(system=system, user_data=user_data, stage=stage, count_budget=count_budget)
            snapshot = capability_snapshot()
            snapshot["goal"] = goal
            return {"capability_pack": snapshot, "candidates": candidates()}

        subject._chat_json = fake_chat  # type: ignore[method-assign]
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

    def test_bootstrap_standard_schema_is_unchanged(self):
        raw = capability_snapshot()
        goal = raw["goal"]
        normalized = normalize_bootstrap_capability_snapshot(copy.deepcopy(raw), goal)
        self.assertEqual(normalized, raw)
        self.assertEqual(set(normalized), set(CAPABILITY_SNAPSHOT_FIELDS))

    def test_bootstrap_normalizes_single_string_list_fields_without_semantic_fill(self):
        raw = capability_snapshot()
        expected = {}
        for field in (
            "platforms",
            "tone",
            "preferred_terms",
            "avoided_terms",
            "evidence_requirements",
            "prohibited_claims",
            "visual_direction",
            "assumptions",
        ):
            raw[field] = raw[field][0]
            expected[field] = [raw[field]]
        normalized = normalize_bootstrap_capability_snapshot(raw, raw["goal"])
        for field, value in expected.items():
            self.assertEqual(normalized[field], value)

    def test_bootstrap_normalizes_scalar_single_item_arrays_and_chinese_risk(self):
        raw = capability_snapshot()
        goal = raw["goal"]
        for field in ("label", "industry", "goal", "audience", "content_purpose"):
            raw[field] = [raw[field]]
        raw["risk_level"] = ["高风险"]
        normalized = normalize_bootstrap_capability_snapshot(raw, goal)
        self.assertEqual(normalized["goal"], goal)
        self.assertEqual(normalized["risk_level"], "high")
        self.assertEqual(normalized["label"], "本地餐饮门店内容包")

    def test_bootstrap_normalizes_flat_visual_direction_object(self):
        raw = capability_snapshot()
        raw["visual_direction"] = {
            "画幅": "1080×1920竖屏\uff0c主体居中\uff1b留白\uff1a充足",
            "构图": "门店环境与信息卡片",
        }
        normalized = normalize_bootstrap_capability_snapshot(raw, raw["goal"])
        self.assertEqual(
            normalized["visual_direction"],
            ["构图：门店环境与信息卡片", "画幅：1080×1920竖屏\uff0c主体居中\uff1b留白\uff1a充足"],
        )

    def test_bootstrap_visual_object_rejects_composite_sensitive_keys(self):
        sensitive_keys = (
            "authorization_header",
            "refresh_token",
            "client_secret",
            "cookie_value",
            "api_key_copy",
            "authorizationHeader",
            "authorizationheader",
            "accesstoken",
            "sessiontoken",
            "bearertoken",
            "authtoken",
            "idtoken",
            "apisecret",
            "consumersecret",
            "webhooksecret",
            "signingsecret",
            "authentication_header",
            "authenticationheader",
            "passwd",
            "pwd",
            "refreshtoken",
            "clientsecret",
            "cookievalue",
            "apikeycopy",
            "access-token-copy",
            "session_token_copy",
            "apiSecretCopy",
            "authenticationHeaderCopy",
            "api\uff53\uff45\uff43\uff52\uff45\uff54",
            "access\uff54\uff4f\uff4b\uff45\uff4e",
            "访问令牌备份",
            "客户授权信息",
            "接口密钥副本",
            "登录凭据说明",
        )
        for key in sensitive_keys:
            with self.subTest(key=key):
                raw = capability_snapshot()
                raw["visual_direction"] = {key: "信息卡片"}
                with self.assertRaises(ProviderError) as raised:
                    normalize_bootstrap_capability_snapshot(raw, raw["goal"])
                self.assertNotIn(key, str(raised.exception))

        sensitive_values = (
            "authorization\uff1aBearer opaque-credential-value",
            "api\uff3fkey\uff1dopaque-credential-value",
            "password\uff1dopaque-credential-value",
        )
        for value in sensitive_values:
            with self.subTest(value=value):
                raw = capability_snapshot()
                raw["visual_direction"] = {"palette": value}
                with self.assertRaises(ProviderError) as raised:
                    normalize_bootstrap_capability_snapshot(raw, raw["goal"])
                self.assertNotIn(value, str(raised.exception))

    def test_bootstrap_visual_object_order_is_hash_stable_and_normalized_duplicates_fail(self):
        first_raw = capability_snapshot()
        first_raw["visual_direction"] = {"palette": "暖色", "shot": "近景"}
        second_raw = capability_snapshot()
        second_raw["visual_direction"] = {"shot": "近景", "palette": "暖色"}
        first_snapshot = normalize_bootstrap_capability_snapshot(first_raw, first_raw["goal"])
        second_snapshot = normalize_bootstrap_capability_snapshot(second_raw, second_raw["goal"])
        self.assertEqual(first_snapshot["visual_direction"], second_snapshot["visual_direction"])
        first_pack = normalize_capability_pack(first_snapshot, first_raw["goal"], "deepseek")
        second_pack = normalize_capability_pack(second_snapshot, second_raw["goal"], "deepseek")
        self.assertEqual(first_pack["sha256"], second_pack["sha256"])

        duplicate = capability_snapshot()
        duplicate["visual_direction"] = {"shot-type": "近景", "shot_type": "远景"}
        with self.assertRaisesRegex(ProviderError, "规范化重名键"):
            normalize_bootstrap_capability_snapshot(duplicate, duplicate["goal"])

    def test_bootstrap_maps_only_explicit_equivalent_risk_levels(self):
        cases = {
            "low": "low",
            "LOW": "low",
            "低": "low",
            "低风险": "low",
            "medium": "medium",
            "中": "medium",
            "中风险": "medium",
            "high": "high",
            "高": "high",
            "高风险": "high",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                raw = capability_snapshot()
                raw["risk_level"] = value
                normalized = normalize_bootstrap_capability_snapshot(raw, raw["goal"])
                self.assertEqual(normalized["risk_level"], expected)
        for value in ("moderate", "较高", "一般", "1"):
            with self.subTest(rejected=value):
                raw = capability_snapshot()
                raw["risk_level"] = value
                with self.assertRaises(ProviderError):
                    normalize_bootstrap_capability_snapshot(raw, raw["goal"])

    def test_bootstrap_rejects_unsafe_or_malformed_snapshot_shapes(self):
        cases = {}

        unknown = capability_snapshot()
        unknown["extra"] = "unexpected"
        cases["unknown_field"] = unknown

        missing = capability_snapshot()
        missing.pop("industry")
        cases["missing_field"] = missing

        nested = capability_snapshot()
        nested["audience"] = {"segment": "上班族"}
        cases["nested_object"] = nested

        nested_visual = capability_snapshot()
        nested_visual["visual_direction"] = {"layout": {"position": "center"}}
        cases["nested_visual_object"] = nested_visual

        mixed = capability_snapshot()
        mixed["tone"] = ["克制", 3]
        cases["mixed_list"] = mixed

        url_value = capability_snapshot()
        url_value["tone"] = ["参见 https://example.com"]
        cases["url"] = url_value

        compatibility_url = capability_snapshot()
        compatibility_url["tone"] = ["参见 \uff48\uff54\uff54\uff50\uff53\uff1a\uff0f\uff0fexample.com"]
        cases["compatibility_url"] = compatibility_url

        compatibility_path = capability_snapshot()
        compatibility_path["audience"] = "\uff23\uff1a\uff3cUsers\uff3copaque"
        cases["compatibility_path"] = compatibility_path

        command_value = capability_snapshot()
        command_value["visual_direction"] = {"构图": "powershell -enc AAA"}
        cases["command"] = command_value

        compatibility_command = capability_snapshot()
        compatibility_command["visual_direction"] = {
            "构图": "\uff50\uff4f\uff57\uff45\uff52\uff53\uff48\uff45\uff4c\uff4c \uff0d\uff45\uff4e\uff43 AAA"
        }
        cases["compatibility_command"] = compatibility_command

        secret_value = capability_snapshot()
        secret_value["preferred_terms"] = ["sk-1234567890abcdef"]
        cases["secret"] = secret_value

        sensitive_key = capability_snapshot()
        sensitive_key["visual_direction"] = {"api_key": "只做信息卡片"}
        cases["sensitive_key"] = sensitive_key

        empty = capability_snapshot()
        empty["label"] = "   "
        cases["empty_required"] = empty

        null_value = capability_snapshot()
        null_value["industry"] = None
        cases["null"] = null_value

        number_value = capability_snapshot()
        number_value["audience"] = 7
        cases["number"] = number_value

        boolean_value = capability_snapshot()
        boolean_value["content_purpose"] = True
        cases["boolean"] = boolean_value

        wrong_goal = capability_snapshot()
        wrong_goal["goal"] = "模型擅自改写后的其他目标"
        cases["goal_mismatch"] = wrong_goal

        for name, raw in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ProviderError) as raised:
                    normalize_bootstrap_capability_snapshot(
                        raw,
                        capability_snapshot()["goal"],
                    )
                details = raised.exception.details
                self.assertIsInstance(details, dict)
                self.assertEqual(
                    set(details),
                    {"missing_fields", "unknown_fields", "field_types", "list_element_types"},
                )
                serialized = json.dumps(details, ensure_ascii=False)
                self.assertNotIn("sk-1234567890abcdef", serialized)
                self.assertNotIn("https://example.com", serialized)
                self.assertNotIn("powershell -enc AAA", serialized)

    def test_bootstrap_structure_diagnostics_contain_types_but_no_values(self):
        raw = capability_snapshot()
        raw.pop("industry")
        raw["unexpected"] = {"secret": "sk-1234567890abcdef"}
        raw["api_key=sk-1234567890abcdef"] = "should not enter diagnostics"
        raw["customer-id-440123199001011234"] = "should not enter diagnostics"
        raw["tone"] = ["克制", 3, False]
        details = bootstrap_capability_schema_diagnostics(raw)
        self.assertEqual(details["missing_fields"], ["industry"])
        self.assertEqual(details["unknown_fields"], ["<redacted-unknown-field>"])
        self.assertEqual(details["field_types"]["tone"], "array")
        self.assertEqual(details["list_element_types"]["tone"], ["boolean", "number", "string"])
        serialized = json.dumps(details, ensure_ascii=False)
        self.assertNotIn("sk-1234567890abcdef", serialized)
        self.assertNotIn("克制", serialized)
        self.assertNotIn("customer-id-440123199001011234", serialized)
        self.assertNotIn("unexpected", serialized)
        error = ProviderError("schema failed", details=details)
        self.assertIn("missing_fields", str(error))
        self.assertNotIn("sk-1234567890abcdef", str(error))
        self.assertNotIn("克制", str(error))
        self.assertNotIn("customer-id-440123199001011234", str(error))

    def test_bootstrap_adapter_still_uses_final_pack_constraints_and_stable_hash(self):
        raw = capability_snapshot()
        raw["platforms"] = "抖音"
        raw["visual_direction"] = {"构图": "证据卡片优先"}
        raw["risk_level"] = "中风险"
        adapted = normalize_bootstrap_capability_snapshot(raw, raw["goal"])
        first = normalize_capability_pack(adapted, raw["goal"], "deepseek")
        second = normalize_capability_pack(adapted, raw["goal"], "deepseek")
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertIn("事实性主张必须有可追溯证据", first["snapshot"]["evidence_requirements"])
        self.assertIn(
            "不得虚构事实、数据、用户证言、案例或来源",
            first["snapshot"]["prohibited_claims"],
        )

    def test_bootstrap_semantic_budget_success_and_failure_counts_are_preserved(self):
        failed_budget = BudgetLedger(limit=3)
        failed_subject = OpenAICompatibleProvider(
            {"base_url": "https://api.deepseek.com", "model": "test-model"},
            "test-only-key",
            failed_budget,
        )
        failed_token = failed_budget.begin("project_bootstrap")
        failed_budget.finish(failed_token, ok=True)
        failed_subject._last_budget_token = failed_token
        malformed = capability_snapshot()
        malformed["tone"] = ["克制", 3]
        failed_subject._chat_json = lambda *args, **kwargs: {  # type: ignore[method-assign]
            "capability_pack": malformed,
            "candidates": candidates(),
        }
        with self.assertRaises(ProviderError):
            failed_subject.bootstrap_project(malformed["goal"])
        self.assertEqual(failed_budget.snapshot()["attempted"], 1)
        self.assertEqual(failed_budget.snapshot()["succeeded"], 0)
        self.assertEqual(failed_budget.snapshot()["failed"], 1)
        self.assertEqual(failed_budget.snapshot()["events"][0]["error_type"], "invalid_capability_pack_schema")

        success_budget = BudgetLedger(limit=3)
        success_subject = OpenAICompatibleProvider(
            {"base_url": "https://api.deepseek.com", "model": "test-model"},
            "test-only-key",
            success_budget,
        )
        success_token = success_budget.begin("project_bootstrap")
        success_budget.finish(success_token, ok=True)
        success_subject._last_budget_token = success_token
        standard = capability_snapshot()
        success_subject._chat_json = lambda *args, **kwargs: {  # type: ignore[method-assign]
            "capability_pack": standard,
            "candidates": candidates(),
        }
        success_subject.bootstrap_project(standard["goal"])
        self.assertEqual(success_budget.snapshot()["attempted"], 1)
        self.assertEqual(success_budget.snapshot()["succeeded"], 1)
        self.assertEqual(success_budget.snapshot()["failed"], 0)

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
