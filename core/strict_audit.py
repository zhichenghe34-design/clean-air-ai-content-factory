from __future__ import annotations

import hashlib
import json
import re
from typing import Any


TRUSTED_SOURCE_TYPES = {
    "government_law",
    "government_standard_metadata",
    "government",
    "institutional_primary",
    "institutional_secondary",
    "media_original",
}
ABSOLUTE_OR_MEDICAL_MARKERS = (
    "所有产品", "全部产品", "保证", "彻底", "永久", "百分之百", "零甲醛",
    "无风险", "最好", "第一", "根治", "治愈", "预防疾病", "不会复发",
)


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value).replace("³", "3").replace("㎡", "m2")).strip()


def _numeric_tokens(value: Any) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?%?", _normalized_text(value)))


def _captured_pages(tool_trace: list[dict[str, Any]]) -> dict[str, str]:
    pages: dict[str, str] = {}
    for call in tool_trace:
        if not isinstance(call, dict) or call.get("tool") != "extract_url":
            continue
        result = call.get("result") if isinstance(call.get("result"), dict) else {}
        if result.get("ok") is not True:
            continue
        text = _normalized_text(result.get("text", ""))
        if not text:
            continue
        for key in (result.get("url"), result.get("final_url")):
            if key:
                pages[str(key)] = text
    return pages


def strict_audit_research(
    research: dict[str, Any],
    tool_trace: list[dict[str, Any]],
    *,
    model_review: dict[str, Any] | None = None,
    require_model_review: bool = False,
) -> dict[str, Any]:
    """Assume every finding is false; only exact, scoped proof can overturn rejection."""
    audited = dict(research)
    pages = _captured_pages(tool_trace)
    model_rows = model_review.get("findings", []) if isinstance(model_review, dict) else []
    model_by_claim = {
        str(item.get("claim", "")).strip(): dict(item)
        for item in model_rows
        if isinstance(item, dict) and str(item.get("claim", "")).strip()
    }
    model_by_id = {
        str(item.get("audit_id", "")).strip(): dict(item)
        for item in model_rows
        if isinstance(item, dict) and str(item.get("audit_id", "")).strip()
    }
    findings: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []
    passed: list[dict[str, Any]] = []

    for raw in audited.get("findings", []):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        claim = str(item.get("claim", "")).strip()
        audit_id = hashlib.sha256(claim.encode("utf-8")).hexdigest()[:16]
        evidence = [entry for entry in item.get("evidence", []) if isinstance(entry, dict)]
        excerpts = [_normalized_text(entry.get("excerpt", "")) for entry in evidence]
        exact_checks = []
        for entry, excerpt in zip(evidence, excerpts):
            url = str(entry.get("url", ""))
            exact_checks.append(bool(excerpt and url in pages and excerpt in pages[url]))
        claim_numbers = _numeric_tokens(claim)
        evidence_numbers = set().union(*(_numeric_tokens(value) for value in excerpts)) if excerpts else set()
        source_types = {str(entry.get("source_type", "")) for entry in evidence}
        checks = {
            "previous_evidence_gate": item.get("script_eligible") is True,
            "captured_exact_excerpt": bool(exact_checks) and all(exact_checks),
            "trusted_source_type": bool(source_types) and source_types.issubset(TRUSTED_SOURCE_TYPES),
            "numbers_present_in_evidence": claim_numbers.issubset(evidence_numbers),
            "explicit_scope_limit": bool(item.get("limitations")),
            "binding_is_not_model_assertion": item.get("binding_method") == "exact_tool_excerpt" or _normalized_text(claim) in " ".join(excerpts),
            "no_absolute_or_medical_overreach": not any(marker in claim for marker in ABSOLUTE_OR_MEDICAL_MARKERS),
        }
        local_pass = all(checks.values())
        model_row = model_by_id.get(audit_id) or model_by_claim.get(claim, {})
        model_verdict = str(model_row.get("verdict", "missing"))
        model_pass = model_verdict == "supported_limited"
        final_pass = local_pass and (model_pass if require_model_review else True)
        reasons = [name for name, ok in checks.items() if not ok]
        if require_model_review and not model_pass:
            reasons.append(f"adversarial_model_{model_verdict}")
        item["strict_review_status"] = "proven_for_limited_use" if final_pass else "rejected_unproven"
        item["script_eligible"] = final_pass
        findings.append(item)
        audit_row = {
            "audit_id": audit_id,
            "claim": claim,
            "initial_assumption": "false",
            "local_checks": checks,
            "local_verdict": "supported_limited" if local_pass else "unproven",
            "model_verdict": model_verdict if require_model_review else "not_required",
            "final_verdict": "supported_limited" if final_pass else "rejected_unproven",
            "rejection_reasons": reasons,
            "model_reasons": model_row.get("reasons", []),
        }
        audit_rows.append(audit_row)
        if final_pass:
            passed.append(item)

    reviewed_verdicts = {"supported_limited", "insufficient", "contradicted"}
    model_review_complete = bool(audit_rows) and all(
        row["model_verdict"] in reviewed_verdicts for row in audit_rows
    )
    provider_reported_status = (
        str(model_review.get("status", "unknown"))
        if isinstance(model_review, dict)
        else ("missing" if require_model_review else "not_required")
    )

    audited["findings"] = findings
    audited["script_eligible_findings"] = passed
    audited["strict_audit"] = {
        "policy": "assume_all_claims_false_until_independently_proven",
        "promotion_rule": "模型只能否决或降级，不能推翻本地证据失败项",
        "model_review_required": require_model_review,
        # This status describes whether every finding was reviewed, not whether
        # the provider liked the evidence. Per-finding verdicts carry that decision.
        "model_review_status": "complete" if require_model_review and model_review_complete else provider_reported_status,
        "model_provider_reported_status": provider_reported_status,
        "status": "passed" if passed and len(passed) == len(findings) else "blocked_or_partial",
        "finding_count": len(findings),
        "passed_count": len(passed),
        "rejected_count": len(findings) - len(passed),
        "findings": audit_rows,
        "model_review_findings": [
            {
                "audit_id": str(item.get("audit_id", "")),
                "verdict": str(item.get("verdict", "")),
                "reasons": item.get("reasons", []),
                "safe_scope": str(item.get("safe_scope", "")),
            }
            for item in model_rows if isinstance(item, dict)
        ],
    }
    audited["evidence_review"] = {
        "finding_count": len(findings),
        "script_eligible_count": len(passed),
        "excluded_count": len(findings) - len(passed),
    }
    if len(passed) < len(findings):
        audited["status"] = "partial"
    return audited


def adversarial_review_payload(research: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "audit_id": hashlib.sha256(str(item.get("claim", "")).encode("utf-8")).hexdigest()[:16],
            "claim": str(item.get("claim", "")),
            "evidence": item.get("evidence", []),
            "limitations": item.get("limitations", []),
            "local_binding_method": item.get("binding_method", ""),
        }
        for item in research.get("script_eligible_findings", [])
        if isinstance(item, dict)
    ]
