from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def require(text: str, markers: list[str], label: str, errors: list[str]) -> None:
    for marker in markers:
        if marker not in text:
            errors.append(f"{label} 缺少口径：{marker}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate v2 documentation and proposal consistency.")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--pdf", type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    pdf_path = (args.pdf or repo / "docs" / "competition-proposal.pdf").resolve()
    errors: list[str] = []

    files = {
        "README": repo / "README.md",
        "ARCHITECTURE": repo / "docs" / "ARCHITECTURE.md",
        "COMPETITION": repo / "docs" / "COMPETITION.md",
        "SAFETY": repo / "docs" / "SAFETY.md",
        "SUBMISSION_TEXT": repo / "docs" / "SUBMISSION_TEXT.md",
    }
    texts = {label: path.read_text(encoding="utf-8") for label, path in files.items()}
    require(texts["README"], ["v2", "127.0.0.1:8765", "7", "SHA-256", "13 个能力包", "72 项 Python 测试", "legacy_read_only", "DPAPI"], "README", errors)
    require(texts["ARCHITECTURE"], ["v2", "7", "SHA-256", "legacy_read_only"], "ARCHITECTURE", errors)
    require(texts["COMPETITION"], ["v2", "127.0.0.1:8765", "7", "SHA-256", "13 个当前能力包", "72 项 Python 测试", "legacy"], "COMPETITION", errors)
    require(texts["SAFETY"], ["v2", "7", "SHA-256", "DPAPI"], "SAFETY", errors)
    require(texts["SUBMISSION_TEXT"], ["v2", "127.0.0.1", "7", "SHA-256", "两次审批", "不会覆盖上一成功视频", "45–60 秒"], "SUBMISSION_TEXT", errors)
    for label, content in texts.items():
        for stale in ("56 项 Python 测试", "v2 真实联调只会在", "最终 v2 DeepSeek 证据包将在"):
            if stale in content:
                errors.append(f"{label} 仍含过期口径：{stale}")
        for port in re.findall(r"127\.0\.0\.1:(\d+)", content):
            if port != "8765":
                errors.append(f"{label} 出现不一致端口：{port}")

    catalog = json.loads((repo / "catalog" / "package-catalog.json").read_text(encoding="utf-8"))
    if len(catalog.get("packages", [])) != 13:
        errors.append(f"能力目录数量为 {len(catalog.get('packages', []))}，应为 13")

    package = json.loads((repo / "package.json").read_text(encoding="utf-8"))
    dev = package.get("devDependencies", {})
    if dev.get("hyperframes") != "0.7.86" or dev.get("@playwright/test") != "1.62.1":
        errors.append("Node 依赖版本未锁定为 HyperFrames 0.7.86 / Playwright 1.62.1")

    source_files = [*repo.glob("core/*.py"), *repo.glob("static/*.js"), *repo.glob("video-compositions/*/package.json")]
    for path in source_files:
        content = path.read_text(encoding="utf-8", errors="replace")
        if "npx --yes" in content:
            errors.append(f"运行文件仍允许 npx --yes：{path.relative_to(repo)}")

    try:
        from pypdf import PdfReader
    except ImportError as exc:
        errors.append(f"无法检查 PDF：{exc}")
    else:
        reader = PdfReader(str(pdf_path))
        if len(reader.pages) != 8:
            errors.append(f"PDF 页数为 {len(reader.pages)}，应为 8")
        pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        require(pdf_text, ["净界 AI 内容工厂 v2", "127.0.0.1:8765", "13 项", "72", "DPAPI", "legacy"], "PDF", errors)
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

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("CONSISTENCY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
