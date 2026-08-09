from __future__ import annotations

import unicodedata
from typing import Any, Mapping


HUMAN_STAGE_REVIEW = "human"
AGENT_TEST_REVIEW = "agent_test"
STAGE_REVIEW_MODES = {HUMAN_STAGE_REVIEW, AGENT_TEST_REVIEW}
CODEX_TEST_REVIEWER = "Codex 测试代理"

HUMAN_IDENTITY = {
    "actor_type": "human",
    "review_mode": "formal",
    "interaction_mode": "browser_operated",
    "authority": "stage_review",
    "human_approval_claimed": True,
    "test_only": False,
}
AGENT_TEST_IDENTITY = {
    "actor_type": "agent",
    "review_mode": "test",
    "interaction_mode": "browser_operated",
    "authority": "test_progress_only",
    "human_approval_claimed": False,
    "test_only": True,
}
IDENTITY_FIELDS = tuple(HUMAN_IDENTITY)

_AUTOMATION_REVIEWER_MARKERS = frozenset(
    {
        "codex",
        "agent",
        "automation",
        "automated",
        "system",
        "test",
        "mock",
        "bot",
        "ci",
        "代理",
        "自动化",
        "系统",
        "测试",
        "机器人",
    }
)
_AUTOMATION_REVIEWER_ROLE_FRAGMENTS = frozenset(
    {
        "review",
        "reviewer",
        "approval",
        "approver",
        "audit",
        "auditor",
        "assistant",
        "operator",
        "runner",
        "user",
        "account",
        "审核",
        "审核员",
        "审批",
        "审批人",
        "审查",
        "审查员",
        "用户",
        "账号",
        "账户",
    }
)
_CHINESE_AUTOMATION_REVIEWER_MARKERS = frozenset({"代理", "自动化", "系统", "测试", "机器人"})


