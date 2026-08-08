from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path


def require(text: str, markers: list[str], label: str, errors: list[str]) -> None:
    for marker in markers:
        if marker not in text:
            errors.append(f"{label} 缺少口径：{marker}")


def is_legacy_path(path: Path) -> bool:
    return any("legacy" in part.casefold() or part == "初赛提交" for part in path.parts)


def discover_test_count(repo: Path, errors: list[str]) -> int:
    loader = unittest.TestLoader()
    sys.path.insert(0, str(repo))
    try:
        suite = loader.discover(str(repo / "tests"), pattern="test*.py")
    finally:
        try:
            sys.path.remove(str(repo))
        except ValueError:
            pass
    if loader.errors:
        errors.extend(f"unittest discovery 失败：{message}" for message in loader.errors)
    return suite.countTestCases()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def load_v2_baseline(repo: Path) -> tuple[dict, dict]:
    baseline_path = repo / "docs" / "release-baselines" / "v0.2.0.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    return baseline["historical_facts"], baseline["artifacts"]


def embedded_font_names(reader) -> set[str]:
    names: set[str] = set()
    for page in reader.pages:
        resources = page.get("/Resources")
        if resources is None:
            continue
        fonts = resources.get_object().get("/Font")
        if fonts is None:
            continue
        for font in fonts.get_object().values():
            base_font = font.get_object().get("/BaseFont")
            if base_font:
                names.add(str(base_font))
    return names


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Validate v2 documentation and proposal consistency.")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--skip-pdf", action="store_true", help="Validate source facts before the refreshed PDF is built.")
    parser.add_argument("--check-rebuild", action="store_true", help="Require a deterministic rebuild to match the committed PDF byte-for-byte.")
    args = parser.parse_args()
    repo = args.repo.resolve()
    pdf_path = (args.pdf or repo / "docs" / "competition-proposal.pdf").resolve()
    errors: list[str] = []
    try:
        historical, artifacts = load_v2_baseline(repo)
        historical_test_count = int(historical["python_test_count"])
        historical_package_count = int(historical["local_tool_capability_pack_count"])
        expected_pdf_hash = str(historical["proposal_pdf_sha256"])
        expected_evidence_hash = str(historical["real_evidence_zip_sha256"])
        evidence_zip = (repo / artifacts["real_evidence_zip"]).resolve()
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: 无法读取 v0.2.0 历史基线：{exc}")
        return 1

    files = {
        "README": repo / "README.md",
        "ARCHITECTURE": repo / "docs" / "ARCHITECTURE.md",
        "COMPETITION": repo / "docs" / "COMPETITION.md",
        "SAFETY": repo / "docs" / "SAFETY.md",
        "SUBMISSION_TEXT": repo / "docs" / "SUBMISSION_TEXT.md",
    }
    texts = {label: path.read_text(encoding="utf-8") for label, path in files.items()}
    test_count = discover_test_count(repo, errors)
    catalog = json.loads((repo / "catalog" / "package-catalog.json").read_text(encoding="utf-8"))
    package_count = len(catalog.get("packages", []))
    if package_count != historical_package_count:
        errors.append(f"v2 本地工具能力目录数量为 {package_count}，历史基线应为 {historical_package_count}")

    if sha256_file(evidence_zip) != expected_evidence_hash:
        errors.append(f"v2 真实证据 ZIP 哈希变化：{evidence_zip}")
    if not args.skip_pdf and sha256_file(pdf_path) != expected_pdf_hash:
        errors.append(f"v2 PDF 哈希变化：{pdf_path}")

    require(texts["README"], ["v2", "v3", "127.0.0.1:8765", "7", "SHA-256", f"{historical_package_count} 个本地工具能力包", "动态行业能力包", f"当前 {test_count} 项 Python 测试", "legacy_read_only", "DPAPI", "/api/agent/topics"], "README", errors)
    require(texts["ARCHITECTURE"], ["v2", "7", "SHA-256", "legacy_read_only"], "ARCHITECTURE", errors)
    require(texts["COMPETITION"], ["v2", "127.0.0.1:8765", "7", "SHA-256", f"{historical_package_count} 个本地工具能力包", f"{historical_test_count} 项 Python 测试", "三选一", "两处人工暂停", "legacy"], "COMPETITION", errors)
    require(texts["SAFETY"], ["v2", "7", "SHA-256", "DPAPI"], "SAFETY", errors)
    require(texts["SUBMISSION_TEXT"], ["v2", "127.0.0.1", "7", "SHA-256", f"{historical_package_count} 个本地工具能力包", f"{historical_test_count} 项 Python 测试", "两次审批", "不会覆盖上一成功视频", "45-60 秒", "三个候选", "两处人工暂停"], "SUBMISSION_TEXT", errors)
    general_kernel = (repo / "docs" / "GENERAL_AGENT_KERNEL.md").read_text(encoding="utf-8")
    require(general_kernel, ["动态行业能力包", f"当前 {test_count} 项 Python 测试"], "GENERAL_AGENT_KERNEL", errors)

    current_docs = [repo / "README.md", *(repo / "docs").rglob("*.md")]
    stale_markers = (
        "72 项 Python 测试",
        "56 项 Python 测试",
        "32 项 Python 测试",
        "12 个能力包",
        "127.0.0.1:8766",
        "v2 真实联调只会在",
        "最终 v2 DeepSeek 证据包将在",
    )
    for path in current_docs:
        if is_legacy_path(path):
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        label = str(path.relative_to(repo))
        for stale in stale_markers:
            if stale in content:
                errors.append(f"{label} 仍含过期口径：{stale}")
        for port in re.findall(r"127\.0\.0\.1:(\d+)", content):
            if port != "8765":
                errors.append(f"{label} 出现不一致端口：{port}")

    package = json.loads((repo / "package.json").read_text(encoding="utf-8"))
    dev = package.get("devDependencies", {})
    if dev.get("hyperframes") != "0.7.86" or dev.get("@playwright/test") != "1.62.1":
        errors.append("Node 依赖版本未锁定为 HyperFrames 0.7.86 / Playwright 1.62.1")

    source_files = [*repo.glob("core/*.py"), *repo.glob("static/*.js"), *repo.glob("video-compositions/*/package.json")]
    for path in source_files:
        content = path.read_text(encoding="utf-8", errors="replace")
        if "npx --yes" in content:
            errors.append(f"运行文件仍允许 npx --yes：{path.relative_to(repo)}")

    if not args.skip_pdf:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            errors.append(f"无法检查 PDF：{exc}")
        else:
            reader = PdfReader(str(pdf_path))
            if len(reader.pages) != 8:
                errors.append(f"PDF 页数为 {len(reader.pages)}，应为 8")
            pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
            require(pdf_text, ["净界 AI 内容工厂 v2", "Agent 工作台", "三选一", "两处人工确认", "127.0.0.1:8765", str(historical_package_count), str(historical_test_count), "DPAPI", "legacy"], "PDF", errors)
            if re.search(r"(?:A\s+I|Deep\s+Seek|Vox\s+CPM)", pdf_text, re.IGNORECASE):
                errors.append("PDF 仍存在英文缩写异常拆字")
            link_count = sum(
                1
                for page in reader.pages
                for annotation in (page.get("/Annots", []) or [])
                if annotation.get_object().get("/Subtype") == "/Link"
            )
            if link_count < 3:
                errors.append(f"PDF 可点击链接只有 {link_count} 个，应至少为 3 个")
            font_names = embedded_font_names(reader)
            if not any("NotoSansSC" in name for name in font_names):
                errors.append(f"PDF 未嵌入 Noto Sans SC：{sorted(font_names)}")
            if any("MSYH" in name.upper() or "MICROSOFTYAHEI" in name.upper() for name in font_names):
                errors.append(f"PDF 仍嵌入系统付费字体：{sorted(font_names)}")

        if args.check_rebuild:
            result = subprocess.run(
                [sys.executable, str(repo / "tools" / "build_submission_pdf.py"), "--check", "--output", str(pdf_path)],
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if result.returncode:
                details = (result.stdout + result.stderr).strip()
                errors.append(f"PDF 确定性重建不一致：{details}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("CONSISTENCY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
