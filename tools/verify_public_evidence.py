from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


CANONICAL = [
    "research.json", "insight.json", "script_variants.json", "approved_script.json",
    "review.json", "voice.wav", "captions.srt", "motion_plan.json", "final.mp4",
    "run_report.json",
]
REQUIRED = set(CANONICAL) | {"approvals.json", "manifest.json", "VALIDATION.md"}
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


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_text(path: Path) -> list[str]:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    findings = []
    for label, pattern in SECRET_PATTERNS.items():
        if pattern.search(text):
            findings.append(f"{path.name}: {label}")
    sensitive_fields = re.compile(r'(?i)"(?:api_?key|token|secret|password|cookie|authorization)"\s*:\s*"(?!\s*")[^"]+"')
    if sensitive_fields.search(text):
        findings.append(f"{path.name}: sensitive configuration field")
    return findings


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
            errors.append(f"captions.srt 第 {index} 块格式错误")
            continue
        start_text, end_text = lines[1].split(" --> ", 1)
        start, end = parse_srt_time(start_text), parse_srt_time(end_text)
        if abs(start - cursor) > 0.05:
            errors.append(f"captions.srt 第 {index} 块与上一块不连续")
        if end <= start:
            errors.append(f"captions.srt 第 {index} 块结束时间无效")
        cursor = end
    if abs(cursor - duration) > 0.15:
        errors.append(f"字幕终点 {cursor:.3f}s 与视频 {duration:.3f}s 不一致")
    return errors


def verify(folder: Path) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    actual = {item.name for item in folder.iterdir() if item.is_file()}
    missing = sorted(REQUIRED - actual)
    extra = sorted(actual - REQUIRED)
    if missing:
        errors.append(f"缺少公开产物：{', '.join(missing)}")
    if extra:
        errors.append(f"存在未声明文件：{', '.join(extra)}")
    if missing:
        return errors, {}

    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    approvals = json.loads((folder / "approvals.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 2 or manifest.get("stage") != "render" or manifest.get("status") != "complete":
        errors.append("manifest 不是成功的 v2 render 清单")
    entries = {str(item.get("name")): item for item in manifest.get("artifacts", []) if isinstance(item, dict)}
    expected_entries = REQUIRED - {"manifest.json"}
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
    if research_approval.get("artifact_sha256") != sha256(folder / "research.json"):
        errors.append("研究审批哈希与 research.json 不一致")
    if compliance_approval.get("artifact_sha256") != sha256(folder / "review.json"):
        errors.append("合规审批哈希与 review.json 不一致")
    if compliance_approval.get("script_sha256") != sha256(folder / "approved_script.json"):
        errors.append("合规审批脚本哈希不一致")
    for gate in (research_approval, compliance_approval):
        if not gate.get("reviewer") or not gate.get("reviewed_at"):
            errors.append("人工审批缺少操作者或时间")

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
    errors.extend(validate_srt(folder / "captions.srt", media["duration_seconds"]))
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
    print(json.dumps({"status": "PUBLIC_EVIDENCE_OK", "files": len(REQUIRED), "media": media}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
