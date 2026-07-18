from __future__ import annotations

import json
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


def local_fallback_plan(goal: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
    by_capability: dict[str, list[dict[str, Any]]] = {}
    for tool in tools:
        for capability in tool.get("capabilities", []):
            by_capability.setdefault(capability, []).append(tool)

    steps = []
    for index, (capability, name) in enumerate(PIPELINE, start=1):
        candidates = by_capability.get(capability, [])
        tool = candidates[0] if candidates else None
        steps.append(
            {
                "id": f"step-{index}",
                "name": name,
                "capability": capability,
                "tool_id": tool.get("id") if tool else None,
                "input": "上一步输出或用户提供的素材/上下文",
                "output": f"{name}阶段的结构化产物",
                "requires_approval": capability in {"compliance_review", "video_generation", "video_editing", "human_refinement"},
                "risk": "工具尚未启用，只生成计划" if tool and not tool.get("enabled") else ("尚未发现适配工具" if not tool else "低"),
            }
        )
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

    def create(self, plan: dict[str, Any], production_input: dict[str, Any] | None = None) -> dict[str, Any]:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        job_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        job = {
            "id": job_id,
            "status": "planned",
            "created_at": now,
            "updated_at": now,
            "approved_at": None,
            "plan": plan,
            "production_input": production_input or None,
            "artifacts": [],
            "last_error": None,
            "step_states": [{"id": step.get("id"), "status": "pending"} for step in plan.get("steps", [])],
        }
        folder = self.jobs_dir / job_id
        folder.mkdir(parents=True, exist_ok=False)
        self._write(folder / "job.json", job)
        self._event(folder, "job_created", {"status": "planned"})
        return job

    def list(self) -> list[dict[str, Any]]:
        jobs = []
        for path in self.jobs_dir.glob("*/job.json"):
            try:
                jobs.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return jobs[:100]

    def get(self, job_id: str) -> dict[str, Any]:
        job, _ = self._load(job_id)
        return job

    def approve(self, job_id: str) -> dict[str, Any]:
        job, folder = self._load(job_id)
        if job["status"] not in {"planned", "needs_attention"}:
            raise ValueError("只有待确认任务可以批准")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        job["status"] = "approved"
        job["approved_at"] = now
        job["updated_at"] = now
        self._write(folder / "job.json", job)
        self._event(folder, "job_approved", {})
        return job

    def run_safe(self, job_id: str, allow_external_commands: bool = False) -> dict[str, Any]:
        job, folder = self._load(job_id)
        if job["status"] != "approved":
            raise ValueError("任务必须先人工批准")
        waiting = 0
        for state, step in zip(job["step_states"], job["plan"].get("steps", [])):
            if step.get("capability") == "human_refinement":
                state["status"] = "waiting_human"
                waiting += 1
            elif not step.get("tool_id"):
                state["status"] = "waiting_adapter"
                waiting += 1
            elif not allow_external_commands:
                state["status"] = "waiting_adapter"
                waiting += 1
            else:
                state["status"] = "ready"
        job["status"] = "needs_attention" if waiting else "ready"
        job["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self._write(folder / "job.json", job)
        self._event(folder, "safe_run_checked", {"waiting_steps": waiting, "external_commands_executed": False})
        return job

    def run_production(self, job_id: str, runner: Any) -> dict[str, Any]:
        job, folder = self._load(job_id)
        if job["status"] not in {"approved", "needs_attention", "failed", "complete"}:
            raise ValueError("生产任务必须先人工批准")
        if not isinstance(job.get("production_input"), dict):
            return self.run_safe(job_id)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        job["status"] = "running"
        job["updated_at"] = now
        job["last_error"] = None
        for state in job.get("step_states", []):
            state["status"] = "running"
        self._write(folder / "job.json", job)
        self._event(folder, "production_started", {"trusted_adapter": True})
        try:
            report = runner.run(folder, job["production_input"])
        except Exception as exc:
            job["status"] = "failed"
            job["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            job["last_error"] = str(exc)
            for state in job.get("step_states", []):
                if state["status"] == "running":
                    state["status"] = "failed"
            self._write(folder / "job.json", job)
            self._event(folder, "production_failed", {"error": str(exc)})
            raise
        job["status"] = "complete"
        job["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        job["artifacts"] = list(report.get("artifacts", []))
        for state in job.get("step_states", []):
            state["status"] = "complete"
        self._write(folder / "job.json", job)
        self._event(folder, "production_completed", {"wall_clock_seconds": report.get("wall_clock_seconds")})
        return job

    def update_script(self, job_id: str, script: str) -> dict[str, Any]:
        job, folder = self._load(job_id)
        value = str(script or "").strip()
        if len(value) < 40 or len(value) > 1000:
            raise ValueError("脚本长度必须在40到1000字之间")
        payload = {
            "id": "human-edited",
            "hook_type": "人工精修",
            "script": value,
            "reason": "用户在控制台修改",
            "approved_by": "human",
            "approved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        self._write(folder / "approved_script.json", payload)
        job["status"] = "approved"
        job["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        job["artifacts"] = [name for name in job.get("artifacts", []) if name not in {"voice.wav", "captions.srt", "final.mp4", "run_report.json"}]
        self._write(folder / "job.json", job)
        self._event(folder, "script_updated", {"character_count": len(value)})
        return job

    def _load(self, job_id: str) -> tuple[dict[str, Any], Path]:
        if not job_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in job_id):
            raise ValueError("任务ID无效")
        folder = self.jobs_dir / job_id
        path = folder / "job.json"
        if not path.exists():
            raise FileNotFoundError("任务不存在")
        return json.loads(path.read_text(encoding="utf-8")), folder

    @staticmethod
    def _write(path: Path, data: dict[str, Any]) -> None:
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    @staticmethod
    def _event(folder: Path, event: str, data: dict[str, Any]) -> None:
        record = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            "data": data,
        }
        with (folder / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
