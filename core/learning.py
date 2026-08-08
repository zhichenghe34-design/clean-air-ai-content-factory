from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.capability_pack import validate_capability_pack


LEARNING_SCHEMA_VERSION = 1
CORRECTION_KINDS = {"style", "content", "evidence", "capability", "process"}
CORRECTION_FIELDS = {"message", "scope", "actor", "mode", "kind", "job_id", "capability_pack"}
SCOPES = {"task", "project", "workspace"}
RULE_FIELDS = {
    "id",
    "version",
    "scope",
    "pack_id",
    "job_id",
    "instruction",
    "status",
    "created_at",
    "updated_at",
    "source_correction_ids",
    "source_pack_sha256",
    "success_job_ids",
    "skill_id",
    "skill_version",
    "promoted_at",
    "sha256",
}
RULE_STATE_FIELDS = {"schema_version", "updated_at", "rules", "sha256"}
CORRECTION_RECORD_FIELDS = {
    "schema_version",
    "id",
    "recorded_at",
    "message",
    "mode",
    "kind",
    "scope",
    "requested_scope",
    "actor",
    "pack_id",
    "job_id",
    "pack_sha256",
    "rule_id",
    "sha256",
}
LEGACY_CORRECTION_RECORD_FIELDS = CORRECTION_RECORD_FIELDS - {"kind"}
SKILL_FILES = {"SKILL.md", "skill.json", "examples.json", "tests.json"}
SKILL_METADATA_FIELDS = {
    "schema_version",
    "id",
    "name",
    "version",
    "sha256",
    "generated_at",
    "instruction_only",
    "instruction",
    "scope",
    "source_rule_ids",
    "source_correction_ids",
    "source_pack_sha256",
    "success_job_ids",
    "files",
    "rollback",
}
ROLLBACK_FIELDS = {"previous_version", "current_version", "available"}

