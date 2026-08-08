from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import Any


CANONICAL = [
    "research.json", "insight.json", "script_variants.json", "approved_script.json",
    "review.json", "voice.wav", "captions.srt", "motion_plan.json", "final.mp4",
    "run_report.json",
]
REQUIRED = set(CANONICAL) | {"approvals.json", "manifest.json", "VALIDATION.md"}
MPT_ENGINE_ARTIFACTS = {"engine_report.json", "material_sources.json"}
MPT_ENGINE_IDENTITY = {
    "name": "MoneyPrinterTurbo",
    "version": "1.3.3",
    "commit": "254cd028906ee657eab844dc94087cdbea2a7aa8",
    "mode": "local_http",
}
TEXT_SUFFIXES = {".json", ".md", ".srt", ".txt", ".log", ".yml", ".yaml"}
SECRET_PATTERNS = {
    "API Key": re.compile(r"\b(?:sk|ds)-[A-Za-z0-9_-]{12,}\b"),
    "Authorization": re.compile(r"(?i)authorization\s*[:=]\s*(?!null\b|none\b)[^\s,}\]]+"),
    "Cookie": re.compile(r"(?i)(?:set-cookie|cookie)\s*[:=]\s*(?!null\b|none\b)[^\s,}\]]+"),
    "Windows absolute path": re.compile(r"(?i)(?:^|[\s\"'])(?:[A-Z]:\\|\\\\[^\\\s]+\\)"),
    "User home path": re.compile(r"(?i)(?:/Users/|/home/|C:\\Users\\)[^\s\"']+"),
    "file URI": re.compile(r"(?i)file:///[A-Za-z]:/"),
    "email": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    "mainland phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
}
SENSITIVE_FIELDS = re.compile(
    r'(?i)"(?:api_?key|token|secret|password|cookie|authorization)"\s*:\s*"(?!\s*")[^"]+"'
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_content(name: str, text: str) -> list[str]:
    findings = []
    for label, pattern in SECRET_PATTERNS.items():
        if pattern.search(text):
            findings.append(f"{name}: {label}")
    if SENSITIVE_FIELDS.search(text):
        findings.append(f"{name}: sensitive configuration field")
    return findings


def scan_text(path: Path) -> list[str]:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return []
    return scan_content(path.name, path.read_text(encoding="utf-8", errors="replace"))


def find_ffprobe() -> str | None:
    configured = os.getenv("FFPROBE_PATH")
    if configured and Path(configured).is_file():
        return configured
    return shutil.which("ffprobe")


def probe_video(path: Path) -> dict[str, Any]:
    command = find_ffprobe()
    if not command:
        raise RuntimeError("FFprobe 不可用，无法验证公开成片")
    output = subprocess.check_output(
        [command, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    data = json.loads(output)
    video = next((item for item in data.get("streams", []) if item.get("codec_type") == "video"), {})
    audio = next((item for item in data.get("streams", []) if item.get("codec_type") == "audio"), {})
    return {
        "duration_seconds": round(float(data.get("format", {}).get("duration", 0)), 3),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
    }


def parse_srt_time(value: str) -> float:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value.strip())
    if not match:
        raise ValueError("invalid SRT timestamp")
    hours, minutes, seconds, milliseconds = (int(item) for item in match.groups())
    if minutes >= 60 or seconds >= 60:
        raise ValueError("invalid SRT timestamp")
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def caption_binding_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and unicodedata.category(character)[0] not in {"C", "P", "Z"}
    )


def validate_legacy_srt(path: Path, duration: float) -> list[str]:
    """Preserve the frozen v2 subtitle timing contract."""
    errors: list[str] = []
    blocks = re.split(r"\r?\n\s*\r?\n", path.read_text(encoding="utf-8-sig").strip())
    cursor = 0.0
    for index, block in enumerate(blocks, start=1):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or " --> " not in lines[1]:
            errors.append(f"captions.srt 第 {index} 块格式错误")
            continue
        start_text, end_text = lines[1].split(" --> ", 1)
        try:
            start, end = parse_srt_time(start_text), parse_srt_time(end_text)
        except ValueError:
            errors.append(f"captions.srt 第 {index} 块时间格式无效")
            continue
        if abs(start - cursor) > 0.05:
            errors.append(f"captions.srt 第 {index} 块与上一块不连续")
        if end <= start:
            errors.append(f"captions.srt 第 {index} 块结束时间无效")
        cursor = end
    if abs(cursor - duration) > 0.15:
        errors.append(f"字幕终点 {cursor:.3f}s 与视频 {duration:.3f}s 不一致")
    return errors


def validate_mpt_srt(path: Path, duration: float, approved_script: str) -> list[str]:
    """Mirror the production MPT contract: full text, no overlap, and gaps <= 2 seconds."""
    errors: list[str] = []
    blocks = [
        item for item in re.split(
            r"\r?\n\s*\r?\n",
            path.read_text(encoding="utf-8-sig").strip(),
        )
        if item.strip()
    ]
    if not 1 <= len(blocks) <= 500:
        return ["captions.srt 字幕条目数量无效"]

    previous_end = 0.0
    maximum_gap = 0.0
    caption_fragments: list[str] = []
    for index, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        if len(lines) < 3 or lines[0].strip() != str(index) or " --> " not in lines[1]:
            errors.append(f"captions.srt 第 {index} 块编号或结构不连续")
            continue
        start_text, end_text = lines[1].split(" --> ", 1)
        try:
            start, end = parse_srt_time(start_text), parse_srt_time(end_text)
        except ValueError:
            errors.append(f"captions.srt 第 {index} 块时间格式无效")
            continue
        caption_text = "".join(lines[2:]).strip()
        if start + 0.001 < previous_end:
            errors.append(f"captions.srt 第 {index} 块与上一块重叠")
        if end <= start:
            errors.append(f"captions.srt 第 {index} 块结束时间无效")
        if not caption_text:
            errors.append(f"captions.srt 第 {index} 块文本为空")
        maximum_gap = max(maximum_gap, start - previous_end)
        previous_end = end
        caption_fragments.extend(lines[2:])

    trailing_gap = max(0.0, duration - previous_end)
    maximum_gap = max(maximum_gap, trailing_gap)
    if previous_end > duration + 0.75:
        errors.append(f"字幕终点 {previous_end:.3f}s 超出视频 {duration:.3f}s")
    if maximum_gap > 2.0:
        errors.append(f"字幕最大空隙 {maximum_gap:.3f}s 超过 2.000s")

    approved_binding = caption_binding_text(approved_script)
    caption_binding = caption_binding_text("".join(caption_fragments))
    if not approved_binding or caption_binding != approved_binding:
        errors.append("captions.srt 正文与 approved_script.json 不一致")
    return errors


def validate_srt(
    path: Path,
    duration: float,
    approved_script: str = "",
    *,
    contract: str = "legacy_v2",
) -> list[str]:
    if contract == "legacy_v2":
        return validate_legacy_srt(path, duration)
    if contract == "mpt_v0.3":
        return validate_mpt_srt(path, duration, approved_script)
    return [f"未知字幕验证合同：{contract}"]


def _engine_contract(
    actual_files: set[str],
    manifest_files: set[str],
) -> tuple[str, list[str]]:
    errors: list[str] = []
    actual_engine = actual_files & MPT_ENGINE_ARTIFACTS
    manifest_engine = manifest_files & MPT_ENGINE_ARTIFACTS
    for label, present in (("公开包", actual_engine), ("manifest", manifest_engine)):
        if present and present != MPT_ENGINE_ARTIFACTS:
            errors.append(f"{label} 的 MPT 证据必须成对包含 engine_report.json 与 material_sources.json")
    if actual_engine != manifest_engine:
        errors.append("MPT 证据文件与 manifest 声明不一致")
    if actual_engine == MPT_ENGINE_ARTIFACTS and manifest_engine == MPT_ENGINE_ARTIFACTS:
        return "mpt_v0.3", errors
    return "legacy_v2", errors


def _validate_mpt_evidence(
    folder: Path,
    engine_report: dict[str, Any],
    approved_script: str,
) -> list[str]:
    errors: list[str] = []
    if engine_report.get("schema_version") != 1 or engine_report.get("status") != "complete":
        errors.append("engine_report.json 不是完成的 MPT v1 报告")
    if engine_report.get("engine") != MPT_ENGINE_IDENTITY:
        errors.append("engine_report.json 的 MPT 固定版本身份不一致")
    expected_script_hash = hashlib.sha256(approved_script.encode("utf-8")).hexdigest().upper()
    if engine_report.get("script_sha256") != expected_script_hash:
        errors.append("engine_report.json 与 approved_script.json 正文不一致")

    artifacts = engine_report.get("artifacts")
    artifact_index: dict[str, dict[str, Any]] = {}
    if isinstance(artifacts, list):
        for item in artifacts:
            if not isinstance(item, dict) or not isinstance(item.get("relative_path"), str):
                errors.append("engine_report.json 产物清单结构无效")
                continue
            relative_path = str(item["relative_path"])
            if relative_path in artifact_index:
                errors.append("engine_report.json 包含重复产物")
            artifact_index[relative_path] = item
    else:
        errors.append("engine_report.json 缺少产物清单")
    if set(artifact_index) != {"final.mp4", "captions.srt", "material_sources.json"}:
        errors.append("engine_report.json 的公开产物集合不符合 MPT 合同")
    for name, item in artifact_index.items():
        path = folder / name
        if not path.is_file():
            errors.append(f"MPT 报告产物缺失：{name}")
            continue
        if item.get("size") != path.stat().st_size or str(item.get("sha256", "")).upper() != sha256(path).upper():
            errors.append(f"MPT 报告产物哈希或大小不一致：{name}")

    control = engine_report.get("control_layer_validation")
    if not isinstance(control, dict) or control.get("status") != "passed":
        errors.append("engine_report.json 缺少通过的控制层验证")
    else:
        for field, name in (
            ("canonical_voice_sha256", "voice.wav"),
            ("final_video_sha256", "final.mp4"),
            ("captions_sha256", "captions.srt"),
        ):
            if str(control.get(field, "")).upper() != sha256(folder / name).upper():
                errors.append(f"MPT 控制层哈希不一致：{name}")

    try:
        material_sources = json.loads((folder / "material_sources.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        material_sources = None
    if (
        not isinstance(material_sources, dict)
        or material_sources.get("schema_version") != 1
        or not isinstance(material_sources.get("sources"), list)
        or not material_sources.get("sources")
        or material_sources.get("task_id") != engine_report.get("task_id")
    ):
        errors.append("material_sources.json 与 MPT 任务或来源清单不一致")
    return errors


def verify(folder: Path) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    actual = {item.name for item in folder.iterdir() if item.is_file()}
    missing = sorted(REQUIRED - actual)
    if missing:
        errors.append(f"缺少公开产物：{', '.join(missing)}")
    if missing:
        return errors, {}

    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    approvals = json.loads((folder / "approvals.json").read_text(encoding="utf-8"))
    approved_script = json.loads((folder / "approved_script.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 2 or manifest.get("stage") not in {"render", "report_rebuild"} or manifest.get("status") != "complete":
        errors.append("manifest 不是成功发布的 v2 render/report_rebuild 清单")
    entries = {str(item.get("name")): item for item in manifest.get("artifacts", []) if isinstance(item, dict)}
    contract, contract_errors = _engine_contract(actual, set(entries))
    errors.extend(contract_errors)
    expected_files = REQUIRED | (MPT_ENGINE_ARTIFACTS if contract == "mpt_v0.3" else set())
    extra = sorted(actual - expected_files)
    if extra:
        errors.append(f"存在未声明文件：{', '.join(extra)}")
    expected_entries = expected_files - {"manifest.json"}
    if set(entries) != expected_entries:
        errors.append("manifest 文件集合与公开包不一致")
    for name in sorted(expected_entries):
        path = folder / name
        entry = entries.get(name, {})
        if entry.get("sha256") != sha256(path):
            errors.append(f"{name} SHA-256 不一致")
        if entry.get("size") != path.stat().st_size:
            errors.append(f"{name} 大小不一致")
        expected_mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if entry.get("mime") != expected_mime:
            errors.append(f"{name} MIME 不一致")

    research_approval = approvals.get("research", {})
    compliance_approval = approvals.get("compliance", {})
    if research_approval.get("status") != "approved" or compliance_approval.get("status") != "approved":
        errors.append("两道人工审批未全部通过")
    source_research_hash = research_approval.get("source_artifact_sha256", research_approval.get("artifact_sha256"))
    if research_approval.get("artifact_sha256") != source_research_hash:
        errors.append("研究审批原始哈希记录不一致")
    if manifest.get("approval_hashes", {}).get("research") != source_research_hash:
        errors.append("研究审批原始哈希与源 manifest 记录不一致")
    public_research_hash = research_approval.get("public_artifact_sha256", research_approval.get("artifact_sha256"))
    if public_research_hash != sha256(folder / "research.json"):
        errors.append("研究审批公开副本哈希与 research.json 不一致")
    if compliance_approval.get("artifact_sha256") != sha256(folder / "review.json"):
        errors.append("合规审批哈希与 review.json 不一致")
    if compliance_approval.get("script_sha256") != sha256(folder / "approved_script.json"):
        errors.append("合规审批脚本哈希不一致")
    for gate in (research_approval, compliance_approval):
        if not gate.get("reviewer") or not gate.get("reviewed_at"):
            errors.append("人工审批缺少操作者或时间")

    run_report = json.loads((folder / "run_report.json").read_text(encoding="utf-8"))
    engine_summary = run_report.get("production_engine")
    approved_script_text = str(approved_script.get("script", ""))
    if contract == "mpt_v0.3":
        expected_summary = {
            "name": MPT_ENGINE_IDENTITY["name"],
            "version": MPT_ENGINE_IDENTITY["version"],
            "commit": MPT_ENGINE_IDENTITY["commit"],
            "mode": MPT_ENGINE_IDENTITY["mode"],
            "status": "complete",
        }
        if not isinstance(engine_summary, dict) or any(
            engine_summary.get(key) != value for key, value in expected_summary.items()
        ):
            errors.append("run_report.json 的 MPT 引擎摘要与固定版本不一致")
        try:
            engine_report = json.loads((folder / "engine_report.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            engine_report = {}
            errors.append("engine_report.json 不是有效 JSON")
        errors.extend(_validate_mpt_evidence(folder, engine_report, approved_script_text))
    elif engine_summary not in (None, {}):
        errors.append("旧 v2 合同不得声明未随包交付的生产引擎")

    budget = manifest.get("budget", {})
    attempted = int(budget.get("attempted", 0))
    limit = int(budget.get("limit", 0))
    if limit != 7 or attempted > limit:
        errors.append(f"预算记录异常：attempted={attempted}, limit={limit}")

    for path in folder.iterdir():
        if path.is_file():
            errors.extend(scan_text(path))

    media = probe_video(folder / "final.mp4")
    if not 45 <= media["duration_seconds"] <= 60:
        errors.append("成片时长不在 45–60 秒")
    if (media["width"], media["height"]) != (1080, 1920):
        errors.append("成片不是 1080×1920")
    if media["video_codec"] != "h264" or media["audio_codec"] != "aac":
        errors.append("成片不是 H.264/AAC")
    errors.extend(validate_srt(
        folder / "captions.srt",
        media["duration_seconds"],
        approved_script_text,
        contract=contract,
    ))
    media["evidence_contract"] = contract
    return errors, media


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a sanitized v2 public evidence package.")
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    errors, media = verify(args.folder.resolve())
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    file_count = len([item for item in args.folder.resolve().iterdir() if item.is_file()])
    print(json.dumps({"status": "PUBLIC_EVIDENCE_OK", "files": file_count, "media": media}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
