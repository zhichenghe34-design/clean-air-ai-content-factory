from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.capability_pack import validate_capability_pack
from core.review_policy import (
    HUMAN_STAGE_REVIEW,
    classify_approval_record,
    evidence_status_for_policy,
    normalize_review_policy,
)


CANONICAL = {
    "research.json", "insight.json", "script_variants.json", "approved_script.json",
    "review.json", "voice.wav", "captions.srt", "motion_plan.json", "final.mp4", "run_report.json",
}
TASK_FILES = CANONICAL | {"approvals.json", "manifest.json"}
TASK_DIRS = ("task-1-real", "task-2-local", "task-3-local")
SKILL_FILES = {"SKILL.md", "skill.json", "examples.json", "tests.json"}
ROOT_FILES = {
    "VALIDATION.md", "evidence-manifest.json", "SHA256SUMS.txt", "PILOT.md",
    "provider-validation.json", "topics-response.json", "capability-pack.json",
    "capability-review.json", "correction-event.json", "learning-snapshots.json",
}
TEXT_SUFFIXES = {".json", ".md", ".srt", ".txt", ".log", ".yaml", ".yml"}
SECRET_PATTERNS = {
    "API key": re.compile(r"\b(?:sk|ds)-[A-Za-z0-9_-]{12,}\b"),
    "Authorization": re.compile(r"(?i)authorization\s*[:=]\s*(?!null\b|none\b)[^\s,}\]]+"),
    "Cookie": re.compile(r"(?i)(?:set-cookie|cookie)\s*[:=]\s*(?!null\b|none\b)[^\s,}\]]+"),
    "Windows absolute path": re.compile(r"(?i)(?:^|[\s\"'])(?:[A-Z]:\\|\\\\[^\\\s]+\\)"),
    "file URI": re.compile(r"(?i)file:///[A-Za-z]:/"),
}
SECRET_PATTERNS["user home path"] = re.compile(
    r"(?i)(?:" + "|".join(re.escape(value) for value in ("/" + "Users" + "/", "/" + "home" + "/")) + r")[^\s\"']+"
)
SENSITIVE_FIELDS = re.compile(
    r'(?i)"(?:api_?key|token|secret|password|cookie|authorization)"\s*:\s*"(?!\s*")[^"]+"'
)


def expected_paths() -> set[str]:
    paths = set(ROOT_FILES)
    paths.update(f"skill/{name}" for name in SKILL_FILES)
    for directory in TASK_DIRS:
        paths.update(f"{directory}/{name}" for name in TASK_FILES)
    return paths


EXPECTED_PATHS = expected_paths()
PAYLOAD_PATHS = EXPECTED_PATHS - {"evidence-manifest.json", "SHA256SUMS.txt"}
EXPECTED_DIRS = {"skill", *TASK_DIRS}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name}必须是JSON对象")
    return value


def _actual_paths(folder: Path) -> set[str]:
    return {
        path.relative_to(folder).as_posix()
        for path in folder.rglob("*")
        if path.is_file()
    }


def _actual_dirs(folder: Path) -> set[str]:
    return {path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_dir()}


def _scan_text(path: Path, relative: str) -> list[str]:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    errors = [f"{relative}包含{label}" for label, pattern in SECRET_PATTERNS.items() if pattern.search(text)]
    if SENSITIVE_FIELDS.search(text):
        errors.append(f"{relative}包含秘密或认证字段")
    return errors


def _ffprobe() -> str | None:
    configured = os.getenv("FFPROBE_PATH")
    if configured and Path(configured).is_file():
        return configured
    return shutil.which("ffprobe")


