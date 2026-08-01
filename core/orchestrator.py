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
from typing import Any


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
REVIEW_ARTIFACTS = {"research.json", "approved_script.json", "review.json"}
TOPIC_STRONG_MARKERS = ("甲醛", "除醛", "测醛", "室内空气", "装修污染")
TOPIC_CONTEXTUAL_MARKERS = ("通风", "检测报告", "气味")
TOPIC_CONTEXT_MARKERS = (
    "室内", "新房", "装修", "入住", "房间", "家居", "居家", "新居", "住宅",
    "甲醛", "除醛", "测醛",
)
# Kept as the complete public vocabulary for callers that display the supported
# field. Validation deliberately does not accept the contextual terms alone.
TOPIC_MARKERS = TOPIC_STRONG_MARKERS + TOPIC_CONTEXTUAL_MARKERS
RUNNING_STATES = {"research_running", "content_running", "rendering"}
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
PRODUCTION_INPUT_FIELDS = {
    "topic", "audience", "target_duration_seconds", "pattern_card_ids", "voice_engine",
    "aspect_ratio", "render_mode", "require_animation", "enable_web_research", "source_urls",
    "motion_scenes", "animation_quality",
}
PLAN_FIELDS = {"goal", "summary", "steps", "missing", "estimated_cost_level", "planner"}
PLAN_STEP_FIELDS = {"id", "name", "capability", "tool_id", "input", "output", "requires_approval", "risk"}


class WorkflowError(RuntimeError):
    status = 400
    code = "workflow_error"

    def __init__(self, message: str, *, details: Any | None = None):
        super().__init__(message)
        self.details = details


class ConflictError(WorkflowError):
    status = 409
    code = "conflict"


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
    """Return whether text is unambiguously inside the competition domain."""
    text = str(value).strip()
    if any(marker in text for marker in TOPIC_STRONG_MARKERS):
        return True
    return (
        any(marker in text for marker in TOPIC_CONTEXTUAL_MARKERS)
        and any(marker in text for marker in TOPIC_CONTEXT_MARKERS)
    )


