from __future__ import annotations

import argparse
import json
import mimetypes
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.review_policy import approval_validation_line, script_edit_validation_line
from core.evidence_report import (
    HUMAN_REPORT_NAME,
    build_human_evidence_report,
)

from tools.verify_public_evidence import (
    CANONICAL,
    REQUIRED,
    SECRET_PATTERNS,
    _engine_contract,
    evidence_artifacts_for_contract,
    probe_video,
    scan_text,
    sha256,
    validate_srt,
    verify,
)


EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
MAINLAND_PHONE_PATTERN = SECRET_PATTERNS["mainland phone"]


class PublicEvidenceBuildError(RuntimeError):
    pass


def sanitize_research_text(text: str) -> tuple[str, int, int]:
    text, redacted_email_count = EMAIL_PATTERN.subn("[REDACTED_EMAIL]", text)
    text, redacted_phone_count = MAINLAND_PHONE_PATTERN.subn("[REDACTED_PHONE]", text)
    return text, redacted_email_count, redacted_phone_count


def record_public_sanitization(
    approvals: dict[str, object],
    research_path: Path,
    redacted_email_count: int,
    redacted_phone_count: int,
) -> None:
    research_approval = approvals.get("research")
    if not isinstance(research_approval, dict):
        raise RuntimeError("源运行缺少研究审批记录")
    research_approval["source_artifact_sha256"] = research_approval.get("artifact_sha256")
    research_approval["public_artifact_sha256"] = sha256(research_path)
    research_approval["public_sanitization"] = {
        "email_addresses_redacted": redacted_email_count,
        "mainland_phone_numbers_redacted": redacted_phone_count,
        "scope": "source-page contact text only",
    }


def public_sanitization_validation_line(redacted_email_count: int, redacted_phone_count: int) -> str:
    return (
        "公开副本脱敏：research.json 中 "
        f"{redacted_email_count} 处来源页联系邮箱替换为 [REDACTED_EMAIL]，"
        f"{redacted_phone_count} 处大陆手机号替换为 [REDACTED_PHONE]；"
        "原审批哈希保留在 approvals.json，公开副本哈希另行记录。"
    )


