from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


PAYLOAD_NAMES = (
    "00_交付清单与演示顺序.md",
    "01_项目介绍与开题文本_v2.md",
    "02_系统与证据说明_v2.md",
    "净界AI内容工厂_v2补充材料.pdf",
    "净界AI内容工厂_Agent工作台演示.mp4",
    "净界AI内容工厂_设计基准样片.mp4",
    "净界AI内容工厂_v2真实联调成片.mp4",
    "v2-real-deepseek-20260801-022153.zip",
)
RELEASE_NAMES = (*PAYLOAD_NAMES, "release-manifest.json", "SHA256SUMS.txt")
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
EXPECTED_BRANCH = "agent/agent-workbench-release"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mime_for(name: str) -> str:
    suffixes = {
        ".md": "text/markdown",
        ".pdf": "application/pdf",
        ".mp4": "video/mp4",
        ".zip": "application/zip",
    }
    return suffixes.get(Path(name).suffix.lower(), "application/octet-stream")


def parse_release_time(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("released-at 必须包含时区")
    return parsed.isoformat(timespec="seconds")


def git_text(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "未知 Git 错误"
        raise ValueError(f"无法核验发布来源：{detail}")
    return completed.stdout.strip()


def github_repo_from_remote(remote: str) -> str:
    patterns = (
        r"https://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$",
        r"ssh://git@github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$",
        r"git@github\.com:(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, remote, flags=re.IGNORECASE)
        if match:
            return match.group("repo")
    raise ValueError("origin 不是可识别的 GitHub 仓库地址")


def validate_release_source(repo: Path, source_commit: str, pr_url: str) -> None:
    top_level = Path(git_text(repo, "rev-parse", "--show-toplevel")).resolve()
    if top_level != repo:
        raise ValueError("--repo 必须指向 Git 工作区根目录")

    head = git_text(repo, "rev-parse", "HEAD").lower()
    if source_commit.lower() != head:
        raise ValueError(f"source-commit 必须等于当前 HEAD：{head}")
    branch = git_text(repo, "branch", "--show-current")
    if branch != EXPECTED_BRANCH:
        raise ValueError(f"发布只能从 {EXPECTED_BRANCH} 构建，当前为 {branch or 'detached HEAD'}")
    upstream_head = git_text(repo, "rev-parse", "@{upstream}").lower()
    if upstream_head != head:
        raise ValueError("当前 HEAD 尚未与远端跟踪分支同步")
    dirty = git_text(repo, "status", "--porcelain", "--untracked-files=all")
    if dirty:
        raise ValueError("工作区不干净，拒绝生成带有虚假提交来源的发布包")

    origin_repo = github_repo_from_remote(git_text(repo, "remote", "get-url", "origin"))
    parsed = urlsplit(pr_url)
    parts = [part for part in parsed.path.split("/") if part]
    valid_url = (
        parsed.scheme == "https"
        and parsed.netloc.lower() == "github.com"
        and not parsed.username
        and not parsed.password
        and not parsed.port
        and not parsed.query
        and not parsed.fragment
        and len(parts) == 4
        and parts[2] == "pull"
        and parts[3].isdigit()
        and "/".join(parts[:2]).casefold() == origin_repo.casefold()
    )
    if not valid_url:
        raise ValueError(f"pr-url 必须是当前 origin 仓库的 HTTPS Pull Request 地址：{origin_repo}")


def write_delivery_guide(path: Path, manifest_context: dict[str, str]) -> None:
    path.write_text(
        "# 净界 AI 内容工厂 v2 交付清单与演示顺序\n\n"
        "本目录是当前 Agent 工作台发布包。2026-07-18 初赛材料保留为 legacy，不在本包重复收录。\n\n"
        "## 建议查看顺序\n\n"
        "1. 阅读 `01_项目介绍与开题文本_v2.md`。\n"
        "2. 打开 `净界AI内容工厂_v2补充材料.pdf`。\n"
        "3. 播放 `净界AI内容工厂_Agent工作台演示.mp4`，了解三选一与两处人工确认。\n"
        "4. 播放设计基准样片和 v2 真实联调成片。\n"
        "5. 需要审计时，解压 13 项真实证据包并对照 `02_系统与证据说明_v2.md`。\n"
        "6. 使用 `SHA256SUMS.txt` 复算文件哈希。\n\n"
        "## 版本\n\n"
        f"- Release ID：`{manifest_context['release_id']}`\n"
        f"- Git commit：`{manifest_context['source_commit']}`\n"
        f"- Draft PR：{manifest_context['pr_url']}\n"
        "- Demo 是固定数据的界面流程演示，不调用 Provider；真实结果以证据包为准。\n",
        encoding="utf-8",
    )


def copy_inputs(repo: Path, staging: Path, context: dict[str, str]) -> None:
    evidence = repo / "evidence" / "v2-real-deepseek-20260801-022153"
    sources = {
        "01_项目介绍与开题文本_v2.md": repo / "docs" / "SUBMISSION_TEXT.md",
        "净界AI内容工厂_v2补充材料.pdf": repo / "docs" / "competition-proposal.pdf",
        "净界AI内容工厂_Agent工作台演示.mp4": repo / "media" / "agent-workbench-demo.mp4",
        "净界AI内容工厂_设计基准样片.mp4": repo / "media" / "sample.mp4",
        "净界AI内容工厂_v2真实联调成片.mp4": evidence / "final.mp4",
        "v2-real-deepseek-20260801-022153.zip": repo / "evidence" / "v2-real-deepseek-20260801-022153.zip",
    }
    missing = [str(path.relative_to(repo)) for path in sources.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("发布源文件缺失：" + ", ".join(missing))

    write_delivery_guide(staging / "00_交付清单与演示顺序.md", context)
    for name, source in sources.items():
        shutil.copy2(source, staging / name)

    competition = (repo / "docs" / "COMPETITION.md").read_text(encoding="utf-8").strip()
    validation = (evidence / "VALIDATION.md").read_text(encoding="utf-8").strip()
    (staging / "02_系统与证据说明_v2.md").write_text(
        competition + "\n\n---\n\n" + validation + "\n", encoding="utf-8"
    )


def write_manifest(staging: Path, context: dict[str, str]) -> None:
    artifacts = []
    for name in PAYLOAD_NAMES:
        path = staging / name
        artifacts.append(
            {
                "name": name,
                "mime": mime_for(name),
                "size": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    manifest = {
        "schema_version": 1,
        "release_id": context["release_id"],
        "released_at": context["released_at"],
        "source_commit": context["source_commit"],
        "branch": EXPECTED_BRANCH,
        "draft_pr_url": context["pr_url"],
        "legacy_submission": "legacy-20260718",
        "public_evidence": "v2-real-deepseek-20260801-022153",
        "artifacts": artifacts,
    }
    (staging / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    sums = [f"{sha256(staging / name)}  {name}" for name in (*PAYLOAD_NAMES, "release-manifest.json")]
    (staging / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")


def write_zip(staging: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in RELEASE_NAMES:
            info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (staging / name).read_bytes())


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the exact Agent workbench competition release bundle.")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--zip", dest="zip_path", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--pr-url", required=True)
    parser.add_argument("--released-at", required=True)
    args = parser.parse_args()

    repo = args.repo.resolve()
    output = args.output.resolve()
    zip_path = args.zip_path.resolve()
    if output.exists() or zip_path.exists():
        raise SystemExit("输出目录或 ZIP 已存在；发布构建禁止覆盖")
    if output.parent != zip_path.parent:
        raise SystemExit("输出目录和 ZIP 必须位于同一父目录，以保证同卷原子发布")
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_commit.lower()):
        raise SystemExit("source-commit 必须是完整 40 位 Git SHA")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", args.release_id):
        raise SystemExit("release-id 必须是 3–64 位小写安全标识")
    try:
        validate_release_source(repo, args.source_commit, args.pr_url)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    context = {
        "release_id": args.release_id,
        "source_commit": args.source_commit.lower(),
        "pr_url": args.pr_url,
        "released_at": parse_release_time(args.released_at),
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="agent-release-", dir=output.parent))
    staged_zip = staging.parent / f".{zip_path.name}.{staging.name}.tmp"
    published_output = False
    try:
        copy_inputs(repo, staging, context)
        write_manifest(staging, context)
        from verify_release_bundle import verify_folder, verify_zip

        errors = verify_folder(staging, verify_embedded_evidence=True)
        if errors:
            raise RuntimeError("发布目录验证失败：" + "; ".join(errors))
        write_zip(staging, staged_zip)
        zip_errors = verify_zip(staged_zip)
        if zip_errors:
            raise RuntimeError("发布 ZIP 验证失败：" + "; ".join(zip_errors))
        staging.replace(output)
        published_output = True
        staged_zip.replace(zip_path)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        if published_output and output.exists():
            shutil.rmtree(output)
        if staged_zip.exists():
            staged_zip.unlink()
        raise

    print(json.dumps({
        "status": "RELEASE_BUNDLE_BUILT",
        "release_id": args.release_id,
        "files": len(RELEASE_NAMES),
        "folder": str(output),
        "zip": str(zip_path),
        "zip_sha256": sha256(zip_path),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