def probe_video(path: Path) -> dict[str, Any]:
    command = _ffprobe()
    if not command:
        raise RuntimeError("FFprobe不可用，无法验证v3成片")
    output = subprocess.check_output(
        [command, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        text=True, encoding="utf-8", errors="replace",
    )
    data = json.loads(output)
    video = next((item for item in data.get("streams", []) if item.get("codec_type") == "video"), {})
    audio = next((item for item in data.get("streams", []) if item.get("codec_type") == "audio"), {})
    return {
        "duration_seconds": round(float(data.get("format", {}).get("duration", 0)), 3),
        "width": video.get("width"), "height": video.get("height"),
        "video_codec": video.get("codec_name"), "audio_codec": audio.get("codec_name"),
    }


def _srt_seconds(value: str) -> float:
    hours, minutes, tail = value.strip().split(":")
    seconds, milliseconds = tail.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(milliseconds) / 1000


def validate_srt(path: Path, duration: float) -> list[str]:
    errors: list[str] = []
    blocks = re.split(r"\r?\n\s*\r?\n", path.read_text(encoding="utf-8-sig").strip())
    cursor = 0.0
    for index, block in enumerate(blocks, start=1):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or " --> " not in lines[1]:
            errors.append(f"字幕第{index}块格式错误")
            continue
        start_text, end_text = lines[1].split(" --> ", 1)
        start, end = _srt_seconds(start_text), _srt_seconds(end_text)
        if abs(start - cursor) > 0.05:
            errors.append(f"字幕第{index}块不连续")
        if end <= start:
            errors.append(f"字幕第{index}块时长无效")
        cursor = end
    if abs(cursor - duration) > 0.15:
        errors.append("字幕终点与成片时长不一致")
    return errors


def _verify_package_manifest(folder: Path, errors: list[str]) -> None:
    manifest = _json(folder / "evidence-manifest.json")
    if manifest.get("schema_version") != 1 or manifest.get("evidence_type") != "v3-general-local-cafe":
        errors.append("evidence-manifest类型或版本错误")
    if manifest.get("file_count") != 50:
        errors.append("evidence-manifest文件数不是50")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        errors.append("evidence-manifest.files不是数组")
        return
    by_name = {str(item.get("name")): item for item in entries if isinstance(item, dict)}
    expected = EXPECTED_PATHS - {"evidence-manifest.json"}
    if set(by_name) != expected:
        errors.append("evidence-manifest文件白名单不一致")
        return
    for name in sorted(expected):
        path = folder / name
        entry = by_name[name]
        if entry.get("sha256") != sha256(path):
            errors.append(f"{name}清单SHA-256不一致")
        if entry.get("size") != path.stat().st_size:
            errors.append(f"{name}清单大小不一致")
        expected_mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if entry.get("mime") != expected_mime:
            errors.append(f"{name}清单MIME不一致")


def _verify_sums(folder: Path, errors: list[str]) -> None:
    actual: dict[str, str] = {}
    for line in (folder / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip() or "  " not in line:
            errors.append("SHA256SUMS格式错误")
            continue
        digest, name = line.split("  ", 1)
        actual[name] = digest
    if set(actual) != PAYLOAD_PATHS:
        errors.append("SHA256SUMS载荷白名单不一致")
        return
    for name in sorted(PAYLOAD_PATHS):
        if actual[name] != sha256(folder / name):
            errors.append(f"{name}在SHA256SUMS中不一致")


def _verify_semantics(folder: Path, errors: list[str], media: dict[str, Any]) -> None:
    validation = (folder / "VALIDATION.md").read_text(encoding="utf-8")
    for phrase in ("受控演示场景", "只有第一个任务使用真实 DeepSeek", "另外两个只证明本地降级与 Skill 晋升机制", "没有真实企业采用或业务效果数据", "v0.3 尚未发布"):
        if phrase not in validation:
            errors.append(f"VALIDATION.md缺少边界说明：{phrase}")

    pack = validate_capability_pack(_json(folder / "capability-pack.json"))
    review = _json(folder / "capability-review.json")
    topics = _json(folder / "topics-response.json")
    provider = _json(folder / "provider-validation.json")
    correction = _json(folder / "correction-event.json")
    snapshots = _json(folder / "learning-snapshots.json")
    if topics.get("source") != "deepseek_bootstrap" or len(topics.get("candidates", [])) != 3:
        errors.append("选题没有形成DeepSeek bootstrap的三个候选")
    if (topics.get("capability_pack") or {}).get("sha256") != pack["sha256"]:
        errors.append("选题响应能力包与根能力包不一致")
    topic_budget = topics.get("pretask_provider_budget", topics.get("topic_provider_budget", {}))
    if int(topic_budget.get("attempted", 99)) > 3:
        errors.append("预任务Provider预算超过3")
    if review.get("status") != "passed" or (pack.get("audit") or {}).get("status") != "passed":
        errors.append("动态能力包反证审核未通过")
    if provider.get("connection_succeeded") is not True or provider.get("source") != "deepseek_bootstrap":
        errors.append("Provider验证没有证明真实DeepSeek bootstrap")
    if provider.get("formal_secret_sha256_before") != provider.get("formal_secret_sha256_after"):
        errors.append("正式秘密文件操作前后哈希变化")
    if provider.get("isolated_secret_removed") is not True or provider.get("restart_provider_state") != "unconfigured":
        errors.append("隔离Key删除或无Key重启状态未验证")
    correction_record = correction.get("correction", correction)
    if correction.get("correction_kind", correction_record.get("kind")) != "style":
        errors.append("纠错kind不是style")
    if correction.get("effective_scope", correction_record.get("scope")) != "project":
        errors.append("纠错scope不是project")
    if correction.get("interrupt_supported") is not False:
        errors.append("纠错没有明确interrupt_supported=false")
    rule_id = str(correction_record.get("rule_id", ""))
    correction_id = str(correction_record.get("id", ""))
    rows = snapshots.get("snapshots")
    if not isinstance(rows, list) or len(rows) != 4:
        errors.append("学习快照不是四阶段")
        rows = []
    else:
        counts = [row.get("success_count") for row in rows if isinstance(row, dict)]
        if counts != [0, 1, 2, 3]:
            errors.append("学习成功次数不是0/1/2/3")
        if any(row.get("rule_id") != rule_id for row in rows if isinstance(row, dict)):
            errors.append("四阶段学习快照没有绑定同一规则")
        skill_counts = [len(row.get("skill_ids", [])) for row in rows if isinstance(row, dict)]
        if skill_counts != [0, 0, 0, 1]:
            errors.append("学习快照没有在第三次成功后生成唯一Skill")

    skill = _json(folder / "skill" / "skill.json")
    if skill.get("instruction_only") is not True or skill.get("source_rule_ids") != [rule_id]:
        errors.append("Skill不是同一规则晋升的instruction-only Skill")
    if correction_id not in skill.get("source_correction_ids", []):
        errors.append("Skill没有保留纠错来源")
    success_jobs = skill.get("success_job_ids", [])
    if len(success_jobs) != 3 or len(set(success_jobs)) != 3:
        errors.append("Skill没有三个不同成功Job ID")

    job_ids: list[str] = []
    reviewers: set[str] = set()
    for index, directory in enumerate(TASK_DIRS, start=1):
        root = folder / directory
        manifest = _json(root / "manifest.json")
        approvals = _json(root / "approvals.json")
        research = _json(root / "research.json")
        variants = _json(root / "script_variants.json")
        report = _json(root / "run_report.json")
        review_payload = _json(root / "review.json")
        job_id = str(manifest.get("job_id", ""))
        job_ids.append(job_id)
        if manifest.get("schema_version") != 2 or manifest.get("status") != "complete" or manifest.get("stage") not in {"render", "report_rebuild"}:
            errors.append(f"{directory}不是成功v2发布运行")
        raw_review_policy = manifest.get("review_policy")
        if not isinstance(raw_review_policy, dict):
            errors.append(f"{directory}缺少显式人工 review_policy")
        else:
            try:
                review_policy = normalize_review_policy(raw_review_policy)
            except (TypeError, ValueError):
                errors.append(f"{directory}的 review_policy 无效")
            else:
                if review_policy["stage_review_mode"] != HUMAN_STAGE_REVIEW:
                    errors.append(f"{directory}的 review_policy 不是正式人工审查")
                if manifest.get("evidence_status") != evidence_status_for_policy(review_policy):
                    errors.append(f"{directory}的 evidence_status 与 review_policy 不一致")
        if (manifest.get("capability_pack") or {}).get("sha256") != pack["sha256"]:
            errors.append(f"{directory}能力包哈希不同")
        if (
            rule_id not in manifest.get("learning_rule_ids", [])
            or rule_id not in report.get("learning_rule_ids", [])
            or rule_id not in review_payload.get("learning_rule_ids", [])
        ):
            errors.append(f"{directory}没有实际采用同一学习规则")
        budget = manifest.get("budget", {})
        attempted, succeeded = int(budget.get("attempted", -1)), int(budget.get("succeeded", -1))
        if index == 1:
            if not 1 <= succeeded <= attempted <= 7:
                errors.append("真实任务Provider成功/预算记录无效")
            if (report.get("provider") or {}).get("source") != "DeepSeek":
                errors.append("真实任务运行报告没有证明DeepSeek脚本来源")
        else:
            if attempted != 0 or research.get("status") != "offline":
                errors.append(f"{directory}不是attempted=0的离线研究")
            if (variants.get("provider") or {}).get("source") != "local_deterministic":
                errors.append(f"{directory}脚本不是本地确定性模板")
        for gate_name in ("research", "compliance"):
            gate = approvals.get(gate_name, {})
            reviewer = str(gate.get("reviewer", "")).strip()
            reviewers.add(reviewer)
            review_type, identity_errors = classify_approval_record(
                gate, allow_legacy_human=False
            )
            if (
                gate.get("status") != "approved"
                or not reviewer
                or not gate.get("reviewed_at")
                or review_type != HUMAN_STAGE_REVIEW
                or identity_errors
            ):
                errors.append(f"{directory}的{gate_name}不是有效人工审批")
        if approvals.get("research", {}).get("artifact_sha256") != sha256(root / "research.json"):
            errors.append(f"{directory}研究审批哈希不一致")
        if approvals.get("compliance", {}).get("artifact_sha256") != sha256(root / "review.json"):
            errors.append(f"{directory}合规审批哈希不一致")
        if approvals.get("compliance", {}).get("script_sha256") != sha256(root / "approved_script.json"):
            errors.append(f"{directory}脚本审批哈希不一致")
        approved_text = (root / "approved_script.json").read_text(encoding="utf-8")
        for banned in ("闭眼入", "冲就完了", "全城最低价"):
            if banned in approved_text:
                errors.append(f"{directory}最终脚本仍包含被纠错禁用的措辞：{banned}")
        task_media = probe_video(root / "final.mp4")
        media[directory] = task_media
        if not 45 <= task_media["duration_seconds"] <= 60:
            errors.append(f"{directory}成片不在45–60秒")
        if (task_media["width"], task_media["height"]) != (1080, 1920) or task_media["video_codec"] != "h264" or task_media["audio_codec"] != "aac":
            errors.append(f"{directory}不是1080×1920 H.264/AAC")
        errors.extend(f"{directory}: {item}" for item in validate_srt(root / "captions.srt", task_media["duration_seconds"]))
    if len(set(job_ids)) != 3 or set(job_ids) != set(success_jobs):
        errors.append("三个任务Job ID不唯一或与Skill成功来源不同")
    if "" in reviewers:
        errors.append("审批人为空")


def verify(folder: Path) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    media: dict[str, Any] = {}
    actual = _actual_paths(folder)
    if actual != EXPECTED_PATHS:
        missing, extra = sorted(EXPECTED_PATHS - actual), sorted(actual - EXPECTED_PATHS)
        if missing:
            errors.append("缺少文件：" + ", ".join(missing))
        if extra:
            errors.append("存在白名单外文件：" + ", ".join(extra))
        return errors, media
    actual_dirs = _actual_dirs(folder)
    if actual_dirs != EXPECTED_DIRS:
        missing_dirs, extra_dirs = sorted(EXPECTED_DIRS - actual_dirs), sorted(actual_dirs - EXPECTED_DIRS)
        if missing_dirs:
            errors.append("缺少目录：" + ", ".join(missing_dirs))
        if extra_dirs:
            errors.append("存在白名单外目录：" + ", ".join(extra_dirs))
        return errors, media
    for name in sorted(actual):
        errors.extend(_scan_text(folder / name, name))
    try:
        _verify_package_manifest(folder, errors)
        _verify_sums(folder, errors)
        _verify_semantics(folder, errors, media)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        errors.append(f"证据结构解析失败：{type(exc).__name__}: {exc}")
    return errors, media


def main() -> int:
    parser = argparse.ArgumentParser(description="验证v0.3通用餐饮证据闭环")
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    errors, media = verify(args.folder.resolve())
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(json.dumps({"status": "V3_EVIDENCE_OK", "files": 50, "media": media}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
