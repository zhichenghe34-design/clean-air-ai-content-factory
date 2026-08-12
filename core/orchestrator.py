from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import threading
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable

from core.capability_pack import (
    EXECUTABLE_AUDIT_STATUSES,
    LEGACY_CLEAN_AIR_PACK_ID,
    legacy_clean_air_pack,
    local_capability_pack,
    validate_capability_pack,
    validate_goal,
)
from core.capability_registry import CapabilityPackRegistry, CapabilityPackRegistryError
from core.review_policy import (
    HUMAN_STAGE_REVIEW,
    MECHANICAL_REVIEWER,
    MECHANICAL_STAGE_REVIEW,
    approval_identity,
    BROWSER_SCRIPT_EDIT_LABELS,
    build_review_policy,
    classify_script_edit_record,
    evidence_status_for_policy,
    normalize_review_policy,
    script_edit_identity,
)
from core.voice_contract import normalize_voice_chunk_max_chars, normalize_voice_engine


PIPELINE = [
    ("content_insight", "内容洞察"),
    ("script_generation", "脚本生成"),
    ("compliance_review", "证据与合规"),
    ("video_generation", "画面与语音"),
    ("video_editing", "自动合成"),
    ("human_refinement", "人工精修"),
]

CANONICAL_ARTIFACTS = [
    "research.json",
    "insight.json",
    "script_variants.json",
    "approved_script.json",
    "review.json",
    "voice.wav",
    "captions.srt",
    "motion_plan.json",
    "final.mp4",
    "run_report.json",
]
DIRECTOR_ARTIFACTS = [
    "motion_storyboard.json",
]
OPTIONAL_ENGINE_ARTIFACTS = [
    "material_sources.json",
    "engine_report.json",
]
OPTIONAL_VISUAL_QC_ARTIFACTS = [
    "contact-sheet.png",
    "visual-qc.json",
]
PUBLIC_ARTIFACTS = [
    *CANONICAL_ARTIFACTS,
    *DIRECTOR_ARTIFACTS,
    *OPTIONAL_ENGINE_ARTIFACTS,
    *OPTIONAL_VISUAL_QC_ARTIFACTS,
]
REVIEW_ARTIFACTS = {"research.json", "approved_script.json", "review.json"}
LEGACY_CLEAN_AIR_MARKERS = ("甲醛", "除醛", "测醛", "室内空气", "装修污染")
RUNNING_STATES = {"research_running", "content_running", "rendering"}
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
PRODUCTION_INPUT_FIELDS = {
    "topic", "audience", "target_duration_seconds", "pattern_card_ids", "voice_engine", "voice_chunk_max_chars",
    "aspect_ratio", "production_mode", "render_mode", "require_animation", "enable_web_research", "source_urls",
    "motion_scenes", "animation_quality", "capability_pack", "learning_rules", "project_id",
    "selection_bundle_id", "candidate_id",
}
PLAN_FIELDS = {"goal", "summary", "steps", "missing", "estimated_cost_level", "planner"}
PLAN_STEP_FIELDS = {"id", "name", "capability", "tool_id", "input", "output", "requires_approval", "risk"}
EMPTY_RESEARCH_APPROVAL_NOTE = (
    "本次确认无可采信 finding；后续仅允许使用不含行业事实主张的本地安全模板"
)
MECHANICAL_RESEARCH_APPROVAL_NOTE = (
    "反向机械审核逐项复核证据、适用边界和禁止外推项；仅批准严格证据绑定的内部候选"
)
MECHANICAL_COMPLIANCE_APPROVAL_NOTE = (
    "反向机械审核确认本地合规规则通过、无阻断与警告；仅允许继续生成内部候选"
)
MECHANICAL_STRICT_FINDING_STATUSES = {
    "proven_for_limited_use",
    "supported_limited",
    "passed",
    "evidence_bound",
}


class WorkflowError(RuntimeError):
    status = 400
    code = "workflow_error"

    def __init__(self, message: str, *, details: Any | None = None):
        super().__init__(message)
        self.details = details


class ConflictError(WorkflowError):
    status = 409
    code = "conflict"


class IdempotencyConflictError(ConflictError):
    code = "idempotency_conflict"


class UnprocessableError(WorkflowError):
    status = 422
    code = "unprocessable"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonical_sha256(data: Any) -> str:
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def topic_in_scope(value: object) -> bool:
    """Return whether text is a supported, low-risk content-production goal."""
    try:
        validate_goal(value, minimum=4, maximum=200)
        return True
    except (TypeError, ValueError):
        return False


def is_legacy_footage_input(value: object) -> bool:
    """Identify persisted pre-``production_mode`` jobs that used the MPT route.

    New jobs are normalized before they are written and therefore always carry
    ``production_mode``.  This narrow signature is only for reading an existing
    v2 job; callers must not add the missing field back to that historical
    record merely to dispatch a retry.
    """

    return bool(
        isinstance(value, dict)
        and "production_mode" not in value
        and value.get("render_mode") != "simple"
        and value.get("require_animation") is not True
    )


def preserve_legacy_footage_contract(
    original: dict[str, Any], normalized: dict[str, Any]
) -> dict[str, Any]:
    """Keep legacy mode fields byte-for-byte absent/present after safe migration."""

    if not is_legacy_footage_input(original):
        return normalized
    preserved = dict(normalized)
    preserved.pop("production_mode", None)
    for field in ("render_mode", "require_animation"):
        if field in original:
            preserved[field] = original[field]
        else:
            preserved.pop(field, None)
    return preserved


def render_result_is_diagnostic(value: object) -> bool:
    """Return whether a render result is ineligible for a formal manifest."""

    if not isinstance(value, dict):
        return False
    if value.get("status") == "diagnostic_only":
        return True
    render = value.get("render")
    return isinstance(render, dict) and render.get("diagnostic_only") is True


