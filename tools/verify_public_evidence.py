from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.review_policy import (
    approval_validation_line,
    classify_approval_record,
    classify_script_edit_record,
    evidence_status_for_policy,
    normalize_review_policy,
    script_edit_validation_line,
)
from core.motion_director import MotionPlanError, validate_motion_plan
from core.motion_runtime_contract import (
    H264_CODEC_STRATEGY,
    HYPERFRAMES_PATCHED_CLI_SHA256,
    HYPERFRAMES_PATCH_ID,
    HYPERFRAMES_PATCH_VERSION,
    HYPERFRAMES_RENDERER,
    HYPERFRAMES_VERSION,
    MOTION_ENGINE_NAME,
    SYSTEM_BROWSER_MINIMUM_MAJOR,
    SYSTEM_BROWSER_STRATEGY,
)
from core.voice_contract import fixed_voice_delivery_violations


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
MOTION_EVIDENCE_ARTIFACTS = {"contact-sheet.png", "visual-qc.json"}
MOTION_ENGINE_IDENTITY = {
    "name": MOTION_ENGINE_NAME,
    "version": HYPERFRAMES_VERSION,
    "renderer": HYPERFRAMES_RENDERER,
    "mode": "local_cli",
    "selected_mode": "motion",
    "health": "completed",
    "codec_strategy": H264_CODEC_STRATEGY,
    "patch_id": HYPERFRAMES_PATCH_ID,
    "patch_version": HYPERFRAMES_PATCH_VERSION,
    "patched_cli_sha256": HYPERFRAMES_PATCHED_CLI_SHA256,
}
TEXT_SUFFIXES = {".json", ".md", ".srt", ".txt", ".log", ".yml", ".yaml"}
_WINDOWS_USER_HOME_PATTERN = r"C:" + r"\\Users\\"
SECRET_PATTERNS = {
    "API Key": re.compile(r"\b(?:sk|ds)-[A-Za-z0-9_-]{12,}\b"),
    "Authorization": re.compile(r"(?i)authorization\s*[:=]\s*(?!null\b|none\b)[^\s,}\]]+"),
    "Cookie": re.compile(r"(?i)(?:set-cookie|cookie)\s*[:=]\s*(?!null\b|none\b)[^\s,}\]]+"),
    "Windows absolute path": re.compile(r"(?i)(?:^|[\s\"'])(?:[A-Z]:\\|\\\\[^\\\s]+\\)"),
    "User home path": re.compile(r"(?i)(?:/Users/|/home/|" + _WINDOWS_USER_HOME_PATTERN + r")[^\s\"']+"),
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


def probe_video(path: Path, ffprobe_path: Path | str | None = None) -> dict[str, Any]:
    command = str(ffprobe_path) if ffprobe_path is not None else find_ffprobe()
    if ffprobe_path is not None and not Path(command).is_file():
        command = None
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
    duration = float(data.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("FFprobe 返回了无效成片时长")
    return {
        "duration_seconds": round(duration, 3),
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
    """Validate the v0.3 full-text subtitle contract shared by formal engines."""
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
    if contract in {"mpt_v0.3", "motion_v0.3"}:
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
    actual_motion = actual_files & MOTION_EVIDENCE_ARTIFACTS
    manifest_motion = manifest_files & MOTION_EVIDENCE_ARTIFACTS
    for label, present in (("公开包", actual_motion), ("manifest", manifest_motion)):
        if present and present != MOTION_EVIDENCE_ARTIFACTS:
            errors.append(f"{label} 的纯动画视觉证据必须成对包含 contact-sheet.png 与 visual-qc.json")
    if actual_motion != manifest_motion:
        errors.append("纯动画视觉证据文件与 manifest 声明不一致")
    if actual_engine == MPT_ENGINE_ARTIFACTS and manifest_engine == MPT_ENGINE_ARTIFACTS:
        return "mpt_v0.3", errors
    if actual_motion == MOTION_EVIDENCE_ARTIFACTS and manifest_motion == MOTION_EVIDENCE_ARTIFACTS:
        return "motion_v0.3", errors
    return "legacy_v2", errors


def evidence_artifacts_for_contract(contract: str) -> set[str]:
    if contract == "mpt_v0.3":
        return set(MPT_ENGINE_ARTIFACTS)
    if contract == "motion_v0.3":
        return set(MOTION_EVIDENCE_ARTIFACTS)
    if contract == "legacy_v2":
        return set()
    raise ValueError(f"未知公开证据合同：{contract}")


def _validate_v03_review_contract(
    manifest: dict[str, Any], approval_modes: list[str], label: str
) -> list[str]:
    """Bind the v0.3 evidence claim to one explicit server-pinned review policy."""
    raw_policy = manifest.get("review_policy")
    if not isinstance(raw_policy, dict):
        return [f"{label} manifest 缺少显式有效的 review_policy"]
    try:
        policy = normalize_review_policy(raw_policy)
    except (TypeError, ValueError):
        return [f"{label} manifest 的 review_policy 无效"]

    errors: list[str] = []
    policy_mode = policy["stage_review_mode"]
    if len(approval_modes) != 2 or any(mode != policy_mode for mode in approval_modes):
        errors.append(f"{label} manifest 的 review_policy 与两道阶段审查身份不一致")
    if manifest.get("evidence_status") != evidence_status_for_policy(policy):
        errors.append(f"{label} manifest 的 evidence_status 与 review_policy 不一致")
    return errors


def _validate_script_edit_contract(
    manifest: dict[str, Any], approved_script: dict[str, Any], contract: str
) -> list[str]:
    allow_legacy_human = contract == "legacy_v2"
    edit_mode, errors = classify_script_edit_record(
        approved_script, allow_legacy_human=allow_legacy_human
    )
    if errors or edit_mode is None:
        return errors
    if allow_legacy_human:
        return [] if edit_mode == "human" else ["旧 v2 改稿身份不能声明为代理测试"]
    try:
        policy_mode = normalize_review_policy(manifest.get("review_policy"))[
            "stage_review_mode"
        ]
    except (TypeError, ValueError):
        return ["批准稿编辑身份无法绑定到有效 review_policy"]
    if edit_mode != policy_mode:
        return ["批准稿编辑身份与 manifest review_policy 不一致"]
    return []


def _validate_mpt_review_contract(
    manifest: dict[str, Any], approval_modes: list[str]
) -> list[str]:
    return _validate_v03_review_contract(manifest, approval_modes, "MPT")


def _validate_motion_review_contract(
    manifest: dict[str, Any], approval_modes: list[str]
) -> list[str]:
    return _validate_v03_review_contract(manifest, approval_modes, "Motion")


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


def _validate_motion_evidence(
    folder: Path,
    run_report: dict[str, Any],
    approved_script: str,
) -> list[str]:
    """Bind a public motion claim to HyperFrames, its plan, subtitles, and visual QC."""
    errors: list[str] = []
    if run_report.get("status") != "complete" or run_report.get("production_mode") != "motion":
        errors.append("run_report.json 不是完成的 motion_v0.3 运行")
    production_engine = run_report.get("production_engine")
    if not isinstance(production_engine, dict) or any(
        production_engine.get(key) != value for key, value in MOTION_ENGINE_IDENTITY.items()
    ):
        errors.append("run_report.json 的 HyperFrames 固定版本身份不一致")
        production_engine = {}

    render = run_report.get("render")
    if not isinstance(render, dict):
        errors.append("run_report.json 缺少纯动画渲染报告")
        render = {}
    expected_render = {
        "mode": "animated_hyperframes",
        "production_mode": "motion",
        "runtime_version": HYPERFRAMES_VERSION,
        "renderer": HYPERFRAMES_RENDERER,
        "video_codec": "h264",
        "audio_codec": "aac",
        "width": 1080,
        "height": 1920,
    }
    if any(render.get(key) != value for key, value in expected_render.items()):
        errors.append("run_report.json 的纯动画渲染身份或媒体合同不一致")
    if render.get("diagnostic_only") is True:
        errors.append("motion_v0.3 不得使用诊断渲染适配器")
    if render.get("runtime_source") != "packaged":
        errors.append("motion_v0.3 必须来自正式便携包内置 HyperFrames 运行时")
    browser_version = render.get("browser_version")
    browser_identity_valid = (
        render.get("browser_strategy") == SYSTEM_BROWSER_STRATEGY
        and render.get("browser_minimum_major") == SYSTEM_BROWSER_MINIMUM_MAJOR
        and isinstance(browser_version, str)
        and re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", browser_version) is not None
        and int(browser_version.split(".", 1)[0]) >= SYSTEM_BROWSER_MINIMUM_MAJOR
    )
    if not browser_identity_valid:
        errors.append("motion_v0.3 缺少启动器验证的受信系统Edge身份")
    if any(
        production_engine.get(key) != render.get(key)
        for key in ("browser_strategy", "browser_version", "browser_minimum_major")
    ):
        errors.append("run_report.json 的生产引擎与渲染浏览器身份不一致")

    try:
        motion_plan = json.loads((folder / "motion_plan.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        motion_plan = None
        errors.append("motion_plan.json 不是有效 JSON")
    motion_validation: dict[str, Any] | None = None
    if isinstance(motion_plan, dict):
        try:
            motion_validation = validate_motion_plan(motion_plan)
        except (MotionPlanError, TypeError, ValueError) as exc:
            errors.append(f"motion_plan.json 未通过可信动画积木验证：{exc}")
        scenes = motion_plan.get("scenes")
        if isinstance(scenes, list):
            approved_binding = caption_binding_text(approved_script)
            plan_binding = caption_binding_text(
                "".join(str(scene.get("caption", "")) for scene in scenes if isinstance(scene, dict))
            )
            if not approved_binding or plan_binding != approved_binding:
                errors.append("motion_plan.json 字幕正文与 approved_script.json 不一致")
        else:
            errors.append("motion_plan.json 缺少有效场景")
        if motion_plan.get("format") != {
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "aspect_ratio": "9:16",
        }:
            errors.append("motion_plan.json 画幅合同无效")
        try:
            plan_duration = float(motion_plan.get("duration_seconds", 0))
            render_duration = float(render.get("duration_seconds", 0))
        except (TypeError, ValueError):
            errors.append("motion_plan.json 或 run_report.json 的时长无效")
        else:
            if (
                not math.isfinite(plan_duration)
                or not math.isfinite(render_duration)
                or not 45 <= plan_duration <= 60
                or abs(plan_duration - render_duration) > 0.05
            ):
                errors.append("motion_plan.json 与纯动画渲染时长不一致")
    if motion_validation is not None and render.get("motion_validation") != motion_validation:
        errors.append("run_report.json 的可信动画积木验证摘要不一致")
    voice_violations = fixed_voice_delivery_violations(
        run_report.get("voice"),
        script=approved_script,
        voice_path=folder / "voice.wav",
        motion_plan=motion_plan,
    )
    if voice_violations:
        errors.append("run_report.json 固定大众播报配音合同无效：" + ",".join(voice_violations))

    approved_binding = caption_binding_text(approved_script)
    expected_caption_hash = hashlib.sha256(approved_binding.encode("utf-8")).hexdigest().upper()
    caption_validation = render.get("caption_validation")
    if (
        not isinstance(caption_validation, dict)
        or caption_validation.get("status") != "passed"
        or caption_validation.get("text_sha256") != expected_caption_hash
    ):
        errors.append("run_report.json 缺少与批准稿全文绑定的动画字幕验证")
    elif isinstance(motion_plan, dict) and isinstance(motion_plan.get("scenes"), list):
        if caption_validation.get("cue_count") != len(motion_plan["scenes"]):
            errors.append("动画字幕条目数量与 motion_plan.json 场景不一致")

    visual_path = folder / "visual-qc.json"
    contact_path = folder / "contact-sheet.png"
    try:
        visual_qc = json.loads(visual_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        visual_qc = None
        errors.append("visual-qc.json 不是有效 JSON")
    contact_header = b""
    if contact_path.is_file():
        with contact_path.open("rb") as source:
            contact_header = source.read(8)
    if contact_header != b"\x89PNG\r\n\x1a\n":
        errors.append("contact-sheet.png 不是有效 PNG 视觉证据")
    if not isinstance(visual_qc, dict):
        return errors
    if (
        visual_qc.get("schema_version") != 1
        or visual_qc.get("status") != "passed"
        or visual_qc.get("sample_count") != 12
        or visual_qc.get("blocking_reasons") != []
        or visual_qc.get("review_reasons") != []
        or not isinstance(visual_qc.get("checks"), dict)
        or not isinstance(visual_qc.get("frames"), list)
        or len(visual_qc.get("frames", [])) != 12
    ):
        errors.append("visual-qc.json 不是通过的 12 帧正式视觉门禁报告")
    video = visual_qc.get("video")
    final_path = folder / "final.mp4"
    if (
        not isinstance(video, dict)
        or video.get("name") != "final.mp4"
        or video.get("size") != final_path.stat().st_size
        or str(video.get("sha256", "")).upper() != sha256(final_path).upper()
    ):
        errors.append("visual-qc.json 与 final.mp4 不一致")
    visual_summary = render.get("visual_qc")
    expected_visual_summary = {
        "status": "passed",
        "sample_count": 12,
        "blocking_reasons": [],
        "review_reasons": [],
        "report_sha256": sha256(visual_path).upper(),
        "contact_sheet_sha256": sha256(contact_path).upper(),
    }
    if visual_summary != expected_visual_summary:
        errors.append("run_report.json 的视觉门禁摘要与公开视觉证据不一致")
    return errors


def verify(
    folder: Path,
    *,
    ffprobe_path: Path | str | None = None,
) -> tuple[list[str], dict[str, Any]]:
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
    errors.extend(_validate_script_edit_contract(manifest, approved_script, contract))
    expected_files = REQUIRED | evidence_artifacts_for_contract(contract)
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
        errors.append("两道阶段审查门禁未全部通过")
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
    approval_modes: list[str] = []
    for gate in (research_approval, compliance_approval):
        if not gate.get("reviewer") or not gate.get("reviewed_at"):
            errors.append("阶段审查缺少操作者或时间")
        mode, identity_errors = classify_approval_record(
            gate, allow_legacy_human=contract == "legacy_v2"
        )
        errors.extend(identity_errors)
        if mode:
            approval_modes.append(mode)
    validation_text = (folder / "VALIDATION.md").read_text(encoding="utf-8")
    expected_approval_line, line_errors = approval_validation_line(
        approvals, allow_legacy_human=contract == "legacy_v2"
    )
    errors.extend(line_errors)
    if expected_approval_line and expected_approval_line not in validation_text:
        errors.append("VALIDATION.md 的审查身份说明与 approvals.json 不一致")
    expected_edit_line, edit_line_errors = script_edit_validation_line(
        approved_script, allow_legacy_human=contract == "legacy_v2"
    )
    errors.extend(edit_line_errors)
    if expected_edit_line and expected_edit_line not in validation_text:
        errors.append("VALIDATION.md 的改稿身份说明与 approved_script.json 不一致")
    if contract == "mpt_v0.3":
        errors.extend(_validate_mpt_review_contract(manifest, approval_modes))
    elif contract == "motion_v0.3":
        errors.extend(_validate_motion_review_contract(manifest, approval_modes))

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
    elif contract == "motion_v0.3":
        errors.extend(_validate_motion_evidence(folder, run_report, approved_script_text))
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

    media = probe_video(folder / "final.mp4", ffprobe_path=ffprobe_path)
    try:
        media_duration = float(media["duration_seconds"])
    except (KeyError, TypeError, ValueError):
        media_duration = 0.0
    if not math.isfinite(media_duration) or not 45 <= media_duration <= 60:
        errors.append("成片时长不在 45–60 秒")
    if (media["width"], media["height"]) != (1080, 1920):
        errors.append("成片不是 1080×1920")
    if media["video_codec"] != "h264" or media["audio_codec"] != "aac":
        errors.append("成片不是 H.264/AAC")
    if contract == "motion_v0.3":
        render = run_report.get("render") if isinstance(run_report.get("render"), dict) else {}
        try:
            reported_duration = float(render.get("duration_seconds", 0))
        except (TypeError, ValueError):
            reported_duration = 0.0
        if (
            not math.isfinite(reported_duration)
            or not math.isfinite(media_duration)
            or abs(reported_duration - media_duration) > 0.15
        ):
            errors.append("run_report.json 的纯动画时长与 final.mp4 不一致")
    errors.extend(validate_srt(
        folder / "captions.srt",
        media_duration,
        approved_script_text,
        contract=contract,
    ))
    media["evidence_contract"] = contract
    # Several independent contract layers may identify the same root cause.
    # Keep the CLI actionable without weakening any check.
    return list(dict.fromkeys(errors)), media


def verify_archive(
    archive_path: Path,
    *,
    ffprobe_path: Path | str | None = None,
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    """Verify a public evidence ZIP without trusting archive paths.

    The public package is intentionally flat.  Refusing nested, duplicate,
    encrypted, or oversized entries keeps this helper safe for the in-app
    replay check as well as for release QA.
    """

    archive_path = Path(archive_path)
    if not archive_path.is_file():
        return ["公开证据 ZIP 不存在"], {}, {}
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            allowed_names = REQUIRED | MPT_ENGINE_ARTIFACTS | MOTION_EVIDENCE_ARTIFACTS
            unsafe = [
                name
                for name in names
                if not name
                or "\\" in name
                or Path(name).name != name
                or name in {".", ".."}
                or name not in allowed_names
            ]
            if unsafe:
                return ["公开证据 ZIP 含不安全路径"], {}, {}
            if len(names) != len(set(names)):
                return ["公开证据 ZIP 含重复文件名"], {}, {}
            if any(info.is_dir() or info.flag_bits & 0x1 for info in infos):
                return ["公开证据 ZIP 含目录或加密条目"], {}, {}
            if len(infos) > 32 or sum(info.file_size for info in infos) > 2_000_000_000:
                return ["公开证据 ZIP 超出安全大小限制"], {}, {}
            with tempfile.TemporaryDirectory(prefix="shiyi-public-evidence-verify-") as temp_name:
                folder = Path(temp_name)
                for info in infos:
                    target = folder / info.filename
                    with archive.open(info) as source, target.open("wb") as destination:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
                errors, media = verify(folder, ffprobe_path=ffprobe_path)
                manifest: dict[str, Any] = {}
                if (folder / "manifest.json").is_file():
                    try:
                        payload = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
                        if isinstance(payload, dict):
                            manifest = payload
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                        pass
                return errors, media, manifest
    except Exception as exc:
        return [f"公开证据 ZIP 无法读取：{type(exc).__name__}"], {}, {}


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