def _reviewer_name_views(value: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Return punctuation-insensitive and token views for identity safety checks."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    collapsed = "".join(character for character in normalized if character.isalnum())
    tokens: list[str] = []
    current: list[str] = []
    for character in normalized:
        if character.isalnum():
            current.append(character)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    ascii_segments: list[str] = []
    current_ascii: list[str] = []
    for character in normalized:
        if character.isascii() and character.isalnum():
            current_ascii.append(character)
        elif character.isalnum() and current_ascii:
            ascii_segments.append("".join(current_ascii))
            current_ascii = []
    if current_ascii:
        ascii_segments.append("".join(current_ascii))
    return collapsed, tuple(tokens), tuple(ascii_segments)


def _is_obvious_automation_reviewer(value: str) -> bool:
    collapsed, tokens, ascii_segments = _reviewer_name_views(value)
    if not collapsed:
        return False
    if collapsed in _AUTOMATION_REVIEWER_MARKERS:
        return True
    if any(token in _AUTOMATION_REVIEWER_MARKERS for token in tokens):
        return True
    if any(segment in _AUTOMATION_REVIEWER_MARKERS for segment in ascii_segments):
        return True
    marker_counts = [-1] * (len(collapsed) + 1)
    marker_counts[0] = 0
    for start in range(len(collapsed)):
        if marker_counts[start] < 0:
            continue
        for marker in _AUTOMATION_REVIEWER_MARKERS:
            if collapsed.startswith(marker, start):
                end = start + len(marker)
                marker_counts[end] = max(marker_counts[end], marker_counts[start] + 1)
    if marker_counts[-1] >= 2:
        return True
    if any(marker in collapsed for marker in _CHINESE_AUTOMATION_REVIEWER_MARKERS):
        return True
    for marker in _AUTOMATION_REVIEWER_MARKERS - _CHINESE_AUTOMATION_REVIEWER_MARKERS:
        if collapsed.startswith(marker):
            remainder = collapsed[len(marker) :]
            if remainder.isdigit() or remainder in _AUTOMATION_REVIEWER_ROLE_FRAGMENTS:
                return True
        if collapsed.endswith(marker):
            remainder = collapsed[: -len(marker)]
            if remainder.isdigit() or remainder in _AUTOMATION_REVIEWER_ROLE_FRAGMENTS:
                return True
    return False


def _automation_reviewer_error(reviewer: str) -> list[str]:
    if _is_obvious_automation_reviewer(reviewer):
        return ["正式人审不能使用代理、自动化、系统或测试身份"]
    return []


def build_review_policy(stage_review_mode: str = HUMAN_STAGE_REVIEW) -> dict[str, Any]:
    mode = str(stage_review_mode).strip()
    if mode not in STAGE_REVIEW_MODES:
        raise ValueError("stage_review_mode无效")
    return {
        "stage_review_mode": mode,
        "final_human_acceptance_required": mode == AGENT_TEST_REVIEW,
    }


def normalize_review_policy(value: Any) -> dict[str, Any]:
    if value is None:
        return build_review_policy()
    if not isinstance(value, Mapping):
        raise ValueError("review_policy结构无效")
    mode = str(value.get("stage_review_mode", "")).strip()
    policy = build_review_policy(mode)
    if value.get("final_human_acceptance_required") is not policy["final_human_acceptance_required"]:
        raise ValueError("review_policy与最终验收边界不一致")
    return policy


def approval_identity(stage_review_mode: str, reviewer: str) -> dict[str, Any]:
    mode = build_review_policy(stage_review_mode)["stage_review_mode"]
    supplied = str(reviewer).strip()
    if mode == AGENT_TEST_REVIEW:
        if supplied != CODEX_TEST_REVIEWER:
            raise ValueError("代理测试审查人必须由服务器固定为Codex测试代理")
        return {"reviewer": CODEX_TEST_REVIEWER, **AGENT_TEST_IDENTITY}
    if not 2 <= len(supplied) <= 80:
        raise ValueError("审批人名称必须在2到80字之间")
    automation_errors = _automation_reviewer_error(supplied)
    if automation_errors:
        raise ValueError(automation_errors[0])
    return {"reviewer": supplied, **HUMAN_IDENTITY}


def classify_approval_record(record: Any, *, allow_legacy_human: bool) -> tuple[str | None, list[str]]:
    if not isinstance(record, Mapping):
        return None, ["审批记录结构无效"]
    reviewer = str(record.get("reviewer", "")).strip()
    if not reviewer:
        return None, ["审批人为空"]
    present = {field for field in IDENTITY_FIELDS if field in record}
    if not present:
        if allow_legacy_human:
            automation_errors = _automation_reviewer_error(reviewer)
            if automation_errors:
                return None, automation_errors
            return HUMAN_STAGE_REVIEW, []
        return None, ["新审批记录缺少结构化审查身份"]
    if present != set(IDENTITY_FIELDS):
        return None, ["结构化审查身份字段不完整"]
    actual = {field: record.get(field) for field in IDENTITY_FIELDS}
    if actual == HUMAN_IDENTITY:
        automation_errors = _automation_reviewer_error(reviewer)
        if automation_errors:
            return None, automation_errors
        return HUMAN_STAGE_REVIEW, []
    if actual == AGENT_TEST_IDENTITY and reviewer == CODEX_TEST_REVIEWER:
        return AGENT_TEST_REVIEW, []
    return None, ["审查身份字段组合不一致"]


def approval_validation_line(approvals: Mapping[str, Any], *, allow_legacy_human: bool) -> tuple[str, list[str]]:
    modes: list[str] = []
    errors: list[str] = []
    for gate in ("research", "compliance"):
        mode, gate_errors = classify_approval_record(
            approvals.get(gate), allow_legacy_human=allow_legacy_human
        )
        if gate_errors:
            errors.extend(f"{gate}: {error}" for error in gate_errors)
        elif mode is not None:
            modes.append(mode)
    if errors:
        return "", errors
    if modes == [HUMAN_STAGE_REVIEW, HUMAN_STAGE_REVIEW]:
        return "审批：研究 finding 逐项决定与最终脚本合规放行均来自用户本人操作；自动流程不代签。", []
    if modes == [AGENT_TEST_REVIEW, AGENT_TEST_REVIEW]:
        return (
            "审查：研究 finding 与最终脚本两道测试门禁均由 Codex 测试代理通过本地浏览器操作；"
            "记录明确为 test_only，未冒充用户本人签署；用户最终成片验收另行记录。",
            [],
        )
    return (
        "审查：研究与合规门禁采用混合审查身份，具体记录见 approvals.json；"
        "不得统一声称均由用户本人签署。",
        [],
    )


def evidence_status_for_policy(policy: Any) -> str:
    mode = normalize_review_policy(policy)["stage_review_mode"]
    return (
        "test_only_pending_human_acceptance"
        if mode == AGENT_TEST_REVIEW
        else "human_stage_reviews_complete"
    )