_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RULE_ID_RE = re.compile(r"^rule-[0-9a-f]{20}$")
_CORRECTION_ID_RE = re.compile(r"^correction-[0-9a-f]{32}$")
_SKILL_ID_RE = re.compile(r"^learned-[0-9a-f]{20}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_RE = re.compile(r"(?i)(?:https?|file|ftp)://")
_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|(?:^|\s)/(?:etc|home|users|var|tmp)/|\.\.[\\/])")
_SECRET_RE = re.compile(
    r"(?i)(?:api[_ -]?key|access[_ -]?token|password|authorization|cookie)\s*[:=]\s*\S+|\bsk-[A-Za-z0-9_-]{12,}\b"
)
_EXECUTABLE_RE = re.compile(
    r"(?i)(?:\b(?:powershell|cmd\.exe|bash|zsh)\s+(?:-[a-z]|/c)"
    r"|\b(?:curl|wget)\s+(?:-[A-Za-z]+\s+)*(?:https?|ftp)://"
    r"|\brm\s+-rf\b|\binvoke-expression\b|\bsubprocess\."
    r"|```(?:bash|sh|powershell|cmd|python|javascript))"
)
_PROMPT_OVERRIDE_RE = re.compile(
    r"(?i)(?:ignore\s+(?:all\s+)?(?:previous|prior|system|developer)\s+instructions?"
    r"|reveal\s+(?:the\s+)?system\s+prompt"
    r"|忽略(?:之前|所有|系统)(?:的)?(?:指令|要求|规则)"
    r"|输出(?:系统|开发者)提示词)"
)
_FORBIDDEN_KEYS = {
    "prompt",
    "system_prompt",
    "developer_prompt",
    "regex",
    "regexp",
    "path",
    "filepath",
    "directory",
    "command",
    "cmd",
    "shell",
    "script",
    "scripts",
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
_TASK_MARKERS = ("只改这次", "仅改这次", "只用这次", "仅用这次", "仅本次", "本次任务", "this task only")
_PROJECT_MARKERS = ("以后", "今后", "下次", "后续", "这个项目", "本项目", "同类内容", "from now on", "next time")
_WORKSPACE_MARKERS = ("所有项目", "全部项目", "每个项目", "以后所有项目", "今后所有项目", "全局", "all projects", "workspace-wide")


class LearningError(ValueError):
    """Raised for invalid or tampered learning-store data."""

    status = 422
    code = "invalid_learning_data"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _safe_text(value: Any, *, field: str, minimum: int = 1, maximum: int = 1000) -> str:
    if not isinstance(value, str):
        raise LearningError(f"{field} must be a string")
    text = _clean_space(value)
    if not minimum <= len(text) <= maximum:
        raise LearningError(f"{field} length must be between {minimum} and {maximum}")
    if _CONTROL_RE.search(text):
        raise LearningError(f"{field} contains control characters")
    if _URL_RE.search(text) or _PATH_RE.search(text) or _SECRET_RE.search(text) or _EXECUTABLE_RE.search(text):
        raise LearningError(f"{field} may contain instruction text only")
    if _PROMPT_OVERRIDE_RE.search(text):
        raise LearningError(f"{field} contains an instruction override")
    return text


def _safe_machine_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _MACHINE_ID_RE.fullmatch(value):
        raise LearningError(f"{field} must be a safe machine identifier")
    return value


def _assert_safe_keys(value: Any, *, location: str = "value") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise LearningError(f"{location} contains a non-string key")
            normalized = key.strip().casefold().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS:
                raise LearningError(f"{location} contains forbidden field: {key}")
            _assert_safe_keys(nested, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_safe_keys(nested, location=f"{location}[{index}]")


def _parse_timestamp(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise LearningError(f"{field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LearningError(f"{field} must be a timestamp") from exc
    if parsed.tzinfo is None:
        raise LearningError(f"{field} must include a timezone")
    return value


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _rule_hash(rule: dict[str, Any]) -> str:
    return _sha256({key: value for key, value in rule.items() if key != "sha256"})


def _state_hash(state: dict[str, Any]) -> str:
    return _sha256({key: value for key, value in state.items() if key != "sha256"})


def _correction_hash(correction: dict[str, Any]) -> str:
    return _sha256({key: value for key, value in correction.items() if key != "sha256"})


def _validate_rule(rule: Any) -> dict[str, Any]:
    if not isinstance(rule, dict) or set(rule) != RULE_FIELDS:
        raise LearningError("rule does not match the immutable schema")
    _assert_safe_keys(rule, location="rule")
    if not isinstance(rule["id"], str) or not _RULE_ID_RE.fullmatch(rule["id"]):
        raise LearningError("invalid rule id")
    if not isinstance(rule["version"], int) or isinstance(rule["version"], bool) or rule["version"] < 1:
        raise LearningError("invalid rule version")
    if rule["scope"] not in SCOPES:
        raise LearningError("invalid rule scope")
    if rule["pack_id"] != "*":
        _safe_machine_id(rule["pack_id"], field="rule.pack_id")
    if rule["scope"] == "workspace" and rule["pack_id"] != "*":
        raise LearningError("workspace rules must target every pack")
    if rule["scope"] != "workspace" and rule["pack_id"] == "*":
        raise LearningError("non-workspace rules must target one pack")
    if rule["job_id"] is not None:
        _safe_machine_id(rule["job_id"], field="rule.job_id")
    if rule["scope"] == "task" and rule["job_id"] is None:
        raise LearningError("task rules require a job id")
    if rule["scope"] != "task" and rule["job_id"] is not None:
        raise LearningError("only task rules may retain a job id")
    _safe_text(rule["instruction"], field="rule.instruction", minimum=4, maximum=1000)
    if rule["status"] not in {"active", "disabled"}:
        raise LearningError("invalid rule status")
    _parse_timestamp(rule["created_at"], field="rule.created_at")
    _parse_timestamp(rule["updated_at"], field="rule.updated_at")
    if not isinstance(rule["source_correction_ids"], list) or not 1 <= len(rule["source_correction_ids"]) <= 256:
        raise LearningError("invalid source correction ids")
    if len(set(rule["source_correction_ids"])) != len(rule["source_correction_ids"]):
        raise LearningError("duplicate source correction ids")
    if not all(isinstance(value, str) and _CORRECTION_ID_RE.fullmatch(value) for value in rule["source_correction_ids"]):
        raise LearningError("invalid source correction id")
    if not isinstance(rule["source_pack_sha256"], str) or not _SHA_RE.fullmatch(rule["source_pack_sha256"]):
        raise LearningError("invalid source pack hash")
    if not isinstance(rule["success_job_ids"], list) or len(rule["success_job_ids"]) > 10000:
        raise LearningError("invalid success job ids")
    if len(set(rule["success_job_ids"])) != len(rule["success_job_ids"]):
        raise LearningError("duplicate success job ids")
    for value in rule["success_job_ids"]:
        _safe_machine_id(value, field="success_job_id")
    if rule["skill_id"] is not None and (not isinstance(rule["skill_id"], str) or not _SKILL_ID_RE.fullmatch(rule["skill_id"])):
        raise LearningError("invalid skill id")
    if (rule["skill_id"] is None) != (rule["skill_version"] is None):
        raise LearningError("skill id and version must be set together")
    if rule["skill_version"] is not None and rule["skill_version"] != "1.0.0":
        raise LearningError("unsupported learned skill version")
    if rule["promoted_at"] is not None:
        _parse_timestamp(rule["promoted_at"], field="rule.promoted_at")
    if (rule["skill_id"] is None) != (rule["promoted_at"] is None):
        raise LearningError("promoted_at must match skill promotion state")
    if not isinstance(rule["sha256"], str) or rule["sha256"] != _rule_hash(rule):
        raise LearningError("rule hash mismatch")
    return copy.deepcopy(rule)


def _validate_correction(correction: Any) -> dict[str, Any]:
    if not isinstance(correction, dict) or set(correction) not in (
        CORRECTION_RECORD_FIELDS,
        LEGACY_CORRECTION_RECORD_FIELDS,
    ):
        raise LearningError("correction record does not match the immutable schema")
    _assert_safe_keys(correction, location="correction")
    if correction["schema_version"] != LEARNING_SCHEMA_VERSION:
        raise LearningError("unsupported correction schema")
    if not isinstance(correction["id"], str) or not _CORRECTION_ID_RE.fullmatch(correction["id"]):
        raise LearningError("invalid correction id")
    _parse_timestamp(correction["recorded_at"], field="correction.recorded_at")
    _safe_text(correction["message"], field="correction.message", minimum=4, maximum=1000)
    if correction["mode"] not in {"defer", "interrupt"}:
        raise LearningError("invalid correction mode")
    if "kind" in correction and correction["kind"] not in CORRECTION_KINDS:
        raise LearningError("invalid correction kind")
    if correction["scope"] not in SCOPES:
        raise LearningError("invalid correction scope")
    if correction["requested_scope"] is not None and correction["requested_scope"] not in SCOPES:
        raise LearningError("invalid requested correction scope")
    _safe_text(correction["actor"], field="correction.actor", maximum=80)
    _safe_machine_id(correction["pack_id"], field="correction.pack_id")
    if correction["job_id"] is not None:
        _safe_machine_id(correction["job_id"], field="correction.job_id")
    if correction["scope"] == "task" and correction["job_id"] is None:
        raise LearningError("task correction requires a job id")
    if not isinstance(correction["pack_sha256"], str) or not _SHA_RE.fullmatch(correction["pack_sha256"]):
        raise LearningError("invalid correction pack hash")
    if not isinstance(correction["rule_id"], str) or not _RULE_ID_RE.fullmatch(correction["rule_id"]):
        raise LearningError("invalid correction rule id")
    if not isinstance(correction["sha256"], str) or correction["sha256"] != _correction_hash(correction):
        raise LearningError("correction hash mismatch")
    return copy.deepcopy(correction)


class SkillCompiler:
    """Compile proven correction rules into non-executable local skills."""

    def __init__(self, skills_dir: Path, staging_dir: Path):
        self.skills_dir = Path(skills_dir)
        self.staging_dir = Path(staging_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _metadata_hash(metadata: dict[str, Any]) -> str:
        return _sha256({key: value for key, value in metadata.items() if key != "sha256"})

    @staticmethod
    def _skill_id(rule: dict[str, Any]) -> str:
        # Rule ids distinguish identical instructions that were recorded with
        # different durable correction kinds.  Using the source rule prevents
        # their later instruction-only Skills from colliding.
        identity = {"source_rule_id": rule["id"]}
        return f"learned-{_sha256(identity)[:20]}"

    @staticmethod
    def _skill_markdown(skill_id: str, instruction: str) -> str:
        return (
            "---\n"
            f"name: {skill_id}\n"
            "description: Apply one locally verified content correction before generation and review.\n"
            "---\n\n"
            "# 已验证纠错规则\n\n"
            "## 适用时机\n\n"
            "当前任务与该规则的作用域匹配时，在生成前应用，并在交付前再检查一次。\n\n"
            "## 必须遵守\n\n"
            f"- {instruction}\n"
            "- 如果与当前用户的更新指示冲突，暂停并请求人工确认。\n"
            "- 不得因该规则降低证据、合规、隐私或人工审批要求。\n\n"
            "## 验收\n\n"
            "确认产出没有重现已记录的错误，同时保留当前任务的证据和审批链。\n"
        )

    @staticmethod
    def _assert_instruction_only_text(text: str, *, field: str) -> None:
        if _URL_RE.search(text) or _PATH_RE.search(text) or _SECRET_RE.search(text) or _EXECUTABLE_RE.search(text):
            raise LearningError(f"{field} contains executable or sensitive material")
        if _PROMPT_OVERRIDE_RE.search(text):
            raise LearningError(f"{field} contains an instruction override")

    def validate_skill(self, directory: Path) -> dict[str, Any]:
        directory = Path(directory)
        if not directory.is_dir() or directory.is_symlink():
            raise LearningError("skill output must be a real directory")
        entries = {entry.name for entry in directory.iterdir()}
        if entries != SKILL_FILES or any(not entry.is_file() or entry.is_symlink() for entry in directory.iterdir()):
            raise LearningError("skill output contains files outside the instruction-only whitelist")
        if sum(entry.stat().st_size for entry in directory.iterdir()) > 128 * 1024:
            raise LearningError("skill output is too large")

        markdown = (directory / "SKILL.md").read_text(encoding="utf-8")
        self._assert_instruction_only_text(markdown, field="SKILL.md")
        try:
            metadata = json.loads((directory / "skill.json").read_text(encoding="utf-8"))
            examples = json.loads((directory / "examples.json").read_text(encoding="utf-8"))
            tests = json.loads((directory / "tests.json").read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LearningError("skill JSON is invalid") from exc
        if not isinstance(metadata, dict) or set(metadata) != SKILL_METADATA_FIELDS:
            raise LearningError("skill metadata does not match the whitelist")
        _assert_safe_keys(metadata, location="skill")
        _assert_safe_keys(examples, location="examples")
        _assert_safe_keys(tests, location="tests")
        for name, value in (("skill", metadata), ("examples", examples), ("tests", tests)):
            self._assert_instruction_only_text(_canonical_json(value), field=name)
        if metadata["schema_version"] != LEARNING_SCHEMA_VERSION:
            raise LearningError("unsupported skill schema")
        if not isinstance(metadata["id"], str) or not _SKILL_ID_RE.fullmatch(metadata["id"]):
            raise LearningError("invalid learned skill id")
        if directory.name != metadata["id"]:
            raise LearningError("skill directory and metadata id differ")
        if metadata["name"] != metadata["id"] or metadata["version"] != "1.0.0":
            raise LearningError("invalid learned skill identity")
        if metadata["instruction_only"] is not True:
            raise LearningError("learned skill must be instruction-only")
        _safe_text(metadata["instruction"], field="skill.instruction", minimum=4, maximum=1000)
        if metadata["scope"] not in {"project", "workspace"}:
            raise LearningError("task-only rules cannot become reusable skills")
        _parse_timestamp(metadata["generated_at"], field="skill.generated_at")
        if not isinstance(metadata["source_rule_ids"], list) or len(metadata["source_rule_ids"]) != 1:
            raise LearningError("skill must identify exactly one source rule")
        if not all(isinstance(value, str) and _RULE_ID_RE.fullmatch(value) for value in metadata["source_rule_ids"]):
            raise LearningError("invalid source rule id")
        if not isinstance(metadata["source_correction_ids"], list) or not metadata["source_correction_ids"]:
            raise LearningError("skill must identify source corrections")
        if not all(isinstance(value, str) and _CORRECTION_ID_RE.fullmatch(value) for value in metadata["source_correction_ids"]):
            raise LearningError("invalid skill correction provenance")
        if not isinstance(metadata["source_pack_sha256"], str) or not _SHA_RE.fullmatch(metadata["source_pack_sha256"]):
            raise LearningError("invalid source pack hash")
        if not isinstance(metadata["success_job_ids"], list) or len(set(metadata["success_job_ids"])) < 3:
            raise LearningError("a skill requires three distinct successful jobs")
        for job_id in metadata["success_job_ids"]:
            _safe_machine_id(job_id, field="skill.success_job_id")
        if not isinstance(metadata["files"], dict) or set(metadata["files"]) != {"SKILL.md", "examples.json", "tests.json"}:
            raise LearningError("invalid skill file hash inventory")
        for name, digest in metadata["files"].items():
            if not isinstance(digest, str) or not _SHA_RE.fullmatch(digest) or digest != _file_sha256(directory / name):
                raise LearningError(f"skill file hash mismatch: {name}")
        if not isinstance(metadata["rollback"], dict) or set(metadata["rollback"]) != ROLLBACK_FIELDS:
            raise LearningError("invalid skill rollback metadata")
        if metadata["rollback"] != {"previous_version": None, "current_version": "1.0.0", "available": False}:
            raise LearningError("invalid initial rollback state")
        if not isinstance(metadata["sha256"], str) or metadata["sha256"] != self._metadata_hash(metadata):
            raise LearningError("skill metadata hash mismatch")
        return copy.deepcopy(metadata)

    def compile(self, rule: dict[str, Any]) -> dict[str, Any]:
        validated_rule = _validate_rule(rule)
        if validated_rule["scope"] == "task":
            raise LearningError("task-only rules cannot be compiled into reusable skills")
        if len(set(validated_rule["success_job_ids"])) < 3:
            raise LearningError("a rule needs three distinct successful jobs before compilation")
        skill_id = self._skill_id(validated_rule)
        target = self.skills_dir / skill_id
        if target.exists():
            return self.validate_skill(target)

        staging = Path(tempfile.mkdtemp(prefix=f".{skill_id}.", dir=self.staging_dir))
        published = False
        try:
            markdown = self._skill_markdown(skill_id, validated_rule["instruction"])
            examples = {
                "schema_version": LEARNING_SCHEMA_VERSION,
                "examples": [
                    {
                        "situation": "后续内容任务命中同一作用域",
                        "expected": f"生成前应用纠错规则：{validated_rule['instruction']}",
                    },
                    {
                        "situation": "新指示与已学习规则冲突",
                        "expected": "暂停自动应用并请求人工确认",
                    },
                ],
            }
            tests = {
                "schema_version": LEARNING_SCHEMA_VERSION,
                "checks": [
                    {"type": "instruction_present", "value": validated_rule["instruction"]},
                    {"type": "distinct_success_jobs", "minimum": 3},
                    {"type": "instruction_only", "expected": True},
                    {"type": "human_conflict_gate", "expected": True},
                ],
            }
            (staging / "SKILL.md").write_text(markdown, encoding="utf-8", newline="\n")
            (staging / "examples.json").write_text(
                json.dumps(examples, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            (staging / "tests.json").write_text(
                json.dumps(tests, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            metadata = {
                "schema_version": LEARNING_SCHEMA_VERSION,
                "id": skill_id,
                "name": skill_id,
                "version": "1.0.0",
                "sha256": "",
                "generated_at": _now(),
                "instruction_only": True,
                "instruction": validated_rule["instruction"],
                "scope": validated_rule["scope"],
                "source_rule_ids": [validated_rule["id"]],
                "source_correction_ids": list(validated_rule["source_correction_ids"]),
                "source_pack_sha256": validated_rule["source_pack_sha256"],
                "success_job_ids": list(validated_rule["success_job_ids"]),
                "files": {
                    name: _file_sha256(staging / name) for name in ("SKILL.md", "examples.json", "tests.json")
                },
                "rollback": {"previous_version": None, "current_version": "1.0.0", "available": False},
            }
            metadata["sha256"] = self._metadata_hash(metadata)
            (staging / "skill.json").write_text(
                json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
            )

            # Validation uses the directory name as part of the identity. Rename
            # inside staging first, then publish that already-validated directory.
            validation_parent = self.staging_dir / f".validated-{uuid.uuid4().hex}"
            validation_parent.mkdir()
            validation_dir = validation_parent / skill_id
            os.replace(staging, validation_dir)
            try:
                metadata = self.validate_skill(validation_dir)
                os.replace(validation_dir, target)
                published = True
            finally:
                if validation_dir.exists():
                    shutil.rmtree(validation_dir)
                if validation_parent.exists():
                    validation_parent.rmdir()
            return metadata
        finally:
            if not published and staging.exists():
                shutil.rmtree(staging)


class LearningStore:
    """Append corrections, apply scoped rules, and promote proven rules safely."""

    def __init__(self, runtime_dir: Path | str):
        self.runtime_dir = Path(runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.corrections_path = self.runtime_dir / "corrections.jsonl"
        self.rules_path = self.runtime_dir / "rules.json"
        self.skills_dir = self.runtime_dir / "skills"
        self.staging_dir = self.runtime_dir / ".skill-staging"
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.compiler = SkillCompiler(self.skills_dir, self.staging_dir)
        if not self.rules_path.exists():
            self._write_rules([])
        else:
            self._load_state()
        self._recover_missing_rules()

    def _write_rules(self, rules: list[dict[str, Any]]) -> dict[str, Any]:
        validated = [_validate_rule(rule) for rule in rules]
        ids = [rule["id"] for rule in validated]
        if len(ids) != len(set(ids)):
            raise LearningError("rules.json contains duplicate rule ids")
        state = {
            "schema_version": LEARNING_SCHEMA_VERSION,
            "updated_at": _now(),
            "rules": validated,
            "sha256": "",
        }
        state["sha256"] = _state_hash(state)
        _atomic_json(self.rules_path, state)
        return state

    def _load_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.rules_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LearningError("rules.json is missing or invalid") from exc
        if not isinstance(state, dict) or set(state) != RULE_STATE_FIELDS:
            raise LearningError("rules.json does not match the whitelist schema")
        if state["schema_version"] != LEARNING_SCHEMA_VERSION:
            raise LearningError("unsupported rules schema")
        _parse_timestamp(state["updated_at"], field="rules.updated_at")
        if not isinstance(state["rules"], list):
            raise LearningError("rules must be a list")
        state["rules"] = [_validate_rule(rule) for rule in state["rules"]]
        ids = [rule["id"] for rule in state["rules"]]
        if len(ids) != len(set(ids)):
            raise LearningError("rules.json contains duplicate rule ids")
        if not isinstance(state["sha256"], str) or state["sha256"] != _state_hash(state):
            raise LearningError("rules.json hash mismatch")
        return state

    @staticmethod
    def _rule_from_correction(correction: dict[str, Any]) -> dict[str, Any]:
        """Rebuild the exact initial rule represented by one durable event."""

        target_pack = "*" if correction["scope"] == "workspace" else correction["pack_id"]
        target_job = correction["job_id"] if correction["scope"] == "task" else None
        identity = {
            "scope": correction["scope"],
            "pack_id": target_pack,
            "job_id": target_job,
            "instruction": correction["message"],
        }
        # Records created before v0.3.0's kind persistence retain their exact
        # historical identity.  Every new record carries kind, making a later
        # evidence/capability reclassification a separate, auditable rule.
        if "kind" in correction:
            identity["kind"] = correction["kind"]
        expected_rule_id = f"rule-{_sha256(identity)[:20]}"
        if correction["rule_id"] != expected_rule_id:
            raise LearningError("correction record rule identity mismatch")
        rule = {
            "id": expected_rule_id,
            "version": 1,
            "scope": correction["scope"],
            "pack_id": target_pack,
            "job_id": target_job,
            "instruction": correction["message"],
            "status": "active",
            "created_at": correction["recorded_at"],
            "updated_at": correction["recorded_at"],
            "source_correction_ids": [correction["id"]],
            "source_pack_sha256": correction["pack_sha256"],
            "success_job_ids": [],
            "skill_id": None,
            "skill_version": None,
            "promoted_at": None,
            "sha256": "",
        }
        rule["sha256"] = _rule_hash(rule)
        return _validate_rule(rule)

    def _recover_missing_rules(self) -> None:
        """Replay correction events not yet reflected in the derived rule index.

        ``corrections.jsonl`` is written and fsynced before ``rules.json``.  A
        process exit between those writes must therefore repair the derived
        index on the next startup without duplicating already-applied events.
        """

        with self._lock:
            corrections = self.list_memories()
            if not corrections:
                return
            state = self._load_state()
            rules_by_id = {rule["id"]: rule for rule in state["rules"]}
            correction_owner = {
                correction_id: rule["id"]
                for rule in state["rules"]
                for correction_id in rule["source_correction_ids"]
            }
            changed = False
            for correction in corrections:
                rebuilt = self._rule_from_correction(correction)
                existing_owner = correction_owner.get(correction["id"])
                if existing_owner is not None and existing_owner != rebuilt["id"]:
                    raise LearningError("correction event is attached to the wrong rule")
                rule = rules_by_id.get(rebuilt["id"])
                if rule is None:
                    state["rules"].append(rebuilt)
                    rules_by_id[rebuilt["id"]] = rebuilt
                    correction_owner[correction["id"]] = rebuilt["id"]
                    changed = True
                    continue
                if correction["id"] in rule["source_correction_ids"]:
                    continue
                rule["source_correction_ids"].append(correction["id"])
                rule["version"] += 1
                rule["updated_at"] = correction["recorded_at"]
                rule["sha256"] = _rule_hash(rule)
                _validate_rule(rule)
                correction_owner[correction["id"]] = rule["id"]
                changed = True
            if changed:
                self._write_rules(state["rules"])

    def set_rule_status(self, rule_id: str, status: str) -> dict[str, Any]:
        """Atomically enable or disable one learned rule without deleting history."""

        if not isinstance(rule_id, str) or not _RULE_ID_RE.fullmatch(rule_id):
            raise LearningError("invalid rule id")
        if status not in {"active", "disabled"}:
            raise LearningError("rule status must be active or disabled")
        with self._lock:
            state = self._load_state()
            rule = next((item for item in state["rules"] if item["id"] == rule_id), None)
            if rule is None:
                raise LearningError(f"unknown rule id: {rule_id}")
            if rule["status"] == status:
                return copy.deepcopy(rule)
            rule["status"] = status
            rule["version"] += 1
            rule["updated_at"] = _now()
            rule["sha256"] = _rule_hash(rule)
            _validate_rule(rule)
            self._write_rules(state["rules"])
            return copy.deepcopy(rule)

    def disable_rule(self, rule_id: str) -> dict[str, Any]:
        """Stop a rule from future application while retaining its audit trail."""

        return self.set_rule_status(rule_id, "disabled")

    def enable_rule(self, rule_id: str) -> dict[str, Any]:
        """Re-enable a previously disabled rule."""

        return self.set_rule_status(rule_id, "active")

    @staticmethod
    def _resolve_scope(message: str, requested_scope: str | None, *, has_job: bool) -> str:
        lowered = message.casefold()
        if any(marker.casefold() in lowered for marker in _TASK_MARKERS):
            return "task"
        if any(marker.casefold() in lowered for marker in _WORKSPACE_MARKERS):
            return "workspace"
        if any(marker.casefold() in lowered for marker in _PROJECT_MARKERS):
            return "project"
        if requested_scope == "task":
            return "task"
        if requested_scope == "project":
            return "project"
        # A workspace-wide memory requires explicit natural-language intent;
        # a client-supplied enum alone may not silently widen its effect.
        return "task" if has_job else "project"

    @staticmethod
    def _new_rule(
        *, correction_id: str, message: str, kind: str, scope: str, pack: dict[str, Any], job_id: str | None
    ) -> dict[str, Any]:
        target_pack = "*" if scope == "workspace" else pack["id"]
        target_job = job_id if scope == "task" else None
        identity = {"scope": scope, "pack_id": target_pack, "job_id": target_job, "instruction": message, "kind": kind}
        timestamp = _now()
        rule = {
            "id": f"rule-{_sha256(identity)[:20]}",
            "version": 1,
            "scope": scope,
            "pack_id": target_pack,
            "job_id": target_job,
            "instruction": message,
            "status": "active",
            "created_at": timestamp,
            "updated_at": timestamp,
            "source_correction_ids": [correction_id],
            "source_pack_sha256": pack["sha256"],
            "success_job_ids": [],
            "skill_id": None,
            "skill_version": None,
            "promoted_at": None,
            "sha256": "",
        }
        rule["sha256"] = _rule_hash(rule)
        return _validate_rule(rule)

    def _append_correction(self, correction: dict[str, Any]) -> None:
        validated = _validate_correction(correction)
        self.corrections_path.parent.mkdir(parents=True, exist_ok=True)
        with self.corrections_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(_canonical_json(validated) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def record_correction(
        self,
        payload: dict[str, Any],
        pack: dict[str, Any],
        job_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise LearningError("correction payload must be an object")
        _assert_safe_keys(payload, location="correction_payload")
        unknown = sorted(set(payload) - CORRECTION_FIELDS)
        if unknown:
            raise LearningError(f"correction payload contains unknown fields: {', '.join(unknown)}")
        message = _safe_text(payload.get("message"), field="message", minimum=4, maximum=1000)
        mode = payload.get("mode", "defer")
        if mode not in {"defer", "interrupt"}:
            raise LearningError("mode must be defer or interrupt")
        kind = payload.get("kind", "content")
        if kind not in CORRECTION_KINDS:
            raise LearningError("kind must be style, content, evidence, capability, or process")
        requested_scope = payload.get("scope")
        if requested_scope is not None and requested_scope not in SCOPES:
            raise LearningError("scope must be task, project, or workspace")
        actor = _safe_text(payload.get("actor", "operator"), field="actor", maximum=80)
        validated_pack = validate_capability_pack(pack)
        embedded_pack = payload.get("capability_pack")
        if embedded_pack is not None:
            validated_embedded_pack = validate_capability_pack(embedded_pack)
            if validated_embedded_pack["sha256"] != validated_pack["sha256"]:
                raise LearningError("payload capability_pack does not match the associated pack")
        payload_job_id = payload.get("job_id")
        if payload_job_id is not None:
            payload_job_id = _safe_machine_id(payload_job_id, field="payload.job_id")
            if job_id is not None and payload_job_id != job_id:
                raise LearningError("payload job_id does not match the associated job")
            if job_id is None:
                job_id = payload_job_id
        if job_id is not None:
            job_id = _safe_machine_id(job_id, field="job_id")
        scope = self._resolve_scope(message, requested_scope, has_job=job_id is not None)
        if scope == "task" and job_id is None:
            raise LearningError("task-scoped corrections require job_id")

        with self._lock:
            state = self._load_state()
            correction_id = f"correction-{uuid.uuid4().hex}"
            provisional_rule = self._new_rule(
                correction_id=correction_id,
                message=message,
                kind=kind,
                scope=scope,
                pack=validated_pack,
                job_id=job_id,
            )
            rule_by_id = {rule["id"]: rule for rule in state["rules"]}
            rule = rule_by_id.get(provisional_rule["id"])
            if rule is None:
                rule = provisional_rule
                state["rules"].append(rule)
            else:
                if correction_id not in rule["source_correction_ids"]:
                    rule["source_correction_ids"].append(correction_id)
                    rule["version"] += 1
                    rule["updated_at"] = _now()
                    rule["sha256"] = _rule_hash(rule)
                    _validate_rule(rule)

            correction = {
                "schema_version": LEARNING_SCHEMA_VERSION,
                "id": correction_id,
                "recorded_at": _now(),
                "message": message,
                "mode": mode,
                "kind": kind,
                "scope": scope,
                "requested_scope": requested_scope,
                "actor": actor,
                "pack_id": validated_pack["id"],
                "job_id": job_id if scope == "task" else None,
                "pack_sha256": validated_pack["sha256"],
                "rule_id": rule["id"],
                "sha256": "",
            }
            correction["sha256"] = _correction_hash(correction)
            _validate_correction(correction)
            # The immutable event is durable before the derived rules index.
            # If the process stops between the two writes, the event remains
            # recoverable and no previous event is rewritten.
            self._append_correction(correction)
            self._write_rules(state["rules"])
            result = copy.deepcopy(correction)
            result["rule"] = copy.deepcopy(rule)
            return result

    def rules_for(self, pack_id: str, job_id: str | None = None) -> list[dict[str, Any]]:
        pack_id = _safe_machine_id(pack_id, field="pack_id")
        if job_id is not None:
            job_id = _safe_machine_id(job_id, field="job_id")
        with self._lock:
            rules = self._load_state()["rules"]
            applicable = [
                rule
                for rule in rules
                if rule["status"] == "active"
                and (
                    rule["scope"] == "workspace"
                    or (rule["scope"] == "project" and rule["pack_id"] == pack_id)
                    or (
                        rule["scope"] == "task"
                        and rule["pack_id"] == pack_id
                        and job_id is not None
                        and rule["job_id"] == job_id
                    )
                )
            ]
            ordered = sorted(applicable, key=lambda item: (item["created_at"], item["id"]))
            return [
                {
                    "rule_id": rule["id"],
                    "scope": rule["scope"],
                    "instruction": rule["instruction"],
                    "pack_id": rule["pack_id"],
                    "source_event_ids": list(rule["source_correction_ids"]),
                }
                for rule in ordered
            ]

    def correction_kinds_for_rules(self, rules: list[dict[str, Any]]) -> dict[str, list[str]]:
        """Return durable correction kinds for each derived rule.

        Older ``corrections.jsonl`` rows predate the kind field.  They remain
        valid and intentionally return no durable kind so the caller can retain
        the historical instruction-based fallback only for those legacy rows.
        """

        if not isinstance(rules, list):
            raise LearningError("rules must be a list")
        with self._lock:
            corrections = {item["id"]: item for item in self.list_memories()}
            result: dict[str, list[str]] = {}
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                rule_id = str(rule.get("rule_id", ""))
                if not _RULE_ID_RE.fullmatch(rule_id):
                    continue
                kinds: list[str] = []
                for correction_id in rule.get("source_event_ids", []):
                    correction = corrections.get(correction_id)
                    kind = correction.get("kind") if correction else None
                    if kind in CORRECTION_KINDS and kind not in kinds:
                        kinds.append(kind)
                result[rule_id] = kinds
            return result

    def memory_snapshot(self, pack_id: str | None = None, job_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            if pack_id is None:
                internal_rules = [rule for rule in self._load_state()["rules"] if rule["status"] == "active"]
                rules = [
                    {
                        "rule_id": rule["id"],
                        "scope": rule["scope"],
                        "instruction": rule["instruction"],
                        "pack_id": rule["pack_id"],
                        "source_event_ids": list(rule["source_correction_ids"]),
                    }
                    for rule in internal_rules
                ]
            else:
                rules = self.rules_for(pack_id, job_id=job_id)
            snapshot = {
                "schema_version": LEARNING_SCHEMA_VERSION,
                "pack_id": pack_id,
                "job_id": job_id,
                "rule_count": len(rules),
                "rules": copy.deepcopy(rules),
            }
            snapshot["sha256"] = _sha256(snapshot)
            return snapshot

    def mark_job_success(self, rule_ids: list[str], job_id: str) -> dict[str, Any]:
        if not isinstance(rule_ids, list) or len(rule_ids) > 256:
            raise LearningError("rule_ids must be a list with at most 256 items")
        normalized_ids: list[str] = []
        for rule_id in rule_ids:
            if not isinstance(rule_id, str) or not _RULE_ID_RE.fullmatch(rule_id):
                raise LearningError("invalid rule id")
            if rule_id not in normalized_ids:
                normalized_ids.append(rule_id)
        job_id = _safe_machine_id(job_id, field="job_id")

        with self._lock:
            state = self._load_state()
            by_id = {rule["id"]: rule for rule in state["rules"]}
            missing = sorted(set(normalized_ids) - set(by_id))
            if missing:
                raise LearningError(f"unknown rule ids: {', '.join(missing)}")
            active_ids = [rule_id for rule_id in normalized_ids if by_id[rule_id]["status"] == "active"]
            disabled_ids = [rule_id for rule_id in normalized_ids if by_id[rule_id]["status"] == "disabled"]
            for rule_id in active_ids:
                rule = by_id[rule_id]
                if rule["scope"] == "task" and rule["job_id"] != job_id:
                    raise LearningError("a task rule cannot be credited to another job")
                if job_id not in rule["success_job_ids"]:
                    rule["success_job_ids"].append(job_id)
                    rule["version"] += 1
                    rule["updated_at"] = _now()
                    rule["sha256"] = _rule_hash(rule)
            self._write_rules(state["rules"])

            generated: list[dict[str, Any]] = []
            for rule_id in active_ids:
                rule = by_id[rule_id]
                if rule["scope"] != "task" and len(set(rule["success_job_ids"])) >= 3 and rule["skill_id"] is None:
                    metadata = self.compiler.compile(rule)
                    rule["skill_id"] = metadata["id"]
                    rule["skill_version"] = metadata["version"]
                    rule["promoted_at"] = _now()
                    rule["version"] += 1
                    rule["updated_at"] = _now()
                    rule["sha256"] = _rule_hash(rule)
                    generated.append(metadata)
            if generated:
                self._write_rules(state["rules"])
            return {
                "job_id": job_id,
                "marked_rule_ids": active_ids,
                "ignored_disabled_rule_ids": disabled_ids,
                "generated_skills": copy.deepcopy(generated),
                "rules": copy.deepcopy([by_id[rule_id] for rule_id in active_ids]),
            }

    def list_memories(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.corrections_path.exists():
                return []
            memories: list[dict[str, Any]] = []
            try:
                with self.corrections_path.open("r", encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if not line.strip():
                            continue
                        try:
                            value = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise LearningError(f"invalid correction record at line {line_number}") from exc
                        memories.append(_validate_correction(value))
            except UnicodeDecodeError as exc:
                raise LearningError("corrections.jsonl is not valid UTF-8") from exc
            ids = [item["id"] for item in memories]
            if len(ids) != len(set(ids)):
                raise LearningError("corrections.jsonl contains duplicate event ids")
            return memories

    def list_rules(self, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            rules = self._load_state()["rules"]
            if not include_disabled:
                rules = [rule for rule in rules if rule["status"] == "active"]
            return copy.deepcopy(sorted(rules, key=lambda item: (item["created_at"], item["id"])))

    def list_skills(self) -> list[dict[str, Any]]:
        with self._lock:
            active_skill_ids = {
                str(rule["skill_id"])
                for rule in self._load_state()["rules"]
                if rule["status"] == "active" and rule["skill_id"] is not None
            }
            skills: list[dict[str, Any]] = []
            for entry in sorted(self.skills_dir.iterdir(), key=lambda path: path.name):
                if entry.name.startswith("."):
                    continue
                metadata = self.compiler.validate_skill(entry)
                if metadata["id"] in active_skill_ids:
                    skills.append(metadata)
            return skills