def validate_topic_input(production_input: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(production_input, dict):
        raise UnprocessableError("production_input必须是JSON对象")
    topic = str(production_input.get("topic", "")).strip()
    audience = str(production_input.get("audience", "新房家庭")).strip()
    if not 4 <= len(topic) <= 80:
        raise UnprocessableError("选题长度必须在4到80字之间")
    if not topic_in_scope(topic):
        raise UnprocessableError("当前原型只支持甲醛、除醛与室内空气相关选题")
    if not 2 <= len(audience) <= 80:
        raise UnprocessableError("受众长度必须在2到80字之间")
    unknown = sorted(set(production_input) - PRODUCTION_INPUT_FIELDS)
    if unknown:
        raise UnprocessableError("production_input包含不允许的字段", details={"fields": unknown})
    normalized = dict(production_input)
    normalized.update({"topic": topic, "audience": audience})
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
    if "voice_engine" in normalized and normalized["voice_engine"] not in {"voxcpm2", "qwen3-tts", "gpt-sovits", "windows_sapi"}:
        raise UnprocessableError("voice_engine不在允许范围内")
    if "aspect_ratio" in normalized and normalized["aspect_ratio"] != "9:16":
        raise UnprocessableError("当前原型只允许9:16竖屏")
    if "render_mode" in normalized and normalized["render_mode"] not in {"animated", "simple"}:
        raise UnprocessableError("render_mode必须是animated或simple")
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
    def __init__(self, runtime_dir: Path):
        self.jobs_dir = Path(runtime_dir) / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._reconcile_strict_rejections()
        self._reconcile_content_rejections()

    def create(self, plan: dict[str, Any], production_input: dict[str, Any] | None = None) -> dict[str, Any]:
        safe_plan = validate_plan(plan)
        steps = safe_plan["steps"]
        normalized_input = validate_topic_input(production_input) if production_input is not None else None
        timestamp = now_iso()
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
            "approvals": {
                "research": {"status": "pending"},
                "compliance": {"status": "pending"},
            },
            "runs": [],
            "active_run_id": None,
            "current_run_id": None,
            "artifacts": [],
            "last_error": None,
            "last_failed_stage": None,
            "budget": {"limit": 7, "attempted": 0, "succeeded": 0, "failed": 0, "events": []},
            "step_states": [{"id": step.get("id"), "status": "pending"} for step in steps],
        }
        folder = self.jobs_dir / job_id
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "draft").mkdir()
        (folder / "runs").mkdir()
        self._write(folder / "job.json", job)
        self._event(folder, "job_created", {"status": "planned", "schema_version": 2})
        return self._public(job)

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
            replayed = next((run for run in job.get("runs", []) if run.get("idempotency_key") == idempotency_key), None)
            if replayed:
                result = self._public(job)
                result["replayed"] = True
                return result
            stage = self._next_stage(job)
            lock_path = self._acquire_disk_lock(folder, idempotency_key)
            run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
            run_dir = folder / "runs" / run_id
            staging = run_dir / "staging"
            staging.mkdir(parents=True, exist_ok=False)
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
                self._copy_draft(folder / "draft", staging)
                if stage == "research":
                    runner.run_research_stage(staging, job["production_input"])
                    self._prepare_research(staging / "research.json")
                    self._publish_draft(staging, folder / "draft", ["research.json", "insight.json"])
                    job["approvals"]["research"] = {"status": "pending"}
                    job["approvals"]["compliance"] = {"status": "pending"}
                    job.pop("automatic_research_gate", None)
                    if not self._apply_strict_rejection(job, folder, folder / "draft" / "research.json"):
                        job["status"] = "awaiting_research_approval"
                elif stage == "content":
                    runner.run_content_stage(staging, job["production_input"], job["approvals"]["research"])
                    self._publish_draft(staging, folder / "draft", ["research.json", "insight.json", "script_variants.json", "approved_script.json", "review.json"])
                    review = json.loads((staging / "review.json").read_text(encoding="utf-8"))
                    job["approvals"]["compliance"] = {"status": "pending"}
                    job.pop("automatic_content_gate", None)
                    job["status"] = "blocked_compliance" if review.get("status") == "blocked" else "awaiting_compliance_approval"
                else:
                    runner.run_render_stage(staging, job["production_input"], job["approvals"])
                    self._write(staging / "approvals.json", job["approvals"])
                    manifest = self._build_manifest(job, run, staging, runner)
                    self._write(staging / "manifest.json", manifest)
                    published = run_dir / "artifacts"
                    staging.replace(published)
                    run["artifacts"] = [item["name"] for item in manifest["artifacts"]]
                    run["manifest_sha256"] = file_sha256(published / "manifest.json")
                    job["current_run_id"] = run_id
                    job["artifacts"] = [name for name in CANONICAL_ARTIFACTS if (published / name).is_file()]
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

    def approve_research(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        job, folder = self._load_v2(job_id)
        if job["status"] not in {"awaiting_research_approval", "awaiting_research_revision"}:
            raise ConflictError("当前任务不在研究审批阶段")
        research_path = folder / "draft" / "research.json"
        research = json.loads(research_path.read_text(encoding="utf-8"))
        digest = file_sha256(research_path)
        if payload.get("artifact_sha256") != digest:
            raise ConflictError("研究文件已经变化，请刷新后重新审批")
        reviewer = self._reviewer(payload)
        decision = str(payload.get("decision", ""))
        if decision not in {"approved", "rejected"}:
            raise UnprocessableError("decision必须是approved或rejected")
        eligible = {str(item.get("finding_id")) for item in research.get("findings", []) if item.get("auto_review_status") == "eligible"}
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
        record = {
            "status": decision,
            "reviewer": reviewer,
            "reviewed_at": now_iso(),
            "artifact_sha256": digest,
            "note": str(payload.get("note", "")).strip()[:1000],
            "findings": list(submitted.values()),
        }
        job["approvals"]["research"] = record
        job["approvals"]["compliance"] = {"status": "pending"}
        job["status"] = "research_approved" if decision == "approved" else "awaiting_research_revision"
        job["updated_at"] = now_iso()
        self._write(folder / "job.json", job)
        self._event(folder, "research_reviewed", {"decision": decision, "reviewer": reviewer, "artifact_sha256": digest})
        return self._public(job)

    def approve_compliance(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        job, folder = self._load_v2(job_id)
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
        reviewer = self._reviewer(payload)
        decision = str(payload.get("decision", ""))
        if decision not in {"approved", "rejected"}:
            raise UnprocessableError("decision必须是approved或rejected")
        job["approvals"]["compliance"] = {
            "status": decision,
            "reviewer": reviewer,
            "reviewed_at": now_iso(),
            "artifact_sha256": review_digest,
            "script_sha256": script_digest,
            "note": str(payload.get("note", "")).strip()[:1000],
        }
        job["status"] = "compliance_approved" if decision == "approved" else "awaiting_script_revision"
        job["updated_at"] = now_iso()
        self._write(folder / "job.json", job)
        self._event(folder, "compliance_reviewed", {"decision": decision, "reviewer": reviewer, "artifact_sha256": review_digest})
        return self._public(job)

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
            allowed_names = set(CANONICAL_ARTIFACTS) | {"approvals.json"}
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
            if seen_names != allowed_names:
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
                job["artifacts"] = [name for name in CANONICAL_ARTIFACTS if (published / name).is_file()]
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

    def update_script(self, job_id: str, script: str, review: dict[str, Any], estimate: dict[str, Any]) -> dict[str, Any]:
        job, folder = self._load_v2(job_id)
        if job["status"] not in {"awaiting_compliance_approval", "blocked_compliance", "awaiting_script_revision", "compliance_approved", "complete", "failed"}:
            raise ConflictError("当前阶段不能修改脚本")
        draft = folder / "draft"
        if not (draft / "research.json").is_file():
            raise ConflictError("尚未完成研究和内容阶段")
        payload = {
            "id": "human-edited",
            "hook_type": "人工精修",
            "script": str(script).strip(),
            "selected_by": "human_editor",
            "edited_at": now_iso(),
            "duration_estimate": estimate,
        }
        self._write(draft / "approved_script.json", payload)
        self._write(draft / "review.json", review)
        job["approvals"]["compliance"] = {"status": "pending"}
        job["status"] = "blocked_compliance" if review.get("status") == "blocked" else "awaiting_compliance_approval"
        job["updated_at"] = now_iso()
        self._write(folder / "job.json", job)
        self._event(folder, "script_updated", {"character_count": len(payload["script"]), "estimated_seconds": estimate.get("estimated_seconds")})
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
        if name not in set(CANONICAL_ARTIFACTS) | {"manifest.json", "approvals.json"}:
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

    def resolve_review_artifact(self, job_id: str, name: str) -> Path:
        if name not in REVIEW_ARTIFACTS:
            raise WorkflowError("只允许查看待审研究、脚本和合规文件")
        job, folder = self._load_v2(job_id)
        target = (folder / "draft" / name).resolve()
        if target.parent != (folder / "draft").resolve() or not target.is_file():
            raise FileNotFoundError("待审产物不存在")
        return target

    def _next_stage(self, job: dict[str, Any]) -> str:
        status = job.get("status")
        if status in {"authorized", "awaiting_research_revision"}:
            return "research"
        if status == "research_approved":
            return "content"
        if status == "compliance_approved":
            return "render"
        if status == "failed" and job.get("last_failed_stage") in {"research", "content", "render"}:
            return str(job["last_failed_stage"])
        if status in RUNNING_STATES:
            raise ConflictError("任务正在运行")
        raise ConflictError("当前状态必须先完成人工门禁，不能继续运行", details={"status": status})

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
            "reason": "没有任何finding完成反向举证，自动退回研究；未生成或冒充人工审批。",
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
        public_names = set(CANONICAL_ARTIFACTS) | {"approvals.json"}
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
            "approval_hashes": {
                "research": job.get("approvals", {}).get("research", {}).get("artifact_sha256"),
                "compliance": job.get("approvals", {}).get("compliance", {}).get("artifact_sha256"),
            },
            "started_at": run["started_at"],
            "finished_at": now_iso(),
            "budget": runner.budget.snapshot() if getattr(runner, "budget", None) is not None else job.get("budget"),
            "artifacts": artifacts,
        }

    def _copy_draft(self, draft: Path, staging: Path) -> None:
        if not draft.exists():
            return
        for path in draft.iterdir():
            if path.is_file() and path.name in set(CANONICAL_ARTIFACTS):
                shutil.copy2(path, staging / path.name)

    def _publish_draft(self, staging: Path, draft: Path, names: list[str]) -> None:
        draft.mkdir(exist_ok=True)
        for name in names:
            source = staging / name
            if source.is_file():
                shutil.copy2(source, draft / name)

    def _sync_steps(self, job: dict[str, Any]) -> None:
        status = job.get("status")
        for index, state in enumerate(job.get("step_states", [])):
            if status in {"complete"}:
                state["status"] = "complete"
            elif status in {"awaiting_research_approval", "awaiting_research_revision"}:
                state["status"] = "complete" if index == 0 else ("waiting_human" if index == 2 else "pending")
            elif status in {"awaiting_compliance_approval", "blocked_compliance", "awaiting_script_revision", "compliance_approved"}:
                state["status"] = "complete" if index < 2 else ("waiting_human" if index in {2, 5} else "pending")
            elif status in RUNNING_STATES:
                state["status"] = "running"
            elif status == "failed" and state.get("status") == "running":
                state["status"] = "failed"

    def _reviewer(self, payload: dict[str, Any]) -> str:
        reviewer = str(payload.get("reviewer", "")).strip()
        if not 2 <= len(reviewer) <= 80:
            raise UnprocessableError("审批人名称必须在2到80字之间")
        return reviewer

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

    def _public(self, job: dict[str, Any]) -> dict[str, Any]:
        result = json.loads(json.dumps(job, ensure_ascii=False))
        if result.get("schema_version") != 2:
            result["schema_version"] = 1
            result["legacy_original_status"] = result.get("status")
            result["status"] = "legacy_read_only"
            result["legacy_read_only"] = True
        for run in result.get("runs", []):
            run.pop("idempotency_key", None)
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
