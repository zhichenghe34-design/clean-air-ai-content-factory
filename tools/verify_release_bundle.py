from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from build_release_bundle import (
    EXPECTED_BRANCH,
    FIXED_ZIP_TIME,
    PAYLOAD_NAMES,
    RELEASE_NAMES,
    mime_for,
)
from verify_public_evidence import scan_content, scan_text, verify as verify_public_evidence


EXPECTED_EVIDENCE_SHA256 = "CF21EF0D6083928BA814ABE10F6C72400566F6679E9182304E5E4DE249FA3858"
EXPECTED_SAMPLE_SHA256 = "A117026C78F9656FF7A2E923242D0F5DF737D872652A1A446D1F786BE31E3D02"
EXPECTED_FINAL_SHA256 = "2EE8A80E9DE7C3BB821BFA50D8ABC46E20E72DDA999C18EC11A5975A967B4403"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def find_ffprobe() -> str | None:
    configured = os.getenv("FFPROBE_PATH")
    if configured and Path(configured).is_file():
        return configured
    return shutil.which("ffprobe")


def probe(path: Path) -> dict:
    ffprobe = find_ffprobe()
    if not ffprobe:
        raise RuntimeError("FFprobe 不可用")
    raw = subprocess.check_output(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    data = json.loads(raw)
    video = next((item for item in data.get("streams", []) if item.get("codec_type") == "video"), {})
    audio = next((item for item in data.get("streams", []) if item.get("codec_type") == "audio"), None)
    fps_text = str(video.get("avg_frame_rate", "0/1"))
    numerator, denominator = (float(part) for part in fps_text.split("/", 1))
    metadata = {
        "format_tags": data.get("format", {}).get("tags", {}),
        "stream_tags": [item.get("tags", {}) for item in data.get("streams", [])],
    }
    return {
        "duration": float(data.get("format", {}).get("duration", 0)),
        "codec": video.get("codec_name"),
        "pixel_format": video.get("pix_fmt"),
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": numerator / denominator if denominator else 0,
        "audio_codec": audio.get("codec_name") if audio else None,
        "metadata": metadata,
        "faststart": has_faststart(path),
    }


def has_faststart(path: Path) -> bool:
    atoms: list[str] = []
    file_size = path.stat().st_size
    with path.open("rb") as handle:
        position = 0
        while position + 8 <= file_size:
            handle.seek(position)
            header = handle.read(8)
            size = int.from_bytes(header[:4], "big")
            atom_type = header[4:8].decode("latin-1")
            header_size = 8
            if size == 1:
                extended = handle.read(8)
                if len(extended) != 8:
                    break
                size = int.from_bytes(extended, "big")
                header_size = 16
            elif size == 0:
                size = file_size - position
            if size < header_size or position + size > file_size:
                break
            atoms.append(atom_type)
            position += size
    return "moov" in atoms and "mdat" in atoms and atoms.index("moov") < atoms.index("mdat")


def validate_pdf(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        return [f"无法读取 PDF：{exc}"]
    reader = PdfReader(str(path))
    if len(reader.pages) != 8:
        errors.append(f"PDF 页数为 {len(reader.pages)}，应为 8")
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    errors.extend(scan_content(f"{path.name} 提取文本", text))
    metadata_text = json.dumps(dict(reader.metadata or {}), ensure_ascii=False, default=str)
    errors.extend(scan_content(f"{path.name} 元数据", metadata_text))
    for marker in ("净界 AI 内容工厂 v2", "127.0.0.1:8765", "13", "74", "Agent"):
        if marker not in text:
            errors.append(f"PDF 缺少口径：{marker}")
    if re.search(r"(?:A\s+I|Deep\s+Seek|Vox\s+CPM)", text, re.IGNORECASE):
        errors.append("PDF 存在英文缩写异常拆字")
    links = sum(
        1
        for page in reader.pages
        for annotation in (page.get("/Annots", []) or [])
        if annotation.get_object().get("/Subtype") == "/Link"
    )
    if links < 3:
        errors.append(f"PDF 可点击链接只有 {links} 个，应至少 3 个")
    return errors


def validate_media(folder: Path) -> list[str]:
    errors: list[str] = []
    specifications = {
        "净界AI内容工厂_Agent工作台演示.mp4": {"size": (1440, 900), "duration": (60, 90), "audio": None},
        "净界AI内容工厂_设计基准样片.mp4": {"size": (1080, 1920), "duration": (45, 60), "audio": "aac"},
        "净界AI内容工厂_v2真实联调成片.mp4": {"size": (1080, 1920), "duration": (45, 60), "audio": "aac"},
    }
    for name, expected in specifications.items():
        try:
            media = probe(folder / name)
        except Exception as exc:
            errors.append(f"{name} 无法探测：{exc}")
            continue
        if media["codec"] != "h264" or media["pixel_format"] != "yuv420p":
            errors.append(f"{name} 不是 H.264/yuv420p")
        if (media["width"], media["height"]) != expected["size"]:
            errors.append(f"{name} 分辨率异常：{media['width']}×{media['height']}")
        low, high = expected["duration"]
        if not low <= media["duration"] <= high:
            errors.append(f"{name} 时长异常：{media['duration']:.3f} 秒")
        if expected["audio"] != media["audio_codec"]:
            errors.append(f"{name} 音轨异常：{media['audio_codec']}")
        if name.endswith("Agent工作台演示.mp4") and abs(media["fps"] - 30) > 0.05:
            errors.append(f"{name} 帧率异常：{media['fps']:.3f}")
        if name.endswith("Agent工作台演示.mp4") and not media["faststart"]:
            errors.append(f"{name} 未启用 faststart")
        metadata_text = json.dumps(media["metadata"], ensure_ascii=False, default=str)
        errors.extend(scan_content(f"{name} 元数据", metadata_text))
    return errors


def verify_folder(folder: Path, verify_embedded_evidence: bool = True) -> list[str]:
    errors: list[str] = []
    actual = {item.name for item in folder.iterdir() if item.is_file()}
    if actual != set(RELEASE_NAMES):
        errors.append(f"发布文件集合不一致：{sorted(actual)}")
        return errors

    manifest = json.loads((folder / "release-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        errors.append("release-manifest schema_version 不是 1")
    if manifest.get("branch") != EXPECTED_BRANCH:
        errors.append("release-manifest 分支不正确")
    if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("source_commit", ""))):
        errors.append("release-manifest 缺少完整 source commit")
    if not re.fullmatch(
        r"https://github\.com/zhichenghe34-design/clean-air-ai-content-factory/pull/\d+",
        str(manifest.get("draft_pr_url", "")),
    ):
        errors.append("release-manifest Draft PR 地址不属于当前仓库")
    entries = {entry.get("name"): entry for entry in manifest.get("artifacts", []) if isinstance(entry, dict)}
    if set(entries) != set(PAYLOAD_NAMES):
        errors.append("release-manifest 产物集合不正确")
    for name in PAYLOAD_NAMES:
        entry = entries.get(name, {})
        path = folder / name
        if str(entry.get("sha256", "")).upper() != sha256(path):
            errors.append(f"{name} manifest SHA-256 不一致")
        if entry.get("size") != path.stat().st_size:
            errors.append(f"{name} manifest 大小不一致")
        if entry.get("mime") != mime_for(name):
            errors.append(f"{name} manifest MIME 不一致")

    expected_sums = {
        name: sha256(folder / name)
        for name in (*PAYLOAD_NAMES, "release-manifest.json")
    }
    actual_sums = {}
    for line in (folder / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        actual_sums[name] = digest.upper()
    if actual_sums != expected_sums:
        errors.append("SHA256SUMS.txt 与文件不一致")

    if sha256(folder / "v2-real-deepseek-20260801-022153.zip") != EXPECTED_EVIDENCE_SHA256:
        errors.append("真实证据 ZIP 哈希发生变化")
    if sha256(folder / "净界AI内容工厂_设计基准样片.mp4") != EXPECTED_SAMPLE_SHA256:
        errors.append("设计基准样片哈希发生变化")
    if sha256(folder / "净界AI内容工厂_v2真实联调成片.mp4") != EXPECTED_FINAL_SHA256:
        errors.append("真实联调成片哈希发生变化")

    for name in RELEASE_NAMES:
        errors.extend(scan_text(folder / name))
    errors.extend(validate_pdf(folder / "净界AI内容工厂_v2补充材料.pdf"))
    errors.extend(validate_media(folder))

    if verify_embedded_evidence:
        with tempfile.TemporaryDirectory(prefix="verify-evidence-") as temporary:
            target = Path(temporary)
            with zipfile.ZipFile(folder / "v2-real-deepseek-20260801-022153.zip") as archive:
                names = archive.namelist()
                if any(Path(name).name != name for name in names):
                    errors.append("真实证据 ZIP 含目录穿越或嵌套路径")
                else:
                    archive.extractall(target)
                    evidence_errors, _ = verify_public_evidence(target)
                    errors.extend(f"真实证据：{item}" for item in evidence_errors)
                    embedded_final = target / "final.mp4"
                    public_final = folder / "净界AI内容工厂_v2真实联调成片.mp4"
                    if embedded_final.is_file() and sha256(embedded_final) != sha256(public_final):
                        errors.append("顶层真实联调成片与固定证据 ZIP 内 final.mp4 不一致")
                    embedded_validation = target / "VALIDATION.md"
                    if embedded_validation.is_file():
                        validation_text = embedded_validation.read_text(encoding="utf-8").strip()
                        system_text = (folder / "02_系统与证据说明_v2.md").read_text(encoding="utf-8").strip()
                        if not system_text.endswith(validation_text):
                            errors.append("系统与证据说明未逐字包含固定证据 ZIP 的验证说明")
    return errors


def verify_zip(path: Path) -> list[str]:
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="verify-release-") as temporary:
        target = Path(temporary)
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if names != list(RELEASE_NAMES):
                errors.append(f"发布 ZIP 文件顺序或集合不正确：{names}")
                return errors
            if any(Path(name).name != name for name in names):
                return ["发布 ZIP 含目录穿越或嵌套路径"]
            for info in archive.infolist():
                if info.date_time != FIXED_ZIP_TIME:
                    errors.append(f"发布 ZIP 时间戳不固定：{info.filename} {info.date_time}")
                if info.compress_type != zipfile.ZIP_DEFLATED:
                    errors.append(f"发布 ZIP 压缩方式不正确：{info.filename}")
            archive.extractall(target)
        errors.extend(verify_folder(target, verify_embedded_evidence=True))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an Agent workbench release folder and optional ZIP.")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--zip", dest="zip_path", type=Path)
    args = parser.parse_args()
    errors = verify_folder(args.folder.resolve(), verify_embedded_evidence=True)
    if args.zip_path:
        errors.extend(verify_zip(args.zip_path.resolve()))
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(json.dumps({"status": "RELEASE_BUNDLE_OK", "files": len(RELEASE_NAMES)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
