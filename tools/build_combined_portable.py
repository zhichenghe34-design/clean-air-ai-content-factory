from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import tomllib
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PACKAGE_ROOT_NAME = "Shiyi"
PACKAGE_VERSION = "0.3.0"
PACKAGE_MANIFEST = "PACKAGE-MANIFEST.json"
CHECKSUMS_FILE = "SHA256SUMS.txt"
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
EXPECTED_MPT_COMMIT = "254cd028906ee657eab844dc94087cdbea2a7aa8"
EXPECTED_MPT_VERSION = "1.3.3"

REPO_FILE_ALLOWLIST = (
    "app.py",
    "LICENSE",
    "scripts/launch_combined.py",
    "scripts/launch_combined.ps1",
    "tools/verify_combined_portable.py",
    "docs/fonts/NotoSansSC-Regular.ttf",
    "docs/fonts/NotoSansSC-Bold.ttf",
    "docs/fonts/OFL.txt",
    "docs/fonts/SOURCE.md",
    "third_party/moneyprinterturbo/LICENSE",
    "third_party/moneyprinterturbo/README.md",
    "third_party/moneyprinterturbo/upstream-lock.json",
)
REPO_TREE_ALLOWLIST: dict[str, frozenset[str]] = {
    "core": frozenset({".py", ".ps1"}),
    "static": frozenset(
        {".css", ".html", ".js", ".json", ".jpeg", ".jpg", ".lucide", ".png", ".svg", ".webp", ".woff", ".woff2"}
    ),
    "catalog": frozenset({".json", ".md", ".txt", ".yaml", ".yml"}),
    "agent-skills": frozenset({".css", ".html", ".js", ".json", ".md", ".py", ".txt", ".yaml", ".yml"}),
}
MPT_APP_SUFFIX_ALLOWLIST = frozenset({".py", ".json"})
SKIPPED_DIRECTORY_NAMES = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__", "node_modules"}
)
SKIPPED_SUFFIXES = frozenset({".log", ".pyc", ".pyo", ".tmp"})
PYTHON_LICENSE_NAMES = ("LICENSE.txt", "LICENSE", "LICENSE.md", "license.txt")
ROOT_LAUNCHER_NAME = "启动时宜Agent内容工厂.bat"
USAGE_NAME = "使用说明.txt"


