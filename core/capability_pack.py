from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable


CAPABILITY_PACK_SCHEMA_VERSION = 1
PACK_FIELDS = {
    "schema_version",
    "id",
    "version",
    "sha256",
    "generated_at",
    "source",
    "snapshot",
    "audit",
}
SNAPSHOT_FIELDS = {
    "label",
    "industry",
    "goal",
    "audience",
    "platforms",
    "content_purpose",
    "tone",
    "preferred_terms",
    "avoided_terms",
    "evidence_requirements",
    "prohibited_claims",
    "visual_direction",
    "assumptions",
    "risk_level",
}
LIST_FIELDS = {
    "platforms",
    "tone",
    "preferred_terms",
    "avoided_terms",
    "evidence_requirements",
    "prohibited_claims",
    "visual_direction",
    "assumptions",
}
AUDIT_FIELDS = {
    "status",
    "generated_by",
    "reviewer",
    "reviewed_at",
    "note",
    "warnings",
    "checks",
    "risk_flags",
    "constraints_added",
}
AUDIT_LIST_FIELDS = {"warnings", "checks", "risk_flags", "constraints_added"}
AUDIT_STATUSES = frozenset({
    "local_unreviewed",
    "local_safe_fallback",
    "passed",
    "needs_revision",
    "blocked",
    "legacy_compatibility",
})
EXECUTABLE_AUDIT_STATUSES = frozenset({"local_safe_fallback", "passed", "legacy_compatibility"})
LEGACY_CLEAN_AIR_PACK_ID = "legacy-clean-air-v2"
LEGACY_CLEAN_AIR_PACK_VERSION = "2.0.0"
LEGACY_CLEAN_AIR_SOURCE = "legacy"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_URL_RE = re.compile(r"(?i)(?:https?|file|ftp)://")
_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|(?:^|\s)/(?:etc|home|users|var|tmp)/|\.\.[\\/])")
_SECRET_RE = re.compile(
    r"(?i)(?:api[_ -]?key|access[_ -]?token|password|authorization|cookie)\s*[:=]\s*\S+|\bsk-[A-Za-z0-9_-]{12,}\b"
)
_COMMAND_RE = re.compile(
    r"(?i)(?:\b(?:powershell|cmd\.exe|bash|zsh)\s+(?:-[a-z]|/c)"
    r"|\b(?:curl|wget)\s+(?:-[A-Za-z]+\s+)*(?:https?|ftp)://"
    r"|\brm\s+-rf\b|\binvoke-expression\b|\bsubprocess\.)"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_FORBIDDEN_KEYS = {
    "prompt",
    "system_prompt",
    "developer_prompt",
    "instruction_prompt",
    "regex",
    "regexp",
    "path",
    "filepath",
    "directory",
    "command",
    "cmd",
    "shell",
    "script",
    "code",
    "secret",
    "secrets",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "password",
    "cookie",
    "authorization",
    "url",
    "endpoint",
    "headers",
}

_PROMPT_INJECTION_PATTERNS = (
    re.compile(r"(?i)ignore\s+(?:all\s+)?(?:previous|prior|above|system|developer)\s+(?:instructions?|messages?)"),
    re.compile(r"(?i)(?:reveal|show|print|leak)\s+(?:the\s+)?(?:system|developer)\s+(?:prompt|message|instructions?)"),
    re.compile(r"(?i)(?:jailbreak|developer\s+mode|\bDAN\b|bypass\s+(?:safety|policy|guardrails?))"),
    re.compile(r"忽略(?:之前|以上|先前|所有|系统|开发者)(?:的)?(?:指令|要求|消息|规则)"),
    re.compile(r"无视(?:之前|以上|所有|系统)(?:的)?(?:指令|要求|规则)"),
    re.compile(r"(?:输出|显示|泄露|告诉我)(?:你的|当前的)?(?:系统|开发者)(?:提示词|指令|消息)"),
    re.compile(r"(?:越狱|绕过)(?:安全|审核|限制|规则|政策)"),
    re.compile(r"<\|(?:system|assistant|developer|user)\|>|\[/?INST\]", re.IGNORECASE),
)

_MALICIOUS_PATTERNS = (
    re.compile(r"(?:写|制作|开发|生成|编写|搭建).{0,16}(?:钓鱼网站|木马|勒索软件|恶意软件|盗号工具|窃取密码|炸弹|爆炸物)"),
    re.compile(r"(?:如何|帮我|教我).{0,12}(?:诈骗|洗钱|人肉|盗号|入侵|窃取|销毁证据|逃避执法|绕过风控)"),
    re.compile(r"(?:忽略|无视|绕过).{0,16}(?:前面|以上|之前|系统|开发者|安全|指令|规则)"),
    re.compile(r"(?i)(?:build|write|create).{0,30}(?:phishing|ransomware|malware|credential\s+stealer|explosive)"),
    re.compile(r"(?i)(?:how\s+to|help\s+me).{0,30}(?:defraud|launder\s+money|steal\s+credentials|evade\s+law\s+enforcement)"),
)

_PERSONAL_MARKERS = (
    "我的", "我有", "我现在", "我应该", "我该", "帮我", "给我", "替我", "本人",
    "my ", "i have", "should i", "for me", "tell me which",
)
_MEDICAL_MARKERS = (
    "症状", "疾病", "诊断", "治疗", "处方", "用药", "药物", "剂量", "手术", "胸痛", "癌症", "怀孕",
    "血液", "化验", "检验报告", "医学报告",
    "diagnos", "prescription", "dosage", "symptom", "treatment",
)
_MEDICAL_ACTIONS = (
    "吃什么药", "吃哪种药", "开药", "确诊", "制定治疗", "调整剂量", "停药", "替代医生", "解读", "分析报告",
    "what medicine", "which medicine", "diagnose me", "treatment plan", "adjust my dose",
)
_FINANCE_MARKERS = (
    "股票", "基金", "期货", "期权", "加密货币", "投资", "仓位", "贷款", "理财", "交易",
    "stock", "fund", "futures", "crypto", "portfolio", "investment", "trade",
)
_FINANCE_ACTIONS = (
    "买哪只", "买什么", "卖不卖", "买入卖出", "仓位配置", "具体投资建议", "保证收益", "稳赚",
    "which stock", "what should i buy", "buy or sell", "portfolio allocation", "guaranteed return",
)
_LEGAL_MARKERS = (
    "诉讼", "律师", "法律", "合同纠纷", "刑事", "案情", "赔偿", "责任", "取保", "离婚",
    "lawsuit", "legal case", "criminal", "liability", "legal advice",
)
_LEGAL_ACTIONS = (
    "规避法律责任", "逃避责任", "对付律师", "销毁证据", "伪造证据", "具体诉讼策略", "代替律师",
    "evade liability", "destroy evidence", "fabricate evidence", "specific litigation strategy",
)
_PERSONAL_MEDICAL_DECISIONS = ("怎么办", "怎么回事", "帮我判断", "是否需要就医", "该不该去医院", "what should i do")
_PERSONAL_FINANCE_DECISIONS = ("怎么办", "帮我判断", "值不值得买", "该不该买", "该不该卖", "怎么看这只", "worth buying")
_PERSONAL_LEGAL_DECISIONS = ("怎么办", "怎么处理", "帮我判断", "该不该起诉", "如何应对", "what should i do")

_BASE_EVIDENCE_REQUIREMENTS = (
    "事实性主张必须有可追溯证据",
    "数据、效果和对比声明必须标明来源与适用边界",
)
_BASE_PROHIBITED_CLAIMS = (
    "不得虚构事实、数据、用户证言、案例或来源",
    "不得作出无法验证的保证、绝对化承诺或虚假稀缺表述",
)


class CapabilityPackError(ValueError):
    """Raised when a goal or capability pack violates the declarative contract."""

    status = 422
    code = "invalid_capability_pack"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _identity_payload(pack: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable, security-relevant capability-pack identity.

    ``generated_at`` is deliberately transport metadata rather than authority:
    regenerating the same built-in pack must keep one stable identity.  The ID,
    version, source, declarative snapshot and complete audit record are all
    bound by ``sha256`` so none can be relabelled independently.
    """

    return {
        "schema_version": pack["schema_version"],
        "id": pack["id"],
        "version": pack["version"],
        "source": pack["source"],
        "snapshot": pack["snapshot"],
        "audit": pack["audit"],
    }


def _clean_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _contains_any(text: str, markers: Iterable[str]) -> bool:
    lowered = text.casefold()
    return any(marker.casefold() in lowered for marker in markers)


def _assert_natural_text(value: str, *, field: str) -> None:
    if _CONTROL_RE.search(value):
        raise CapabilityPackError(f"{field} contains control characters")
    if _URL_RE.search(value) or _PATH_RE.search(value) or _COMMAND_RE.search(value) or _SECRET_RE.search(value):
        raise CapabilityPackError(f"{field} may contain declarative text only")
    if any(pattern.search(value) for pattern in _PROMPT_INJECTION_PATTERNS):
        raise CapabilityPackError(f"{field} contains instruction-override text")


def _assert_no_forbidden_keys(value: Any, *, location: str = "value") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise CapabilityPackError(f"{location} contains a non-string key")
            normalized = key.strip().casefold().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS:
                raise CapabilityPackError(f"{location} contains forbidden field: {key}")
            _assert_no_forbidden_keys(nested, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_no_forbidden_keys(nested, location=f"{location}[{index}]")


def validate_goal(goal: object, *, minimum: int = 4, maximum: int = 200) -> str:
    """Validate a general content goal without tying it to a single industry."""

    if not isinstance(goal, str):
        raise CapabilityPackError("目标必须是字符串")
    if (
        not isinstance(minimum, int)
        or isinstance(minimum, bool)
        or not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or minimum < 1
        or maximum < minimum
        or maximum > 200
    ):
        raise CapabilityPackError("目标长度边界无效")
    text = _clean_space(goal)
    if not minimum <= len(text) <= maximum:
        raise CapabilityPackError(f"目标长度必须在 {minimum} 到 {maximum} 字之间")
    _assert_natural_text(text, field="goal")
    if any(pattern.search(text) for pattern in _MALICIOUS_PATTERNS):
        raise CapabilityPackError("目标包含恶意、侵害或欺骗性请求")

    personalized = _contains_any(text, _PERSONAL_MARKERS) or bool(
        re.search(r"(?:^|[，。！？；\s])我(?:的|有|患|胸|头|肚|该|应|想|需要|正在)", text)
    )
    if _contains_any(text, _MEDICAL_MARKERS) and (
        _contains_any(text, _MEDICAL_ACTIONS)
        or (personalized and _contains_any(text, _PERSONAL_MEDICAL_DECISIONS))
    ):
        raise CapabilityPackError("不支持个性化医疗诊断、用药或治疗决策")
    if _contains_any(text, _FINANCE_MARKERS) and (
        _contains_any(text, _FINANCE_ACTIONS)
        or (personalized and _contains_any(text, _PERSONAL_FINANCE_DECISIONS))
    ):
        raise CapabilityPackError("不支持个性化投资、交易或金融决策")
    if _contains_any(text, _LEGAL_MARKERS) and (
        _contains_any(text, _LEGAL_ACTIONS)
        or (personalized and _contains_any(text, _PERSONAL_LEGAL_DECISIONS))
    ):
        raise CapabilityPackError("不支持个性化高风险法律策略")
    return text


def _bounded_text(value: Any, *, field: str, minimum: int = 1, maximum: int = 200) -> str:
    if not isinstance(value, str):
        raise CapabilityPackError(f"{field} must be a string")
    text = _clean_space(value)
    if not minimum <= len(text) <= maximum:
        raise CapabilityPackError(f"{field} length must be between {minimum} and {maximum}")
    _assert_natural_text(text, field=field)
    return text


def _normalize_list(value: Any, *, field: str, default: Iterable[str] = (), maximum_items: int = 24) -> list[str]:
    if value is None:
        values: list[Any] = list(default)
    elif isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise CapabilityPackError(f"{field} must be a string list")
    if len(values) > maximum_items:
        raise CapabilityPackError(f"{field} contains too many items")
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _bounded_text(item, field=field, maximum=300)
        folded = text.casefold()
        if folded not in seen:
            seen.add(folded)
            result.append(text)
    return result


def _merge_constraints(base: Iterable[str], extra: Any, *, field: str) -> list[str]:
    values = list(base)
    if extra is not None:
        if isinstance(extra, str):
            values.append(extra)
        elif isinstance(extra, list):
            values.extend(extra)
        else:
            raise CapabilityPackError(f"{field} must be a string list")
    return _normalize_list(values, field=field)


def _infer_industry(goal: str) -> str:
    groups = (
        (("餐饮", "饭店", "餐厅", "食品", "咖啡", "restaurant", "food"), "餐饮与食品"),
        (("酒店", "旅游", "景区", "民宿", "hotel", "travel"), "酒店与文旅"),
        (("教育", "课程", "培训", "学校", "education", "course"), "教育与知识服务"),
        (("软件", "saas", "app", "人工智能", "ai产品"), "软件与数字服务"),
        (("医院", "医疗", "健康", "药品", "health", "medical"), "医疗健康科普"),
        (("银行", "保险", "金融", "投资", "finance", "insurance"), "金融知识服务"),
        (("律所", "律师", "法律", "law firm", "legal"), "法律知识服务"),
        (("装修", "家居", "室内空气", "甲醛", "renovation", "home"), "家居与本地服务"),
        (("电商", "零售", "商品", "门店", "ecommerce", "retail"), "零售与电商"),
        (("制造", "工厂", "设备", "manufacturing", "factory"), "制造与企业服务"),
    )
    for markers, label in groups:
        if _contains_any(goal, markers):
            return label
    return "通用企业与内容服务"


def _infer_audience(goal: str) -> str:
    """Keep an explicit audience from the user's goal instead of a UI placeholder."""

    match = re.search(r"(?:^|[，。；;])(?:主要)?面向([^，。；;]{2,24})", goal)
    if not match:
        match = re.search(r"^(?:主要)?面向([^，。；;]{2,24})", goal)
    if match:
        audience = _clean_space(match.group(1)).strip("，。；;:：")
        if audience:
            return audience
    return "目标客户与内容受众"


def _infer_platforms(goal: str) -> list[str]:
    known = ("抖音", "TikTok", "小红书", "视频号", "快手", "B站", "YouTube")
    selected = [platform for platform in known if platform.casefold() in goal.casefold()]
    return selected or ["抖音", "TikTok"]


def _infer_purpose(goal: str) -> str:
    if _contains_any(goal, ("营销", "宣传", "宣发", "获客", "销售", "推广", "marketing", "advertis")):
        return "品牌传播与负责任的营销沟通"
    if _contains_any(goal, ("科普", "教程", "知识", "解释", "教学", "education", "tutorial")):
        return "知识讲解与可信传播"
    return "信息说明、品牌传播与用户沟通"


def _infer_risk(goal: str) -> str:
    if _contains_any(goal, _MEDICAL_MARKERS + _FINANCE_MARKERS + _LEGAL_MARKERS):
        return "high"
    if _contains_any(goal, ("功效", "检测", "安全", "儿童", "母婴", "保证", "收益", "合规")):
        return "medium"
    return "low"


def _normalize_audit(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise CapabilityPackError("audit must be an object")
    unknown = sorted(set(value) - AUDIT_FIELDS)
    if unknown:
        raise CapabilityPackError(f"audit contains unknown fields: {', '.join(unknown)}")
    status = _bounded_text(value.get("status", "local_unreviewed"), field="audit.status", maximum=40)
    if status not in AUDIT_STATUSES:
        raise CapabilityPackError(
            f"audit.status must be one of: {', '.join(sorted(AUDIT_STATUSES))}"
        )
    result: dict[str, Any] = {
        "status": status,
        "warnings": _normalize_list(value.get("warnings", []), field="audit.warnings", maximum_items=24),
        "checks": _normalize_list(
            value.get("checks", ["strict_field_whitelist", "canonical_identity_hash"]),
            field="audit.checks",
            maximum_items=24,
        ),
        "risk_flags": _normalize_list(value.get("risk_flags", []), field="audit.risk_flags", maximum_items=24),
    }
    for field in ("generated_by", "reviewer", "reviewed_at", "note"):
        if field in value:
            result[field] = _bounded_text(value[field], field=f"audit.{field}", maximum=500 if field == "note" else 100)
    if "constraints_added" in value:
        result["constraints_added"] = _normalize_list(
            value["constraints_added"], field="audit.constraints_added", maximum_items=24
        )
    return result


def _snapshot_from_raw(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if "snapshot" in raw:
        unknown = sorted(set(raw) - PACK_FIELDS)
        if unknown:
            raise CapabilityPackError(f"capability pack contains unknown fields: {', '.join(unknown)}")
        snapshot = raw.get("snapshot")
        metadata = raw
    else:
        unknown = sorted(set(raw) - SNAPSHOT_FIELDS)
        if unknown:
            raise CapabilityPackError(f"capability snapshot contains unknown fields: {', '.join(unknown)}")
        snapshot = raw
        metadata = {}
    if not isinstance(snapshot, dict):
        raise CapabilityPackError("snapshot must be an object")
    unknown_snapshot = sorted(set(snapshot) - SNAPSHOT_FIELDS)
    if unknown_snapshot:
        raise CapabilityPackError(f"snapshot contains unknown fields: {', '.join(unknown_snapshot)}")
    return snapshot, metadata


def _legacy_snapshot() -> dict[str, Any]:
    return {
        "label": "净界室内空气内容能力包",
        "industry": "家居与室内空气服务",
        "goal": "生成审慎、可溯源的室内空气与甲醛科普短视频",
        "audience": "关注新居、装修污染与室内空气的家庭",
        "platforms": ["抖音", "视频号"],
        "content_purpose": "室内空气科普与负责任的服务沟通",
        "tone": ["审慎", "专业", "易懂"],
        "preferred_terms": ["检测条件", "适用边界", "可追溯证据"],
        "avoided_terms": ["零甲醛", "百分百安全", "一次根治"],
        "evidence_requirements": [
            *_BASE_EVIDENCE_REQUIREMENTS,
            "检测数据须回指完整报告并说明检测条件",
        ],
        "prohibited_claims": [
            *_BASE_PROHIBITED_CLAIMS,
            "无已批准证据时不得宣称具体除醛率或健康结果",
        ],
        "visual_direction": ["清洁留白", "证据卡片优先", "竖屏大字可读"],
        "assumptions": ["未提供真实企业资料时不补写产品功效"],
        "risk_level": "high",
    }


def _legacy_audit() -> dict[str, Any]:
    return {
        "status": "legacy_compatibility",
        "warnings": ["仅用于历史项目兼容"],
        "checks": ["strict_field_whitelist", "canonical_identity_hash"],
        "risk_flags": [],
    }


def _assert_reserved_identity(pack: dict[str, Any]) -> None:
    """Keep the privileged legacy identity reserved for its exact built-in pack."""

    is_legacy_id = pack["id"] == LEGACY_CLEAN_AIR_PACK_ID
    claims_legacy_authority = (
        pack["source"] == LEGACY_CLEAN_AIR_SOURCE
        or pack["audit"]["status"] == "legacy_compatibility"
    )
    if not is_legacy_id:
        if claims_legacy_authority:
            raise CapabilityPackError("legacy source and audit status are reserved for the built-in legacy pack")
        return

    if (
        pack["version"] != LEGACY_CLEAN_AIR_PACK_VERSION
        or pack["source"] != LEGACY_CLEAN_AIR_SOURCE
        or pack["snapshot"] != _legacy_snapshot()
        or pack["audit"] != _legacy_audit()
    ):
        raise CapabilityPackError("legacy-clean-air-v2 identity is reserved for the exact built-in pack")


def normalize_capability_pack(
    raw: dict[str, Any],
    goal: object,
    source: str,
    audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap untrusted model/local output in an immutable declarative snapshot."""

    normalized_goal = validate_goal(goal)
    if not isinstance(raw, dict):
        raise CapabilityPackError("raw capability pack must be an object")
    _assert_no_forbidden_keys(raw, location="capability_pack")
    snapshot_raw, metadata = _snapshot_from_raw(raw)
    if "goal" in snapshot_raw:
        # The caller-owned goal remains authoritative, but an untrusted model
        # may not hide unsafe or incorrectly typed content in its duplicate.
        validate_goal(snapshot_raw["goal"])

    clean_source = _bounded_text(source, field="source", maximum=64)
    if not _SOURCE_RE.fullmatch(clean_source):
        raise CapabilityPackError("source must be a short machine identifier")
    industry = _bounded_text(
        snapshot_raw.get("industry", _infer_industry(normalized_goal)), field="industry", maximum=80
    )
    label = _bounded_text(
        snapshot_raw.get("label", f"{industry}内容能力包"), field="label", maximum=80
    )
    risk_level = _bounded_text(
        snapshot_raw.get("risk_level", _infer_risk(normalized_goal)), field="risk_level", maximum=16
    ).casefold()
    if risk_level not in {"low", "medium", "high"}:
        raise CapabilityPackError("risk_level must be low, medium, or high")

    snapshot = {
        "label": label,
        "industry": industry,
        "goal": normalized_goal,
        "audience": _bounded_text(
            snapshot_raw.get("audience", "目标客户与内容受众"), field="audience", maximum=80
        ),
        "platforms": _normalize_list(
            snapshot_raw.get("platforms", _infer_platforms(normalized_goal)), field="platforms", maximum_items=12
        ),
        "content_purpose": _bounded_text(
            snapshot_raw.get("content_purpose", _infer_purpose(normalized_goal)),
            field="content_purpose",
            maximum=160,
        ),
        "tone": _normalize_list(
            snapshot_raw.get("tone", ["清晰", "可信", "克制"]), field="tone", maximum_items=12
        ),
        "preferred_terms": _normalize_list(
            snapshot_raw.get("preferred_terms", []), field="preferred_terms", maximum_items=24
        ),
        "avoided_terms": _normalize_list(
            snapshot_raw.get("avoided_terms", []), field="avoided_terms", maximum_items=24
        ),
        "evidence_requirements": _merge_constraints(
            _BASE_EVIDENCE_REQUIREMENTS,
            snapshot_raw.get("evidence_requirements"),
            field="evidence_requirements",
        ),
        "prohibited_claims": _merge_constraints(
            _BASE_PROHIBITED_CLAIMS,
            snapshot_raw.get("prohibited_claims"),
            field="prohibited_claims",
        ),
        "visual_direction": _normalize_list(
            snapshot_raw.get("visual_direction", ["与行业语义一致", "移动端竖屏清晰可读"]),
            field="visual_direction",
            maximum_items=16,
        ),
        "assumptions": _normalize_list(
            snapshot_raw.get("assumptions", ["缺少企业资料时使用明确占位并提示人工补充"]),
            field="assumptions",
            maximum_items=16,
        ),
        "risk_level": risk_level,
    }
    if not snapshot["platforms"] or not snapshot["tone"]:
        raise CapabilityPackError("platforms and tone may not be empty")

    pack_id = metadata.get("id")
    if pack_id is None:
        # Model wording can drift between calls.  Use the deterministic local
        # industry taxonomy inferred from the user-owned goal so a harmless
        # label rewrite does not fragment the memory/Skill identity.
        identity = {"industry_key": _infer_industry(normalized_goal)}
        pack_id = f"pack-{_canonical_sha256(identity)[:16]}"
    if not isinstance(pack_id, str) or not _ID_RE.fullmatch(pack_id):
        raise CapabilityPackError("id must be a safe lowercase machine identifier")
    version = metadata.get("version", "1.0.0")
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise CapabilityPackError("version must be a short version identifier")

    audit_value = audit if audit is not None else metadata.get("audit")
    normalized_audit = _normalize_audit(audit_value)
    pack = {
        "schema_version": CAPABILITY_PACK_SCHEMA_VERSION,
        "id": pack_id,
        "version": version,
        "sha256": "",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": clean_source,
        "snapshot": snapshot,
        "audit": normalized_audit,
    }
    pack["sha256"] = _canonical_sha256(_identity_payload(pack))
    return validate_capability_pack(pack)


def validate_capability_pack(pack: object) -> dict[str, Any]:
    """Validate the complete wrapper and detect any snapshot mutation."""

    if not isinstance(pack, dict):
        raise CapabilityPackError("capability pack must be an object")
    _assert_no_forbidden_keys(pack, location="capability_pack")
    missing = sorted(PACK_FIELDS - set(pack))
    unknown = sorted(set(pack) - PACK_FIELDS)
    if missing or unknown:
        raise CapabilityPackError(f"invalid capability pack fields; missing={missing}, unknown={unknown}")
    if pack.get("schema_version") != CAPABILITY_PACK_SCHEMA_VERSION:
        raise CapabilityPackError("unsupported capability pack schema_version")
    if not isinstance(pack.get("id"), str) or not _ID_RE.fullmatch(pack["id"]):
        raise CapabilityPackError("invalid capability pack id")
    if not isinstance(pack.get("version"), str) or not _VERSION_RE.fullmatch(pack["version"]):
        raise CapabilityPackError("invalid capability pack version")
    if not isinstance(pack.get("source"), str) or not _SOURCE_RE.fullmatch(pack["source"]):
        raise CapabilityPackError("invalid capability pack source")
    if not isinstance(pack.get("sha256"), str) or not _SHA256_RE.fullmatch(pack["sha256"]):
        raise CapabilityPackError("invalid capability pack sha256")
    if not isinstance(pack.get("generated_at"), str):
        raise CapabilityPackError("generated_at must be an ISO timestamp")
    try:
        generated = datetime.fromisoformat(pack["generated_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapabilityPackError("generated_at must be an ISO timestamp") from exc
    if generated.tzinfo is None:
        raise CapabilityPackError("generated_at must include a timezone")

    snapshot = pack.get("snapshot")
    if not isinstance(snapshot, dict) or set(snapshot) != SNAPSHOT_FIELDS:
        raise CapabilityPackError("snapshot must contain exactly the declared fields")
    for field, maximum in (("label", 80), ("industry", 80), ("audience", 80), ("content_purpose", 160)):
        _bounded_text(snapshot[field], field=field, maximum=maximum)
    if validate_goal(snapshot["goal"]) != snapshot["goal"]:
        raise CapabilityPackError("snapshot goal must be normalized")
    if snapshot["risk_level"] not in {"low", "medium", "high"}:
        raise CapabilityPackError("invalid risk_level")
    for field in LIST_FIELDS:
        if not isinstance(snapshot[field], list):
            raise CapabilityPackError(f"{field} must be a list")
        maximum = 12 if field in {"platforms", "tone"} else 24
        clean = _normalize_list(snapshot[field], field=field, maximum_items=maximum)
        if clean != snapshot[field]:
            raise CapabilityPackError(f"{field} must be normalized and unique")
    if not snapshot["platforms"] or not snapshot["tone"]:
        raise CapabilityPackError("platforms and tone may not be empty")
    if not all(item in snapshot["evidence_requirements"] for item in _BASE_EVIDENCE_REQUIREMENTS):
        raise CapabilityPackError("capability pack may not weaken universal evidence requirements")
    if not all(item in snapshot["prohibited_claims"] for item in _BASE_PROHIBITED_CLAIMS):
        raise CapabilityPackError("capability pack may not weaken universal claim restrictions")
    expected_sha = _canonical_sha256(_identity_payload(pack))
    if pack["sha256"] != expected_sha:
        raise CapabilityPackError("capability pack identity hash mismatch")
    normalized_audit = _normalize_audit(pack.get("audit"))
    if normalized_audit != pack["audit"]:
        raise CapabilityPackError("audit must use the normalized whitelist schema")
    _assert_reserved_identity(pack)
    if len(_canonical_json(pack)) > 32_768:
        raise CapabilityPackError("capability pack is too large")
    return copy.deepcopy(pack)


def _memory_rule_texts(memory_rules: Any) -> list[str]:
    if memory_rules is None:
        return []
    if not isinstance(memory_rules, list) or len(memory_rules) > 24:
        raise CapabilityPackError("memory_rules must be a list with at most 24 items")
    values: list[str] = []
    for index, rule in enumerate(memory_rules):
        if isinstance(rule, str):
            value = rule
        elif isinstance(rule, dict):
            _assert_no_forbidden_keys(rule, location=f"memory_rules[{index}]")
            unknown = sorted(set(rule) - {
                "id", "rule_id", "message", "instruction", "scope", "pack_id", "job_id", "status", "source_event_ids"
            })
            if unknown:
                raise CapabilityPackError(f"memory rule contains unknown fields: {', '.join(unknown)}")
            value = rule.get("instruction", rule.get("message"))
        else:
            raise CapabilityPackError("memory rule must be text or an object")
        values.append(_bounded_text(value, field="memory_rule", minimum=4, maximum=300))
    return values


def local_capability_pack(goal: object, memory_rules: list[Any] | None = None) -> dict[str, Any]:
    """Create a safe offline pack that remains specific to the supplied goal."""

    normalized_goal = validate_goal(goal)
    learned_constraints = [f"已确认纠错规则：{text}" for text in _memory_rule_texts(memory_rules)]
    raw = {
        "industry": _infer_industry(normalized_goal),
        "goal": normalized_goal,
        "audience": _infer_audience(normalized_goal),
        "platforms": _infer_platforms(normalized_goal),
        "content_purpose": _infer_purpose(normalized_goal),
        "risk_level": _infer_risk(normalized_goal),
        "prohibited_claims": learned_constraints,
    }
    return normalize_capability_pack(
        raw,
        normalized_goal,
        "local",
        audit={
            "status": "local_safe_fallback",
            "generated_by": "deterministic_local_generator",
            "constraints_added": learned_constraints,
        },
    )


def legacy_clean_air_pack() -> dict[str, Any]:
    """Return the former clean-air specialization as an explicit legacy pack."""

    goal = "生成审慎、可溯源的室内空气与甲醛科普短视频"
    raw = {
        "id": LEGACY_CLEAN_AIR_PACK_ID,
        "version": LEGACY_CLEAN_AIR_PACK_VERSION,
        "snapshot": {
            "label": "净界室内空气内容能力包",
            "industry": "家居与室内空气服务",
            "goal": goal,
            "audience": "关注新居、装修污染与室内空气的家庭",
            "platforms": ["抖音", "视频号"],
            "content_purpose": "室内空气科普与负责任的服务沟通",
            "tone": ["审慎", "专业", "易懂"],
            "preferred_terms": ["检测条件", "适用边界", "可追溯证据"],
            "avoided_terms": ["零甲醛", "百分百安全", "一次根治"],
            "evidence_requirements": ["检测数据须回指完整报告并说明检测条件"],
            "prohibited_claims": ["无已批准证据时不得宣称具体除醛率或健康结果"],
            "visual_direction": ["清洁留白", "证据卡片优先", "竖屏大字可读"],
            "assumptions": ["未提供真实企业资料时不补写产品功效"],
            "risk_level": "high",
        },
    }
    return normalize_capability_pack(
        raw,
        goal,
        LEGACY_CLEAN_AIR_SOURCE,
        audit={"status": "legacy_compatibility", "warnings": ["仅用于历史项目兼容"]},
    )


def _short_title(value: str, maximum: int = 56) -> str:
    text = _clean_space(value).strip("，。；;:：!?！？")
    return text if len(text) <= maximum else f"{text[: maximum - 1]}…"


def local_topic_candidates(
    goal: object,
    pack: dict[str, Any],
    excluded: list[str] | None,
) -> list[dict[str, str]]:
    """Return three deterministic, industry-neutral offline topic candidates."""

    normalized_goal = validate_goal(goal)
    validated = validate_capability_pack(pack)
    if validated["snapshot"]["goal"] != normalized_goal:
        raise CapabilityPackError("goal does not match the capability pack snapshot")
    if excluded is None:
        excluded = []
    if not isinstance(excluded, list) or len(excluded) > 24:
        raise CapabilityPackError("excluded must be a list with at most 24 items")
    excluded_values = {
        _bounded_text(value, field="excluded", maximum=200).casefold() for value in excluded
    }

    snapshot = validated["snapshot"]
    focus = _short_title(normalized_goal, 42)
    audience = snapshot["audience"]
    label = _short_title(snapshot["label"], 28)
    templates = (
        (f"先讲清楚：{focus}", "从用户最先需要理解的问题切入，先建立清晰判断。"),
        (f"{audience}最容易忽略的三个判断点", "用有限要点降低理解成本，同时保留证据和适用边界。"),
        (f"从需求到结果：{label}的可靠选择路径", "以真实决策过程组织内容，避免只堆叠功能和口号。"),
        (f"别先看口号，先核对{focus}的证据", "把可验证信息放在宣传表述之前，适合建立信任。"),
        (f"一个真实场景，看懂{focus}", "用具体但不虚构的场景讲解使用条件、限制和下一步。"),
        (f"在60秒内说清{focus}的边界", "主动说明什么能做、什么不能承诺，增强可信度。"),
        (f"{audience}可以立即使用的检查清单", "将抽象信息转成可执行的核对步骤，不代替专业判断。"),
        (f"常见误解反转：重新理解{focus}", "以“误解—证据—边界”结构完成一次简洁纠偏。"),
    )

    candidates: list[dict[str, str]] = []
    for title, reason in templates:
        title = _short_title(title)
        candidate_id = f"topic-{hashlib.sha256((validated['id'] + '|' + title).encode('utf-8')).hexdigest()[:12]}"
        if title.casefold() in excluded_values or candidate_id.casefold() in excluded_values:
            continue
        candidates.append({"id": candidate_id, "title": title, "reason": reason, "audience": audience})
        if len(candidates) == 3:
            return candidates

    nonce = 1
    while len(candidates) < 3:
        title = _short_title(f"{focus}：新视角 {nonce}")
        candidate_id = f"topic-{hashlib.sha256((validated['id'] + '|' + title).encode('utf-8')).hexdigest()[:12]}"
        nonce += 1
        if title.casefold() in excluded_values or candidate_id.casefold() in excluded_values:
            continue
        candidates.append(
            {
                "id": candidate_id,
                "title": title,
                "reason": "使用新的叙事切口，但继续遵守当前能力包的证据与表达约束。",
                "audience": audience,
            }
        )
    return candidates
