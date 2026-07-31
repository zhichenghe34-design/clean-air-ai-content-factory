from __future__ import annotations

import argparse
import json
import mimetypes
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from verify_public_evidence import CANONICAL, REQUIRED, probe_video, scan_text, sha256, validate_srt, verify


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an exact, sanitized v2 public evidence package.")
    parser.add_argument("source", type=Path, help="Successful runs/<run_id>/artifacts directory")
    parser.add_argument("output", type=Path, help="Destination folder; must not already exist")
    parser.add_argument("--zip", dest="zip_path", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    zip_path = (args.zip_path or output.with_suffix(".zip")).resolve()
    if output.exists() or zip_path.exists():
        raise SystemExit("输出目录或 ZIP 已存在；为避免覆盖证据，请使用新路径")

    source_manifest_path = source / "manifest.json"
    if not source_manifest_path.is_file():
        raise SystemExit("源目录没有 manifest.json")
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    if source_manifest.get("schema_version") != 2 or source_manifest.get("stage") != "render" or source_manifest.get("status") != "complete":
        raise SystemExit("源目录不是成功的 v2 render 运行")
    needed = set(CANONICAL) | {"approvals.json"}
    missing = sorted(name for name in needed if not (source / name).is_file())
    if missing:
        raise SystemExit(f"源运行缺少产物：{', '.join(missing)}")

    temp_parent = output.parent
    temp_parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="public-evidence-", dir=temp_parent))
    try:
        for name in sorted(needed):
            shutil.copy2(source / name, temporary / name)
        source_findings = []
        for path in temporary.iterdir():
            source_findings.extend(scan_text(path))
        if source_findings:
            raise RuntimeError("公开前扫描失败：" + "; ".join(source_findings))

        media = probe_video(temporary / "final.mp4")
        subtitle_errors = validate_srt(temporary / "captions.srt", media["duration_seconds"])
        if subtitle_errors:
            raise RuntimeError("字幕校验失败：" + "; ".join(subtitle_errors))
        validation = (
            "# v2 公开证据验证说明\n\n"
            f"- Job ID：`{source_manifest.get('job_id')}`\n"
            f"- Run ID：`{source_manifest.get('run_id')}`\n"
            f"- 源运行 manifest SHA-256：`{sha256(source_manifest_path)}`\n"
            f"- 单任务 Provider 预算：`{source_manifest.get('budget', {}).get('attempted')}/7 attempted`\n"
            f"- 成片：`{media['duration_seconds']}` 秒，`{media['width']}×{media['height']}`，`{media['video_codec']}/{media['audio_codec']}`\n"
            "- 字幕：时间轴从 0 连续到成片末尾，允许误差 0.15 秒。\n"
            "- 审批：研究 finding 逐项决定与最终脚本合规放行均来自用户本人操作；自动流程不代签。\n"
            "- 脱敏：包内不含 Key、Cookie、Authorization、本机绝对路径、原始配置、邮箱或手机号。\n"
            "- 清单：公开包在不改动原十项产物和 approvals.json 的前提下，加入本说明并重新计算逐文件哈希。\n"
        )
        (temporary / "VALIDATION.md").write_text(validation, encoding="utf-8")

        manifest = dict(source_manifest)
        manifest["public_package"] = True
        manifest["source_manifest_sha256"] = sha256(source_manifest_path)
        manifest["packaged_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        manifest["artifacts"] = []
        for name in sorted(REQUIRED - {"manifest.json"}):
            path = temporary / name
            manifest["artifacts"].append({
                "name": name,
                "stage": "render" if name != "VALIDATION.md" else "validation",
                "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
                "size": path.stat().st_size,
                "sha256": sha256(path),
            })
        (temporary / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        errors, _ = verify(temporary)
        if errors:
            raise RuntimeError("公开包终验失败：" + "; ".join(errors))
        temporary.replace(output)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in sorted(REQUIRED):
                data = (output / name).read_bytes()
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
        print(json.dumps({"status": "PUBLIC_EVIDENCE_BUILT", "folder": output.name, "zip": zip_path.name, "files": len(REQUIRED)}, ensure_ascii=False))
        return 0
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
