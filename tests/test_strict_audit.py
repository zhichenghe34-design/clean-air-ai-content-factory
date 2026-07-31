from __future__ import annotations

import unittest

from core.strict_audit import strict_audit_research


URL = "https://example.com/report"
EXCERPT = "1罐产品在1m3试验舱内24小时除醛率为99.93%。"


def research(claim: str = "报道中的99.93%对应1罐产品、1m³试验舱和24小时条件。") -> dict:
    return {
        "status": "complete",
        "findings": [{
            "claim": claim,
            "source_urls": [URL],
            "evidence": [{
                "url": URL,
                "excerpt": EXCERPT,
                "source_type": "media_original",
                "retrieved_at": "2026-08-01T01:00:00+08:00",
            }],
            "limitations": ["不能外推到所有产品"],
            "binding_method": "exact_tool_excerpt",
            "script_eligible": True,
        }],
        "script_eligible_findings": [],
    }


def trace(text: str = EXCERPT) -> list[dict]:
    return [{
        "tool": "extract_url",
        "result": {"ok": True, "url": URL, "final_url": URL, "text": text},
    }]


class StrictAuditTests(unittest.TestCase):
    def test_default_false_can_be_overturned_only_by_exact_scoped_proof(self):
        result = strict_audit_research(research(), trace())
        self.assertEqual(result["strict_audit"]["passed_count"], 1)
        self.assertEqual(result["findings"][0]["strict_review_status"], "proven_for_limited_use")

    def test_missing_exact_excerpt_is_rejected(self):
        result = strict_audit_research(research(), trace("页面没有这段原文"))
        self.assertEqual(result["strict_audit"]["passed_count"], 0)
        self.assertIn("captured_exact_excerpt", result["strict_audit"]["findings"][0]["rejection_reasons"])

    def test_unsupported_number_is_rejected(self):
        result = strict_audit_research(research("报道证明真实家庭除醛率达到100%。"), trace())
        self.assertEqual(result["strict_audit"]["passed_count"], 0)
        self.assertIn("numbers_present_in_evidence", result["strict_audit"]["findings"][0]["rejection_reasons"])

    def test_model_can_veto_but_cannot_promote_local_failure(self):
        valid = research()
        claim = valid["findings"][0]["claim"]
        vetoed = strict_audit_research(
            valid,
            trace(),
            model_review={"findings": [{"claim": claim, "verdict": "insufficient", "reasons": ["范围仍不清楚"]}]},
            require_model_review=True,
        )
        self.assertEqual(vetoed["strict_audit"]["passed_count"], 0)

        invalid = research("报道证明真实家庭除醛率达到100%。")
        invalid_claim = invalid["findings"][0]["claim"]
        not_promoted = strict_audit_research(
            invalid,
            trace(),
            model_review={"findings": [{"claim": invalid_claim, "verdict": "supported_limited", "reasons": []}]},
            require_model_review=True,
        )
        self.assertEqual(not_promoted["strict_audit"]["passed_count"], 0)
        self.assertEqual(not_promoted["strict_audit"]["findings"][0]["model_verdict"], "supported_limited")

    def test_model_review_matches_by_immutable_audit_id_even_if_claim_is_rephrased(self):
        local = strict_audit_research(research(), trace())
        audit_id = local["strict_audit"]["findings"][0]["audit_id"]
        result = strict_audit_research(
            research(),
            trace(),
            model_review={"status": "complete", "findings": [{
                "audit_id": audit_id,
                "claim": "模型改写了这句话",
                "verdict": "supported_limited",
                "reasons": ["限定范围内有直接摘录"],
                "safe_scope": "仅限报道中的测试条件",
            }]},
            require_model_review=True,
        )
        self.assertEqual(result["strict_audit"]["passed_count"], 1)
        self.assertEqual(result["strict_audit"]["model_review_findings"][0]["audit_id"], audit_id)

    def test_review_coverage_is_complete_even_when_provider_global_status_is_insufficient(self):
        local = strict_audit_research(research(), trace())
        audit_id = local["strict_audit"]["findings"][0]["audit_id"]
        result = strict_audit_research(
            research(),
            trace(),
            model_review={"status": "insufficient", "findings": [{
                "audit_id": audit_id,
                "verdict": "supported_limited",
                "reasons": ["有限范围内有直接证据"],
                "safe_scope": "仅限原测试条件",
            }]},
            require_model_review=True,
        )
        self.assertEqual(result["strict_audit"]["model_review_status"], "complete")
        self.assertEqual(result["strict_audit"]["model_provider_reported_status"], "insufficient")


if __name__ == "__main__":
    unittest.main()
