from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from typing import Any


LOCAL_SOURCE_TYPES = {"government", "education_research", "source_page"}
LEGACY_LOCALLY_BOUND_SOURCE_TYPES = {
    "government_law",
    "government_standard_metadata",
    "media_original",
}
ABSOLUTE_OR_MEDICAL_MARKERS = (
    "所有产品", "全部产品", "保证", "彻底", "永久", "百分之百", "零甲醛",
    "无风险", "最好", "第一", "唯一", "顶级", "根治", "治愈", "包治",
    "药到病除", "疗效保证", "预防疾病", "不会复发", "保证收益", "承诺收益",
    "保本", "保收益", "稳赚", "必赚", "稳赚不赔", "包胜诉", "保证胜诉",
    "必然胜诉", "胜诉率百分之百", "行业排名第一", "全国排名第一", "销量第一",
    "权威认证", "官方认证", "唯一认证",
)
PAGE_ATTRIBUTION_MARKERS = (
    "该页面", "来源页面", "页面称", "页面介绍", "网页称", "网站称", "官网称",
    "企业官网", "机构页面", "报道中", "文章称", "文中称", "资料称", "发布者称", "据该页面",
)
NUMERIC_EFFICACY_MARKERS = (
    "有效", "效果", "功效", "提升", "提高", "降低", "减少", "增长", "改善", "减重",
    "转化率", "成功率", "治愈率", "去除率", "除菌率", "合格率", "满意率", "收益率",
    "准确率", "效率", "销量", "业绩",
)
BOUNDARY_MARKER_GROUPS = (
    ("测试", "试验", "报告", "研究", "数据口径"),
    ("样本", "对象", "人群", "客户", "用户", "门店", "病例", "参与者"),
    ("时间", "期间", "小时", "天", "月", "年", "截至"),
    ("条件", "场景", "环境", "方法", "口径", "范围", "适用"),
)


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value).replace("³", "3").replace("㎡", "m2")).strip()


def _numeric_tokens(value: Any) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?%?", _normalized_text(value)))


def extracted_page_records(tool_trace: list[dict[str, Any]] | None) -> dict[str, dict[str, str]]:
    """Return successful local extraction records keyed by requested and final URL."""
    pages: dict[str, dict[str, str]] = {}
    for call in tool_trace or []:
        if not isinstance(call, dict) or call.get("tool") != "extract_url":
            continue
        result = call.get("result") if isinstance(call.get("result"), dict) else {}
        if result.get("ok") is not True:
            continue
        text = _normalized_text(result.get("text", ""))
        if not text:
            continue
        requested_url = str(result.get("url") or "").strip()
        final_url = str(result.get("final_url") or requested_url).strip()
        record = {
            "text": text,
            "requested_url": requested_url,
            "final_url": final_url,
            "title": _normalized_text(result.get("title", "")),
        }
        for key in (requested_url, final_url):
            if key:
                pages[key] = dict(record)
    return pages


