from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import secrets
import threading
import time
import webbrowser
from collections import OrderedDict
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from core.catalog import HardwareProbe, PackageCatalog
from core.capability_pack import (
    local_capability_pack,
    local_topic_candidates,
    normalize_capability_pack,
    validate_capability_pack,
    validate_goal,
)
from core.capability_registry import CapabilityPackConflictError, CapabilityPackRegistry
from core.config import ConfigStore
from core.discovery import ProjectDiscovery
from core.learning import LearningError, LearningStore
from core.orchestrator import (
    ConflictError,
    IDEMPOTENCY_RE,
    JobStore,
    UnprocessableError,
    WorkflowError,
    local_fallback_plan,
    topic_in_scope,
    validate_topic_input,
)
from core.production import DEFAULT_INPUT, ProductionRunner, estimate_narration_duration, review_script
from core.production_engine import (
    ENGINE_COMMIT,
    ENGINE_MODE,
    ENGINE_NAME,
    ENGINE_VERSION,
    ProductionEngineAdapter,
    ProductionEngineError,
)
from core.provider import (
    BudgetLedger,
    CAPABILITY_SNAPSHOT_FIELDS,
    CAPABILITY_SNAPSHOT_LIST_FIELDS,
    OpenAICompatibleProvider,
    ProviderError,
    normalize_capability_review,
    sanitize_bootstrap_schema_diagnostic,
)


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
RUNTIME_DIR = Path(os.environ.get("SHIYI_RUNTIME_DIR", APP_DIR / "runtime")).expanduser().resolve()
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
DISCOVERY_CACHE = RUNTIME_DIR / "discovery.json"
CATALOG_FILE = APP_DIR / "catalog" / "package-catalog.json"

config_store = ConfigStore(RUNTIME_DIR)
config_store.ensure_storage_layout()
job_store = JobStore(RUNTIME_DIR)
learning_store = LearningStore(RUNTIME_DIR)
capability_registry = CapabilityPackRegistry(RUNTIME_DIR)
package_catalog = PackageCatalog(CATALOG_FILE)
state_lock = threading.Lock()
SESSION_COOKIE = "shiyi_session"
SESSION_ID = secrets.token_urlsafe(32)
CSRF_TOKEN = secrets.token_urlsafe(32)
provider_session_state = {"verified_signature": None, "verified_at": None, "revision": 0}
PRETASK_PROVIDER_LIMIT = 3
pretask_provider_budgets: OrderedDict[str, BudgetLedger] = OrderedDict()
agent_create_replays: dict[str, dict[str, str]] = {}
correction_replays: OrderedDict[str, dict] = OrderedDict()
topic_selection_bundles: OrderedDict[str, dict] = OrderedDict()
SELECTION_BUNDLE_TTL_SECONDS = 2 * 60 * 60


def production_engine_binding(*, strict: bool) -> tuple[ProductionEngineAdapter | None, dict, dict]:
    enabled = os.environ.get("SHIYI_MPT_ENABLED", "").strip() == "1"
    summary = {
        "name": ENGINE_NAME,
        "version": ENGINE_VERSION,
        "commit": ENGINE_COMMIT,
        "mode": ENGINE_MODE,
        "enabled": enabled,
        "health": "disabled",
        "material_strategy": None,
        "material_count": 0,
    }
    if not enabled:
        return None, {}, summary
    try:
        timeout_seconds = float(os.environ.get("SHIYI_MPT_TIMEOUT_SECONDS", "1800"))
        if not 60 <= timeout_seconds <= 3600:
            raise ValueError("timeout outside release bounds")
        base_url = os.environ.get("SHIYI_MPT_BASE_URL", "http://127.0.0.1:8080/api/v1")
        material_root_value = os.environ.get("SHIYI_MPT_LOCAL_MATERIAL_DIR", "").strip()
        material_strategy = os.environ.get(
            "SHIYI_MPT_MATERIAL_STRATEGY", "local" if material_root_value else "pexels"
        ).strip().lower()
        if material_strategy not in {"local", "pexels", "pixabay", "coverr"}:
            raise ProductionEngineError(
                "invalid_material_strategy",
                "Material strategy is not allowed.",
                stage="configuration",
            )
        material_root = Path(material_root_value).expanduser() if material_root_value else None
        local_material_paths: list[Path] | None = None
        if material_strategy == "local":
            if material_root is None or not material_root.is_dir():
                raise ProductionEngineError(
                    "local_material_root_required",
                    "Local material root is unavailable.",
                    stage="configuration",
                )
            local_material_paths = sorted(
                (path for path in material_root.glob("*.mp4") if path.is_file()),
                key=lambda path: path.name.casefold(),
            )
            if not 1 <= len(local_material_paths) <= 24:
                raise ProductionEngineError(
                    "invalid_local_materials",
                    "Local material count is outside release bounds.",
                    stage="configuration",
                )
        adapter = ProductionEngineAdapter(
            base_url,
            timeout_seconds=timeout_seconds,
            local_material_root=material_root,
        )
        options = {
            "material_strategy": material_strategy,
            "voice_strategy": "edge_tts",
            "local_material_paths": local_material_paths,
        }
        summary.update(
            {
                "health": (
                    "ready"
                    if os.environ.get("SHIYI_MPT_HEALTH_VERIFIED", "").strip() == "1"
                    else "configured_unverified"
                ),
                "material_strategy": material_strategy,
                "material_count": len(local_material_paths or []),
            }
        )
        return adapter, options, summary
    except (OSError, ValueError, ProductionEngineError) as exc:
        code = getattr(exc, "code", "invalid_engine_configuration")
        summary.update({"health": "misconfigured", "error_code": code})
        if strict:
            error = UnprocessableError("MPT生产引擎配置未通过安全检查", details={"code": code})
            raise error from exc
        return None, {}, summary


def job_with_engine_summary(job: dict) -> dict:
    result = dict(job)
    _adapter, _options, summary = production_engine_binding(strict=False)
    summary = dict(summary)
    if "engine_report.json" in result.get("artifacts", []):
        summary["last_successful_run"] = result.get("current_run_id")
    result["production_engine"] = summary
    return result


def pretask_budget_for(goal: str) -> BudgetLedger:
    """Return a hard pre-task ledger scoped to one normalized project goal."""

    key = hashlib.sha256(goal.strip().encode("utf-8")).hexdigest()
    with state_lock:
        ledger = pretask_provider_budgets.get(key)
        if ledger is None:
            ledger = BudgetLedger(limit=PRETASK_PROVIDER_LIMIT)
            pretask_provider_budgets[key] = ledger
            while len(pretask_provider_budgets) > 128:
                pretask_provider_budgets.popitem(last=False)
        else:
            pretask_provider_budgets.move_to_end(key)
        return ledger


def capability_review_screening(review: dict | None, *, failure_kind: str) -> str:
    """Summarize only local enums and counts; never surface reviewer prose."""

    if isinstance(review, dict):
        counts = {"usable_limited": 0, "needs_evidence": 0, "rejected": 0}
        for item in review.get("candidate_verdicts", []):
            if isinstance(item, dict) and item.get("verdict") in counts:
                counts[item["verdict"]] += 1
        count_text = (
            f"可有限使用 {counts['usable_limited']}、"
            f"需要证据 {counts['needs_evidence']}、已拒绝 {counts['rejected']}"
        )
        if review.get("status") == "passed":
            return f"反证审核通过；候选 {count_text}；仅允许进入研究，不代表事实已证实"
        if review.get("status") == "needs_revision":
            return f"反证审核需要修改；候选 {count_text}；已安全降级，修改后才能重新审核"
        return f"反证审核已阻止；候选 {count_text}；已安全降级，当前内容不得进入研究"
    if failure_kind == "invalid_schema":
        return "反证审核结构或候选身份无效；已安全降级，当前候选不沿用原裁决"
    if failure_kind == "provider_unavailable":
        return "反证审核 Provider 或传输不可用；已安全降级，未把通信失败误作审核结论"
    return "反证审核未执行；已使用本地安全能力包，公开依据仍须在研究阶段逐条核验"


def capability_review_failure_from_budget(budget: BudgetLedger) -> str:
    for event in reversed(budget.snapshot().get("events", [])):
        if event.get("stage") != "capability_pack_adversarial_review":
            continue
        if event.get("error_type") == "invalid_capability_review_schema":
            return "invalid_schema"
        return "provider_unavailable"
    return "provider_unavailable"


def bootstrap_failure_from_budget(budget: BudgetLedger) -> str:
    for event in reversed(budget.snapshot().get("events", [])):
        if event.get("stage") not in {"project_bootstrap", "capability_pack_bootstrap"}:
            continue
        if event.get("error_type") == "invalid_capability_pack_schema":
            return "invalid_capability_pack_schema"
        if event.get("error_type") == "invalid_topic_schema":
            return "invalid_topic_schema"
        return "provider_unavailable"
    return "provider_unavailable"