def build_public_evidence(
    source: Path,
    output: Path,
    zip_path: Path | None = None,
    *,
    ffprobe_path: Path | str | None = None,
) -> dict[str, object]:
    """Build and verify one exact successful-run public evidence package."""

    source = Path(source).resolve()
    output = Path(output).resolve()
    zip_path = Path(zip_path or output.with_suffix(".zip")).resolve()
    if output.exists() or zip_path.exists():
        raise PublicEvidenceBuildError("输出目录或 ZIP 已存在；为避免覆盖证据，请使用新路径")

    source_manifest_path = source / "manifest.json"
    if not source_manifest_path.is_file():
        raise PublicEvidenceBuildError("源目录没有 manifest.json")
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    source_stage = source_manifest.get("stage")
    if source_manifest.get("schema_version") != 2 or source_stage not in {"render", "report_rebuild"} or source_manifest.get("status") != "complete":
        raise PublicEvidenceBuildError("源目录不是成功发布的 v2 render/report_rebuild 运行")
    source_files = {item.name for item in source.iterdir() if item.is_file()}
    source_manifest_entries = {
        str(item.get("name"))
        for item in source_manifest.get("artifacts", [])
        if isinstance(item, dict)
    }
    evidence_contract, contract_errors = _engine_contract(source_files, source_manifest_entries)
    if contract_errors:
        raise PublicEvidenceBuildError("源运行的生产引擎证据不完整：" + "; ".join(contract_errors))
    needed = set(CANONICAL) | {"approvals.json"}
    needed.update(evidence_artifacts_for_contract(evidence_contract))
    missing = sorted(name for name in needed if not (source / name).is_file())
    if missing:
        raise PublicEvidenceBuildError(f"源运行缺少产物：{', '.join(missing)}")
    source_approvals = json.loads((source / "approvals.json").read_text(encoding="utf-8"))
    approval_line, approval_errors = approval_validation_line(
        source_approvals,
        allow_legacy_human=evidence_contract == "legacy_v2",
    )
    if approval_errors:
        raise PublicEvidenceBuildError("源运行审批身份无效：" + "; ".join(approval_errors))
    source_approved_script = json.loads(
        (source / "approved_script.json").read_text(encoding="utf-8")
    )
    edit_line, edit_errors = script_edit_validation_line(
        source_approved_script,
        allow_legacy_human=evidence_contract == "legacy_v2",
    )
    if edit_errors:
        raise PublicEvidenceBuildError("源运行改稿身份无效：" + "; ".join(edit_errors))

    temp_parent = output.parent
    temp_parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="public-evidence-", dir=temp_parent))
    try:
        for name in sorted(needed):
            shutil.copy2(source / name, temporary / name)
        research_path = temporary / "research.json"
        research_text = research_path.read_text(encoding="utf-8")
        research_text, redacted_email_count, redacted_phone_count = sanitize_research_text(research_text)
        if redacted_email_count or redacted_phone_count:
            research_path.write_text(research_text, encoding="utf-8")
        approvals_path = temporary / "approvals.json"
        approvals = json.loads(approvals_path.read_text(encoding="utf-8"))
        record_public_sanitization(
            approvals,
            research_path,
            redacted_email_count,
            redacted_phone_count,
        )
        approvals_path.write_text(json.dumps(approvals, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        source_findings = []
        for path in temporary.iterdir():
            source_findings.extend(scan_text(path))
        if source_findings:
            raise RuntimeError("公开前扫描失败：" + "; ".join(source_findings))

        media = probe_video(temporary / "final.mp4", ffprobe_path=ffprobe_path)
        approved_script = json.loads((temporary / "approved_script.json").read_text(encoding="utf-8"))
        subtitle_errors = validate_srt(
            temporary / "captions.srt",
            media["duration_seconds"],
            str(approved_script.get("script", "")),
            contract=evidence_contract,
        )
        if subtitle_errors:
            raise RuntimeError("字幕校验失败：" + "; ".join(subtitle_errors))
        subtitle_contract_text = (
            "字幕：正文与已批准脚本全文绑定，无重叠，首段、段间和尾部最大空隙均不超过 2 秒。"
            if evidence_contract in {"mpt_v0.3", "motion_v0.3"}
            else "字幕：沿用冻结 v2 合同，时间轴相邻误差不超过 0.05 秒，终点与成片误差不超过 0.15 秒。"
        )
        motion_contract_text = (
            "- 纯动画：固定 HyperFrames 版本、可信动画积木凭证与 12 帧视觉门禁均随包验证。\n"
            if evidence_contract == "motion_v0.3"
            else ""
        )
        edit_contract_text = f"- {edit_line}\n" if edit_line else ""
        validation = (
            "# 公开证据验证说明\n\n"
            f"- Job ID：`{source_manifest.get('job_id')}`\n"
            f"- Run ID：`{source_manifest.get('run_id')}`\n"
            f"- 证据合同：`{evidence_contract}`\n"
            f"- 源运行 manifest SHA-256：`{sha256(source_manifest_path)}`\n"
            f"- 单任务 Provider 预算：`{source_manifest.get('budget', {}).get('attempted')}/7 attempted`\n"
            f"- 成片：`{media['duration_seconds']}` 秒，`{media['width']}×{media['height']}`，`{media['video_codec']}/{media['audio_codec']}`\n"
            f"- {subtitle_contract_text}\n"
            f"{motion_contract_text}"
            f"- {approval_line}\n"
            f"{edit_contract_text}"
            f"- {public_sanitization_validation_line(redacted_email_count, redacted_phone_count)}\n"
            "- 脱敏：包内不含 Key、Cookie、Authorization、本机绝对路径、原始配置、邮箱或手机号。\n"
            "- 清单：公开包加入本说明，并对公开副本重新计算逐文件大小与 SHA-256。\n"
        )
        (temporary / "VALIDATION.md").write_text(validation, encoding="utf-8")

        (temporary / HUMAN_REPORT_NAME).write_text(
            build_human_evidence_report(
                temporary,
                job_id=str(source_manifest.get("job_id", "")),
                run_id=str(source_manifest.get("run_id", "")),
                # The report and media are extracted as sibling files.  Keep
                # the HTML small and deterministic instead of duplicating the
                # entire MP4 as base64 in both the player and download link.
                embed_media=False,
            ),
            encoding="utf-8",
        )
        human_report_findings = scan_text(temporary / HUMAN_REPORT_NAME)
        if human_report_findings:
            raise RuntimeError("普通人验收报告脱敏失败：" + "; ".join(human_report_findings))

        manifest = dict(source_manifest)
        manifest["public_package"] = True
        manifest["public_package_schema_version"] = 2
        manifest["source_manifest_sha256"] = sha256(source_manifest_path)
        # A public package is a deterministic projection of an immutable run.
        # Reusing the source completion time avoids changing bytes on replay.
        manifest["packaged_at"] = str(
            source_manifest.get("finished_at")
            or source_manifest.get("started_at")
            or datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat(timespec="seconds")
        )
        manifest["artifacts"] = []
        public_files = REQUIRED | {HUMAN_REPORT_NAME} | evidence_artifacts_for_contract(evidence_contract)
        for name in sorted(public_files - {"manifest.json"}):
            path = temporary / name
            manifest["artifacts"].append({
                "name": name,
                "stage": (
                    "validation" if name == "VALIDATION.md"
                    else "human_report" if name == HUMAN_REPORT_NAME
                    else source_stage
                ),
                "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
                "size": path.stat().st_size,
                "sha256": sha256(path),
            })
        (temporary / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        errors, _ = verify(temporary, ffprobe_path=ffprobe_path)
        if errors:
            raise RuntimeError("公开包终验失败：" + "; ".join(errors))
        temporary.replace(output)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in sorted(public_files):
                data = (output / name).read_bytes()
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
        result = {
            "status": "PUBLIC_EVIDENCE_BUILT",
            "folder": output.name,
            "zip": zip_path.name,
            "files": len(public_files),
            "evidence_contract": evidence_contract,
            "source_manifest_sha256": sha256(source_manifest_path),
            "public_manifest_sha256": sha256(output / "manifest.json"),
            "archive_sha256": sha256(zip_path),
            "archive_size": zip_path.stat().st_size,
        }
        return result
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an exact, sanitized v2 public evidence package.")
    parser.add_argument("source", type=Path, help="Successful runs/<run_id>/artifacts directory")
    parser.add_argument("output", type=Path, help="Destination folder; must not already exist")
    parser.add_argument("--zip", dest="zip_path", type=Path)
    args = parser.parse_args()
    try:
        result = build_public_evidence(args.source, args.output, args.zip_path)
    except PublicEvidenceBuildError as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