def validate_topic_input(
    production_input: dict[str, Any], *, allow_learning_rules: bool = False,
) -> dict[str, Any]:
    if not isinstance(production_input, dict):
        raise UnprocessableError("production_input必须是JSON对象")
    try:
        topic = validate_goal(production_input.get("topic", ""), minimum=4, maximum=80)
    except (TypeError, ValueError) as exc:
        raise UnprocessableError(str(exc)) from exc
    raw_pack = production_input.get("capability_pack")
    if raw_pack is None:
        # Existing v2 callers predate dynamic packs. Clean-air topics retain the
        # released rules; every other new topic receives a deterministic generic
        # snapshot instead of silently falling back to formaldehyde copy.
        raw_pack = (
            legacy_clean_air_pack()
            if any(marker in topic for marker in LEGACY_CLEAN_AIR_MARKERS)
            else local_capability_pack(topic)
        )
    try:
        capability_pack = validate_capability_pack(raw_pack)
    except (TypeError, ValueError) as exc:
        raise UnprocessableError(f"行业能力包无效：{exc}") from exc
    default_audience = str(capability_pack.get("snapshot", {}).get("audience") or "目标受众")
    audience = str(production_input.get("audience", default_audience)).strip()
    if not 2 <= len(audience) <= 80:
        raise UnprocessableError("受众长度必须在2到80字之间")
    unknown = sorted(set(production_input) - PRODUCTION_INPUT_FIELDS)
    if unknown:
        raise UnprocessableError("production_input包含不允许的字段", details={"fields": unknown})
    normalized = dict(production_input)
    normalized.update({"topic": topic, "audience": audience, "capability_pack": capability_pack})
    if "project_id" in normalized:
        project_id = str(normalized["project_id"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{4,128}", project_id):
            raise UnprocessableError("project_id格式无效")
        normalized["project_id"] = project_id
    if "learning_rules" in normalized:
        if not allow_learning_rules:
            raise UnprocessableError("learning_rules只能由服务端记忆库绑定")
        rules = normalized["learning_rules"]
        if not isinstance(rules, list) or len(rules) > 50 or not all(isinstance(item, dict) for item in rules):
            raise UnprocessableError("learning_rules必须是不超过50项的规则对象数组")
        safe_rules = []
        for item in rules:
            unknown_rule = sorted(set(item) - {"rule_id", "scope", "instruction", "pack_id", "source_event_ids"})
            if unknown_rule:
                raise UnprocessableError("learning_rules包含不允许的字段", details={"fields": unknown_rule})
            rule_id = str(item.get("rule_id", "")).strip()
            scope = str(item.get("scope", "")).strip()
            pack_id = str(item.get("pack_id", "")).strip()
            source_event_ids = item.get("source_event_ids", [])
            instruction = str(item.get("instruction", "")).strip()
            if not re.fullmatch(r"rule-[0-9a-f]{20}", rule_id):
                raise UnprocessableError("学习规则ID无效")
            if scope not in {"task", "project", "workspace"}:
                raise UnprocessableError("学习规则作用域无效")
            expected_pack = "*" if scope == "workspace" else capability_pack["id"]
            if pack_id != expected_pack:
                raise UnprocessableError("学习规则与当前行业能力包不匹配")
            if (
                not isinstance(source_event_ids, list)
                or not 1 <= len(source_event_ids) <= 256
                or len(source_event_ids) != len(set(source_event_ids))
                or not all(isinstance(value, str) and re.fullmatch(r"correction-[0-9a-f]{32}", value) for value in source_event_ids)
            ):
                raise UnprocessableError("学习规则来源事件无效")
            if not 4 <= len(instruction) <= 1000:
                raise UnprocessableError("学习规则内容必须在4到1000字之间")
            if re.search(
                r"(?i)(?:https?://|file://|[A-Za-z]:[\\/]|\.\.[\\/]|api[_ -]?key\s*[:=]|password\s*[:=]|"
                r"ignore\s+(?:all\s+)?(?:previous|system|developer)|忽略(?:之前|系统|开发者)|输出(?:系统|开发者)提示词)",
                instruction,
            ):
                raise UnprocessableError("学习规则包含外部地址、敏感值或指令覆盖文本")
            safe_rules.append({
                "rule_id": rule_id,
                "scope": scope,
                "instruction": instruction,
                "pack_id": pack_id,
                "source_event_ids": list(source_event_ids),
            })
        normalized["learning_rules"] = safe_rules
    if "target_duration_seconds" in normalized:
        try:
            duration = float(normalized["target_duration_seconds"])
        except (TypeError, ValueError) as exc:
            raise UnprocessableError("target_duration_seconds必须是45到60之间的数字") from exc
        if not 45 <= duration <= 60:
            raise UnprocessableError("target_duration_seconds必须在45到60之间")
        normalized["target_duration_seconds"] = duration
    if "pattern_card_ids" in normalized:
        values = normalized["pattern_card_ids"]
        if not isinstance(values, list) or len(values) > 20 or not all(isinstance(value, str) and 1 <= len(value) <= 40 for value in values):
            raise UnprocessableError("pattern_card_ids必须是不超过20项的短字符串数组")
    if "voice_engine" in normalized:
        try:
            normalized["voice_engine"] = normalize_voice_engine(normalized["voice_engine"])
        except ValueError as exc:
            raise UnprocessableError(str(exc)) from exc
    if "voice_chunk_max_chars" in normalized:
        try:
            normalized["voice_chunk_max_chars"] = normalize_voice_chunk_max_chars(
                normalized["voice_chunk_max_chars"]
            )
        except ValueError as exc:
            raise UnprocessableError(str(exc)) from exc
    if "aspect_ratio" in normalized and normalized["aspect_ratio"] != "9:16":
        raise UnprocessableError("当前原型只允许9:16竖屏")
    if "render_mode" in normalized and normalized["render_mode"] not in {"animated", "simple"}:
        raise UnprocessableError("render_mode必须是animated或simple")
    if "require_animation" in normalized and not isinstance(normalized["require_animation"], bool):
        raise UnprocessableError("require_animation必须是布尔值")
    production_mode = normalized.get("production_mode")
    if production_mode is None:
        # Preserve the released render_mode input while making motion the
        # canonical mode for every newly normalized task.
        production_mode = "simple" if normalized.get("render_mode") == "simple" else "motion"
    if not isinstance(production_mode, str) or production_mode not in {
        "motion", "footage", "hybrid", "simple",
    }:
        raise UnprocessableError("production_mode必须是motion、footage、hybrid或simple")
    normalized["production_mode"] = production_mode
    if production_mode == "motion":
        normalized["render_mode"] = "animated"
        normalized["require_animation"] = True
    elif production_mode in {"footage", "hybrid"}:
        # render_mode is retained only as a legacy response field.  These
        # modes are dispatched independently by ProductionRunner.
        normalized["render_mode"] = "animated"
        normalized["require_animation"] = False
    else:
        normalized["render_mode"] = "simple"
        normalized["require_animation"] = False
    if "animation_quality" in normalized and normalized["animation_quality"] not in {"draft", "standard", "high"}:
        raise UnprocessableError("animation_quality不在允许范围内")
    for field in ("require_animation", "enable_web_research"):
        if field in normalized and not isinstance(normalized[field], bool):
            raise UnprocessableError(f"{field}必须是布尔值")
    if "source_urls" in normalized:
        urls = normalized["source_urls"]
        if not isinstance(urls, list) or len(urls) > 20:
            raise UnprocessableError("source_urls必须是不超过20项的URL数组")
        clean_urls = []
        for url in urls:
            clean_url = str(url).strip()
            parsed = urllib.parse.urlsplit(clean_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise UnprocessableError("source_urls只允许无凭据的HTTP/HTTPS地址")
            clean_urls.append(clean_url)
        normalized["source_urls"] = clean_urls
    if "motion_scenes" in normalized:
        scenes = normalized["motion_scenes"]
        if not isinstance(scenes, list) or not 4 <= len(scenes) <= 8 or not all(isinstance(scene, dict) for scene in scenes):
            raise UnprocessableError("motion_scenes必须包含4到8个场景对象")
        safe_scenes = []
        for scene in scenes:
            if set(scene) - {"kicker", "title", "caption"}:
                raise UnprocessableError("motion_scenes包含不允许的字段")
            clean = {key: str(scene.get(key, "")).strip() for key in ("kicker", "title", "caption")}
            if any(len(value) > 300 for value in clean.values()):
                raise UnprocessableError("motion_scenes文字过长")
            safe_scenes.append(clean)
        normalized["motion_scenes"] = safe_scenes
    return normalized


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise UnprocessableError("plan必须是JSON对象")
    unknown = sorted(set(plan) - PLAN_FIELDS)
    if unknown:
        raise UnprocessableError("plan包含不允许的字段", details={"fields": unknown})
    steps = plan.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 30 or not all(isinstance(step, dict) for step in steps):
        raise UnprocessableError("plan.steps必须包含1到30个JSON对象")
    safe_steps = []
    for index, step in enumerate(steps, start=1):
        step_unknown = sorted(set(step) - PLAN_STEP_FIELDS)
        if step_unknown:
            raise UnprocessableError("plan.steps包含不允许的字段", details={"index": index, "fields": step_unknown})
        safe_step = {
            "id": str(step.get("id") or f"step-{index}").strip()[:80],
            "name": str(step.get("name") or "未命名阶段").strip()[:120],
            "capability": str(step.get("capability") or "").strip()[:120],
            "tool_id": None if step.get("tool_id") is None else str(step.get("tool_id")).strip()[:160],
            "input": str(step.get("input") or "").strip()[:1000],
            "output": str(step.get("output") or "").strip()[:1000],
            "requires_approval": bool(step.get("requires_approval", False)),
            "risk": str(step.get("risk") or "").strip()[:500],
        }
        if not safe_step["id"] or not safe_step["capability"]:
            raise UnprocessableError("plan.steps的id和capability不能为空", details={"index": index})
        safe_steps.append(safe_step)
    missing = plan.get("missing", [])
    if not isinstance(missing, list) or len(missing) > 50:
        raise UnprocessableError("plan.missing必须是不超过50项的数组")
    return {
        "goal": str(plan.get("goal") or "").strip()[:2000],
        "summary": str(plan.get("summary") or "").strip()[:4000],
        "steps": safe_steps,
        "missing": [str(value).strip()[:200] for value in missing],
        "estimated_cost_level": str(plan.get("estimated_cost_level") or "未估算").strip()[:200],
        "planner": str(plan.get("planner") or "unknown").strip()[:120],
    }


def local_fallback_plan(goal: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
    by_capability: dict[str, list[dict[str, Any]]] = {}
    for tool in tools:
        for capability in tool.get("capabilities", []):
            by_capability.setdefault(capability, []).append(tool)
    steps = []
    for index, (capability, name) in enumerate(PIPELINE, start=1):
        candidates = by_capability.get(capability, [])
        tool = candidates[0] if candidates else None
        steps.append({
            "id": f"step-{index}",
            "name": name,
            "capability": capability,
            "tool_id": tool.get("id") if tool else None,
            "input": "上一步输出或用户提供的素材/上下文",
            "output": f"{name}阶段的结构化产物",
            "requires_approval": capability in {"compliance_review", "video_generation", "video_editing", "human_refinement"},
            "risk": "工具尚未启用，只生成计划" if tool and not tool.get("enabled") else ("尚未发现适配工具" if not tool else "低"),
        })
    missing = [name for capability, name in PIPELINE if capability != "human_refinement" and not by_capability.get(capability)]
    return {
        "goal": goal,
        "summary": "这是未调用外部模型的本地规则计划；配置DeepSeek Key后可结合工具细节重新规划。",
        "steps": steps,
        "missing": missing,
        "estimated_cost_level": "未估算",
        "planner": "local_fallback",
    }


class JobStore:
    def __init__(self, runtime_dir: Path, *, stage_review_mode: str = HUMAN_STAGE_REVIEW):
        self.runtime_dir = Path(runtime_dir)
        self.jobs_dir = self.runtime_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.review_policy = build_review_policy(stage_review_mode)
        except ValueError as exc:
            raise WorkflowError("任务审查策略无效") from exc
        self.capability_registry = CapabilityPackRegistry(self.runtime_dir)
        self._create_lock = threading.Lock()
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._reconcile_strict_rejections()
        self._reconcile_content_rejections()

    def create(
        self,
        plan: dict[str, Any],
        production_input: dict[str, Any] | None = None,
        *,
        creation_request: dict[str, str] | None = None,
        trusted_learning_rules: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        safe_plan = validate_plan(plan)
        steps = safe_plan["steps"]
        candidate_input = dict(production_input) if production_input is not None else None
        if candidate_input is not None and trusted_learning_rules is not None:
            candidate_input["learning_rules"] = trusted_learning_rules
        normalized_input = (
            validate_topic_input(
                candidate_input,
                allow_learning_rules=trusted_learning_rules is not None,
            )
            if candidate_input is not None
            else None
        )
        if normalized_input is not None:
            normalized_input = dict(normalized_input)
            normalized_input["capability_pack"] = self._trusted_capability_pack(
                normalized_input["capability_pack"]
            )
        timestamp = now_iso()
        safe_creation_request = (
            self._validate_creation_request(creation_request)
            if creation_request is not None
            else None
        )
        job_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        job = {
            "schema_version": 2,
            "id": job_id,
            "status": "planned",
            "created_at": timestamp,
            "updated_at": timestamp,
            "authorized_at": None,
            "plan": safe_plan,
            "production_input": normalized_input,
            "capability_pack": (
                {
                    "id": normalized_input["capability_pack"]["id"],
                    "version": normalized_input["capability_pack"]["version"],
                    "sha256": normalized_input["capability_pack"]["sha256"],
                }
                if normalized_input is not None else None
            ),
            "learning_rule_ids": [
                str(item.get("rule_id", ""))
                for item in (normalized_input or {}).get("learning_rules", [])
                if item.get("rule_id")
            ],
            "approvals": {
                "research": {"status": "pending"},
                "compliance": {"status": "pending"},
            },
            "review_policy": dict(self.review_policy),
            "runs": [],
            "active_run_id": None,
            "current_run_id": None,
            "artifacts": [],
            "last_error": None,
            "last_failed_stage": None,
            "budget": {"limit": 7, "attempted": 0, "succeeded": 0, "failed": 0, "events": []},
            "step_states": [{"id": step.get("id"), "status": "pending"} for step in steps],
        }
        if safe_creation_request is not None:
            job["creation_request"] = safe_creation_request
        folder = self.jobs_dir / job_id
        folder.mkdir(parents=True, exist_ok=False)
        try:
            (folder / "draft").mkdir()
            (folder / "runs").mkdir()
            self._write(folder / "job.json", job)
            self._event(folder, "job_created", {"status": "planned", "schema_version": 2})
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return self._public(job)

    def lookup_creation_replay(self, idempotency_key: str, fingerprint: str) -> dict[str, Any] | None:
        """Resolve a persisted create replay before volatile selection state is read."""
        request = self._validate_creation_request({
            "idempotency_key": idempotency_key,
            "fingerprint": fingerprint,
        })
        with self._create_lock:
            disk_lock = self._acquire_creation_lock(request["idempotency_key"])
            try:
                return self._find_creation_replay(request)
            finally:
                self._release_creation_lock(disk_lock)

    def create_idempotent(
        self,
        plan: dict[str, Any],
        production_input: dict[str, Any],
        *,
        idempotency_key: str,
        fingerprint: str,
        trusted_learning_rules: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        request = self._validate_creation_request({
            "idempotency_key": idempotency_key,
            "fingerprint": fingerprint,
        })
        with self._create_lock:
            disk_lock = self._acquire_creation_lock(request["idempotency_key"])
            try:
                replay = self._find_creation_replay(request)
                if replay is not None:
                    return replay, True
                return self.create(
                    plan,
                    production_input=production_input,
                    creation_request=request,
                    trusted_learning_rules=trusted_learning_rules,
                ), False
            finally:
                self._release_creation_lock(disk_lock)

    def list(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for path in self.jobs_dir.glob("*/job.json"):
            try:
                jobs.append(self._public(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError):
                continue
        jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return jobs[:100]

    def get(self, job_id: str) -> dict[str, Any]:
        job, folder = self._load_raw(job_id)
        if job.get("schema_version") == 2:
            self._recover_stale_lock(job, folder)
        return self._public(job)

    def approve(self, job_id: str) -> dict[str, Any]:
        job, folder = self._load_v2(job_id)
        self._ensure_capability_pack(job, folder)
        if job["status"] != "planned":
            raise ConflictError("只有待授权任务可以批准")
        timestamp = now_iso()
        job["status"] = "authorized"
        job["authorized_at"] = timestamp
        job["updated_at"] = timestamp
        self._write(folder / "job.json", job)
        self._event(folder, "job_authorized", {})
        return self._public(job)

    def run_safe(self, job_id: str, allow_external_commands: bool = False) -> dict[str, Any]:
        job, folder = self._load_v2(job_id)
        if job["status"] != "authorized":
            raise ConflictError("任务必须先授权")
        waiting = 0
        for state, step in zip(job["step_states"], job["plan"].get("steps", [])):
            if step.get("capability") == "human_refinement":
                state["status"] = "waiting_human"
                waiting += 1
            elif not step.get("tool_id") or not allow_external_commands:
                state["status"] = "waiting_adapter"
                waiting += 1
            else:
                state["status"] = "ready"
        job["status"] = "needs_attention" if waiting else "ready"
        job["updated_at"] = now_iso()
        self._write(folder / "job.json", job)
        self._event(folder, "safe_run_checked", {"waiting_steps": waiting, "external_commands_executed": False})
        return self._public(job)

    def advance(self, job_id: str, runner: Any, idempotency_key: str) -> dict[str, Any]:
        if not IDEMPOTENCY_RE.fullmatch(str(idempotency_key or "")):
            raise UnprocessableError("Idempotency-Key必须为8到128位安全字符")
        lock = self._job_lock(job_id)
        if not lock.acquire(blocking=False):
            job, _ = self._load_v2(job_id)
            if any(run.get("idempotency_key") == idempotency_key and run.get("status") == "running" for run in job.get("runs", [])):
                replay = self._public(job)
                replay["replayed"] = True
                return replay
            raise ConflictError("同一任务正在运行")
        lock_path: Path | None = None
        bound_budget = None
        try:
            job, folder = self._load_v2(job_id)
            self._recover_stale_lock(job, folder)
            self._ensure_capability_pack(job, folder)
            replayed = next((run for run in job.get("runs", []) if run.get("idempotency_key") == idempotency_key), None)
            if replayed:
                result = self._public(job)
                result["replayed"] = True
                return result
            stage = self._next_stage(job)
            self._require_current_stage_approvals(job, folder, folder / "draft", stage)
            lock_path = self._acquire_disk_lock(folder, idempotency_key)
            run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
            run_dir = folder / "runs" / run_id
            staging = run_dir / "staging"
            staging.mkdir(parents=True, exist_ok=False)
            try:
                self._copy_draft(folder / "draft", staging)
                # Recheck the exact snapshot the runner will consume. This closes
                # the gap between validating the mutable draft and copying it.
                self._require_current_stage_approvals(job, folder, staging, stage)
            except Exception:
                if run_dir.exists():
                    shutil.rmtree(run_dir)
                raise
            run = {
                "run_id": run_id,
                "stage": stage,
                "status": "running",
                "idempotency_key": idempotency_key,
                "started_at": now_iso(),
                "finished_at": None,
                "error": None,
                "artifacts": [],
            }
            job["runs"].append(run)
            job["active_run_id"] = run_id
            job["status"] = {"research": "research_running", "content": "content_running", "render": "rendering"}[stage]
            job["last_error"] = None
            job["updated_at"] = now_iso()
            self._sync_steps(job)
            self._write(folder / "job.json", job)
            self._event(folder, "stage_started", {"run_id": run_id, "stage": stage})
            bound_budget = getattr(runner, "budget", None)
            if bound_budget is not None:
                def persist_budget(snapshot: dict[str, Any]) -> None:
                    # Budget reservation must reach disk before the Provider
                    # request is allowed to leave the process. This preserves
                    # attempted calls even if the process dies mid-request.
                    job["budget"] = snapshot
                    job["updated_at"] = now_iso()
                    self._write(folder / "job.json", job)

                bound_budget.set_persistence_callback(persist_budget)
            try:
                if stage == "research":
                    runner.run_research_stage(staging, job["production_input"])
                    self._prepare_research(staging / "research.json")
                    self._publish_draft(staging, folder / "draft", ["research.json", "insight.json"])
                    job["approvals"]["research"] = {"status": "pending"}
                    job["approvals"]["compliance"] = {"status": "pending"}
                    job.pop("automatic_research_gate", None)
                    if self._uses_mechanical_stage_review(job):
                        # The reverse mechanical reviewer owns unattended
                        # fallback semantics.  It may approve only an empty,
                        # no-industry-claims scope when strict audit rejects all
                        # web findings; it never promotes those rejected facts.
                        self._apply_mechanical_research_review(
                            job, folder, folder / "draft" / "research.json"
                        )
                    elif not self._apply_strict_rejection(
                        job, folder, folder / "draft" / "research.json"
                    ):
                        job["status"] = "awaiting_research_approval"
                elif stage == "content":
                    runner.run_content_stage(staging, job["production_input"], job["approvals"]["research"])
                    self._publish_draft(staging, folder / "draft", [
                        "research.json", "insight.json", "script_variants.json", "approved_script.json",
                        "review.json", "motion_storyboard.json",
                    ])
                    review = json.loads((staging / "review.json").read_text(encoding="utf-8"))
                    job["approvals"]["compliance"] = {"status": "pending"}
                    job.pop("automatic_content_gate", None)
                    job["status"] = "blocked_compliance" if review.get("status") == "blocked" else "awaiting_compliance_approval"
                    if self._uses_mechanical_stage_review(job):
                        self._apply_mechanical_compliance_review(job, folder)
                else:
                    render_result = runner.run_render_stage(staging, job["production_input"], job["approvals"])
                    if render_result_is_diagnostic(render_result):
                        raise RuntimeError("诊断渲染不能发布为正式成功产物")
                    self._write(staging / "approvals.json", job["approvals"])
                    manifest = self._build_manifest(job, run, staging, runner)
                    self._write(staging / "manifest.json", manifest)
                    published = run_dir / "artifacts"
                    staging.replace(published)
                    run["artifacts"] = [item["name"] for item in manifest["artifacts"]]
                    run["manifest_sha256"] = file_sha256(published / "manifest.json")
                    job["current_run_id"] = run_id
                    job["artifacts"] = [name for name in PUBLIC_ARTIFACTS if (published / name).is_file()]
                    job["status"] = "complete"
                if stage != "render":
                    manifest = self._build_manifest(job, run, staging, runner)
                    self._write(staging / "manifest.json", manifest)
                    published = run_dir / "artifacts"
                    staging.replace(published)
                    run["artifacts"] = [item["name"] for item in manifest["artifacts"]]
                    run["manifest_sha256"] = file_sha256(published / "manifest.json")
                run["status"] = "complete"
                run["finished_at"] = now_iso()
                job["last_failed_stage"] = None
                self._event(folder, "stage_completed", {"run_id": run_id, "stage": stage})
            except Exception as exc:
                run["status"] = "failed"
                run["finished_at"] = now_iso()
                run["error"] = str(exc)
                if staging.exists():
                    failed = run_dir / "failed"
                    if failed.exists():
                        shutil.rmtree(failed)
                    staging.replace(failed)
                workflow_status = getattr(exc, "workflow_status", None)
                job["status"] = workflow_status if workflow_status in {"awaiting_script_revision"} else "failed"
                job["last_error"] = str(exc)
                job["last_failed_stage"] = None if workflow_status else stage
                self._event(folder, "stage_failed", {"run_id": run_id, "stage": stage, "error": str(exc)})
                raise
            finally:
                job["active_run_id"] = None
                if getattr(runner, "budget", None) is not None:
                    job["budget"] = runner.budget.snapshot()
                job["updated_at"] = now_iso()
                self._sync_steps(job)
                self._write(folder / "job.json", job)
            return self._public(job)
        finally:
            if bound_budget is not None:
                bound_budget.set_persistence_callback(None)
            if lock_path is not None and lock_path.exists():
                lock_path.unlink()
            lock.release()

    def advance_automatically(
        self,
        job_id: str,
        runner_factory: Callable[[dict[str, Any]], Any],
        idempotency_key: str,
        *,
        max_stage_attempts: int = 8,
    ) -> dict[str, Any]:
        """Run a mechanical-review job until completion or a safe stop state.

        Each stage keeps its own durable idempotency key and immutable run
        record.  Research/content rejection never gets bypassed: the loop only
        continues from states that the reverse mechanical reviewer explicitly
        advanced.  A bounded retry count prevents weak generated content from
        spinning forever.
        """

        if not IDEMPOTENCY_RE.fullmatch(str(idempotency_key or "")):
            raise UnprocessableError("Idempotency-Key必须为8到128位安全字符")
        if not 1 <= int(max_stage_attempts) <= 16:
            raise UnprocessableError("自动推进次数必须在1到16之间")
        job = self.get(job_id)
        policy = normalize_review_policy(job.get("review_policy"))
        if policy["stage_review_mode"] != MECHANICAL_STAGE_REVIEW:
            return self.advance(job_id, runner_factory(job), idempotency_key)

        runnable = {
            "authorized",
            "research_approved",
            "compliance_approved",
            "awaiting_script_revision",
        }

        def automatically_runnable(candidate: dict[str, Any]) -> bool:
            if candidate.get("status") in runnable:
                return True
            return (
                candidate.get("status") == "failed"
                and candidate.get("last_failed_stage") in {"research", "content", "render"}
            )

        attempts = 0
        while automatically_runnable(job) and attempts < max_stage_attempts:
            stage_fingerprint = {
                "controller_key": idempotency_key,
                "job_id": job_id,
                "status": job.get("status"),
                "run_count": len(job.get("runs", [])),
            }
            stage_key = "mechanical-" + canonical_sha256(stage_fingerprint).lower()
            try:
                job = self.advance(job_id, runner_factory(job), stage_key)
            except Exception as exc:
                # Render can prove that an otherwise compliant script is too
                # short/long for natural portable speech.  The stage runner
                # records this as awaiting_script_revision before re-raising;
                # an unattended controller must ask for a fresh content
                # candidate rather than wait for a browser edit.
                job = self.get(job_id)
                attempts += 1
                if (
                    getattr(exc, "workflow_status", None) == "awaiting_script_revision"
                    and job.get("status") == "awaiting_script_revision"
                ):
                    continue
                raise
            attempts += 1
            if job.get("status") == "complete":
                break

        controller_status = "complete" if job.get("status") == "complete" else (
            "retry_limit_reached"
            if automatically_runnable(job) and attempts >= max_stage_attempts
            else "safe_stop"
        )
        result = dict(job)
        result["automatic_controller"] = {
            "mode": "mechanical",
            "status": controller_status,
            "stage_attempts": attempts,
            "maximum_stage_attempts": max_stage_attempts,
            "human_intervention_required_during_generation": False,
        }
        return result

    def approve_research(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        job, folder = self._load_v2(job_id)
        self._ensure_capability_pack(job, folder)
        self._require_interactive_stage_review(job)
        if job["status"] not in {"awaiting_research_approval", "awaiting_research_revision"}:
            raise ConflictError("当前任务不在研究审批阶段")
        research_path = folder / "draft" / "research.json"
        research = json.loads(research_path.read_text(encoding="utf-8"))
        digest = file_sha256(research_path)
        if payload.get("artifact_sha256") != digest:
            raise ConflictError("研究文件已经变化，请刷新后重新审批")
        identity = self._review_identity(job, payload)
        reviewer = identity["reviewer"]
        decision = str(payload.get("decision", ""))
        if decision not in {"approved", "rejected"}:
            raise UnprocessableError("decision必须是approved或rejected")
        research_findings = research.get("findings", [])
        if not isinstance(research_findings, list):
            raise UnprocessableError("研究finding结构无效")
        eligible = {str(item.get("finding_id")) for item in research_findings if isinstance(item, dict) and item.get("auto_review_status") == "eligible"}
        finding_decisions = payload.get("findings")
        if not isinstance(finding_decisions, list):
            raise UnprocessableError("研究审批必须逐条提交finding决定")
        submitted: dict[str, dict[str, Any]] = {}
        for item in finding_decisions:
            if not isinstance(item, dict) or str(item.get("finding_id")) not in eligible:
                raise UnprocessableError("研究审批包含未知finding_id")
            item_decision = str(item.get("decision", ""))
            evidence_type = str(item.get("evidence_type", ""))
            if item_decision not in {"approved", "rejected"} or evidence_type not in {"verbatim", "paraphrase"}:
                raise UnprocessableError("每条finding必须给出决定和verbatim/paraphrase证据类型")
            submitted[str(item["finding_id"])] = {
                "finding_id": str(item["finding_id"]),
                "decision": item_decision,
                "evidence_type": evidence_type,
                "note": str(item.get("note", "")).strip()[:500],
            }
        if set(submitted) != eligible:
            raise UnprocessableError("所有可入脚本finding都必须逐条审批")
        empty_finding_confirmation = False
        if decision == "approved" and not eligible:
            if research_findings or str(research.get("status", "")) not in {"offline", "disabled"}:
                raise UnprocessableError("只有明确离线或禁用调研且finding为空时，才可确认使用本地安全模板")
            if finding_decisions:
                raise UnprocessableError("无finding的本地安全模板确认不得提交finding决定")
            empty_finding_confirmation = True
        note = (
            EMPTY_RESEARCH_APPROVAL_NOTE
            if empty_finding_confirmation
            else str(payload.get("note", "")).strip()[:1000]
        )
        self._require_agent_test_note(identity, note)
        record = {
            "status": decision,
            **identity,
            "reviewed_at": now_iso(),
            "artifact_sha256": digest,
            "note": note,
            "findings": list(submitted.values()),
        }
        if empty_finding_confirmation:
            record["empty_finding_confirmation"] = {
                "research_status": str(research.get("status")),
                "content_scope": "local_safe_template_without_industry_fact_claims",
            }
        job["approvals"]["research"] = record
        job["approvals"]["compliance"] = {"status": "pending"}
        job["status"] = "research_approved" if decision == "approved" else "awaiting_research_revision"
        job["updated_at"] = now_iso()
        self._write(folder / "job.json", job)
        self._event(folder, "research_reviewed", {
            "decision": decision,
            "reviewer": reviewer,
            "review_mode": identity["review_mode"],
            "artifact_sha256": digest,
        })
        return self._public(job)

    def approve_compliance(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        job, folder = self._load_v2(job_id)
        self._ensure_capability_pack(job, folder)
        self._require_interactive_stage_review(job)
        if job["status"] not in {"awaiting_compliance_approval", "awaiting_script_revision"}:
            raise ConflictError("当前任务不在合规审批阶段")
        draft = folder / "draft"
        review_path = draft / "review.json"
        script_path = draft / "approved_script.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        if review.get("status") == "blocked" or review.get("blocked"):
            raise UnprocessableError("脚本仍被自动合规规则阻断，不能人工放行")
        review_digest = file_sha256(review_path)
        script_digest = file_sha256(script_path)
        if payload.get("artifact_sha256") != review_digest or payload.get("script_sha256") != script_digest:
            raise ConflictError("脚本或合规文件已经变化，请刷新后重新审批")
        identity = self._review_identity(job, payload)
        reviewer = identity["reviewer"]
        decision = str(payload.get("decision", ""))
        if decision not in {"approved", "rejected"}:
            raise UnprocessableError("decision必须是approved或rejected")
        note = str(payload.get("note", "")).strip()[:1000]
        self._require_agent_test_note(identity, note)
        job["approvals"]["compliance"] = {
            "status": decision,
            **identity,
            "reviewed_at": now_iso(),
            "artifact_sha256": review_digest,
            "script_sha256": script_digest,
            "note": note,
        }
        job["status"] = "compliance_approved" if decision == "approved" else "awaiting_script_revision"
        job["updated_at"] = now_iso()
        self._write(folder / "job.json", job)
        self._event(folder, "compliance_reviewed", {
            "decision": decision,
            "reviewer": reviewer,
            "review_mode": identity["review_mode"],
            "artifact_sha256": review_digest,
        })
        return self._public(job)

    @staticmethod
    def _uses_mechanical_stage_review(job: dict[str, Any]) -> bool:
        try:
            return (
                normalize_review_policy(job.get("review_policy"))["stage_review_mode"]
                == MECHANICAL_STAGE_REVIEW
            )
        except ValueError as exc:
            raise UnprocessableError("任务审查策略无效") from exc

    def _require_interactive_stage_review(self, job: dict[str, Any]) -> None:
        if self._uses_mechanical_stage_review(job):
            raise ConflictError("机械审查由服务器在阶段完成时自动执行，浏览器不得代签")

    @staticmethod
    def _mechanical_finding_errors(finding: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        finding_id = str(finding.get("finding_id", "")).strip() or "unknown"
        if finding.get("script_eligible") is not True or finding.get("auto_review_status") != "eligible":
            errors.append(f"{finding_id}: finding未进入严格可用集合")
        if str(finding.get("strict_review_status", "")).strip().lower() not in MECHANICAL_STRICT_FINDING_STATUSES:
            errors.append(f"{finding_id}: 缺少严格通过状态")
        evidence = finding.get("evidence")
        if not isinstance(evidence, list) or not any(
            isinstance(row, dict)
            and str(row.get("url", "")).strip()
            and str(row.get("excerpt", row.get("quote", ""))).strip()
            for row in evidence
        ):
            errors.append(f"{finding_id}: 缺少可回指网址与摘录")
        if not isinstance(finding.get("limitations"), list) or not any(
            str(value).strip() for value in finding.get("limitations", [])
        ):
            errors.append(f"{finding_id}: 缺少适用边界")
        if not str(finding.get("allowed_use", "")).strip():
            errors.append(f"{finding_id}: 缺少允许用法")
        if not str(finding.get("prohibited_use", "")).strip():
            errors.append(f"{finding_id}: 缺少禁止外推项")
        return errors

    def _apply_mechanical_research_review(
        self,
        job: dict[str, Any],
        folder: Path,
        research_path: Path,
    ) -> bool:
        """Approve only evidence-complete research without manufacturing a human action."""

        research = json.loads(research_path.read_text(encoding="utf-8"))
        findings = research.get("findings")
        if not isinstance(findings, list):
            raise UnprocessableError("研究finding结构无效")
        eligible = [
            item
            for item in findings
            if isinstance(item, dict) and item.get("auto_review_status") == "eligible"
        ]
        errors = [
            error
            for finding in eligible
            for error in self._mechanical_finding_errors(finding)
        ]
        empty_research = (
            not findings
            and str(research.get("status", "")) in {"offline", "disabled", "partial", "failed"}
        )
        # A continuously running mechanical workflow must not wait for a person
        # merely because every web finding failed the strict evidence gate.  It
        # may continue only by approving an empty fact set and forcing the
        # content stage onto the local no-industry-claims safety template.  This
        # is deliberately narrower than approving any rejected finding.
        excluded_only = (
            bool(findings)
            and str(research.get("status", "")) in {"partial", "complete"}
            and all(
                isinstance(item, dict)
                and item.get("auto_review_status") == "excluded"
                and item.get("script_eligible") is False
                for item in findings
            )
        )
        empty_safe_scope = empty_research or excluded_only
        if not eligible and not empty_safe_scope:
            errors.append("研究没有可由机械审核批准的严格finding")
        digest = file_sha256(research_path)
        if errors:
            job["automatic_research_gate"] = {
                "engine": "reverse_mechanical_reviewer",
                "decision": "rejected",
                "evaluated_at": now_iso(),
                "artifact_sha256": digest,
                "reason": "; ".join(errors)[:2000],
            }
            job["approvals"]["research"] = {"status": "pending"}
            job["approvals"]["compliance"] = {"status": "pending"}
            job["status"] = "awaiting_research_revision"
            self._event(folder, "research_auto_rejected", {
                "artifact_sha256": digest,
                "engine": "reverse_mechanical_reviewer",
                "reason_count": len(errors),
            })
            return False

        identity = approval_identity(MECHANICAL_STAGE_REVIEW, MECHANICAL_REVIEWER)
        record = {
            "status": "approved",
            **identity,
            "reviewed_at": now_iso(),
            "artifact_sha256": digest,
            "note": EMPTY_RESEARCH_APPROVAL_NOTE if empty_safe_scope else MECHANICAL_RESEARCH_APPROVAL_NOTE,
            "findings": [
                {
                    "finding_id": str(item["finding_id"]),
                    "decision": "approved",
                    "evidence_type": "paraphrase",
                    "note": "机械审核仅允许在原证据边界内改写，不允许扩大结论",
                }
                for item in eligible
            ],
        }
        if empty_safe_scope:
            record["empty_finding_confirmation"] = {
                "research_status": str(research.get("status")),
                "content_scope": "local_safe_template_without_industry_fact_claims",
                "excluded_finding_count": len(findings) if excluded_only else 0,
            }
            if excluded_only:
                record["automatic_fallback"] = {
                    "reason": "all_research_findings_failed_strict_evidence_gate",
                    "original_finding_count": len(findings),
                    "approved_finding_count": 0,
                }
        job["approvals"]["research"] = record
        job["approvals"]["compliance"] = {"status": "pending"}
        job.pop("automatic_research_gate", None)
        job["status"] = "research_approved"
        self._event(folder, "research_reviewed", {
            "decision": "approved",
            "reviewer": MECHANICAL_REVIEWER,
            "review_mode": "mechanical",
            "artifact_sha256": digest,
        })
        return True

    def _apply_mechanical_compliance_review(
        self, job: dict[str, Any], folder: Path
    ) -> bool:
        """Advance only a warning-free, evidence-bound generated script."""

        draft = folder / "draft"
        review_path = draft / "review.json"
        script_path = draft / "approved_script.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        approved_script = json.loads(script_path.read_text(encoding="utf-8"))
        errors: list[str] = []
        if review.get("status") != "passed" or review.get("blocked") is not False:
            errors.append("本地合规审查未明确通过")
        warnings = review.get("warnings")
        if not isinstance(warnings, list) or warnings:
            errors.append("本地合规审查仍含警告")
        if not str(approved_script.get("script", "")).strip():
            errors.append("批准稿正文为空")
        edit_mode, edit_errors = classify_script_edit_record(
            approved_script, allow_legacy_human=False
        )
        errors.extend(edit_errors)
        if edit_mode not in {None, MECHANICAL_STAGE_REVIEW}:
            errors.append("批准稿包含非机械流程的显式改稿身份")

        review_digest = file_sha256(review_path)
        script_digest = file_sha256(script_path)
        if errors:
            job["automatic_content_gate"] = {
                "engine": "reverse_mechanical_reviewer",
                "decision": "rejected",
                "evaluated_at": now_iso(),
                "artifact_sha256": script_digest,
                "review_sha256": review_digest,
                "reason": "; ".join(errors)[:2000],
            }
            job["approvals"]["compliance"] = {"status": "pending"}
            # Return to content generation so the unattended controller can
            # request a new isolated candidate under its retry/budget limits.
            job["status"] = "research_approved"
            self._event(folder, "content_auto_rejected", {
                "artifact_sha256": script_digest,
                "engine": "reverse_mechanical_reviewer",
                "reason_count": len(errors),
            })
            return False

        identity = approval_identity(MECHANICAL_STAGE_REVIEW, MECHANICAL_REVIEWER)
        job["approvals"]["compliance"] = {
            "status": "approved",
            **identity,
            "reviewed_at": now_iso(),
            "artifact_sha256": review_digest,
            "script_sha256": script_digest,
            "note": MECHANICAL_COMPLIANCE_APPROVAL_NOTE,
        }
        job.pop("automatic_content_gate", None)
        job["status"] = "compliance_approved"
        self._event(folder, "compliance_reviewed", {
            "decision": "approved",
            "reviewer": MECHANICAL_REVIEWER,
            "review_mode": "mechanical",
            "artifact_sha256": review_digest,
        })
        return True

    def invalidate_pending_content(self, job_id: str, reason: str) -> dict[str, Any]:
        """Reject an unapproved generated script without manufacturing a human decision."""
        job, folder = self._load_v2(job_id)
        if job["status"] not in {"awaiting_compliance_approval", "blocked_compliance", "awaiting_script_revision"}:
            raise ConflictError("当前任务没有可由自动审核撤销的待审脚本")
        if job.get("approvals", {}).get("compliance", {}).get("status") == "approved":
            raise ConflictError("已有人工作出合规批准，自动审核不能覆盖")
        script_path = folder / "draft" / "approved_script.json"
        job["automatic_content_gate"] = {
            "engine": "evidence_binding_validator",
            "decision": "rejected",
            "evaluated_at": now_iso(),
            "artifact_sha256": file_sha256(script_path) if script_path.is_file() else None,
            "reason": str(reason).strip()[:1000],
        }
        job["approvals"]["compliance"] = {"status": "pending"}
        job["status"] = "research_approved"
        job["updated_at"] = now_iso()
        self._sync_steps(job)
        self._write(folder / "job.json", job)
        self._event(folder, "content_auto_rejected", {
            "engine": "evidence_binding_validator",
            "artifact_sha256": job["automatic_content_gate"]["artifact_sha256"],
        })
        return self._public(job)

    def prepare_render_retry(self, job_id: str, reason: str) -> dict[str, Any]:
        """Schedule a new isolated render from an already approved completed job."""
        job, folder = self._load_v2(job_id)
        if job.get("status") != "complete" or not job.get("current_run_id"):
            raise ConflictError("只有已有成功产物的完成任务可以重新渲染")
        current_run_id = str(job["current_run_id"])
        manifest_path = folder / "runs" / current_run_id / "artifacts" / "manifest.json"
        if not manifest_path.is_file():
            raise ConflictError("当前成功运行缺少manifest，不能作为重渲染基线")
        job["automatic_render_retry"] = {
            "engine": "delivery_validation",
            "decision": "retry",
            "evaluated_at": now_iso(),
            "source_run_id": current_run_id,
            "source_manifest_sha256": file_sha256(manifest_path),
            "reason": str(reason).strip()[:1000],
        }
        job["status"] = "compliance_approved"
        job["last_error"] = None
        job["last_failed_stage"] = None
        job["updated_at"] = now_iso()
        self._sync_steps(job)
        self._write(folder / "job.json", job)
        self._event(folder, "render_retry_scheduled", {
            "source_run_id": current_run_id,
            "source_manifest_sha256": job["automatic_render_retry"]["source_manifest_sha256"],
        })
        return self._public(job)

    def rebuild_successful_delivery(
        self,
        job_id: str,
        runner: Any,
        idempotency_key: str,
        reason: str,
    ) -> dict[str, Any]:
        """Publish a new immutable run by reusing verified media and rebuilding metadata only."""
        if not IDEMPOTENCY_RE.fullmatch(str(idempotency_key or "")):
            raise UnprocessableError("Idempotency-Key必须为8到128位安全字符")
        lock = self._job_lock(job_id)
        if not lock.acquire(blocking=False):
            raise ConflictError("同一任务正在运行")
        lock_path: Path | None = None
        try:
            job, folder = self._load_v2(job_id)
            self._recover_stale_lock(job, folder)
            replayed = next((run for run in job.get("runs", []) if run.get("idempotency_key") == idempotency_key), None)
            if replayed:
                result = self._public(job)
                result["replayed"] = True
                return result
            source_run_id = str(job.get("current_run_id") or "")
            source_dir = folder / "runs" / source_run_id / "artifacts"
            source_manifest_path = source_dir / "manifest.json"
            if not source_run_id or not source_manifest_path.is_file():
                raise ConflictError("没有可用于报告重建的成功运行")
            source_run = next(
                (
                    item for item in job.get("runs", [])
                    if item.get("run_id") == source_run_id
                    and item.get("status") == "complete"
                    and item.get("stage") in {"render", "report_rebuild"}
                ),
                None,
            )
            if not source_run:
                raise ConflictError("报告重建来源不是成功发布的正式运行")
            source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
            if (
                source_manifest.get("schema_version") != 2
                or source_manifest.get("job_id") != job_id
                or source_manifest.get("run_id") != source_run_id
                or source_manifest.get("status") != "complete"
                or source_manifest.get("stage") not in {"render", "report_rebuild"}
            ):
                raise ConflictError("成功运行manifest身份或状态无效")
            source_items = source_manifest.get("artifacts", [])
            if not isinstance(source_items, list) or not source_items:
                raise ConflictError("成功运行manifest没有可复用产物")
            allowed_names = set(PUBLIC_ARTIFACTS) | {"approvals.json"}
            required_names = set(CANONICAL_ARTIFACTS) | {"approvals.json"}
            seen_names: set[str] = set()
            for item in source_items:
                if not isinstance(item, dict):
                    raise ConflictError("成功运行manifest格式无效")
                name = str(item.get("name", ""))
                if name not in allowed_names or name in seen_names:
                    raise ConflictError(f"成功运行manifest含非正式或重复产物: {name}")
                seen_names.add(name)
                source_path = source_dir / name
                if (
                    not source_path.is_file()
                    or source_path.stat().st_size != int(item.get("size", -1))
                    or file_sha256(source_path) != item.get("sha256")
                ):
                    raise ConflictError(f"成功运行产物校验失败: {item.get('name', '')}")
            if not required_names.issubset(seen_names):
                raise ConflictError("成功运行manifest正式产物集合不完整")

            lock_path = self._acquire_disk_lock(folder, idempotency_key)
            run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
            run_dir = folder / "runs" / run_id
            staging = run_dir / "staging"
            staging.mkdir(parents=True, exist_ok=False)
            run = {
                "run_id": run_id,
                "stage": "report_rebuild",
                "status": "running",
                "idempotency_key": idempotency_key,
                "started_at": now_iso(),
                "finished_at": None,
                "error": None,
                "artifacts": [],
                "source_run_id": source_run_id,
                "reason": str(reason).strip()[:1000],
            }
            job["runs"].append(run)
            job["active_run_id"] = run_id
            job["status"] = "rendering"
            job["last_error"] = None
            job["updated_at"] = now_iso()
            self._write(folder / "job.json", job)
            self._event(folder, "stage_started", {"run_id": run_id, "stage": "report_rebuild", "source_run_id": source_run_id})
            try:
                for item in source_items:
                    shutil.copy2(source_dir / str(item["name"]), staging / str(item["name"]))
                visual_qc_stage = getattr(runner, "run_visual_qc_stage", None)
                if not callable(visual_qc_stage):
                    raise RuntimeError("报告重建缺少正式成片视觉门禁")
                visual_qc_stage(staging)
                runner.rebuild_run_report(staging, job["approvals"])
                self._write(staging / "approvals.json", job["approvals"])
                manifest = self._build_manifest(job, run, staging, runner)
                self._write(staging / "manifest.json", manifest)
                published = run_dir / "artifacts"
                staging.replace(published)
                run["artifacts"] = [item["name"] for item in manifest["artifacts"]]
                run["manifest_sha256"] = file_sha256(published / "manifest.json")
                run["status"] = "complete"
                run["finished_at"] = now_iso()
                job["current_run_id"] = run_id
                job["artifacts"] = [name for name in PUBLIC_ARTIFACTS if (published / name).is_file()]
                job["status"] = "complete"
                job["last_failed_stage"] = None
                job.pop("automatic_render_retry", None)
                self._event(folder, "stage_completed", {"run_id": run_id, "stage": "report_rebuild", "source_run_id": source_run_id})
            except Exception as exc:
                run["status"] = "failed"
                run["finished_at"] = now_iso()
                run["error"] = str(exc)
                if staging.exists():
                    staging.replace(run_dir / "failed")
                job["status"] = "failed"
                job["last_error"] = str(exc)
                job["last_failed_stage"] = "report_rebuild"
                self._event(folder, "stage_failed", {"run_id": run_id, "stage": "report_rebuild", "error": str(exc)})
                raise
            finally:
                job["active_run_id"] = None
                if getattr(runner, "budget", None) is not None:
                    job["budget"] = runner.budget.snapshot()
                job["updated_at"] = now_iso()
                self._sync_steps(job)
                self._write(folder / "job.json", job)
            return self._public(job)
        finally:
            if lock_path is not None and lock_path.exists():
                lock_path.unlink()
            lock.release()

    def update_script(
        self,
        job_id: str,
        script: str,
        review: dict[str, Any],
        estimate: dict[str, Any],
        editor: str,
    ) -> dict[str, Any]:
        job, folder = self._load_v2(job_id)
        self._ensure_capability_pack(job, folder)
        if job["status"] not in {"awaiting_compliance_approval", "blocked_compliance", "awaiting_script_revision", "compliance_approved", "complete", "failed"}:
            raise ConflictError("当前阶段不能修改脚本")
        draft = folder / "draft"
        if not (draft / "research.json").is_file():
            raise ConflictError("尚未完成研究和内容阶段")
        try:
            policy = normalize_review_policy(job.get("review_policy"))
            if policy["stage_review_mode"] == MECHANICAL_STAGE_REVIEW:
                raise ConflictError("机械审查任务禁止浏览器改稿；脚本必须由受管无头流程生成")
            editor_identity = script_edit_identity(
                policy["stage_review_mode"], str(editor)
            )
        except ConflictError:
            raise
        except ValueError as exc:
            raise UnprocessableError(str(exc)) from exc
        payload = {
            **BROWSER_SCRIPT_EDIT_LABELS,
            "script": str(script).strip(),
            "editor_identity": editor_identity,
            "edited_at": now_iso(),
            "duration_estimate": estimate,
        }
        self._write(draft / "approved_script.json", payload)
        self._write(draft / "review.json", review)
        # A storyboard is bound to the exact approved script.  Never let a
        # browser edit inherit directions generated for older narration.
        (draft / "motion_storyboard.json").unlink(missing_ok=True)
        job["approvals"]["compliance"] = {"status": "pending"}
        job["status"] = "blocked_compliance" if review.get("status") == "blocked" else "awaiting_compliance_approval"
        job["updated_at"] = now_iso()
        self._write(folder / "job.json", job)
        self._event(folder, "script_updated", {"character_count": len(payload["script"]), "estimated_seconds": estimate.get("estimated_seconds")})
        return self._public(job)

    def apply_learning_rules(
        self,
        job_id: str,
        rules: list[dict[str, Any]],
        reason: str,
        correction_kind: str = "content",
    ) -> dict[str, Any]:
        """Apply a human correction without overwriting the last successful run.

        Corrections are instruction snapshots, not executable code. Research
        approval remains valid because these rules only steer content unless a
        later UI explicitly requests an industry/profile rebuild.
        """
        if correction_kind not in {"style", "content", "evidence", "capability", "process"}:
            raise UnprocessableError("纠错类型无效")
        if correction_kind == "capability":
            # A rule cannot rewrite an immutable capability-pack snapshot.  The
            # HTTP correction path records this for a newly created task and
            # deliberately never calls this mutating method for capability.
            raise UnprocessableError("能力包纠错不能修改当前任务；请通过 /api/agent/topics 创建带新能力包的任务")
        job, folder = self._load_v2(job_id)
        self._ensure_capability_pack(job, folder)
        if job.get("status") in RUNNING_STATES:
            raise ConflictError("任务正在运行；实时打断将在交互层启用后使用，当前请等待本阶段结束")
        production_input = dict(job.get("production_input") or {})
        previous_status = str(job.get("status", ""))
        candidate = dict(production_input)
        candidate["learning_rules"] = rules
        normalized = preserve_legacy_footage_contract(
            production_input,
            validate_topic_input(candidate, allow_learning_rules=True),
        )
        job["production_input"] = normalized
        job["learning_rule_ids"] = [
            str(item.get("rule_id", "")) for item in normalized.get("learning_rules", []) if item.get("rule_id")
        ]
        job["approvals"]["compliance"] = {"status": "pending"}
        research_approved = job.get("approvals", {}).get("research", {}).get("status") == "approved"
        if correction_kind == "evidence":
            # A worker saying that a source, number, report or fact is wrong is
            # stronger than a style preference.  The old finding decisions and
            # every downstream approval immediately lose authority.
            job["approvals"]["research"] = {"status": "pending"}
            job["status"] = "planned" if previous_status == "planned" else "authorized"
            job["revision_required"] = {
                "kind": "evidence",
                "reason": str(reason).strip()[:1000],
                "recorded_at": now_iso(),
            }
        elif previous_status == "planned":
            # A correction is never execution authorization.
            job["status"] = "planned"
        elif previous_status in {"awaiting_research_approval", "awaiting_research_revision"}:
            # Content/style learning does not rewrite an unchanged research file.
            job["status"] = previous_status
        elif research_approved:
            job["status"] = "research_approved"
        else:
            job["status"] = "authorized"
        job["last_error"] = None
        job["last_failed_stage"] = None
        job["updated_at"] = now_iso()
        self._sync_steps(job)
        self._write(folder / "job.json", job)
        self._event(folder, "learning_rules_applied", {
            "rule_ids": job["learning_rule_ids"],
            "correction_kind": correction_kind,
            "reason": str(reason).strip()[:1000],
            "previous_success_run_preserved": job.get("current_run_id"),
        })
        return self._public(job)

    def approved_findings(self, job_id: str) -> list[dict[str, Any]]:
        job, folder = self._load_v2(job_id)
        approved_ids = {
            str(item.get("finding_id"))
            for item in job.get("approvals", {}).get("research", {}).get("findings", [])
            if item.get("decision") == "approved"
        }
        research_path = folder / "draft" / "research.json"
        if not research_path.is_file():
            return []
        research = json.loads(research_path.read_text(encoding="utf-8"))
        return [item for item in research.get("findings", []) if isinstance(item, dict) and str(item.get("finding_id")) in approved_ids]

    def resolve_artifact(self, job_id: str, name: str, run_id: str | None = None) -> Path:
        if name not in set(PUBLIC_ARTIFACTS) | {"manifest.json", "approvals.json"}:
            raise WorkflowError("产物类型不允许")
        job, folder = self._load_raw(job_id)
        if job.get("schema_version") != 2:
            raise FileNotFoundError("旧任务仅供历史状态查看，正式产物接口不再暴露旧报告")
        selected = run_id or job.get("current_run_id")
        if not selected:
            raise FileNotFoundError("尚无成功运行产物")
        run = next((item for item in job.get("runs", []) if item.get("run_id") == selected and item.get("status") == "complete"), None)
        if not run or run.get("stage") not in {"render", "report_rebuild"}:
            raise FileNotFoundError("运行不存在或尚未成功发布")
        root = (folder / "runs" / selected / "artifacts").resolve()
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        allowed = {item.get("name") for item in manifest.get("artifacts", [])} | {"manifest.json"}
        if name not in allowed:
            raise FileNotFoundError("该产物不在成功运行清单中")
        target = (root / name).resolve()
        if target.parent != root or not target.is_file():
            raise FileNotFoundError("产物不存在")
        return target

    def resolve_public_evidence_source(self, job_id: str) -> dict[str, Any]:
        """Resolve only the server-owned current successful run for export."""

        job, folder = self._load_raw(job_id)
        if job.get("schema_version") != 2 or job.get("id") != job_id:
            raise FileNotFoundError("旧任务或无效任务不能导出公开证据")
        selected = job.get("current_run_id")
        if not isinstance(selected, str) or not selected or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for char in selected
        ):
            raise FileNotFoundError("尚无当前成功运行可供导出")
        run = next(
            (
                item
                for item in job.get("runs", [])
                if isinstance(item, dict)
                and item.get("run_id") == selected
                and item.get("status") == "complete"
                and item.get("stage") in {"render", "report_rebuild"}
            ),
            None,
        )
        if run is None:
            raise FileNotFoundError("当前运行尚未成功发布，不能导出公开证据")
        root = (folder / "runs" / selected / "artifacts").resolve()
        expected_parent = (folder / "runs" / selected).resolve()
        if root.parent != expected_parent or not root.is_dir():
            raise FileNotFoundError("当前成功运行产物不存在")
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError("当前成功运行缺少清单")
        manifest_sha256 = file_sha256(manifest_path)
        if run.get("manifest_sha256") != manifest_sha256:
            raise ConflictError("当前成功运行清单与发布记录不一致，已拒绝导出")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConflictError("当前成功运行清单无法验证，已拒绝导出") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 2
            or manifest.get("job_id") != job_id
            or manifest.get("run_id") != selected
            or manifest.get("status") != "complete"
            or manifest.get("stage") not in {"render", "report_rebuild"}
        ):
            raise ConflictError("当前成功运行清单身份无效，已拒绝导出")
        return {
            "job_id": job_id,
            "run_id": selected,
            "source": root,
            "source_manifest_sha256": manifest_sha256,
        }

    def resolve_review_artifact(self, job_id: str, name: str) -> Path:
        if name not in REVIEW_ARTIFACTS:
            raise WorkflowError("只允许查看待审研究、脚本和合规文件")
        job, folder = self._load_v2(job_id)
        target = (folder / "draft" / name).resolve()
        if target.parent != (folder / "draft").resolve() or not target.is_file():
            raise FileNotFoundError("待审产物不存在")
        return target

    def _require_current_stage_approvals(
        self,
        job: dict[str, Any],
        folder: Path,
        snapshot: Path,
        stage: str,
    ) -> None:
        """Bind every gated stage to the exact files a human approved."""

        approvals = job.setdefault("approvals", {})
        if stage in {"content", "render"}:
            research = approvals.get("research", {})
            research_path = snapshot / "research.json"
            research_matches = (
                research.get("status") == "approved"
                and research_path.is_file()
                and research.get("artifact_sha256") == file_sha256(research_path)
            )
            if not research_matches:
                approvals["research"] = {"status": "pending"}
                approvals["compliance"] = {"status": "pending"}
                job["status"] = "awaiting_research_approval"
                job["last_error"] = None
                job["updated_at"] = now_iso()
                self._sync_steps(job)
                self._write(folder / "job.json", job)
                self._event(folder, "approval_invalidated", {"gate": "research"})
                raise ConflictError("研究文件已变化，原阶段审查已失效，请重新审核")

        if stage == "render":
            compliance = approvals.get("compliance", {})
            review_path = snapshot / "review.json"
            script_path = snapshot / "approved_script.json"
            compliance_matches = (
                compliance.get("status") == "approved"
                and review_path.is_file()
                and script_path.is_file()
                and compliance.get("artifact_sha256") == file_sha256(review_path)
                and compliance.get("script_sha256") == file_sha256(script_path)
            )
            if not compliance_matches:
                approvals["compliance"] = {"status": "pending"}
                job["status"] = "awaiting_compliance_approval"
                job["last_error"] = None
                job["updated_at"] = now_iso()
                self._sync_steps(job)
                self._write(folder / "job.json", job)
                self._event(folder, "approval_invalidated", {"gate": "compliance"})
                raise ConflictError("脚本或合规文件已变化，原阶段审查已失效，请重新审核")

    def _next_stage(self, job: dict[str, Any]) -> str:
        status = job.get("status")
        if status in {"authorized", "awaiting_research_revision"}:
            return "research"
        if status in {"research_approved", "awaiting_script_revision"}:
            return "content"
        if status == "compliance_approved":
            return "render"
        if status == "failed" and job.get("last_failed_stage") in {"research", "content", "render"}:
            return str(job["last_failed_stage"])
        if status in RUNNING_STATES:
            raise ConflictError("任务正在运行")
        raise ConflictError("当前状态必须先完成阶段审查门禁，不能继续运行", details={"status": status})

    def _prepare_research(self, path: Path) -> None:
        data = self._scrub_automatic_human_labels(json.loads(path.read_text(encoding="utf-8")))
        data.pop("evidence_review", None)
        for item in data.get("findings", []):
            if not isinstance(item, dict):
                continue
            item.pop("review_status", None)
            item.pop("reviewer", None)
            item.pop("reviewed_at", None)
            item["finding_id"] = canonical_sha256({"claim": item.get("claim", ""), "source_urls": item.get("source_urls", [])})[:16]
            item["auto_review_status"] = "eligible" if item.get("script_eligible") is True else "excluded"
            for evidence in item.get("evidence", []):
                if isinstance(evidence, dict):
                    evidence["evidence_type"] = "unclassified"
        provenance = data.get("provenance")
        if isinstance(provenance, dict):
            provenance.pop("reviewer", None)
            provenance.pop("reviewed_at", None)
        self._write(path, data)

    def _apply_strict_rejection(self, job: dict[str, Any], folder: Path, research_path: Path) -> bool:
        if not research_path.is_file():
            return False
        research = json.loads(research_path.read_text(encoding="utf-8"))
        audit = research.get("strict_audit") if isinstance(research.get("strict_audit"), dict) else {}
        if not (
            audit.get("model_review_required") is True
            and audit.get("model_review_status") == "complete"
            and int(audit.get("passed_count", -1)) == 0
        ):
            return False
        digest = file_sha256(research_path)
        job["automatic_research_gate"] = {
            "engine": "strict_adversarial_audit",
            "decision": "rejected",
            "evaluated_at": now_iso(),
            "artifact_sha256": digest,
            "policy": str(audit.get("policy", "assume_all_claims_false")),
                    "reason": "没有任何finding完成反向举证，自动退回研究；未生成或冒充阶段审查记录。",
        }
        job["status"] = "awaiting_research_revision"
        self._event(folder, "research_auto_rejected", {"artifact_sha256": digest, "engine": "strict_adversarial_audit"})
        return True

    def _reconcile_strict_rejections(self) -> None:
        for path in self.jobs_dir.glob("*/job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if job.get("schema_version") != 2 or job.get("status") != "awaiting_research_approval":
                    continue
                folder = path.parent
                research_path = folder / "draft" / "research.json"
                research = json.loads(research_path.read_text(encoding="utf-8"))
                audit = research.get("strict_audit") if isinstance(research.get("strict_audit"), dict) else {}
                audit_rows = audit.get("findings", []) if isinstance(audit.get("findings"), list) else []
                reviewed = {"supported_limited", "insufficient", "contradicted"}
                research_changed = False
                if audit.get("model_review_required") is True and audit_rows and all(
                    isinstance(row, dict) and row.get("model_verdict") in reviewed for row in audit_rows
                ) and audit.get("model_review_status") != "complete":
                    audit["model_provider_reported_status"] = audit.get("model_review_status", "unknown")
                    audit["model_review_status"] = "complete"
                    self._write(research_path, research)
                    research_changed = True
                previous_gate = job.get("automatic_research_gate")
                if int(audit.get("passed_count", 0)) > 0:
                    job.pop("automatic_research_gate", None)
                rejected = self._apply_strict_rejection(job, folder, research_path)
                if rejected or research_changed or previous_gate != job.get("automatic_research_gate"):
                    job["updated_at"] = now_iso()
                    self._sync_steps(job)
                    self._write(path, job)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue

    def _reconcile_content_rejections(self) -> None:
        for path in self.jobs_dir.glob("*/job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                gate = job.get("automatic_content_gate")
                if job.get("schema_version") != 2 or not isinstance(gate, dict):
                    continue
                if job.get("status") not in {"awaiting_compliance_approval", "blocked_compliance"}:
                    continue
                script_path = path.parent / "draft" / "approved_script.json"
                if script_path.is_file() and file_sha256(script_path) != gate.get("artifact_sha256"):
                    job.pop("automatic_content_gate", None)
                    job["updated_at"] = now_iso()
                    self._write(path, job)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue

    @classmethod
    def _scrub_automatic_human_labels(cls, value: Any) -> Any:
        forbidden_keys = {
            "reviewer", "reviewed_at", "approved_by", "approved_at", "human_verified",
            "human_reviewed", "human_verification", "human_approval",
        }
        if isinstance(value, dict):
            return {
                key: cls._scrub_automatic_human_labels(item)
                for key, item in value.items()
                if str(key).lower() not in forbidden_keys
            }
        if isinstance(value, list):
            return [cls._scrub_automatic_human_labels(item) for item in value]
        if isinstance(value, str) and value.lower() in {"human_verified", "human_reviewed", "approved_by_human"}:
            return "unverified"
        return value

    def _build_manifest(self, job: dict[str, Any], run: dict[str, Any], staging: Path, runner: Any) -> dict[str, Any]:
        artifacts = []
        public_names = set(PUBLIC_ARTIFACTS) | {"approvals.json"}
        for path in sorted(staging.iterdir(), key=lambda value: value.name):
            if not path.is_file() or path.name not in public_names:
                continue
            artifacts.append({
                "name": path.name,
                "stage": run["stage"],
                "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "size": path.stat().st_size,
                "sha256": file_sha256(path),
            })
        return {
            "schema_version": 2,
            "job_id": job["id"],
            "run_id": run["run_id"],
            "stage": run["stage"],
            "status": "complete",
            "input_sha256": canonical_sha256(job.get("production_input")),
            "capability_pack": dict(job.get("capability_pack") or {}),
            "learning_rules_sha256": canonical_sha256((job.get("production_input") or {}).get("learning_rules", [])),
            "learning_rule_ids": list(job.get("learning_rule_ids", [])),
            "approval_hashes": {
                "research": job.get("approvals", {}).get("research", {}).get("artifact_sha256"),
                "compliance": job.get("approvals", {}).get("compliance", {}).get("artifact_sha256"),
            },
            "review_policy": normalize_review_policy(job.get("review_policy")),
            "evidence_status": evidence_status_for_policy(job.get("review_policy")),
            "started_at": run["started_at"],
            "finished_at": now_iso(),
            "budget": runner.budget.snapshot() if getattr(runner, "budget", None) is not None else job.get("budget"),
            "artifacts": artifacts,
        }

    def _ensure_capability_pack(self, job: dict[str, Any], folder: Path) -> None:
        """Migrate a pre-v3 v2 task only when an authorized mutation occurs."""
        production_input = job.get("production_input")
        if not isinstance(production_input, dict):
            return
        existing_pack = production_input.get("capability_pack")
        if existing_pack is not None:
            trusted = self._trusted_capability_pack(existing_pack)
            expected_summary = {
                "id": trusted["id"],
                "version": trusted["version"],
                "sha256": trusted["sha256"],
            }
            if job.get("capability_pack") != expected_summary:
                raise ConflictError("任务记录中的行业能力包身份不一致，已拒绝继续执行")
            production_input["capability_pack"] = trusted
            return
        topic = str(production_input.get("topic", ""))
        pack = (
            legacy_clean_air_pack()
            if any(marker in topic for marker in LEGACY_CLEAN_AIR_MARKERS)
            else local_capability_pack(topic or "通用短视频内容任务")
        )
        production_input = dict(production_input)
        production_input["capability_pack"] = pack
        job["production_input"] = preserve_legacy_footage_contract(
            production_input,
            validate_topic_input(production_input, allow_learning_rules=True),
        )
        job["capability_pack"] = {"id": pack["id"], "version": pack["version"], "sha256": pack["sha256"]}
        job.setdefault("learning_rule_ids", [])
        job["updated_at"] = now_iso()
        self._write(folder / "job.json", job)
        self._event(folder, "capability_pack_migrated", {
            "id": pack["id"], "version": pack["version"], "sha256": pack["sha256"],
        })

    @staticmethod
    def _authority_document(pack: dict[str, Any]) -> dict[str, Any]:
        """Compare all authority-bearing fields while ignoring display metadata."""

        return {key: value for key, value in pack.items() if key != "generated_at"}

    def _trusted_capability_pack(self, pack: object) -> dict[str, Any]:
        """Require an executable audit and a verifiable authority source.

        The two built-in deterministic pack families are reproducible trust
        roots.  Model-generated or other dynamic packs must already exist as
        the exact immutable document in this runtime's capability registry.
        Merely supplying a self-consistent JSON document and recomputing its
        public hash is therefore insufficient to start or mutate a job.
        """

        try:
            validated = validate_capability_pack(pack)
        except (TypeError, ValueError) as exc:
            raise UnprocessableError(f"行业能力包无效：{exc}") from exc

        audit_status = validated["audit"]["status"]
        if audit_status not in EXECUTABLE_AUDIT_STATUSES:
            raise UnprocessableError(f"行业能力包审核状态不可执行：{audit_status}")

        if validated["id"] == LEGACY_CLEAN_AIR_PACK_ID:
            # validate_capability_pack already enforces the exact reserved
            # legacy contract, including source, audit, snapshot and identity.
            return validated

        if validated["source"] == "local":
            expected = local_capability_pack(validated["snapshot"]["goal"])
            if self._authority_document(validated) != self._authority_document(expected):
                raise UnprocessableError("本地行业能力包不是可复现的安全内置版本")
            return validated

        try:
            registered = self.capability_registry.get(validated["id"], validated["sha256"])
        except CapabilityPackRegistryError as exc:
            raise UnprocessableError("动态行业能力包未在本机不可变注册表中登记") from exc
        if registered != validated:
            raise UnprocessableError("动态行业能力包与本机注册表中的可信版本不一致")
        return registered

    def _copy_draft(self, draft: Path, staging: Path) -> None:
        if not draft.exists():
            return
        for path in draft.iterdir():
            if path.is_file() and path.name in set(CANONICAL_ARTIFACTS) | set(DIRECTOR_ARTIFACTS):
                shutil.copy2(path, staging / path.name)

    def _publish_draft(self, staging: Path, draft: Path, names: list[str]) -> None:
        draft.mkdir(exist_ok=True)
        for name in names:
            source = staging / name
            if source.is_file():
                shutil.copy2(source, draft / name)

    def _sync_steps(self, job: dict[str, Any]) -> None:
        status = job.get("status")
        try:
            mechanical = (
                normalize_review_policy(job.get("review_policy"))["stage_review_mode"]
                == MECHANICAL_STAGE_REVIEW
            )
        except ValueError:
            mechanical = False
        for index, state in enumerate(job.get("step_states", [])):
            if status in {"complete"}:
                state["status"] = "complete"
            elif status in {"awaiting_research_approval", "awaiting_research_revision"}:
                state["status"] = "complete" if index == 0 else (
                    "pending" if mechanical else ("waiting_human" if index == 2 else "pending")
                )
            elif status in {"awaiting_compliance_approval", "blocked_compliance", "awaiting_script_revision", "compliance_approved"}:
                state["status"] = "complete" if index < 2 else (
                    "pending" if mechanical else ("waiting_human" if index in {2, 5} else "pending")
                )
            elif status in RUNNING_STATES:
                state["status"] = "running"
            elif status == "failed" and state.get("status") == "running":
                state["status"] = "failed"

    def _review_identity(self, job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        try:
            policy = normalize_review_policy(job.get("review_policy"))
            return approval_identity(policy["stage_review_mode"], str(payload.get("reviewer", "")))
        except ValueError as exc:
            raise UnprocessableError(str(exc)) from exc

    @staticmethod
    def _require_agent_test_note(identity: dict[str, Any], note: str) -> None:
        if identity.get("review_mode") == "test" and len(note.strip()) < 8:
            raise UnprocessableError("代理测试审查必须留下至少8字的核验备注")

    def _load_raw(self, job_id: str) -> tuple[dict[str, Any], Path]:
        if not job_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in job_id):
            raise WorkflowError("任务ID无效")
        folder = self.jobs_dir / job_id
        path = folder / "job.json"
        if not path.exists():
            raise FileNotFoundError("任务不存在")
        return json.loads(path.read_text(encoding="utf-8")), folder

    def _load_v2(self, job_id: str) -> tuple[dict[str, Any], Path]:
        job, folder = self._load_raw(job_id)
        if job.get("schema_version") != 2:
            raise ConflictError("旧任务为只读历史格式，请创建v2任务")
        return job, folder

    @staticmethod
    def _validate_creation_request(value: dict[str, str]) -> dict[str, str]:
        if not isinstance(value, dict) or set(value) != {"idempotency_key", "fingerprint"}:
            raise WorkflowError("任务创建幂等记录格式无效")
        key = value.get("idempotency_key")
        fingerprint = value.get("fingerprint")
        if not isinstance(key, str) or not IDEMPOTENCY_RE.fullmatch(key):
            raise WorkflowError("任务创建幂等键无效")
        if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            raise WorkflowError("任务创建请求指纹无效")
        return {"idempotency_key": key, "fingerprint": fingerprint}

    def _find_creation_replay(self, request: dict[str, str]) -> dict[str, Any] | None:
        matches: list[dict[str, Any]] = []
        for path in sorted(self.jobs_dir.glob("*/job.json")):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkflowError("任务创建幂等索引包含不可读任务") from exc
            creation_request = job.get("creation_request")
            if creation_request is None:
                continue
            saved = self._validate_creation_request(creation_request)
            if saved["idempotency_key"] == request["idempotency_key"]:
                matches.append(job)
        if len(matches) > 1:
            raise WorkflowError("任务创建幂等键对应多个任务，已停止自动恢复")
        if not matches:
            return None
        job = matches[0]
        saved = self._validate_creation_request(job["creation_request"])
        if saved["fingerprint"] != request["fingerprint"]:
            raise IdempotencyConflictError("同一Idempotency-Key不能用于不同创建请求")
        return self._public(job)

    def _acquire_creation_lock(self, idempotency_key: str) -> BinaryIO:
        """Acquire a process-owned OS lock for the durable creation index.

        The lock file intentionally persists.  Process exit releases the byte-range
        lock, so stale JSON never needs to be unlinked and cannot race with a new
        owner that has already acquired the same path.
        """
        path = self.jobs_dir / ".create.lock"
        handle = path.open("a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise ConflictError("另一个任务创建请求正在处理") from exc
        try:
            payload = json.dumps(
                {
                    "pid": os.getpid(),
                    "request_sha256": hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
                    "owner_token": uuid.uuid4().hex,
                    "created_at": now_iso(),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            handle.seek(0)
            handle.truncate()
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            return handle
        except Exception:
            self._release_creation_lock(handle)
            raise

    @staticmethod
    def _release_creation_lock(handle: BinaryIO) -> None:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _public(self, job: dict[str, Any]) -> dict[str, Any]:
        result = json.loads(json.dumps(job, ensure_ascii=False))
        if result.get("schema_version") != 2:
            result["schema_version"] = 1
            result["legacy_original_status"] = result.get("status")
            result["status"] = "legacy_read_only"
            result["legacy_read_only"] = True
        for run in result.get("runs", []):
            run.pop("idempotency_key", None)
        result.pop("creation_request", None)
        return result

    def _job_lock(self, job_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(job_id, threading.Lock())

    def _acquire_disk_lock(self, folder: Path, key: str) -> Path:
        path = folder / "run.lock"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                pid = int(data.get("pid", 0))
            except (OSError, ValueError, json.JSONDecodeError):
                pid = 0
            if pid and self._pid_alive(pid):
                raise ConflictError("任务被另一个进程占用")
            path.unlink(missing_ok=True)
        try:
            with path.open("x", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "idempotency_key": key, "created_at": now_iso()}, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise ConflictError("任务被另一个进程占用") from exc
        return path

    def _recover_stale_lock(self, job: dict[str, Any], folder: Path) -> None:
        path = folder / "run.lock"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = int(data.get("pid", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            pid = 0
        if pid and self._pid_alive(pid):
            return
        path.unlink(missing_ok=True)
        if job.get("status") in RUNNING_STATES:
            stage = {"research_running": "research", "content_running": "content", "rendering": "render"}[job["status"]]
            interrupted_at = now_iso()
            job["status"] = "failed"
            job["last_error"] = "上次运行进程中断"
            job["last_failed_stage"] = stage
            job["active_run_id"] = None
            for run in reversed(job.get("runs", [])):
                if run.get("status") == "running":
                    run.update({"status": "interrupted", "finished_at": interrupted_at, "error": "process_interrupted"})
                    break
            budget = job.get("budget")
            interrupted_attempts = 0
            if isinstance(budget, dict) and isinstance(budget.get("events"), list):
                for event in budget["events"]:
                    if isinstance(event, dict) and event.get("status") == "attempted":
                        event.update({
                            "status": "failed",
                            "error_type": "process_interrupted",
                            "finished_at": interrupted_at,
                        })
                        interrupted_attempts += 1
                if interrupted_attempts:
                    budget["failed"] = int(budget.get("failed", 0)) + interrupted_attempts
                    budget["remaining"] = max(0, int(budget.get("limit", 7)) - int(budget.get("attempted", 0)))
            self._write(folder / "job.json", job)
            self._event(folder, "run_interrupted", {"stage": stage, "budget_attempts_failed": interrupted_attempts})

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if pid == os.getpid():
            return True
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            open_process.restype = wintypes.HANDLE
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL
            get_exit_code = kernel32.GetExitCodeProcess
            get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            get_exit_code.restype = wintypes.BOOL
            handle = open_process(process_query_limited_information, False, pid)
            if not handle:
                # Access denied still proves that a protected process owns the PID.
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                return bool(get_exit_code(handle, ctypes.byref(exit_code))) and exit_code.value == still_active
            finally:
                close_handle(handle)
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    @staticmethod
    def _write(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)

    @staticmethod
    def _event(folder: Path, event: str, data: dict[str, Any]) -> None:
        record = {"time": now_iso(), "event": event, "data": data}
        with (folder / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