def bootstrap_schema_diagnostic_summary(diagnostic: dict | None) -> str:
    """Describe only fixed schema categories and known field names."""

    if not isinstance(diagnostic, dict):
        return "项目启动能力包未通过安全结构校验"
    parts: list[str] = []
    missing = diagnostic.get("missing_fields", [])
    if missing:
        parts.append(f"缺少字段：{'、'.join(missing)}")
    if diagnostic.get("unknown_fields"):
        parts.append("含未知字段")
    wrong_types: list[str] = []
    for field, actual in diagnostic.get("field_types", {}).items():
        if field == "capability_pack":
            allowed = {"object"}
        elif field in CAPABILITY_SNAPSHOT_LIST_FIELDS:
            allowed = {"string", "array"}
            if field == "visual_direction":
                allowed.add("object")
        else:
            allowed = {"string", "array"}
        if actual not in allowed:
            wrong_types.append(field)
    if wrong_types:
        parts.append(f"字段类型不符：{'、'.join(wrong_types)}")
    mixed_lists = [
        field
        for field, types in diagnostic.get("list_element_types", {}).items()
        if any(item != "string" for item in types)
    ]
    if mixed_lists:
        parts.append(f"列表元素类型不符：{'、'.join(mixed_lists)}")
    return "；".join(parts) if parts else "项目启动能力包未通过安全结构校验"


def bootstrap_failure_screening(failure_kind: str, diagnostic: dict | None) -> str:
    if failure_kind == "passed":
        return "项目启动结构校验通过"
    if failure_kind == "invalid_capability_pack_schema":
        return bootstrap_schema_diagnostic_summary(diagnostic)
    if failure_kind == "invalid_topic_schema":
        return "项目启动候选结构无效"
    if failure_kind == "provider_unavailable":
        return "项目启动 Provider 或传输不可用"
    return "项目启动结构诊断未执行"


def store_topic_selection_bundle(goal: str, pack: dict, candidates: list[dict]) -> str:
    bundle_id = f"selection-{secrets.token_urlsafe(24)}"
    record = {
        "goal": goal,
        "pack_id": pack["id"],
        "pack_sha256": pack["sha256"],
        "candidates": {str(item["id"]): json.loads(json.dumps(item, ensure_ascii=False)) for item in candidates},
        "created_monotonic": time.monotonic(),
    }
    with state_lock:
        topic_selection_bundles[bundle_id] = record
        while len(topic_selection_bundles) > 128:
            topic_selection_bundles.popitem(last=False)
    return bundle_id


def resolve_topic_selection_bundle(bundle_id: object, candidate_id: object) -> tuple[dict, dict]:
    if not isinstance(bundle_id, str) or not bundle_id.startswith("selection-") or len(bundle_id) > 96:
        raise UnprocessableError("selection_bundle_id格式无效")
    if not isinstance(candidate_id, str) or not 1 <= len(candidate_id) <= 80:
        raise UnprocessableError("candidate_id格式无效")
    with state_lock:
        record = topic_selection_bundles.get(bundle_id)
        if record is None:
            raise UnprocessableError("选题凭证不存在或已失效，请重新获取三个候选")
        if time.monotonic() - float(record["created_monotonic"]) > SELECTION_BUNDLE_TTL_SECONDS:
            topic_selection_bundles.pop(bundle_id, None)
            raise UnprocessableError("选题凭证已过期，请重新获取三个候选")
        candidate = record["candidates"].get(candidate_id)
        if candidate is None:
            raise UnprocessableError("候选选题不属于当前服务端选题凭证")
        record = dict(record)
        candidate = dict(candidate)
    try:
        pack = capability_registry.get(record["pack_id"], record["pack_sha256"])
    except (TypeError, ValueError) as exc:
        raise UnprocessableError("选题关联的行业能力包已失效，请重新生成") from exc
    return candidate, pack


def infer_correction_kind(message: str, requested: object = None) -> str:
    allowed = {"style", "content", "evidence", "capability", "process"}
    if requested is not None:
        if requested not in allowed:
            raise UnprocessableError("kind必须是style、content、evidence、capability或process")
        return str(requested)
    text = str(message or "")
    if any(marker in text for marker in ("行业能力包", "能力包判断", "行业判断", "受众判断", "平台判断")):
        return "capability"
    if any(marker in text for marker in ("来源是假的", "来源不可信", "证据是假的", "证据不可信", "报告是假的", "引用错误", "数据不可信", "数字不对", "这条事实不对")):
        return "evidence"
    if any(marker in text for marker in ("语气", "风格", "措辞", "画面", "字体", "配色", "节奏")):
        return "style"
    if any(marker in text for marker in ("流程", "步骤", "先后顺序", "审批方式", "工作方式")):
        return "process"
    return "content"


def combine_correction_kinds(kinds: list[str]) -> str:
    priority = {"style": 1, "process": 2, "content": 3, "capability": 4, "evidence": 5}
    return max((kind for kind in kinds if kind in priority), key=lambda item: priority[item], default="content")

LEGACY_CLEAN_AIR_TOPIC_POOL = [
    {
        "title": "通风后没有气味，室内空气就安全了吗？",
        "reason": "从常见误区切入，适合新房家庭。",
        "audience": "新房家庭",
    },
    {
        "title": "99%除醛率，到底应该看哪些检测条件？",
        "reason": "数字有冲突感，也能自然引出证据核验。",
        "audience": "关注除醛产品的家庭",
    },
    {
        "title": "入住前看检测报告，最容易漏掉哪三项？",
        "reason": "实用清单型，便于收藏和转发。",
        "audience": "准备入住的新房家庭",
    },
    {
        "title": "测醛前为什么要先确认封闭时间和检测方法？",
        "reason": "从检测流程入手，避免只看一个数字。",
        "audience": "准备做室内检测的家庭",
    },
    {
        "title": "新房通风多久才够，为什么不能只凭气味判断？",
        "reason": "把高频疑问拆成可以公开核验的问题。",
        "audience": "装修后的新房家庭",
    },
    {
        "title": "同一份除醛数据，为什么不能直接套到真实房间？",
        "reason": "解释实验条件与真实使用场景的差异。",
        "audience": "正在比较除醛方案的家庭",
    },
    {
        "title": "室内空气检测报告里，哪些信息决定结论能不能用？",
        "reason": "用报告阅读框架代替简单下结论。",
        "audience": "看不懂检测报告的家庭",
    },
    {
        "title": "治理完成就入住的说法，还缺哪些公开证据？",
        "reason": "以反向举证检查高风险承诺。",
        "audience": "急于入住的新房家庭",
    },
    {
        "title": "装修污染只测甲醛够不够，检测范围应该怎么看？",
        "reason": "从检测范围切入，避免把单项结果当成全部。",
        "audience": "关注室内空气的新房家庭",
    },
    {
        "title": "便携测醛仪的数字，为什么不能直接当检测结论？",
        "reason": "区分日常观察工具与正式检测结论。",
        "audience": "正在自行测醛的家庭",
    },
    {
        "title": "开窗测和关窗测差很多，应该先核对什么？",
        "reason": "用场景差异解释检测条件的重要性。",
        "audience": "准备复测室内空气的家庭",
    },
    {
        "title": "除醛产品写着高去除率，报告里还要找哪些前提？",
        "reason": "帮助用户把宣传数字放回实验条件中理解。",
        "audience": "正在选购除醛产品的家庭",
    },
    {
        "title": "家具进场前后，为什么室内空气结果可能不同？",
        "reason": "从污染源变化切入，避免一次检测代表全部阶段。",
        "audience": "正在装修或添置家具的家庭",
    },
    {
        "title": "检测报告有合格结论，就能忽略采样过程吗？",
        "reason": "引导用户同时核对结论和采样条件。",
        "audience": "收到检测报告的新房家庭",
    },
    {
        "title": "闻不到装修味之后，为什么还要看检测依据？",
        "reason": "把嗅觉判断与可核验检测分开。",
        "audience": "准备入住的新房家庭",
    },
    {
        "title": "治理前后对比数据，怎样判断比较条件是否一致？",
        "reason": "聚焦前后对比最容易忽略的变量控制。",
        "audience": "正在验收治理效果的家庭",
    },
    {
        "title": "一张实验室报告，能不能证明整套房的实际效果？",
        "reason": "解释样品测试与真实空间之间的证据边界。",
        "audience": "正在比较治理服务的家庭",
    },
    {
        "title": "甲醛检测数值接近限值时，应该怎样读结果？",
        "reason": "从临界数值切入，提醒关注方法与不确定性。",
        "audience": "拿到临界检测结果的家庭",
    },
    {
        "title": "夏天和冬天测出的室内空气数据为什么会变化？",
        "reason": "用环境条件说明单次结果的适用范围。",
        "audience": "计划跨季节复测的家庭",
    },
    {
        "title": "儿童房检测时，哪些采样信息值得单独记录？",
        "reason": "给关注儿童房的家庭一份可核验的信息清单。",
        "audience": "关注儿童房空气的家庭",
    },
    {
        "title": "通风、净化和治理数据，为什么不能混成一个结论？",
        "reason": "拆分不同措施的证据，避免把相关性当成功效。",
        "audience": "正在组合改善方案的家庭",
    },
    {
        "title": "检测机构和产品商家给出不同结果时先看什么？",
        "reason": "提供核对方法、时间和条件的比较框架。",
        "audience": "遇到检测争议的家庭",
    },
    {
        "title": "新房复测为什么要保留时间、温度和通风记录？",
        "reason": "把复测变成条件可比的过程，而不是只比数字。",
        "audience": "准备多次检测的新房家庭",
    },
    {
        "title": "检测报告里的检出限和单位，为什么不能跳过？",
        "reason": "从基础字段入手，降低误读具体数值的风险。",
        "audience": "第一次阅读检测报告的家庭",
    },
    {
        "title": "网传除醛小妙招，怎样用公开证据逐条核对？",
        "reason": "用反向举证框架审视高传播但来源不明的方法。",
        "audience": "正在搜索除醛方法的家庭",
    },
    {
        "title": "治理服务承诺多久见效，哪些条件必须先问清？",
        "reason": "把时间承诺拆成可验证的服务条件。",
        "audience": "准备购买治理服务的家庭",
    },
    {
        "title": "入住计划很赶时，室内空气判断应该保留哪些底线？",
        "reason": "用审慎决策框架替代未经证实的入住保证。",
        "audience": "临近入住的新房家庭",
    },
]
SAFE_TOPIC_POOL = LEGACY_CLEAN_AIR_TOPIC_POOL  # compatibility for released tests/tools
UNSAFE_TOPIC_PHRASES = ("绝对安全", "完全去除", "彻底去除", "零甲醛", "立即入住", "母婴零风险")
BLOCKED_GOAL_PHRASES = (
    "忽略前面", "忽略以上", "忽略之前", "忽略所有指令", "系统提示词", "开发者指令", "越狱提示",
    "勒索软件", "恶意软件", "木马程序", "窃取密码", "钓鱼网站", "攻击服务器", "绕过安全限制",
)