@dataclass(frozen=True)
class BuildInputs:
    repo: Path
    mpt_source: Path
    python_runtime: Path
    ffmpeg_runtime: Path
    ffmpeg_license: Path
    materials: tuple[Path, ...]
    output: Path
    zip_path: Path
    ffmpeg_build_info: Path | None = None
    verify_source_control: bool = True
    verify_runtime_executables: bool = True
    repo_commit: str | None = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _git_text(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "未知 Git 错误"
        raise ValueError(f"无法核验 Git 来源：{detail}")
    return completed.stdout.strip()


def _validate_clean_git_source(root: Path, expected_commit: str | None = None) -> str:
    top = Path(_git_text(root, "rev-parse", "--show-toplevel")).resolve()
    if top != root.resolve():
        raise ValueError(f"Git 来源必须指向工作区根目录：{root}")
    head = _git_text(root, "rev-parse", "HEAD").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("Git HEAD 不是完整提交哈希")
    if expected_commit and head != expected_commit.lower():
        raise ValueError(f"MoneyPrinterTurbo 必须锁定到 {expected_commit}，实际为 {head}")
    if _git_text(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError(f"Git 来源不干净，拒绝封包：{root.name}")
    return head


def _require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"缺少{label}：{path}")
    if _is_reparse_point(path):
        raise ValueError(f"{label}不得是符号链接、Junction 或其他重解析点：{path}")


def _require_directory(path: Path, label: str) -> None:
    if not path.is_dir():
        raise FileNotFoundError(f"缺少{label}：{path}")
    if _is_reparse_point(path):
        raise ValueError(f"{label}不得是符号链接、Junction 或其他重解析点：{path}")


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return True
    junction = getattr(path, "is_junction", None)
    return bool(
        path.is_symlink()
        or (callable(junction) and junction())
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _require_within(root: Path, path: Path) -> None:
    try:
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("封包源路径无法安全解析") from exc
    if not resolved_path.is_relative_to(resolved_root):
        raise ValueError("封包源路径越过允许根目录")


def _is_python_startup_hook_name(name: str) -> bool:
    lowered = name.casefold()
    return any(
        lowered == hook or lowered.startswith(f"{hook}.")
        for hook in ("sitecustomize", "usercustomize")
    )


def _copy_file(source: Path, destination: Path) -> None:
    _require_file(source, "封包源文件")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _iter_tree_files(root: Path, allowed_suffixes: frozenset[str] | None = None) -> Iterable[Path]:
    _require_directory(root, "封包源目录")
    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        _require_within(root, current_path)
        kept_directories: list[str] = []
        for name in sorted(directory_names, key=str.casefold):
            candidate = current_path / name
            if _is_python_startup_hook_name(name):
                raise ValueError("便携运行时不得包含 Python 自动启动钩子")
            if _is_reparse_point(candidate):
                raise ValueError("封包源不得包含符号链接、Junction 或其他重解析目录")
            _require_within(root, candidate)
            relative = candidate.relative_to(root)
            if any(part.casefold() in SKIPPED_DIRECTORY_NAMES for part in relative.parts):
                continue
            kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in sorted(file_names, key=str.casefold):
            path = current_path / name
            if _is_python_startup_hook_name(name):
                raise ValueError("便携运行时不得包含 Python 自动启动钩子")
            if _is_reparse_point(path):
                raise ValueError("封包源不得包含符号链接、Junction 或其他重解析文件")
            _require_within(root, path)
            relative = path.relative_to(root)
            if any(part.casefold() in SKIPPED_DIRECTORY_NAMES for part in relative.parts):
                continue
            if path.suffix.casefold() in SKIPPED_SUFFIXES:
                continue
            if allowed_suffixes is not None and path.suffix.casefold() not in allowed_suffixes:
                raise ValueError(f"白名单目录出现未允许的文件类型：{path}")
            yield path


def _copy_tree(source: Path, destination: Path, allowed_suffixes: frozenset[str] | None = None) -> None:
    for path in _iter_tree_files(source, allowed_suffixes):
        _copy_file(path, destination / path.relative_to(source))


def _python_license(runtime: Path) -> Path:
    for name in PYTHON_LICENSE_NAMES:
        candidate = runtime / name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("便携 Python 根目录缺少许可证文件")


def _read_mpt_version(pyproject: Path) -> str:
    try:
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("MoneyPrinterTurbo pyproject.toml 无法解析") from exc
    project = document.get("project", {})
    return str(project.get("version", "")) if isinstance(project, dict) else ""


def _verify_ffmpeg_executable(path: Path, expected_name: str) -> None:
    try:
        completed = subprocess.run(
            [str(path), "-version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"{expected_name} 无法从完整 runtime 中启动：{exc}") from exc
    output = (completed.stdout + "\n" + completed.stderr).casefold()
    if completed.returncode != 0 or f"{expected_name.casefold()} version" not in output:
        raise ValueError(f"{expected_name} -version 未通过，拒绝构建不可运行的便携包")


def _verify_python_executable(path: Path) -> None:
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"便携 Python 无法启动：{exc}") from exc
    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode != 0 or not re.search(r"\bPython 3\.(?:12|14)\.", output):
        raise ValueError("便携 Python --version 未通过或版本不是已验证的 3.12/3.14")


def _validate_inputs(inputs: BuildInputs) -> tuple[str, Path]:
    repo = inputs.repo.resolve()
    mpt = inputs.mpt_source.resolve()
    runtime = inputs.python_runtime.resolve()
    ffmpeg_runtime = inputs.ffmpeg_runtime.resolve()
    output = inputs.output.resolve()
    zip_path = inputs.zip_path.resolve()

    _require_directory(repo, "项目根目录")
    _require_directory(mpt, "MoneyPrinterTurbo 源码")
    _require_directory(runtime, "便携 Python")
    _require_directory(ffmpeg_runtime, "完整 FFmpeg runtime")
    _require_file(runtime / "python.exe", "便携 Python 入口")
    python_license = _python_license(runtime)
    _require_file(ffmpeg_runtime / "ffmpeg.exe", "FFmpeg")
    _require_file(ffmpeg_runtime / "ffprobe.exe", "FFprobe")
    if inputs.verify_runtime_executables:
        _verify_python_executable(runtime / "python.exe")
        _verify_ffmpeg_executable(ffmpeg_runtime / "ffmpeg.exe", "ffmpeg")
        _verify_ffmpeg_executable(ffmpeg_runtime / "ffprobe.exe", "ffprobe")
    _require_file(inputs.ffmpeg_license.resolve(), "FFmpeg 许可证")
    if inputs.ffmpeg_build_info is not None:
        _require_file(inputs.ffmpeg_build_info.resolve(), "FFmpeg 构建说明")

    if output == zip_path or output in zip_path.parents or zip_path in output.parents:
        raise ValueError("输出目录与 ZIP 必须是互不嵌套的独立目标")
    if output.exists() or zip_path.exists():
        raise FileExistsError("输出目录或 ZIP 已存在；为避免覆盖上一成功包，已拒绝构建")
    if not 1 <= len(inputs.materials) <= 24:
        raise ValueError("本地素材必须包含 1 到 24 个 MP4 文件")
    seen_materials: set[Path] = set()
    for material in inputs.materials:
        resolved = material.resolve()
        _require_file(resolved, "已核验 MP4 素材")
        if resolved.suffix.casefold() != ".mp4":
            raise ValueError(f"本地素材不是 MP4：{material.name}")
        if resolved in seen_materials:
            raise ValueError(f"本地素材重复：{material.name}")
        seen_materials.add(resolved)

    for relative in REPO_FILE_ALLOWLIST:
        _require_file(repo / Path(relative), f"项目白名单文件 {relative}")
    for relative in REPO_TREE_ALLOWLIST:
        _require_directory(repo / relative, f"项目白名单目录 {relative}")
    for relative in ("app/asgi.py", "pyproject.toml", "uv.lock", "LICENSE", "resource/public/index.html"):
        _require_file(mpt / Path(relative), f"MoneyPrinterTurbo 文件 {relative}")
    if _read_mpt_version(mpt / "pyproject.toml") != EXPECTED_MPT_VERSION:
        raise ValueError(f"MoneyPrinterTurbo 版本必须为 {EXPECTED_MPT_VERSION}")

    if inputs.verify_source_control:
        repo_commit = _validate_clean_git_source(repo)
        _validate_clean_git_source(mpt, EXPECTED_MPT_COMMIT)
    else:
        repo_commit = str(inputs.repo_commit or "").lower()
        if not re.fullmatch(r"[0-9a-f]{40}", repo_commit):
            raise ValueError("关闭 Git 核验时必须显式提供 40 位 repo_commit")

    lock = json.loads((repo / "third_party" / "moneyprinterturbo" / "upstream-lock.json").read_text(encoding="utf-8"))
    if not isinstance(lock, dict) or (
        lock.get("upstream_commit") != EXPECTED_MPT_COMMIT
        or lock.get("upstream_version") != EXPECTED_MPT_VERSION
        or lock.get("license") != "MIT"
    ):
        raise ValueError("项目中的 MoneyPrinterTurbo 上游锁与正式固定版本不一致")
    license_sha = str(lock.get("license_sha256", "")).upper()
    if not re.fullmatch(r"[0-9A-F]{64}", license_sha) or license_sha != sha256_file(mpt / "LICENSE"):
        raise ValueError("MoneyPrinterTurbo 许可证与上游锁不一致")
    if sha256_file(repo / "third_party" / "moneyprinterturbo" / "LICENSE") != license_sha:
        raise ValueError("项目内 MoneyPrinterTurbo 许可证副本与上游锁不一致")
    return repo_commit, python_license


def _copy_repository_payload(repo: Path, package: Path) -> None:
    for relative in REPO_FILE_ALLOWLIST:
        _copy_file(repo / Path(relative), package / Path(relative))
    for relative, suffixes in REPO_TREE_ALLOWLIST.items():
        _copy_tree(repo / relative, package / relative, suffixes)


def _copy_mpt_payload(source: Path, target: Path, regular_font: Path) -> None:
    _copy_tree(source / "app", target / "app", MPT_APP_SUFFIX_ALLOWLIST)
    for relative in ("pyproject.toml", "uv.lock", "LICENSE", "resource/public/index.html"):
        _copy_file(source / Path(relative), target / Path(relative))
    _copy_file(regular_font, target / "resource" / "fonts" / "NotoSansSC-Regular.ttf")
    (target / "UPSTREAM_COMMIT").write_text(EXPECTED_MPT_COMMIT + "\n", encoding="utf-8")
    (target / "config.toml").write_text(
        'log_level = "WARNING"\n'
        'listen_host = "127.0.0.1"\n'
        'listen_port = 8080\n'
        '\n[app]\n'
        'endpoint = ""\n'
        'hide_config = true\n'
        'edge_tts_timeout = 30\n'
        'tls_verify = true\n'
        'video_source = "local"\n'
        'subtitle_provider = "edge"\n'
        'bgm_type = ""\n'
        'bgm_volume = 0.0\n',
        encoding="utf-8",
    )


def _copy_materials(materials: tuple[Path, ...], target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, object]] = []
    for index, source in enumerate(materials, start=1):
        name = f"material-{index:02d}.mp4"
        destination = target / name
        _copy_file(source.resolve(), destination)
        entries.append({"name": name, "size": destination.stat().st_size, "sha256": sha256_file(destination)})
    (target / "MATERIALS.json").write_text(
        json.dumps({"schema_version": 1, "files": entries}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_root_launcher(package: Path) -> None:
    (package / ROOT_LAUNCHER_NAME).write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n"
        "set \"SHIYI_LAUNCHER_PYTHON=%~dp0runtime\\python\\python.exe\"\r\n"
        "\"%SHIYI_LAUNCHER_PYTHON%\" -I -S -B -X utf8 \"%~dp0tools\\verify_combined_portable.py\" \"%~dp0.\" --startup\r\n"
        "if errorlevel 1 (\r\n"
        "  echo [ERROR] Package integrity verification failed.\r\n"
        "  pause\r\n"
        "  exit /b 3\r\n"
        ")\r\n"
        "\"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe\" -NoLogo -NoProfile -ExecutionPolicy Bypass -File \"%~dp0scripts\\launch_combined.ps1\" "
        "-MptRoot \"%~dp0engine\\MoneyPrinterTurbo\" "
        "-MptPython \"%~dp0runtime\\python\\python.exe\" "
        "-AppPython \"%~dp0runtime\\python\\python.exe\" "
        "-Ffmpeg \"%~dp0runtime\\ffmpeg\\ffmpeg.exe\" "
        "-Ffprobe \"%~dp0runtime\\ffmpeg\\ffprobe.exe\" "
        "-MaterialRoot \"%~dp0engine\\MoneyPrinterTurbo\\storage\\local_videos\"\r\n"
        "set \"SHIYI_EXIT_CODE=%ERRORLEVEL%\"\r\n"
        "if not \"%SHIYI_EXIT_CODE%\"==\"0\" pause\r\n"
        "exit /b %SHIYI_EXIT_CODE%\r\n",
        encoding="ascii",
        newline="",
    )
    (package / USAGE_NAME).write_text(
        "时宜 Agent 内容工厂 v0.3 Windows 组合便携版\n\n"
        "1. 将整个目录解压到可写入的位置，不要只在 ZIP 内运行。\n"
        "   建议直接解压到 Downloads 或其他较短路径，避免旧版 Windows 的 MAX_PATH 限制。\n"
        f"2. 双击“{ROOT_LAUNCHER_NAME}”。\n"
        "3. 工作台和 MoneyPrinterTurbo 仅监听 127.0.0.1，并共用本包预装的 Python。\n"
        "4. 本包不会在运行时安装或下载依赖；视频素材只从 engine/MoneyPrinterTurbo/storage/local_videos 读取。\n"
        "5. API Key 不在包内。需要真实 Provider 时，请在工作台本机界面中配置。\n",
        encoding="utf-8",
    )


def _copy_runtime_and_licenses(inputs: BuildInputs, package: Path, python_license: Path) -> None:
    _copy_tree(inputs.python_runtime.resolve(), package / "runtime" / "python")
    _copy_tree(inputs.ffmpeg_runtime.resolve(), package / "runtime" / "ffmpeg")

    license_dir = package / "licenses"
    _copy_file(inputs.repo.resolve() / "LICENSE", license_dir / "PRODUCT-MIT.txt")
    _copy_file(inputs.mpt_source.resolve() / "LICENSE", license_dir / "MoneyPrinterTurbo-MIT.txt")
    _copy_file(inputs.repo.resolve() / "docs" / "fonts" / "OFL.txt", license_dir / "NotoSansSC-OFL.txt")
    _copy_file(inputs.ffmpeg_license.resolve(), license_dir / "FFmpeg-license.txt")
    _copy_file(python_license, license_dir / "Python-license.txt")
    if inputs.ffmpeg_build_info is not None:
        _copy_file(inputs.ffmpeg_build_info.resolve(), license_dir / "FFmpeg-build-info.txt")
    (license_dir / "README.txt").write_text(
        "本目录集中保存产品、MoneyPrinterTurbo、Noto Sans SC、FFmpeg 与 Python 的许可证副本。\n"
        "Python 运行时所带第三方依赖的许可证元数据仍保留在 runtime/python 内。\n",
        encoding="utf-8",
    )


def _role_for(relative: str) -> str:
    first = relative.split("/", 1)[0]
    roles = {
        "core": "workbench_code",
        "static": "workbench_ui",
        "catalog": "local_tool_catalog",
        "agent-skills": "agent_capabilities",
        "docs": "font_assets",
        "scripts": "launcher",
        "tools": "integrity_verifier",
        "third_party": "third_party_boundary",
        "engine": "production_engine",
        "runtime": "preinstalled_runtime",
        "licenses": "licenses",
    }
    return roles.get(first, "package_root")


def _canonical_payload_sha256(entries: list[dict[str, object]], prefixes: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        relative = str(entry["path"])
        if relative.startswith(prefixes):
            digest.update(f"{relative}\0{entry['size']}\0{entry['sha256']}\n".encode("utf-8"))
    return digest.hexdigest().upper()


def _payload_files(package: Path) -> list[Path]:
    excluded = {PACKAGE_MANIFEST, CHECKSUMS_FILE}
    files: list[Path] = []
    for path in package.rglob("*"):
        if _is_reparse_point(path):
            raise ValueError("成品包不得包含符号链接、Junction 或其他重解析点")
        _require_within(package, path)
        if path.is_file() and path.relative_to(package).as_posix() not in excluded:
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(package).as_posix().casefold())


def _write_manifests(package: Path, repo_commit: str) -> dict[str, object]:
    files = []
    for path in _payload_files(package):
        relative = path.relative_to(package).as_posix()
        files.append(
            {"path": relative, "size": path.stat().st_size, "sha256": sha256_file(path), "role": _role_for(relative)}
        )
    manifest: dict[str, object] = {
        "schema_version": 1,
        "product": "时宜 Agent 内容工厂",
        "version": PACKAGE_VERSION,
        "package_kind": "windows_x64_combined_portable",
        "source": {
            "repository_commit": repo_commit,
            "moneyprinterturbo_version": EXPECTED_MPT_VERSION,
            "moneyprinterturbo_commit": EXPECTED_MPT_COMMIT,
            "mpt_payload_sha256": _canonical_payload_sha256(files, ("engine/MoneyPrinterTurbo/",)),
        },
        "runtime": {
            "shared_python": "runtime/python/python.exe",
            "workbench_entry": "app.py",
            "moneyprinterturbo_entry": "engine/MoneyPrinterTurbo/app/asgi.py",
            "ffmpeg": "runtime/ffmpeg/ffmpeg.exe",
            "ffprobe": "runtime/ffmpeg/ffprobe.exe",
            "runtime_downloads_allowed": False,
            "payload_sha256": _canonical_payload_sha256(files, ("runtime/python/", "runtime/ffmpeg/")),
        },
        "mutable_state": {
            "workbench_root": "runtime",
            "workbench_immutable_children": ["python", "ffmpeg"],
            "moneyprinterturbo_root": "engine/MoneyPrinterTurbo/storage",
            "moneyprinterturbo_immutable_children": ["local_videos"],
            "executable_files_allowed": False,
        },
        "network": {"listen_host": "127.0.0.1", "public_cloud_service": False},
        "materials": {"root": "engine/MoneyPrinterTurbo/storage/local_videos", "count": len([p for p in files if p["path"].endswith(".mp4")])},
        "files": files,
    }
    manifest_path = package / PACKAGE_MANIFEST
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    checksum_paths = [*(_payload_files(package)), manifest_path]
    lines = [f"{sha256_file(path)}  {path.relative_to(package).as_posix()}" for path in checksum_paths]
    (package / CHECKSUMS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def _write_fixed_zip(package: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted((item for item in package.rglob("*") if item.is_file()), key=lambda item: item.relative_to(package).as_posix().casefold()):
            relative = f"{PACKAGE_ROOT_NAME}/{path.relative_to(package).as_posix()}"
            info = zipfile.ZipInfo(relative, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def build_combined_portable(inputs: BuildInputs) -> dict[str, object]:
    repo_commit, python_license = _validate_inputs(inputs)
    output = inputs.output.resolve()
    zip_path = inputs.zip_path.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    staging_parent = Path(tempfile.mkdtemp(prefix=".shiyi-combined-staging-", dir=output.parent))
    package = staging_parent / PACKAGE_ROOT_NAME
    zip_staging = zip_path.parent / f".{zip_path.name}.{uuid.uuid4().hex}.tmp"
    published_output = False
    published_zip = False
    try:
        package.mkdir()
        _copy_repository_payload(inputs.repo.resolve(), package)
        _copy_mpt_payload(
            inputs.mpt_source.resolve(),
            package / "engine" / "MoneyPrinterTurbo",
            inputs.repo.resolve() / "docs" / "fonts" / "NotoSansSC-Regular.ttf",
        )
        _copy_materials(inputs.materials, package / "engine" / "MoneyPrinterTurbo" / "storage" / "local_videos")
        _copy_runtime_and_licenses(inputs, package, python_license)
        _write_root_launcher(package)
        manifest = _write_manifests(package, repo_commit)

        try:
            from .verify_combined_portable import verify_folder, verify_zip
        except ImportError:
            from verify_combined_portable import verify_folder, verify_zip

        errors = verify_folder(package)
        if errors:
            raise ValueError("组合便携目录验证失败：" + "；".join(errors))
        _write_fixed_zip(package, zip_staging)
        zip_errors = verify_zip(zip_staging)
        if zip_errors:
            raise ValueError("组合便携 ZIP 验证失败：" + "；".join(zip_errors))

        # Both staging targets are on the destination volume.  ``os.rename``
        # is atomic on Windows and, unlike ``os.replace``, will not overwrite a
        # successful package that appeared after the initial preflight.
        os.rename(package, output)
        published_output = True
        os.rename(zip_staging, zip_path)
        published_zip = True
        return manifest
    except Exception:
        if published_zip and zip_path.is_file():
            zip_path.unlink()
        if published_output and output.is_dir():
            shutil.rmtree(output)
        raise
    finally:
        if zip_staging.exists():
            zip_staging.unlink()
        shutil.rmtree(staging_parent, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="构建固定版本的 v0.3 Windows 组合便携包。")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--mpt-source", type=Path, required=True)
    parser.add_argument("--python-runtime", type=Path, required=True)
    parser.add_argument("--ffmpeg-runtime", type=Path, required=True)
    parser.add_argument("--ffmpeg-license", type=Path, required=True)
    parser.add_argument("--ffmpeg-build-info", type=Path)
    parser.add_argument("--material", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--zip", dest="zip_path", type=Path, required=True)
    args = parser.parse_args()

    try:
        manifest = build_combined_portable(
            BuildInputs(
                repo=args.repo,
                mpt_source=args.mpt_source,
                python_runtime=args.python_runtime,
                ffmpeg_runtime=args.ffmpeg_runtime,
                ffmpeg_license=args.ffmpeg_license,
                ffmpeg_build_info=args.ffmpeg_build_info,
                materials=tuple(args.material),
                output=args.output,
                zip_path=args.zip_path,
            )
        )
    except Exception as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "status": "COMBINED_PORTABLE_BUILT",
                "output": str(args.output.resolve()),
                "zip": str(args.zip_path.resolve()),
                "files": len(manifest["files"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