def _hostname(value: str) -> str:
    try:
        return (urllib.parse.urlsplit(value).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def derive_local_source_type(
    url: str,
    records: dict[str, dict[str, str]],
) -> str:
    """Classify a source from the successful extraction record and final URL only."""
    record = records.get(str(url))
    if not record:
        return "web_page_unverified"
    hostname = _hostname(record.get("final_url") or str(url))
    if hostname == "gov.cn" or hostname.endswith(".gov.cn"):
        return "government"
    if (
        hostname == "edu.cn"
        or hostname.endswith(".edu.cn")
        or hostname == "ac.cn"
        or hostname.endswith(".ac.cn")
        or hostname.endswith(".edu")
        or hostname == "cas.cn"
        or hostname.endswith(".cas.cn")
    ):
        return "education_research"
    return "source_page"


def _captured_pages(records: dict[str, dict[str, str]]) -> dict[str, str]:
    return {url: record["text"] for url, record in records.items()}


def _is_page_attributed(claim: str) -> bool:
    return any(marker in claim for marker in PAGE_ATTRIBUTION_MARKERS)


def scope_source_page_claim(claim: str) -> tuple[str, bool]:
    """Make an ordinary-page claim explicitly attributable without adding facts."""
    normalized = _normalized_text(claim)
    if _is_page_attributed(normalized):
        return normalized, False
    return f"该来源页面称：{normalized}", True


def _numeric_efficacy_has_boundaries(claim: str, excerpts: list[str], limitations: list[str]) -> bool:
    combined = " ".join([claim, *excerpts, *limitations])
    if not _numeric_tokens(claim) or not any(marker in combined for marker in NUMERIC_EFFICACY_MARKERS):
        return True
    represented_groups = sum(any(marker in combined for marker in group) for group in BOUNDARY_MARKER_GROUPS)
    return represented_groups >= 2


def strict_audit_research(
    research: dict[str, Any],
    tool_trace: list[dict[str, Any]],
    *,
    model_review: dict[str, Any] | None = None,
    require_model_review: bool = False,
) -> dict[str, Any]:
    """Assume every finding is false; only exact, scoped proof can overturn rejection."""
    audited = dict(research)
    records = extracted_page_records(tool_trace)
    pages = _captured_pages(records)
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
        evidence = [dict(entry) for entry in item.get("evidence", []) if isinstance(entry, dict)]
        excerpts = [_normalized_text(entry.get("excerpt", "")) for entry in evidence]
        exact_checks = []
        local_source_types: list[str] = []
        for entry, excerpt in zip(evidence, excerpts):
            url = str(entry.get("url", ""))
            exact_checks.append(bool(excerpt and url in pages and excerpt in pages[url]))
            local_source_type = derive_local_source_type(url, records)
            local_source_types.append(local_source_type)
            declared_type = str(entry.get("source_type", ""))
            locally_bound_legacy_type = (
                entry.get("source_classification") == "local_exact_rule"
                and declared_type in LEGACY_LOCALLY_BOUND_SOURCE_TYPES
                and (
                    not declared_type.startswith("government")
                    or local_source_type == "government"
                )
            )
            entry["source_type"] = declared_type if locally_bound_legacy_type else local_source_type
            entry["local_source_type"] = local_source_type
            entry["source_scope"] = (
                "source_page_statement_only"
                if local_source_type == "source_page"
                else "higher_trust_domain_page"
                if local_source_type in {"government", "education_research"}
                else "unverified"
            )
        item["evidence"] = evidence
        item["source_scope"] = (
            "source_page_statement_only"
            if local_source_types and all(value == "source_page" for value in local_source_types)
            else "higher_trust_domain_page"
            if local_source_types and all(value in {"government", "education_research"} for value in local_source_types)
            else "mixed_or_unverified"
        )
        # A higher-trust domain can support scoped human review, but domain
        # identity alone is still not a blanket finding of independent truth.
        item["independent_fact_supported"] = (
            False
            if local_source_types and all(value == "source_page" for value in local_source_types)
            else None
        )
        claim_numbers = _numeric_tokens(claim)
        evidence_numbers = set().union(*(_numeric_tokens(value) for value in excerpts)) if excerpts else set()
        limitations = [str(value).strip() for value in item.get("limitations", []) if str(value).strip()]
        ordinary_only = bool(local_source_types) and all(value == "source_page" for value in local_source_types)
        checks = {
            "previous_evidence_gate": item.get("script_eligible") is True,
            "captured_exact_excerpt": bool(exact_checks) and all(exact_checks),
            "locally_classified_source": bool(local_source_types) and all(
                value in LOCAL_SOURCE_TYPES for value in local_source_types
            ),
            "ordinary_page_is_attributed": not ordinary_only or _is_page_attributed(claim),
            "numbers_present_in_evidence": claim_numbers.issubset(evidence_numbers),
            "numeric_efficacy_has_boundaries": _numeric_efficacy_has_boundaries(claim, excerpts, limitations),
            "explicit_scope_limit": bool(limitations),
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
            "local_source_types": local_source_types,
            "evidence_scope": item["source_scope"],
            "independent_fact_supported": item["independent_fact_supported"],
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