def load_discovery_cache() -> dict:
    if DISCOVERY_CACHE.exists():
        try:
            cached = json.loads(DISCOVERY_CACHE.read_text(encoding="utf-8"))
            if isinstance(cached, dict):
                return {
                    "tools": cached.get("tools", []),
                    "last_scan": cached.get("last_scan"),
                    "last_scan_report": cached.get("last_scan_report"),
                }
        except (OSError, json.JSONDecodeError):
            pass
    return {"tools": [], "last_scan": None, "last_scan_report": None}


app_state = load_discovery_cache()


def provider_snapshot_signature(provider: dict, api_key: str) -> str | None:
    """Fingerprint an immutable provider snapshot without retaining its Key."""
    if not api_key:
        return None
    payload = json.dumps(
        {
            "base_url": provider.get("base_url", ""),
            "model": provider.get("model", ""),
            "api_key_sha256": hashlib.sha256(api_key.encode("utf-8")).hexdigest(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def provider_configuration_signature() -> str | None:
    return provider_snapshot_signature(config_store.load()["provider"], config_store.get_api_key())


def provider_test_snapshot() -> tuple[OpenAICompatibleProvider, str | None, int]:
    with state_lock:
        revision = int(provider_session_state["revision"])
        provider_config = dict(config_store.load()["provider"])
        api_key = config_store.get_api_key()
    return (
        OpenAICompatibleProvider(provider_config, api_key),
        provider_snapshot_signature(provider_config, api_key),
        revision,
    )


def mark_provider_connection_verified(signature: str | None, revision: int) -> str | None:
    if not signature:
        raise ProviderError("尚未填写 API Key，也未设置对应环境变量")
    verified_at = datetime.now().astimezone().isoformat(timespec="seconds")
    with state_lock:
        if (
            int(provider_session_state["revision"]) != int(revision)
            or provider_configuration_signature() != signature
        ):
            return None
        provider_session_state["verified_signature"] = signature
        provider_session_state["verified_at"] = verified_at
    return verified_at


def clear_provider_connection_verified() -> None:
    with state_lock:
        provider_session_state["verified_signature"] = None
        provider_session_state["verified_at"] = None


def invalidate_provider_configuration() -> None:
    """Clear verification and advance the generation before any Provider save."""
    with state_lock:
        provider_session_state["revision"] = int(provider_session_state["revision"]) + 1
        provider_session_state["verified_signature"] = None
        provider_session_state["verified_at"] = None


class AppHandler(BaseHTTPRequestHandler):
    server_version = "ShiyiAgentContentFactory/0.3"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/session":
                self.json_response({"csrf_token": CSRF_TOKEN, "same_origin_required": True}, set_session=True)
            elif path == "/api/status":
                self.json_response(self._status())
            elif path == "/api/config":
                self.json_response(config_store.public_config())
            elif path == "/api/tools":
                with state_lock:
                    self.json_response({"tools": app_state["tools"], "last_scan": app_state["last_scan"], "report": app_state["last_scan_report"]})
            elif path == "/api/jobs":
                self.json_response({"jobs": job_store.list()})
            elif path == "/api/learning":
                self.json_response({
                    "memories": learning_store.list_memories(),
                    "rules": learning_store.list_rules(),
                    "skills": learning_store.list_skills(),
                })
            elif path == "/api/capability-packs":
                self.json_response({"capability_packs": capability_registry.list()})
            elif path.startswith("/api/jobs/") and "/review-artifacts/" in path:
                parts = path.strip("/").split("/")
                if len(parts) != 5:
                    raise FileNotFoundError("接口不存在")
                self.serve_file(job_store.resolve_review_artifact(parts[2], parts[4]), parts[4])
            elif path.startswith("/api/jobs/") and "/runs/" in path and "/artifacts/" in path:
                parts = path.strip("/").split("/")
                if len(parts) != 7:
                    raise FileNotFoundError("接口不存在")
                self.serve_file(job_store.resolve_artifact(parts[2], parts[6], run_id=parts[4]), parts[6])
            elif path.startswith("/api/jobs/") and "/artifacts/" in path:
                parts = path.strip("/").split("/")
                if len(parts) != 5:
                    raise FileNotFoundError("接口不存在")
                self.serve_file(job_store.resolve_artifact(parts[2], parts[4]), parts[4])
            elif path.startswith("/api/jobs/"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    raise FileNotFoundError("接口不存在")
                self.json_response(job_with_engine_summary(job_store.get(parts[2])))
            elif path == "/api/catalog":
                self.json_response(package_catalog.load())
            elif path == "/api/hardware":
                catalog = package_catalog.load()
                hardware = HardwareProbe.probe()
                profile = package_catalog.select_profile(catalog, hardware["gpu"]["vram_gb"])
                recommendations = package_catalog.recommendations(catalog, profile["id"])
                self.json_response({
                    "hardware": hardware,
                    "profile": profile,
                    "recommended_package_ids": [item["id"] for item in recommendations],
                    "auto_install_enabled": catalog["policy"]["auto_install_enabled"],
                    "storage": config_store.public_config()["storage"],
                })
            elif path == "/api/storage":
                self.json_response({"storage": config_store.public_config()["storage"]})
            elif path.startswith("/api/"):
                self.error_response("not_found", "接口不存在", HTTPStatus.NOT_FOUND)
            else:
                self.serve_static(path)
        except Exception as exc:
            self.handle_exception(exc)

    def do_POST(self) -> None:
        try:
            self.require_mutation_security()
            path = urlparse(self.path).path
            body = self.read_json()
            if path == "/api/config":
                if "provider" in body:
                    invalidate_provider_configuration()
                self.json_response(config_store.save(body))
            elif path == "/api/discover":
                self.json_response(self._discover(body))
            elif path == "/api/provider/test":
                clear_provider_connection_verified()
                provider, tested_signature, tested_revision = provider_test_snapshot()
                result = provider.test_connection()
                if result.get("ok") is not True:
                    raise ProviderError("Provider连通性测试没有返回成功状态")
                verified_at = mark_provider_connection_verified(tested_signature, tested_revision)
                if verified_at is None:
                    error = WorkflowError("Provider配置在连接测试期间发生变化，请重新测试")
                    error.status, error.code = 409, "provider_config_changed"
                    raise error
                result["connection_verified"] = True
                result["verified_at"] = verified_at
                self.json_response(result)
            elif path == "/api/agent/topics":
                self.json_response(self._suggest_topics(body))
            elif path == "/api/agent/plan":
                self.json_response(self._plan(body))
            elif path == "/api/agent/corrections":
                self.json_response(self._record_correction(body), HTTPStatus.CREATED)
            elif path == "/api/jobs":
                plan = body.get("plan")
                if not isinstance(plan, dict):
                    raise UnprocessableError("缺少有效计划")
                production_input, rules = self._prepare_production_input(body, include_defaults=False)
                job = job_store.create(plan, production_input=production_input)
                if rules:
                    job = job_store.apply_learning_rules(job["id"], rules, "创建任务时应用服务端已验证记忆")
                self.json_response(job, HTTPStatus.CREATED)
            elif path == "/api/demo-job":
                job, replayed = self._create_demo_job(body)
                self.json_response(job, HTTPStatus.OK if replayed else HTTPStatus.CREATED)
            elif path.startswith("/api/jobs/") and path.endswith("/approve"):
                self.json_response(job_store.approve(path.split("/")[3]))
            elif path.startswith("/api/jobs/") and path.endswith("/approvals/research"):
                self.json_response(job_store.approve_research(path.split("/")[3], body))
            elif path.startswith("/api/jobs/") and path.endswith("/approvals/compliance"):
                self.json_response(job_store.approve_compliance(path.split("/")[3], body))
            elif path.startswith("/api/jobs/") and path.endswith("/run"):
                job_id = path.split("/")[3]
                job = job_store.get(job_id)
                job = self._sync_learning_rules(job)
                if not isinstance(job.get("production_input"), dict):
                    allow = bool(config_store.load()["security"].get("allow_external_commands", False))
                    self.json_response(job_store.run_safe(job_id, allow_external_commands=allow))
                else:
                    limit = int(config_store.load().get("research", {}).get("max_provider_calls_per_job", 7))
                    budget = BudgetLedger(limit=limit, snapshot=job.get("budget"))
                    provider = self._provider(budget)
                    render_stage_requested = job.get("status") == "compliance_approved" or (
                        job.get("status") == "failed" and job.get("last_failed_stage") == "render"
                    )
                    engine_adapter, engine_options, _engine_summary = production_engine_binding(
                        strict=render_stage_requested
                    )
                    if not render_stage_requested:
                        engine_adapter, engine_options = None, {}
                    runner = ProductionRunner(
                        provider=provider,
                        research_config=config_store.load().get("research", {}),
                        budget=budget,
                        production_engine_adapter=engine_adapter,
                        production_engine_options=engine_options,
                    )
                    executed_rule_ids = list(job.get("learning_rule_ids", []))
                    result = job_store.advance(job_id, runner, self.headers.get("Idempotency-Key", ""))
                    learning_update = None
                    if result.get("status") == "complete" and executed_rule_ids:
                        try:
                            marked = learning_store.mark_job_success(executed_rule_ids, job_id)
                            learning_update = {
                                "status": "recorded",
                                "generated_skill_ids": [item["id"] for item in marked.get("generated_skills", [])],
                            }
                        except LearningError as exc:
                            # The media run is already atomically published.  A
                            # damaged learning index must not turn that success
                            # into a false 500 response.
                            learning_update = {"status": "failed", "code": exc.code}
                    if result.get("status") == "complete":
                        result = self._sync_learning_rules(result)
                    if learning_update is not None:
                        result["learning_update"] = learning_update
                    self.json_response(result)
            else:
                self.error_response("not_found", "接口不存在", HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.handle_exception(exc)

    def do_PATCH(self) -> None:
        try:
            self.require_mutation_security()
            path = urlparse(self.path).path
            body = self.read_json()
            if path.startswith("/api/jobs/") and path.endswith("/script"):
                value = str(body.get("script", "")).strip()
                estimate = estimate_narration_duration(value)
                if not 35 <= float(estimate["estimated_seconds"]) <= 75:
                    raise UnprocessableError("脚本预计口播时长必须在35到75秒之间", details=estimate)
                job_id = path.split("/")[3]
                job = job_store.get(job_id)
                production_input = job.get("production_input") or {}
                self.json_response(job_store.update_script(
                    job_id,
                    value,
                    review_script(
                        value,
                        job_store.approved_findings(job_id),
                        production_input.get("capability_pack"),
                        production_input.get("learning_rules"),
                    ),
                    estimate,
                ))
            elif path.startswith("/api/learning/rules/"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise FileNotFoundError("接口不存在")
                status = body.get("status")
                if status not in {"active", "disabled"}:
                    raise UnprocessableError("规则状态必须是active或disabled")
                self.json_response({"rule": learning_store.set_rule_status(parts[3], status)})
            else:
                self.error_response("not_found", "接口不存在", HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.handle_exception(exc)

    def do_OPTIONS(self) -> None:
        self.error_response("method_not_allowed", "不提供跨来源请求", HTTPStatus.METHOD_NOT_ALLOWED)

    def _status(self) -> dict:
        config = config_store.public_config()
        provider_configured = bool(config["provider"]["has_api_key"])
        provider_signature = provider_configuration_signature()
        with state_lock:
            tools = list(app_state["tools"])
            provider_connection_verified = bool(
                provider_signature
                and provider_session_state["verified_signature"] == provider_signature
            )
            provider_verified_at = provider_session_state["verified_at"] if provider_connection_verified else None
        catalog = package_catalog.load()
        discovered_capabilities = {cap for tool in tools for cap in tool.get("capabilities", [])}
        bundled_capabilities = {
            cap
            for package in catalog.get("packages", [])
            if package.get("trust_status") == "approved_bundled_skill"
            for cap in package.get("capabilities", [])
        }
        return {
            "name": "时宜 Agent 内容工厂",
            "version": "0.3.0",
            "schema_version": 2,
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "provider": config["provider"]["name"],
            "model": config["provider"]["model"],
            # provider_ready is retained for older clients and means that a Key
            # is available, not that a network connection has been verified.
            "provider_ready": provider_configured,
            "provider_configured": provider_configured,
            "provider_connection_verified": provider_connection_verified,
            "provider_connection_verified_at": provider_verified_at,
            "provider_state": (
                "verified" if provider_connection_verified else "configured" if provider_configured else "unconfigured"
            ),
            "tool_count": len(tools),
            "capabilities": sorted(discovered_capabilities | bundled_capabilities),
            "job_count": len(job_store.list()),
            "safe_mode": not config["security"].get("allow_external_commands", False),
            "catalog_package_count": len(catalog.get("packages", [])),
            "catalog_install_enabled": catalog["policy"]["auto_install_enabled"],
            "memory_count": len(learning_store.list_memories()),
            "learned_skill_count": len(learning_store.list_skills()),
            "dynamic_capability_pack_count": len(capability_registry.list()),
            "production_engine": production_engine_binding(strict=False)[2],
        }

    def _discover(self, body: dict) -> dict:
        config = config_store.load()
        roots = body.get("roots") or config["discovery"].get("roots", [])
        if not isinstance(roots, list):
            raise UnprocessableError("roots必须是数组")
        discovery = ProjectDiscovery(
            max_depth=config["discovery"].get("max_depth", 3),
            max_directories=config["discovery"].get("max_directories", 1500),
        )
        report = discovery.scan(roots)
        with state_lock:
            app_state["tools"] = report["tools"]
            app_state["last_scan"] = datetime.now().astimezone().isoformat(timespec="seconds")
            app_state["last_scan_report"] = {key: value for key, value in report.items() if key != "tools"}
            DISCOVERY_CACHE.write_text(json.dumps(app_state, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def _provider(self, budget: BudgetLedger | None = None) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(config_store.load()["provider"], config_store.get_api_key(), budget=budget)

    def _prepare_production_input(self, body: dict, *, include_defaults: bool) -> tuple[dict | None, list[dict]]:
        selection_bundle_id = body.get("selection_bundle_id")
        if selection_bundle_id is not None:
            unknown = sorted(set(body) - {"plan", "selection_bundle_id", "candidate_id", "production_options"})
            if unknown:
                raise UnprocessableError("选题创建请求包含不允许的字段", details={"fields": unknown})
            options = body.get("production_options", {})
            if not isinstance(options, dict):
                raise UnprocessableError("production_options必须是JSON对象")
            allowed_options = {
                "target_duration_seconds", "pattern_card_ids", "voice_engine", "aspect_ratio", "render_mode",
                "require_animation", "enable_web_research", "source_urls", "motion_scenes", "animation_quality",
            }
            option_unknown = sorted(set(options) - allowed_options)
            if option_unknown:
                raise UnprocessableError("production_options包含不允许的字段", details={"fields": option_unknown})
            candidate, pack = resolve_topic_selection_bundle(selection_bundle_id, body.get("candidate_id"))
            production_input = dict(DEFAULT_INPUT) if include_defaults else {}
            production_input.update({
                "topic": candidate["title"],
                "audience": candidate["audience"],
                "capability_pack": pack,
                "project_id": selection_bundle_id,
                "selection_bundle_id": selection_bundle_id,
                "candidate_id": candidate["id"],
            })
            production_input.update(options)
        else:
            if "production_input" not in body and not include_defaults:
                return None, []
            supplied = {} if "production_input" not in body else body["production_input"]
            if not isinstance(supplied, dict):
                raise UnprocessableError("production_input必须是JSON对象")
            if "learning_rules" in supplied:
                raise UnprocessableError("learning_rules只能由服务端记忆库绑定，客户端不得提交")
            if "selection_bundle_id" in supplied or "candidate_id" in supplied:
                raise UnprocessableError("选题凭证只能通过服务端selection bundle创建")
            production_input = dict(DEFAULT_INPUT) if include_defaults else {}
            production_input.update(supplied)

        supplied_pack = production_input.get("capability_pack")
        normalized = validate_topic_input(production_input)
        pack = normalized["capability_pack"]
        if supplied_pack is None or pack.get("source") in {"local", "legacy"}:
            pack = self._publish_capability_pack(pack)
        else:
            try:
                registered = capability_registry.get(pack["id"], pack["sha256"])
            except (TypeError, ValueError) as exc:
                raise UnprocessableError("行业能力包不在服务端已审核注册表中，请重新生成三个候选") from exc
            if registered != pack:
                raise UnprocessableError("行业能力包与服务端已审核版本不一致")
            pack = registered
        normalized["capability_pack"] = pack
        rules = learning_store.rules_for(pack["id"])
        return normalized, rules

    def _create_demo_job(self, body: dict) -> tuple[dict, bool]:
        production_input, rules = self._prepare_production_input(body, include_defaults=True)
        assert production_input is not None
        plan = local_fallback_plan(f"制作样片：{production_input['topic']}", [])
        for step in plan["steps"]:
            if step.get("capability") != "human_refinement":
                step["tool_id"] = "trusted-local-production-adapter"
                step["risk"] = "固定路径本地适配器，仍需人工批准"
        plan["summary"] = "分阶段内容任务：研究、证据人工审定、脚本合规放行、配音与成片。"

        request_key = self.headers.get("Idempotency-Key", "").strip()
        if request_key and not IDEMPOTENCY_RE.fullmatch(request_key):
            error = WorkflowError("Idempotency-Key格式无效")
            error.status, error.code = 400, "invalid_idempotency_key"
            raise error
        fingerprint = hashlib.sha256(
            json.dumps(production_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with state_lock:
            replay = agent_create_replays.get(request_key) if request_key else None
            if replay:
                if replay["fingerprint"] != fingerprint:
                    error = WorkflowError("同一Idempotency-Key不能用于不同创建请求")
                    error.status, error.code = 409, "idempotency_conflict"
                    raise error
                return job_store.get(replay["job_id"]), True
            job = job_store.create(plan, production_input=production_input)
            if rules:
                job = job_store.apply_learning_rules(job["id"], rules, "创建任务时应用服务端已验证记忆")
            if request_key:
                agent_create_replays[request_key] = {"fingerprint": fingerprint, "job_id": job["id"]}
                while len(agent_create_replays) > 128:
                    agent_create_replays.pop(next(iter(agent_create_replays)))
            return job, False

    @staticmethod
    def _safe_topic_candidates(
        excluded: list[str], goal: str, capability_pack: dict | None = None,
    ) -> list[dict]:
        """Build three deterministic candidates without borrowing another industry."""
        pack = validate_capability_pack(capability_pack or local_capability_pack(goal))
        return local_topic_candidates(goal, pack, excluded)

    @staticmethod
    def _startup_memory_rules(goal: str) -> list[dict[str, object]]:
        """Read rules from the deterministic base pack before regeneration.

        A local pack that incorporates a learned constraint has a new immutable
        hash (and therefore may have a new id).  The base local-pack id remains
        the stable lookup key for project-scoped startup memory.
        """

        base_pack_id = local_capability_pack(goal)["id"]
        return learning_store.rules_for(base_pack_id)

    @classmethod
    def _local_pack_with_startup_memory(cls, goal: str) -> dict:
        return local_capability_pack(goal, cls._startup_memory_rules(goal))

    @staticmethod
    def _validate_topic_goal(value: object) -> str:
        try:
            return validate_goal(value, minimum=4, maximum=200)
        except (TypeError, ValueError) as exc:
            raise UnprocessableError(str(exc)) from exc

    @staticmethod
    def _normalize_topic_candidates(
        raw: list[dict], excluded: list[str], goal: str, capability_pack: dict,
    ) -> tuple[list[dict], bool]:
        pack = validate_capability_pack(capability_pack)
        default_audience = str(pack["snapshot"].get("audience", "目标受众")).strip() or "目标受众"
        excluded_set = {item.strip() for item in excluded if isinstance(item, str) and item.strip()}
        result: list[dict] = []
        seen = set(excluded_set)
        for item in raw:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            reason = str(item.get("reason", "")).strip()
            audience = str(item.get("audience", default_audience)).strip() or default_audience
            if title in seen or any(phrase in title for phrase in UNSAFE_TOPIC_PHRASES):
                continue
            try:
                validate_topic_input({
                    "topic": title,
                    "audience": audience,
                    "target_duration_seconds": 52,
                    "capability_pack": pack,
                })
            except UnprocessableError:
                continue
            if not 4 <= len(reason) <= 100:
                continue
            seen.add(title)
            result.append({"title": title, "reason": reason, "audience": audience})
            if len(result) == 3:
                break
        used_local_fallback = len(result) < 3
        if used_local_fallback:
            fallback = AppHandler._safe_topic_candidates(list(seen), goal, pack)
            for item in fallback:
                if item["title"] in seen:
                    continue
                result.append(item)
                seen.add(item["title"])
                if len(result) == 3:
                    break
        return [dict(item, id=f"topic-{index + 1}") for index, item in enumerate(result)], used_local_fallback

    def _suggest_topics(self, body: dict) -> dict:
        """Release topic flow with a deterministic, server-owned safety boundary.

        Dynamic capability-pack generation and its counter-evidence review remain
        available in ``_suggest_topics_experimental_dynamic`` for offline
        experiments, but they are deliberately not part of the release homepage
        path.  A topic request may spend at most one pre-task Provider call.
        """

        if os.environ.get("SHIYI_EXPERIMENTAL_DYNAMIC_TOPICS", "").strip() == "1":
            return self._suggest_topics_experimental_dynamic(body)

        goal = self._validate_topic_goal(body.get("goal"))
        pretask_budget = pretask_budget_for(goal)
        excluded = [] if "excluded_topics" not in body else body["excluded_topics"]
        if not isinstance(excluded, list) or len(excluded) > 24:
            raise UnprocessableError("excluded_topics必须是不超过24项的数组")
        if any(not isinstance(item, str) for item in excluded):
            raise UnprocessableError("excluded_topics每一项都必须是字符串")
        excluded = [item.strip() for item in excluded if item.strip()]
        if any(len(item) > 80 for item in excluded):
            raise UnprocessableError("excluded_topics每一项最多80字")

        # Preserve the released refresh contract and its tamper checks.  The
        # supplied snapshot is never trusted as the release safety boundary;
        # the server always rebuilds that boundary from the normalized goal and
        # approved startup memory below.
        supplied_pack = body.get("capability_pack")
        if supplied_pack is not None:
            try:
                supplied_pack = validate_capability_pack(supplied_pack)
            except (TypeError, ValueError) as exc:
                raise UnprocessableError(f"行业能力包无效：{exc}") from exc
            if supplied_pack["snapshot"].get("goal") != goal:
                raise UnprocessableError("行业能力包与当前目标不匹配，请重新生成项目上下文")
            try:
                registered_pack = capability_registry.get(supplied_pack["id"], supplied_pack["sha256"])
            except (TypeError, ValueError) as exc:
                raise UnprocessableError("刷新只能使用服务端已登记的行业能力包，请重新生成项目上下文") from exc
            if registered_pack != supplied_pack:
                raise UnprocessableError("行业能力包与服务端已审核版本不一致")

        pack = self._local_pack_with_startup_memory(goal)
        capability_review: dict | None = None
        capability_review_failure_kind = "not_run"
        bootstrap_failure_kind = "not_run"
        bootstrap_schema_diagnostic: dict | None = None
        source = "local_safe_agent"
        status_notice = ""
        remaining_before_call = pretask_budget.snapshot()["remaining"]

        if remaining_before_call <= 0:
            raw = self._safe_topic_candidates(excluded, goal, pack)
            status_notice = "本机会话的预任务 Agent Provider 预算已耗尽，已返回本地安全候选"
        elif not config_store.get_api_key():
            raw = self._safe_topic_candidates(excluded, goal, pack)
            status_notice = "未配置 DeepSeek Key，已返回本地安全候选且未产生 Provider 请求"
        else:
            try:
                provider = self._provider(pretask_budget)
                memory_rules = self._startup_memory_rules(goal)
                # Exactly one release-path Provider method is allowed here.  Do
                # not retry an older signature: a signature/transport/schema
                # failure must safely fall back without spending another call.
                raw = provider.suggest_topics(goal, excluded, pack, memory_rules)
                if not isinstance(raw, list):
                    raise ProviderError("选题 Provider 未返回候选数组")
                source = "deepseek"
                status_notice = "DeepSeek 已返回候选"
            except (ProviderError, TypeError, ValueError):
                raw = self._safe_topic_candidates(excluded, goal, pack)
                source = "local_safe_agent"
                status_notice = "DeepSeek 选题暂不可用，已降级为本地安全候选"

        candidates, used_local_fallback = self._normalize_topic_candidates(raw, excluded, goal, pack)
        if source == "deepseek" and used_local_fallback:
            source = "deepseek_filtered_with_local_fallback"
            status_notice = "部分 DeepSeek 候选未通过本地安全校验，已补足本地安全候选"
        if len(candidates) != 3:
            raise WorkflowError("Agent暂时没有找到三个互不重复的安全角度，请稍后再试")

        pack = self._publish_capability_pack(pack)
        budget = pretask_budget.snapshot()
        source_label = {
            "deepseek": "DeepSeek",
            "deepseek_filtered_with_local_fallback": "DeepSeek与本地安全候选混合",
            "local_safe_agent": "本地安全候选",
        }[source]
        budget_note = (
            f"预任务 Agent Provider 请求 {budget['attempted']}/{budget['limit']}，"
            f"剩余 {budget['remaining']}；来源：{source_label}"
        )
        release_boundary_note = (
            "正式版使用服务端确定性安全能力包；动态能力包生成与反证审核不进入正式主链；"
            "候选仍须在后续研究阶段逐条核验，不代表事实已证实"
        )
        notice = f"{status_notice.rstrip('。')}；{release_boundary_note}；{budget_note}。"
        selection_bundle_id = store_topic_selection_bundle(goal, pack, candidates)
        return {
            "goal": goal,
            "source": source,
            "notice": notice,
            "candidates": candidates,
            "selection_bundle_id": selection_bundle_id,
            "selection_bundle_expires_in_seconds": SELECTION_BUNDLE_TTL_SECONDS,
            "capability_pack": pack,
            "capability_review": capability_review,
            "capability_review_failure_kind": capability_review_failure_kind,
            "bootstrap_failure_kind": bootstrap_failure_kind,
            "bootstrap_schema_diagnostic": bootstrap_schema_diagnostic,
            "context": {
                "project_id": pack["id"],
                "industry_pack_id": pack["id"],
                "industry_pack_label": pack["snapshot"].get("label", "确定性安全能力包"),
                "industry": pack["snapshot"].get("industry", "通用内容行业"),
                "confidence": "limited",
                "selected_by": source,
                "material_count": 0,
            },
            "learning": learning_store.memory_snapshot(pack["id"]),
            "screening": f"{release_boundary_note}；{budget_note}。",
            # Keep the topic-specific public field used by the released UI and
            # expose the shared name used by /api/agent/plan as well.
            "topic_provider_budget": budget,
            "pretask_provider_budget": budget,
        }

    def _suggest_topics_experimental_dynamic(self, body: dict) -> dict:
        """Retained dynamic bootstrap/review path for explicit offline experiments."""
        goal = self._validate_topic_goal(body.get("goal"))
        pretask_budget = pretask_budget_for(goal)
        excluded = [] if "excluded_topics" not in body else body["excluded_topics"]
        if not isinstance(excluded, list) or len(excluded) > 24:
            raise UnprocessableError("excluded_topics必须是不超过24项的数组")
        if any(not isinstance(item, str) for item in excluded):
            raise UnprocessableError("excluded_topics每一项都必须是字符串")
        excluded = [item.strip() for item in excluded if item.strip()]
        if any(len(item) > 80 for item in excluded):
            raise UnprocessableError("excluded_topics每一项最多80字")
        supplied_pack = body.get("capability_pack")
        if supplied_pack is not None:
            try:
                supplied_pack = validate_capability_pack(supplied_pack)
            except (TypeError, ValueError) as exc:
                raise UnprocessableError(f"行业能力包无效：{exc}") from exc
            if supplied_pack["snapshot"].get("goal") != goal:
                raise UnprocessableError("行业能力包与当前目标不匹配，请重新生成项目上下文")
            try:
                registered_pack = capability_registry.get(supplied_pack["id"], supplied_pack["sha256"])
            except (TypeError, ValueError) as exc:
                raise UnprocessableError("刷新只能使用服务端已登记的行业能力包，请重新生成项目上下文") from exc
            if registered_pack != supplied_pack:
                raise UnprocessableError("行业能力包与服务端已审核版本不一致")
            supplied_pack = registered_pack
        raw: list[dict]
        pack = supplied_pack
        capability_review: dict | None = None
        capability_review_failure_kind = "not_run"
        bootstrap_failure_kind = "not_run"
        bootstrap_schema_diagnostic: dict | None = None
        reviewed_candidate_titles: dict[str, str] | None = None
        source = "deepseek_bootstrap"
        notice = ""
        remaining_before_call = pretask_budget.snapshot()["remaining"]
        if pack is None and remaining_before_call < 2:
            # A model-generated pack is never usable without a separate
            # counter-evidence pass.  Do not spend the final request on a
            # bootstrap result that cannot be independently reviewed.
            pack = self._local_pack_with_startup_memory(goal)
            raw = self._safe_topic_candidates(excluded, goal, pack)
            source = "local_safe_agent"
            notice = "预任务预算不足以同时完成能力包生成和独立反证审核，已使用本地安全能力包。"
        elif remaining_before_call <= 0:
            pack = pack or self._local_pack_with_startup_memory(goal)
            raw = self._safe_topic_candidates(excluded, goal, pack)
            source = "local_safe_agent"
            notice = "本机会话的预任务 Agent Provider 预算已耗尽，已使用本地通用能力包和安全候选。"
        else:
            try:
                provider = self._provider(pretask_budget)
                if pack is not None:
                    memory_rules = learning_store.rules_for(pack["id"])
                    try:
                        raw = provider.suggest_topics(goal, excluded, pack, memory_rules)
                    except TypeError:
                        raw = provider.suggest_topics(goal, excluded)
                    source = "deepseek"
                else:
                    bootstrap_method = getattr(provider, "bootstrap_project", None)
                    if not callable(bootstrap_method):
                        pack = self._local_pack_with_startup_memory(goal)
                        raw = provider.suggest_topics(goal, excluded)
                        source = "deepseek"
                        bootstrap = None
                    else:
                        startup_memory_rules = self._startup_memory_rules(goal)
                        bootstrap_failure_kind = "provider_unavailable"
                        bootstrap = bootstrap_method(goal, excluded, startup_memory_rules)
                    if bootstrap is None:
                        pass
                    elif not isinstance(bootstrap, dict):
                        raise ProviderError("项目启动结果不是JSON对象")
                    else:
                        raw_snapshot = bootstrap.get("capability_pack")
                        raw = bootstrap.get("candidates")
                        if not isinstance(raw_snapshot, dict) or not isinstance(raw, list):
                            raise ProviderError("项目启动结果缺少行业能力包或三个候选")
                        pack = normalize_capability_pack(raw_snapshot, goal, "deepseek")
                        bootstrap_failure_kind = "passed"
                        review_method = getattr(provider, "adversarial_review_capability_pack", None)
                        if pretask_budget.snapshot()["remaining"] <= 0 or not callable(review_method):
                            raise ProviderError("动态行业能力包缺少独立反证审核额度或审核器")
                        capability_review_failure_kind = "provider_unavailable"
                        audit = review_method(pack, raw)
                        if not isinstance(audit, dict):
                            capability_review_failure_kind = "invalid_schema"
                            raise ProviderError("严格反证审核没有返回JSON对象")
                        try:
                            capability_review = normalize_capability_review(
                                audit,
                                raw,
                            )
                        except ProviderError:
                            capability_review_failure_kind = "invalid_schema"
                            raise
                        audit = capability_review
                        audit_status = audit["status"]
                        capability_review_failure_kind = audit_status
                        if audit_status != "passed":
                            raise ProviderError("动态行业能力包未通过严格反证审核")
                        reviewed_candidate_titles = {
                            str(item.get("id", "")): str(item.get("title", "")).strip()
                            for item in raw
                            if isinstance(item, dict)
                        }
                        normalized_audit = {
                            "status": "passed",
                            "generated_by": "adversarial_agent",
                            "reviewer": "strict_counterevidence_review",
                            "note": "所有能力包内容默认不可信；仅保留审核后限定用途。",
                            "warnings": list(audit.get("issues", [])),
                            "checks": list(audit.get("safe_scope", [])),
                            "risk_flags": list(audit.get("issues", [])),
                            "constraints_added": list(audit.get("safe_scope", [])),
                        }
                        audited_snapshot = dict(pack["snapshot"])
                        audited_snapshot["evidence_requirements"] = list(dict.fromkeys(
                            list(audited_snapshot.get("evidence_requirements", []))
                            + list(audit.get("safe_scope", []))
                        ))
                        pack = normalize_capability_pack(
                            audited_snapshot, goal, "deepseek", audit=normalized_audit,
                        )
            except (ProviderError, TypeError, ValueError) as exc:
                if bootstrap_failure_kind != "passed" and pack is None:
                    bootstrap_failure_kind = bootstrap_failure_from_budget(pretask_budget)
                    if bootstrap_failure_kind == "invalid_capability_pack_schema" and isinstance(exc, ProviderError):
                        bootstrap_schema_diagnostic = sanitize_bootstrap_schema_diagnostic(exc.details)
                if capability_review_failure_kind == "provider_unavailable":
                    capability_review_failure_kind = capability_review_failure_from_budget(pretask_budget)
                pack = supplied_pack or self._local_pack_with_startup_memory(goal)
                raw = self._safe_topic_candidates(excluded, goal, pack)
                source = "local_safe_agent"
                if bootstrap_failure_kind == "invalid_capability_pack_schema":
                    notice = f"{bootstrap_schema_diagnostic_summary(bootstrap_schema_diagnostic)}，已切换到本地安全能力包。"
                elif bootstrap_failure_kind == "invalid_topic_schema":
                    notice = "项目启动候选结构无效，已切换到本地安全候选。"
                elif bootstrap_failure_kind == "provider_unavailable":
                    notice = "项目启动 Provider 或传输不可用，已切换到本地安全能力包。"
                else:
                    notice = {
                        "needs_revision": "动态行业能力包需要修改后重新审核，已切换到本地安全能力包。",
                        "blocked": "动态行业能力包被反证审核阻止，已切换到本地安全能力包。",
                        "invalid_schema": "反证审核结构或候选身份无效，已切换到本地安全能力包。",
                        "provider_unavailable": "Provider 或传输未返回可用审核结果，已切换到本地安全能力包。",
                    }.get(capability_review_failure_kind, "反证审核未执行，已切换到本地安全能力包。")
        if pack is None:
            pack = self._local_pack_with_startup_memory(goal)
        candidates, used_local_fallback = self._normalize_topic_candidates(raw, excluded, goal, pack)
        if reviewed_candidate_titles is not None:
            returned_candidate_titles = {item["id"]: item["title"] for item in candidates}
            if used_local_fallback or returned_candidate_titles != reviewed_candidate_titles:
                pack = self._local_pack_with_startup_memory(goal)
                raw = self._safe_topic_candidates(excluded, goal, pack)
                candidates, used_local_fallback = self._normalize_topic_candidates(raw, excluded, goal, pack)
                source = "local_safe_agent"
                notice = "反证审核与候选身份绑定不一致，已切换到不沿用原裁决的本地安全候选。"
                capability_review = None
                capability_review_failure_kind = "invalid_schema"
                reviewed_candidate_titles = None
        if source in {"deepseek", "deepseek_bootstrap"} and used_local_fallback:
            source = "deepseek_filtered_with_local_fallback"
            notice = "部分模型候选未通过安全校验，已用本地安全选题补足三个角度。"
        if len(candidates) != 3:
            raise WorkflowError("Agent暂时没有找到三个互不重复的安全角度，请稍后再试")
        pack = self._publish_capability_pack(pack)
        budget = pretask_budget.snapshot()
        source_label = {
            "deepseek": "DeepSeek",
            "deepseek_bootstrap": "DeepSeek动态能力包",
            "deepseek_filtered_with_local_fallback": "DeepSeek与本地安全候选混合",
            "local_safe_agent": "本地安全候选",
        }[source]
        budget_note = (
            f"预任务 Agent Provider 请求 {budget['attempted']}/{budget['limit']}，"
            f"剩余 {budget['remaining']}；来源：{source_label}"
        )
        notice = f"{notice.rstrip('。')}；{budget_note}。" if notice else f"{budget_note}。"
        review_screening = capability_review_screening(
            capability_review,
            failure_kind=capability_review_failure_kind,
        )
        bootstrap_screening = bootstrap_failure_screening(
            bootstrap_failure_kind,
            bootstrap_schema_diagnostic,
        )
        selection_bundle_id = store_topic_selection_bundle(goal, pack, candidates)
        return {
            "goal": goal,
            "source": source,
            "notice": notice,
            "candidates": candidates,
            "selection_bundle_id": selection_bundle_id,
            "selection_bundle_expires_in_seconds": SELECTION_BUNDLE_TTL_SECONDS,
            "capability_pack": pack,
            "capability_review": capability_review,
            "capability_review_failure_kind": capability_review_failure_kind,
            "bootstrap_failure_kind": bootstrap_failure_kind,
            "bootstrap_schema_diagnostic": bootstrap_schema_diagnostic,
            "context": {
                "project_id": pack["id"],
                "industry_pack_id": pack["id"],
                "industry_pack_label": pack["snapshot"].get("label", "动态行业能力包"),
                "industry": pack["snapshot"].get("industry", "通用内容行业"),
                "confidence": (
                    "high"
                    if source.startswith("deepseek")
                    and (pack.get("audit") or {}).get("status") == "passed"
                    else "limited"
                ),
                "selected_by": source,
                "material_count": 0,
            },
            "learning": learning_store.memory_snapshot(pack["id"]),
            "screening": f"{bootstrap_screening}；{review_screening}；{budget_note}。",
            # Keep the topic-specific public field used by the released UI and
            # expose the shared name used by /api/agent/plan as well.
            "topic_provider_budget": budget,
            "pretask_provider_budget": budget,
        }

    @staticmethod
    def _publish_capability_pack(pack: dict) -> dict:
        """Publish once; identical regenerated metadata reuses the immutable snapshot."""
        try:
            return capability_registry.publish(pack)
        except CapabilityPackConflictError:
            existing = capability_registry.get(pack["id"], pack["sha256"])
            current_without_time = {key: value for key, value in pack.items() if key != "generated_at"}
            existing_without_time = {key: value for key, value in existing.items() if key != "generated_at"}
            if current_without_time == existing_without_time:
                return existing
            raise

    def _plan(self, body: dict) -> dict:
        goal = str(body.get("goal", "")).strip()
        if not goal:
            raise UnprocessableError("请先描述要完成的内容任务")
        pretask_budget = pretask_budget_for(goal)
        with state_lock:
            tools = list(app_state["tools"])
        source = "deepseek"
        fallback = False
        notice = ""
        if pretask_budget.snapshot()["remaining"] <= 0:
            plan = local_fallback_plan(goal, tools)
            source = "local_safe_agent"
            fallback = True
            notice = "本机会话的预任务 Agent Provider 预算已耗尽，已只使用本地安全计划。"
        else:
            try:
                plan = self._provider(pretask_budget).plan(goal, tools)
                plan["planner"] = "api"
            except ProviderError as exc:
                plan = local_fallback_plan(goal, tools)
                source = "local_safe_agent"
                fallback = True
                notice = f"DeepSeek本次未返回可用计划，已切换到本地安全计划：{exc}"
        budget = pretask_budget.snapshot()
        source_label = "DeepSeek" if source == "deepseek" else "本地安全计划"
        budget_note = (
            f"预任务 Agent Provider 请求 {budget['attempted']}/{budget['limit']}，"
            f"剩余 {budget['remaining']}；来源：{source_label}"
        )
        notice = f"{notice.rstrip('。')}；{budget_note}。" if notice else f"{budget_note}。"
        return {
            "plan": plan,
            "fallback": fallback,
            "source": source,
            "notice": notice,
            "pretask_provider_budget": budget,
        }

    def _record_correction(self, body: dict) -> dict:
        request_key = self.headers.get("Idempotency-Key", "").strip()
        if not IDEMPOTENCY_RE.fullmatch(request_key):
            error = WorkflowError("纠错请求必须携带有效的Idempotency-Key")
            error.status, error.code = 400, "invalid_idempotency_key"
            raise error
        fingerprint = hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with state_lock:
            replay = correction_replays.get(request_key)
            if replay:
                if replay["fingerprint"] != fingerprint:
                    raise ConflictError("同一Idempotency-Key不能用于不同纠错请求")
                if replay.get("result") is None:
                    raise ConflictError("相同纠错请求正在处理")
                return json.loads(json.dumps(replay["result"], ensure_ascii=False))
            correction_replays[request_key] = {"fingerprint": fingerprint, "result": None}

        completed = False
        try:
            result = self._record_correction_once(body)
            completed = True
        finally:
            if not completed:
                with state_lock:
                    correction_replays.pop(request_key, None)
        with state_lock:
            correction_replays[request_key]["result"] = json.loads(json.dumps(result, ensure_ascii=False))
            correction_replays.move_to_end(request_key)
            while len(correction_replays) > 256:
                correction_replays.popitem(last=False)
        return result

    def _record_correction_once(self, body: dict) -> dict:
        job_id = str(body.get("job_id", "")).strip() or None
        job = job_store.get(job_id) if job_id else None
        if job is not None and job.get("schema_version") != 2:
            raise ConflictError("旧任务只读，不能写入新的纠错记忆")
        pack_value = (
            (job.get("production_input") or {}).get("capability_pack")
            if job else body.get("capability_pack")
        )
        if pack_value is None:
            raise UnprocessableError("纠错必须关联当前任务或行业能力包")
        try:
            pack = validate_capability_pack(pack_value)
        except (TypeError, ValueError) as exc:
            raise UnprocessableError(f"行业能力包无效：{exc}") from exc
        if job is None:
            try:
                registered = capability_registry.get(pack["id"], pack["sha256"])
            except (TypeError, ValueError) as exc:
                raise UnprocessableError("纠错关联的行业能力包不在服务端注册表中") from exc
            if registered != pack:
                raise UnprocessableError("纠错关联的行业能力包与服务端版本不一致")
            pack = registered
        correction_kind = infer_correction_kind(str(body.get("message", "")), body.get("kind"))
        learning_payload = dict(body)
        learning_payload["kind"] = correction_kind
        if correction_kind == "capability":
            requested_scope = body.get("scope")
            if requested_scope is not None and requested_scope not in {"task", "project", "workspace"}:
                raise UnprocessableError("scope必须是task、project或workspace")
            if requested_scope == "task":
                raise UnprocessableError("能力包不可变，能力包纠错不能仅作用于当前任务；请使用project或明确全局范围")
            # Capability corrections can only guide a future task with a newly
            # reviewed pack.  A job-associated correction would otherwise
            # default to task scope and become an unreachable old-job memory.
            if requested_scope is None:
                learning_payload["scope"] = "project"
            resolved_scope = LearningStore._resolve_scope(
                str(body.get("message", "")), learning_payload.get("scope"), has_job=job_id is not None,
            )
            if resolved_scope == "task":
                raise UnprocessableError("能力包不可变，能力包纠错不能仅作用于当前任务；请使用project或明确全局范围")
        correction = learning_store.record_correction(learning_payload, pack, job_id=job_id)
        rules = learning_store.rules_for(pack["id"], job_id=job_id)
        updated_job = None
        queued = False
        requires_new_task = correction_kind == "capability"
        notice = (
            "能力包不可变：本次纠错已安全记录为后续任务规则，当前任务继续使用原能力包；请通过 /api/agent/topics 重新生成候选并创建带新能力包的任务。"
            if requires_new_task
            else None
        )
        if job_id and correction_kind != "capability":
            try:
                updated_job = job_store.apply_learning_rules(
                    job_id,
                    rules,
                    str(body.get("message", "")),
                    correction_kind=correction_kind,
                )
            except ConflictError:
                current = job_store.get(job_id)
                if current.get("status") not in {"research_running", "content_running", "rendering"}:
                    raise
                # A blocking Provider call is not falsely reported as cancelled.
                queued = True
        return {
            "correction": correction,
            "correction_kind": correction_kind,
            "effective_scope": correction["scope"],
            "rules": rules,
            "memory": learning_store.memory_snapshot(pack["id"], job_id=job_id),
            "skills": learning_store.list_skills(),
            "job": updated_job,
            "applied_to_current": bool(updated_job),
            "queued_for_next_stage": queued,
            "interrupt_supported": False,
            "effective_mode": "recorded_for_new_task" if requires_new_task else ("defer" if queued else "applied_at_safe_boundary"),
            "requires_new_task": requires_new_task,
            "notice": notice,
        }

    @staticmethod
    def _sync_learning_rules(job: dict) -> dict:
        """Apply corrections recorded during a running stage at the next safe boundary."""
        production_input = job.get("production_input") or {}
        pack = production_input.get("capability_pack")
        if not isinstance(pack, dict) or not pack.get("id"):
            return job
        rules = learning_store.rules_for(str(pack["id"]), job_id=str(job["id"]))
        durable_kinds = learning_store.correction_kinds_for_rules(rules)
        applicable_rules = [
            item
            for item in rules
            if not (
                "capability" in durable_kinds.get(str(item.get("rule_id", "")), [])
                and "evidence" not in durable_kinds.get(str(item.get("rule_id", "")), [])
            )
        ]
        desired_ids = [str(item.get("rule_id", "")) for item in applicable_rules if item.get("rule_id")]
        current_ids = [str(value) for value in job.get("learning_rule_ids", [])]
        if desired_ids == current_ids:
            return job
        current = set(current_ids)
        new_kinds: list[str] = []
        for item in applicable_rules:
            rule_id = str(item.get("rule_id", ""))
            if rule_id in current:
                continue
            kinds = durable_kinds.get(rule_id, [])
            # Only records created before kind persistence use the historical
            # instruction heuristic.  New queued corrections never lose their
            # explicit request between fsync and the next safe boundary.
            new_kinds.extend(kinds or [infer_correction_kind(str(item.get("instruction", "")))])
        return job_store.apply_learning_rules(
            job["id"],
            applicable_rules,
            "在阶段边界应用已记录的工作人员纠错",
            correction_kind=combine_correction_kinds(new_kinds),
        )

    def require_mutation_security(self) -> None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise UnprocessableError("写接口只接受application/json")
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        session = cookies.get(SESSION_COOKIE)
        if session is None or not secrets.compare_digest(session.value, SESSION_ID):
            error = WorkflowError("本机会话无效，请刷新页面")
            error.status, error.code = 403, "invalid_session"
            raise error
        if not secrets.compare_digest(self.headers.get("X-Shiyi-CSRF", ""), CSRF_TOKEN):
            error = WorkflowError("CSRF校验失败，请刷新页面")
            error.status, error.code = 403, "csrf_failed"
            raise error
        origin = self.headers.get("Origin", "")
        port = int(self.server.server_address[1])
        allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        if origin not in allowed:
            error = WorkflowError("请求来源不受信任")
            error.status, error.code = 403, "origin_rejected"
            raise error

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise UnprocessableError("Content-Length无效") from exc
        if length < 0 or length > 2_000_000:
            raise UnprocessableError("请求内容过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UnprocessableError("请求JSON格式错误") from exc
        if not isinstance(data, dict):
            raise UnprocessableError("请求必须是JSON对象")
        return data

    def json_response(self, data: dict, status: HTTPStatus | int = HTTPStatus.OK, *, set_session: bool = False) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        if set_session:
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}={SESSION_ID}; Path=/; HttpOnly; SameSite=Strict")
        self.end_headers()
        self.wfile.write(payload)

    def error_response(self, code: str, message: str, status: HTTPStatus | int, details: Any | None = None) -> None:
        error = {"code": code, "message": message}
        if details is not None:
            error["details"] = details
        self.json_response({"error": error}, status)

    def handle_exception(self, exc: Exception) -> None:
        if isinstance(exc, WorkflowError):
            self.error_response(exc.code, str(exc), exc.status, exc.details)
        elif isinstance(exc, FileNotFoundError):
            self.error_response("not_found", str(exc), HTTPStatus.NOT_FOUND)
        elif isinstance(exc, ProviderError):
            self.error_response("provider_error", str(exc), HTTPStatus.BAD_GATEWAY)
        elif isinstance(exc, ValueError):
            self.error_response(
                str(getattr(exc, "code", "bad_request")),
                str(exc),
                int(getattr(exc, "status", HTTPStatus.BAD_REQUEST)),
            )
        else:
            self.error_response("internal_error", f"服务器错误: {type(exc).__name__}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def serve_static(self, path: str) -> None:
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.error_response("forbidden", "路径不允许", HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            self.error_response("not_found", "文件不存在", HTTPStatus.NOT_FOUND)
            return
        content = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") or mime == "application/javascript" else mime)
        self.send_header("Content-Length", str(len(content)))
        self.security_headers()
        if relative == "index.html":
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}={SESSION_ID}; Path=/; HttpOnly; SameSite=Strict")
        self.end_headers()
        self.wfile.write(content)

    def serve_file(self, target: Path, name: str) -> None:
        content = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition", f'inline; filename="{name}"')
        self.security_headers()
        self.end_headers()
        self.wfile.write(content)

    def security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'")

    def log_message(self, format: str, *args) -> None:
        line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} {self.client_address[0]} {format % args}\n"
        with (RUNTIME_DIR / "server.log").open("a", encoding="utf-8") as handle:
            handle.write(line)


def find_server(host: str, port: int) -> tuple[ThreadingHTTPServer, int]:
    if host != "127.0.0.1":
        raise ValueError("v2安全模式只允许监听127.0.0.1")
    last_error = None
    for candidate in range(port, port + 20):
        try:
            return ThreadingHTTPServer((host, candidate), AppHandler), candidate
        except OSError as exc:
            last_error = exc
    raise RuntimeError(f"无法找到可用端口: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="时宜 Agent 内容工厂控制台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    args = parser.parse_args()
    server, port = find_server(args.host, args.port)
    url = f"http://{args.host}:{port}"
    (RUNTIME_DIR / "status.json").write_text(
        json.dumps({"status": "running", "url": url, "pid": __import__("os").getpid()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"时宜 Agent 内容工厂已启动: {url}")
    if args.open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        (RUNTIME_DIR / "status.json").write_text(json.dumps({"status": "stopped"}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
