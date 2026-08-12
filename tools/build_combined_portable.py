from __future__ import annotations

import argparse
import ast
import base64
import csv
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import uuid
import warnings
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Iterable


PROJECT_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_SOURCE_ROOT))
from core.motion_runtime_contract import (
    HYPERFRAMES_PATCHED_CLI_SHA256,
    HYPERFRAMES_PATCH_ID,
    HYPERFRAMES_PATCH_VERSION,
    HYPERFRAMES_UPSTREAM_CLI_SHA256,
    HYPERFRAMES_VERSION,
    NODE_MINIMUM_MAJOR,
)


def _load_portable_verifiers():
    """Load sibling verifiers even when the builder runs with Python ``-I``."""

    from tools.verify_combined_portable import verify_folder, verify_zip

    return verify_folder, verify_zip


PACKAGE_ROOT_NAME = "Shiyi"
PACKAGE_VERSION = "0.3.0"
PACKAGE_MANIFEST = "PACKAGE-MANIFEST.json"
CHECKSUMS_FILE = "SHA256SUMS.txt"
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
EXPECTED_MPT_COMMIT = "254cd028906ee657eab844dc94087cdbea2a7aa8"
EXPECTED_MPT_VERSION = "1.3.3"
EXPECTED_MPT_EXCLUDED_COMPONENTS = frozenset(
    {
        "resource/fonts",
        "resource/songs",
        "webui",
        "app/services/llm.py",
        "app/controllers/v1/llm.py",
        "app/services/upload_post.py",
        "app/services/elevenlabs_music.py",
        "app/services/sonilo.py",
    }
)
MPT_OFFLINE_SUBSET_MARKER = "SHIYI_MPT_OFFLINE_SUBSET_V1"
MPT_H264_MF_CODEC_MARKER = "SHIYI_MPT_H264_MF_CODEC_V1"
MPT_DISABLED_MUSIC_PROVIDER_FILES = frozenset(
    {"app/services/elevenlabs_music.py", "app/services/sonilo.py"}
)
EXPECTED_MPT_DETERMINISTIC_MODIFICATIONS = [
    {"path": "app/router.py", "change": "register_video_router_only"},
    {
        "path": "app/services/task.py",
        "change": "remove_llm_and_social_upload_imports_and_add_explicit_disabled_stubs",
    },
    {
        "path": "app/services/video.py",
        "change": "lock_all_formal_video_writes_and_concat_to_h264_mf_quality_72_and_remove_libx264_fallbacks",
    },
]
EXPECTED_MPT_REQUIRED_PROBE = (
    "import_app_asgi_video_route_present_llm_routes_absent_preapproved_script_terms_path_"
    "moviepy_h264_mf_encode_and_concat"
)
H264_CODEC_STRATEGY = "h264_mf"
H264_MF_QUALITY = 72
MOTION_PACKAGE_PROFILE = "motion_primary"
LEGACY_PACKAGE_PROFILE = "legacy_combined"
SYSTEM_EDGE_BROWSER_STRATEGY = "trusted_system_edge"
# HyperFrames 0.7.86 currently closes over puppeteer-core 25.4.0, whose
# reviewed Chromium revision is major 151.  Requiring the same Edge major is
# deliberately conservative; the real startup canary below remains the final
# compatibility gate instead of treating a version string as proof.
SYSTEM_EDGE_MINIMUM_MAJOR = 151
HYPERFRAMES_RUNTIME_MANIFEST = "RUNTIME-MANIFEST.json"
HYPERFRAMES_CLI_RELATIVE = Path("node_modules/hyperframes/bin/hyperframes.mjs")
HYPERFRAMES_PACKAGE_RELATIVE = Path("node_modules/hyperframes/package.json")
HYPERFRAMES_UPSTREAM_COMMIT = "1a52351f05237433006e6ca92db18feafed16fed"
HYPERFRAMES_UPSTREAM_TAG = "v0.7.86"
HYPERFRAMES_REPOSITORY = "https://github.com/heygen-com/hyperframes"
HYPERFRAMES_PATCH_MANIFEST_RELATIVE = Path("third_party/hyperframes/windows-mf-patch.json")
HYPERFRAMES_PATCHER_RELATIVE = Path("tools/apply_hyperframes_windows_mf_patch.py")
FFMPEG_BOUNDARY_RELATIVE = Path("third_party/ffmpeg")
FFMPEG_RUNTIME_RELATIVE = FFMPEG_BOUNDARY_RELATIVE / "runtime" / "win-x64"
FFMPEG_LOCK_RELATIVE = FFMPEG_BOUNDARY_RELATIVE / "upstream-lock.json"
FFMPEG_RUNTIME_LOCK_COPY = Path("licenses/FFmpeg-runtime-lock.json")
FFMPEG_LICENSE_FILES = (
    "FFmpeg-COPYING.LGPLv2.1",
    "FFmpeg-COPYING.LGPLv3",
    "FFmpeg-LICENSE.md",
    "zlib-LICENSE",
)
MOVIEPY_PATCH_MANIFEST_RELATIVE = Path(
    "third_party/python_runtime/moviepy-windows-mf-patch.json"
)
MOVIEPY_PATCHER_RELATIVE = Path("tools/apply_moviepy_windows_mf_patch.py")
MOVIEPY_PATCH_ID = "shiyi-moviepy-windows-mf"
MOVIEPY_PATCH_VERSION = "1.0.0"
MOVIEPY_PATCH_MANIFEST_SHA256 = "A3F347C932956534C746AF7FEDC2C87AFCA55FA3BCAF4C8E4C101DAD8CABE763"
MOVIEPY_PATCHER_SHA256 = "6D307A0D90FDD2D62652704D015D1F4BC42AEE2CF23C115834B91606E464E0C4"
MOVIEPY_DISTRIBUTION_VERSION = "2.2.1"
MOVIEPY_MODULE_REPORTED_VERSION = "2.1.2"
MOVIEPY_UPSTREAM_WRITER_SHA256 = "347E9EE5403A0CBFFDDF6205D7DE9A8B38708BDC9853F22383CFE4987AFA62D3"
MOVIEPY_UPSTREAM_RECORD_SHA256 = "F8D61AAAE58D557D0F67AF5016B5AD15D791A9D79E3B7121BC3CD9C296D78ED8"
MOVIEPY_PATCHED_WRITER_SHA256 = "DFE76CD8AED151B99881DD01FA2BC1E040D0788EC364A8C6EF14020F2009D8B9"
MOVIEPY_PATCHED_RECORD_SHA256 = "4D836329F0D7804F389AED64FB102A48A614821793F5D7C7E419D11EB7A574C5"
PYTHON_RUNTIME_SBOM = "Python-runtime-SBOM.json"
PYTHON_LICENSE_BOUNDARY = Path("third_party/python_runtime")
PYTHON_LICENSE_OVERRIDE_MANIFEST = "dependency-license-overrides.json"
PYTHON_PRUNED_IMPORT_CONTRACT = "pruned-import-boundary.json"
PRIMP_SOURCE_REPOSITORY = "https://github.com/deedy5/primp.git"
PRIMP_SOURCE_COMMIT = "f662999ad2a44bfad4ee433f8d37dd4a231f3154"
PRIMP_PORTABLE_CRATE_PATHS = {
    ("primp", "1.3.1"): "crates/primp",
    ("primp-h2", "0.4.15"): "crates/primp-h2",
    ("primp-hyper", "1.9.1"): "crates/primp-hyper",
    ("primp-hyper-rustls", "0.27.9"): "crates/primp-hyper-rustls",
    ("primp-hyper-util", "0.1.22"): "crates/primp-hyper-util",
    ("primp-reqwest", "0.13.4"): "crates/primp-reqwest",
    ("primp-rustls", "0.23.40"): "crates/primp-rustls/rustls",
    ("primp-tokio-rustls", "0.26.5"): "crates/primp-tokio-rustls",
}
PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS = {
    "altair": "5.5.0",
    "av": "17.0.0",
    "blinker": "1.9.0",
    "cachetools": "5.5.2",
    "ctranslate2": "4.7.1",
    "faster-whisper": "1.1.0",
    "fastuuid": "0.14.0",
    "filelock": "3.25.2",
    "flatbuffers": "25.12.19",
    "fsspec": "2026.3.0",
    "gitdb": "4.0.12",
    "gitpython": "3.1.50",
    "hf-xet": "1.4.3",
    "httptools": "0.8.0",
    "huggingface-hub": "1.8.0",
    "itsdangerous": "2.2.0",
    "jinja2": "3.1.6",
    "jsonschema": "4.23.0",
    "jsonschema-specifications": "2025.9.1",
    "litellm": "1.86.2",
    "markdown-it-py": "4.0.0",
    "markupsafe": "3.0.3",
    "mdurl": "0.1.2",
    "mpmath": "1.3.0",
    "narwhals": "2.18.1",
    "onnxruntime": "1.24.4",
    "pandas": "2.3.3",
    "protobuf": "5.29.6",
    "pyarrow": "23.0.1",
    "pydeck": "0.9.1",
    "pygments": "2.20.0",
    "python-dateutil": "2.9.0.post0",
    "pytz": "2026.1.post1",
    "referencing": "0.37.0",
    "regex": "2026.4.4",
    "rich": "14.3.3",
    "rpds-py": "0.30.0",
    "setuptools": "82.0.1",
    "shellingham": "1.5.4",
    "six": "1.17.0",
    "smmap": "5.0.3",
    "streamlit": "1.59.1",
    "streamlit-tour": "1.1.0",
    "sympy": "1.14.0",
    "tiktoken": "0.12.0",
    "tokenizers": "0.22.2",
    "typer": "0.26.5",
    "tzdata": "2025.3",
    "watchdog": "6.0.0",
}
PYTHON_RUNTIME_PRUNE_ROOTS = frozenset(
    {
        "ctranslate2",
        "faster-whisper",
        "litellm",
        "onnxruntime",
        "streamlit",
        "streamlit-tour",
        "tokenizers",
    }
)
PYTHON_RUNTIME_PRUNE_PROTECTED = frozenset({"openai", "python-multipart", "toml", "uvicorn"})
PYTHON_RUNTIME_PRUNED_REQUIREMENTS = {
    "ctranslate2": "ctranslate2<5,>=4.0",
    "onnxruntime": "onnxruntime<2,>=1.14",
    "tokenizers": "tokenizers<1,>=0.13",
}

REPO_FILE_ALLOWLIST = (
    "app.py",
    "LICENSE",
    "examples/pattern_cards.jsonl",
    "scripts/launch_combined.py",
    "scripts/launch_combined.ps1",
    "tools/verify_combined_portable.py",
    "tools/build_public_evidence.py",
    "tools/verify_public_evidence.py",
    "docs/fonts/NotoSansSC-Regular.ttf",
    "docs/fonts/NotoSansSC-Bold.ttf",
    "docs/fonts/OFL.txt",
    "docs/fonts/SOURCE.md",
    "third_party/moneyprinterturbo/LICENSE",
    "third_party/moneyprinterturbo/README.md",
    "third_party/moneyprinterturbo/upstream-lock.json",
    "third_party/hyperframes/LICENSE",
    "third_party/hyperframes/README.md",
    "third_party/hyperframes/upstream-lock.json",
    "third_party/hyperframes/windows-mf-patch.json",
    "third_party/hyperframes/dependency-license-overrides.json",
    "third_party/ffmpeg/upstream-lock.json",
    "third_party/ffmpeg/licenses/FFmpeg-COPYING.LGPLv2.1",
    "third_party/ffmpeg/licenses/FFmpeg-COPYING.LGPLv3",
    "third_party/ffmpeg/licenses/FFmpeg-LICENSE.md",
    "third_party/ffmpeg/licenses/zlib-LICENSE",
    "tools/apply_hyperframes_windows_mf_patch.py",
    "tools/verify_ffmpeg_distribution.py",
    "third_party/python_runtime/moviepy-windows-mf-patch.json",
    "tools/apply_moviepy_windows_mf_patch.py",
    "third_party/python_runtime/README.md",
    "third_party/python_runtime/dependency-license-overrides.json",
    "third_party/python_runtime/pruned-import-boundary.json",
)
REPO_TREE_ALLOWLIST: dict[str, frozenset[str]] = {
    "core": frozenset({".py", ".ps1"}),
    "static": frozenset(
        {".css", ".html", ".js", ".json", ".jpeg", ".jpg", ".lucide", ".png", ".svg", ".webp", ".woff", ".woff2"}
    ),
    "catalog": frozenset({".json", ".md", ".txt", ".yaml", ".yml"}),
    "agent-skills": frozenset({".css", ".html", ".js", ".json", ".md", ".png", ".py", ".txt", ".yaml", ".yml"}),
}
MPT_APP_SUFFIX_ALLOWLIST = frozenset({".py", ".json"})
SKIPPED_DIRECTORY_NAMES = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__", "node_modules"}
)
SKIPPED_SUFFIXES = frozenset({".log", ".pyc", ".pyo", ".tmp"})
PYTHON_LICENSE_NAMES = ("LICENSE.txt", "LICENSE", "LICENSE.md", "license.txt")
ROOT_LAUNCHER_NAME = "启动时宜Agent内容工厂.bat"
STOP_LAUNCHER_NAME = "关闭时宜Agent内容工厂.bat"
MIGRATION_LAUNCHER_NAME = "迁移旧版数据.bat"
USAGE_NAME = "使用说明.txt"


@dataclass(frozen=True)
class MotionRuntimeInputs:
    node_runtime: Path
    hyperframes_runtime: Path
    node_version: str
    hyperframes_version: str


@dataclass(frozen=True)
class BuildInputs:
    repo: Path
    mpt_source: Path
    python_runtime: Path
    materials: tuple[Path, ...]
    output: Path
    zip_path: Path
    verify_source_control: bool = True
    verify_runtime_executables: bool = True
    repo_commit: str | None = None
    package_profile: str = LEGACY_PACKAGE_PROFILE
    motion_runtime: MotionRuntimeInputs | None = None


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


def _iter_tree_files(
    root: Path,
    allowed_suffixes: frozenset[str] | None = None,
    *,
    allow_node_modules: bool = False,
) -> Iterable[Path]:
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
            skipped = SKIPPED_DIRECTORY_NAMES - ({"node_modules"} if allow_node_modules else set())
            if any(part.casefold() in skipped for part in relative.parts):
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
            skipped = SKIPPED_DIRECTORY_NAMES - ({"node_modules"} if allow_node_modules else set())
            if any(part.casefold() in skipped for part in relative.parts):
                continue
            if path.suffix.casefold() in SKIPPED_SUFFIXES:
                continue
            if allowed_suffixes is not None and path.suffix.casefold() not in allowed_suffixes:
                raise ValueError(f"白名单目录出现未允许的文件类型：{path}")
            yield path


def _copy_tree(source: Path, destination: Path, allowed_suffixes: frozenset[str] | None = None) -> None:
    for path in _iter_tree_files(source, allowed_suffixes):
        _copy_file(path, destination / path.relative_to(source))


def _is_redundant_imageio_ffmpeg_executable(relative: str) -> bool:
    path = PurePosixPath(relative.replace("\\", "/"))
    parts = tuple(part.casefold() for part in path.parts)
    return path.suffix.casefold() == ".exe" and any(
        parts[index : index + 2] == ("imageio_ffmpeg", "binaries")
        for index in range(max(0, len(parts) - 1))
    )


def _copy_python_runtime(source: Path, destination: Path) -> None:
    pruned_paths = _python_pruned_runtime_paths(source)
    for path in _iter_tree_files(source):
        relative = path.relative_to(source)
        relative_posix = relative.as_posix()
        if (
            relative_posix in pruned_paths
            or _is_redundant_imageio_ffmpeg_executable(relative_posix)
        ):
            continue
        _copy_file(path, destination / relative)


def _normalize_python_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.strip()).casefold()


def _python_license_evidence_kind(relative: str) -> str | None:
    compact = re.sub(r"[^a-z]", "", PurePosixPath(relative).name.casefold())
    if compact.startswith(("license", "licence", "copying")):
        return "license"
    if compact.startswith("notice") or "thirdpartynotice" in compact:
        return "notice"
    if compact.startswith("redist"):
        return "redistribution"
    return None


def _python_record_runtime_path(raw: str) -> str:
    value = raw.replace("\\", "/").strip()
    if not value or "\x00" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ValueError(f"Python distribution RECORD 路径不安全：{raw!r}")
    parts = ["Lib", "site-packages"]
    for part in PurePosixPath(value).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ValueError(f"Python distribution RECORD 越过运行时根：{raw!r}")
            parts.pop()
            continue
        if ":" in part:
            raise ValueError(f"Python distribution RECORD 含 Windows 绝对路径：{raw!r}")
        parts.append(part)
    if not parts:
        raise ValueError(f"Python distribution RECORD 路径为空：{raw!r}")
    return "/".join(parts)


def _python_pruned_runtime_paths(runtime_root: Path) -> frozenset[str]:
    """Resolve the approved optional native stack exclusively through exact wheel RECORDs."""
    site_packages = runtime_root / "Lib" / "site-packages"
    _require_directory(site_packages, "Python site-packages")
    found: dict[str, tuple[str, Path, object, set[str]]] = {}
    ownership: dict[str, str] = {}
    versions: dict[str, str] = {}
    dependencies: dict[str, set[str]] = {}
    for dist_info in sorted(site_packages.glob("*.dist-info"), key=lambda path: path.name.casefold()):
        metadata_path = dist_info / "METADATA"
        record_path = dist_info / "RECORD"
        _require_file(metadata_path, f"Python distribution METADATA {dist_info.name}")
        _require_file(record_path, f"Python distribution RECORD {dist_info.name}")
        try:
            metadata = BytesParser().parsebytes(metadata_path.read_bytes())
            rows = list(csv.reader(io.StringIO(record_path.read_text(encoding="utf-8-sig"))))
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise ValueError(f"Python distribution 无法用于受控裁剪：{dist_info.name}") from exc
        name = _normalize_python_distribution_name(str(metadata.get("Name", "") or ""))
        version = str(metadata.get("Version", "") or "").strip()
        if not name or not version or name in versions:
            raise ValueError(f"Python runtime distribution 名称/版本无效或重复：{dist_info.name}")
        versions[name] = version
        dependency_names: set[str] = set()
        for value in metadata.get_all("Requires-Dist", []):  # type: ignore[attr-defined]
            requirement = str(value)
            if re.search(r"(?i)\bextra\s*(?:==|!=|in|not\s+in)", requirement):
                continue
            match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
            if match:
                dependency_names.add(_normalize_python_distribution_name(match.group(1)))
        dependencies[name] = dependency_names
        paths: set[str] = set()
        for row in rows:
            if len(row) != 3:
                raise ValueError(f"Python distribution RECORD 条目无效：{dist_info.name}")
            relative = _python_record_runtime_path(row[0])
            candidate = runtime_root / Path(*PurePosixPath(relative).parts)
            if not candidate.is_file():
                continue
            _require_within(runtime_root, candidate)
            previous = ownership.get(relative)
            if previous is not None:
                raise ValueError(
                    f"Python runtime 文件被多个 distribution 声明，无法安全裁剪：{relative}"
                )
            ownership[relative] = f"{name}@{version}"
            paths.add(relative)
        if name in PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS:
            expected_version = PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS[name]
            if version != expected_version:
                raise ValueError(
                    f"Python runtime 受控裁剪版本漂移：{name}@{version}，仅允许 {expected_version}"
                )
            if name in found:
                raise ValueError(f"Python runtime 受控裁剪 distribution 重复：{name}@{version}")
            found[name] = (version, dist_info, metadata, paths)

    if set(found) != set(PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS):
        missing = sorted(set(PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS) - set(found))
        raise ValueError("Python runtime 缺少受控裁剪锁定 distribution：" + ", ".join(missing))

    faster_metadata = found["faster-whisper"][2]
    requirements = {
        _normalize_python_distribution_name(match.group(1)): str(value).strip()
        for value in faster_metadata.get_all("Requires-Dist", [])  # type: ignore[attr-defined]
        if (match := re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", str(value)))
    }
    if any(requirements.get(name) != locked for name, locked in PYTHON_RUNTIME_PRUNED_REQUIREMENTS.items()):
        raise ValueError("faster-whisper 受控裁剪依赖图与精确锁不一致")

    reverse_dependencies = {name: set() for name in versions}
    for owner, required_names in dependencies.items():
        for required in required_names:
            if required in reverse_dependencies:
                reverse_dependencies[required].add(owner)
    reachable: set[str] = set()
    pending = list(PYTHON_RUNTIME_PRUNE_ROOTS)
    while pending:
        owner = pending.pop()
        for required in dependencies.get(owner, set()):
            if required in versions and required not in reachable:
                reachable.add(required)
                pending.append(required)
    graph_pruned = set(PYTHON_RUNTIME_PRUNE_ROOTS)
    while True:
        newly_orphaned = {
            name
            for name in reachable - graph_pruned - set(PYTHON_RUNTIME_PRUNE_PROTECTED)
            if reverse_dependencies[name] <= graph_pruned
        }
        if not newly_orphaned:
            break
        graph_pruned.update(newly_orphaned)
    if graph_pruned != set(PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS):
        unexpected = sorted(graph_pruned - set(PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS))
        no_longer_orphaned = sorted(set(PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS) - graph_pruned)
        raise ValueError(
            "Python runtime 受控裁剪依赖图漂移：新增孤儿="
            + ",".join(unexpected)
            + "；不再孤儿="
            + ",".join(no_longer_orphaned)
        )

    pruned_paths = set().union(*(item[3] for item in found.values()))
    expected_owned: set[str] = set()
    for name, (_, dist_info, _, _) in found.items():
        expected_owned.update(
            path.relative_to(runtime_root).as_posix()
            for path in _iter_tree_files(dist_info)
        )
        module_root = site_packages / name.replace("-", "_")
        if name == "faster-whisper":
            module_root = site_packages / "faster_whisper"
        if module_root.is_dir():
            expected_owned.update(
                path.relative_to(runtime_root).as_posix()
                for path in _iter_tree_files(module_root)
            )
    missing_from_record = sorted(expected_owned - pruned_paths, key=str.casefold)
    if missing_from_record:
        raise ValueError(
            "Python runtime 受控裁剪发现 RECORD 未拥有的残留文件："
            + ", ".join(missing_from_record[:8])
        )
    return frozenset(pruned_paths)


def _read_python_pruned_import_contract(root: Path) -> dict[str, object]:
    path = root / PYTHON_LICENSE_BOUNDARY / PYTHON_PRUNED_IMPORT_CONTRACT
    _require_file(path, "Python runtime 裁剪静态 import 合同")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Python runtime 裁剪静态 import 合同无法解析") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "status",
        "scope",
        "pruned_distributions",
        "retained_optional_references",
        "formal_exceptions",
        "unresolved",
    }:
        raise ValueError("Python runtime 裁剪静态 import 合同结构无效")
    if (
        raw.get("schema_version") != 1
        or raw.get("status") != "complete"
        or raw.get("scope") != "portable_python_static_import_boundary"
        or raw.get("unresolved") != []
    ):
        raise ValueError("Python runtime 裁剪静态 import 合同尚未闭合")

    pruned = raw.get("pruned_distributions")
    if not isinstance(pruned, list):
        raise ValueError("Python runtime 裁剪 import 模块表不是数组")
    modules: dict[str, tuple[str, ...]] = {}
    pruned_order: list[str] = []
    for item in pruned:
        if not isinstance(item, dict) or set(item) != {"name", "version", "modules"}:
            raise ValueError("Python runtime 裁剪 import 模块条目无效")
        name = str(item.get("name", ""))
        version = str(item.get("version", ""))
        import_modules = item.get("modules")
        if (
            name != _normalize_python_distribution_name(name)
            or PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS.get(name) != version
            or not isinstance(import_modules, list)
            or not import_modules
            or import_modules != sorted(import_modules)
            or len(import_modules) != len(set(import_modules))
            or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", str(value)) for value in import_modules)
            or name in modules
        ):
            raise ValueError(f"Python runtime 裁剪 import 模块锁无效：{name}@{version}")
        modules[name] = tuple(str(value) for value in import_modules)
        pruned_order.append(name)
    if (
        pruned_order != sorted(pruned_order)
        or {name: str(item["version"]) for name, item in zip(pruned_order, pruned, strict=True)}
        != PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS
    ):
        raise ValueError("Python runtime 裁剪 import 模块表与 distribution 锁不一致")

    reference_fields = {
        "distribution",
        "owner",
        "owner_version",
        "path",
        "imports",
        "reason",
    }
    retained_raw = raw.get("retained_optional_references")
    if not isinstance(retained_raw, list):
        raise ValueError("Python runtime 保留包可选 import 合同不是数组")
    retained: dict[tuple[str, str], dict[str, object]] = {}
    retained_order: list[tuple[str, str]] = []
    for item in retained_raw:
        if not isinstance(item, dict) or set(item) != reference_fields:
            raise ValueError("Python runtime 保留包可选 import 合同条目无效")
        distribution = str(item.get("distribution", ""))
        owner = str(item.get("owner", ""))
        owner_version = str(item.get("owner_version", ""))
        relative = PurePosixPath(str(item.get("path", "")))
        imports = item.get("imports")
        reason = str(item.get("reason", "")).strip()
        key = (distribution, relative.as_posix())
        if (
            distribution not in modules
            or owner != _normalize_python_distribution_name(owner)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]*", owner_version)
            or relative.is_absolute()
            or relative.suffix.casefold() != ".py"
            or any(part in ("", ".", "..") for part in relative.parts)
            or not isinstance(imports, list)
            or not imports
            or imports != sorted(imports)
            or len(imports) != len(set(imports))
            or any(
                not any(str(value) == prefix or str(value).startswith(prefix + ".") for prefix in modules[distribution])
                for value in imports
            )
            or len(reason) < 24
            or key in retained
        ):
            raise ValueError(f"Python runtime 保留包可选 import 合同锁无效：{key}")
        retained[key] = {
            "owner": owner,
            "owner_version": owner_version,
            "imports": frozenset(str(value) for value in imports),
        }
        retained_order.append(key)
    if retained_order != sorted(retained_order):
        raise ValueError("Python runtime 保留包可选 import 合同未稳定排序")

    formal_raw = raw.get("formal_exceptions")
    if not isinstance(formal_raw, list):
        raise ValueError("Python runtime 正式链 import 例外合同不是数组")
    formal: dict[tuple[str, str], frozenset[str]] = {}
    formal_order: list[tuple[str, str]] = []
    for item in formal_raw:
        if not isinstance(item, dict) or set(item) != {"distribution", "path", "imports", "reason"}:
            raise ValueError("Python runtime 正式链 import 例外合同条目无效")
        distribution = str(item.get("distribution", ""))
        relative = PurePosixPath(str(item.get("path", "")))
        imports = item.get("imports")
        reason = str(item.get("reason", "")).strip()
        key = (distribution, relative.as_posix())
        if (
            distribution not in modules
            or relative.is_absolute()
            or relative.suffix.casefold() != ".py"
            or any(part in ("", ".", "..") for part in relative.parts)
            or not relative.as_posix().startswith("engine/MoneyPrinterTurbo/app/")
            or not isinstance(imports, list)
            or not imports
            or imports != sorted(imports)
            or len(imports) != len(set(imports))
            or any(
                not any(str(value) == prefix or str(value).startswith(prefix + ".") for prefix in modules[distribution])
                for value in imports
            )
            or len(reason) < 24
            or key in formal
        ):
            raise ValueError(f"Python runtime 正式链 import 例外合同锁无效：{key}")
        formal[key] = frozenset(str(value) for value in imports)
        formal_order.append(key)
    if formal_order != sorted(formal_order):
        raise ValueError("Python runtime 正式链 import 例外合同未稳定排序")
    return {"modules": modules, "retained": retained, "formal": formal}


def _python_static_import_names(path: Path) -> frozenset[str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(text, filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise ValueError(f"Python 静态 import 扫描无法解析：{path.name}") from exc
    imports: set[str] = set()
    for node in ast.walk(tree):
        values: list[str] = []
        if isinstance(node, ast.Import):
            values = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            values = [node.module]
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            function = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if function in {"__import__", "find_spec", "import_module"}:
                values = [node.args[0].value]
        imports.update(values)
    return frozenset(imports)


def _python_pruned_import_matches(
    import_name: str,
    contract: dict[str, object],
) -> list[str]:
    modules = contract["modules"]
    assert isinstance(modules, dict)
    return [
        distribution
        for distribution, prefixes in modules.items()
        if any(import_name == prefix or import_name.startswith(prefix + ".") for prefix in prefixes)
    ]


def _validate_retained_python_import_boundary(
    runtime_root: Path,
    contract: dict[str, object],
    *,
    excluded_paths: frozenset[str] = frozenset(),
) -> None:
    site_packages = runtime_root / "Lib" / "site-packages"
    ownership: dict[str, tuple[str, str]] = {}
    for dist_info in sorted(site_packages.glob("*.dist-info"), key=lambda path: path.name.casefold()):
        metadata_path = dist_info / "METADATA"
        record_path = dist_info / "RECORD"
        _require_file(metadata_path, f"Python distribution METADATA {dist_info.name}")
        _require_file(record_path, f"Python distribution RECORD {dist_info.name}")
        metadata = BytesParser().parsebytes(metadata_path.read_bytes())
        owner = _normalize_python_distribution_name(str(metadata.get("Name", "") or ""))
        owner_version = str(metadata.get("Version", "") or "").strip()
        try:
            rows = csv.reader(io.StringIO(record_path.read_text(encoding="utf-8-sig")))
            for row in rows:
                if len(row) != 3:
                    raise ValueError(f"Python distribution RECORD 条目无效：{dist_info.name}")
                runtime_relative = _python_record_runtime_path(row[0])
                if runtime_relative in ownership:
                    raise ValueError(f"Python runtime 文件被多个 distribution 声明：{runtime_relative}")
                ownership[runtime_relative] = (owner, owner_version)
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise ValueError(f"Python distribution RECORD 无法用于静态 import 审计：{dist_info.name}") from exc

    retained = contract["retained"]
    assert isinstance(retained, dict)
    for path in sorted(site_packages.rglob("*.py"), key=lambda item: item.as_posix().casefold()):
        runtime_relative = path.relative_to(runtime_root).as_posix()
        if runtime_relative in excluded_paths:
            continue
        site_relative = path.relative_to(site_packages).as_posix()
        for imported in _python_static_import_names(path):
            for distribution in _python_pruned_import_matches(imported, contract):
                key = (distribution, site_relative)
                expected = retained.get(key)
                if not isinstance(expected, dict) or imported not in expected["imports"]:
                    raise ValueError(
                        f"Python 保留包出现未审批的已裁依赖 import：{site_relative} -> {imported}"
                    )
                if ownership.get(runtime_relative) != (
                    expected["owner"],
                    expected["owner_version"],
                ):
                    raise ValueError(
                        f"Python 保留包可选 import 的 RECORD 所有者/版本漂移：{site_relative}"
                    )


def _validate_formal_python_import_boundary(package: Path, contract: dict[str, object]) -> None:
    formal = contract["formal"]
    assert isinstance(formal, dict)
    for path in sorted(package.rglob("*.py"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(package).as_posix()
        if relative.startswith("runtime/python/"):
            continue
        for imported in _python_static_import_names(path):
            for distribution in _python_pruned_import_matches(imported, contract):
                approved = formal.get((distribution, relative))
                if not isinstance(approved, frozenset) or imported not in approved:
                    raise ValueError(
                        f"正式 Python payload 出现未审批的已裁依赖 import：{relative} -> {imported}"
                    )


def _python_license_metadata(metadata: object) -> tuple[str, dict[str, object]]:
    expression = str(metadata.get("License-Expression", "") or "").strip()  # type: ignore[attr-defined]
    declared = str(metadata.get("License", "") or "").strip()  # type: ignore[attr-defined]
    classifiers = sorted(
        {
            str(value).removeprefix("License :: ").strip()
            for value in metadata.get_all("Classifier", [])  # type: ignore[attr-defined]
            if str(value).startswith("License :: ")
        },
        key=str.casefold,
    )
    meaningful_declared = declared if declared and declared.upper() != "UNKNOWN" else ""
    license_value = expression or meaningful_declared or " OR ".join(classifiers)
    if not license_value:
        raise ValueError("Python distribution 缺少许可证声明")
    return license_value, {
        "expression": expression,
        "declared": declared,
        "classifiers": classifiers,
    }


def _portable_primp_bom_ref(name: str, version: str) -> str | None:
    crate_path = PRIMP_PORTABLE_CRATE_PATHS.get((name, version))
    if crate_path is None:
        return None
    return (
        f"git+{PRIMP_SOURCE_REPOSITORY}@{PRIMP_SOURCE_COMMIT}"
        f"#{crate_path}@{version}"
    )


def _nonportable_python_bom_ref(bom_ref: str) -> bool:
    folded = bom_ref.casefold()
    return (
        folded.startswith(("path+file:", "file:", "path+"))
        or bom_ref.startswith(("/", "\\"))
        or "\\" in bom_ref
        or re.search(r"(?i)(?:^|[^a-z0-9])[a-z]:[\\/]", bom_ref) is not None
    )


def _normalize_python_native_bom_ref(bom_ref: str, name: str, version: str) -> str:
    if not bom_ref.casefold().startswith("path+file:"):
        if _nonportable_python_bom_ref(bom_ref):
            raise ValueError("Python native wheel SBOM contains a non-portable bom_ref")
        return bom_ref
    crate_path = PRIMP_PORTABLE_CRATE_PATHS.get((name, version))
    portable = _portable_primp_bom_ref(name, version)
    if crate_path is None or portable is None or "\\" in bom_ref:
        raise ValueError("Python native wheel SBOM local bom_ref is outside the frozen primp source map")
    source, separator, fragment = bom_ref.partition("#")
    expected_suffix = f"/primp/{crate_path}".casefold()
    if (
        not separator
        or not source.casefold().startswith("path+file:///")
        or not source.casefold().endswith(expected_suffix)
        or fragment not in {version, f"{name}@{version}"}
    ):
        raise ValueError("Python native wheel SBOM local bom_ref does not match the frozen primp source map")
    return portable


def _python_native_sbom_identity(
    sbom_bytes: bytes,
) -> tuple[list[dict[str, object]], int, str]:
    try:
        payload = json.loads(sbom_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Python native wheel SBOM 无法解析") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("components"), list) or not isinstance(
        payload.get("dependencies"), list
    ):
        raise ValueError("Python native wheel SBOM 结构无效")
    identities: list[dict[str, object]] = []
    seen_refs: set[str] = set()
    for component in payload["components"]:
        if not isinstance(component, dict):
            raise ValueError("Python native wheel SBOM component 结构无效")
        raw_bom_ref = str(component.get("bom-ref", ""))
        name = str(component.get("name", ""))
        version = str(component.get("version", ""))
        bom_ref = _normalize_python_native_bom_ref(raw_bom_ref, name, version)
        licenses = component.get("licenses", [])
        hashes = component.get("hashes", [])
        if (
            not bom_ref
            or bom_ref in seen_refs
            or not isinstance(licenses, list)
            or not isinstance(hashes, list)
        ):
            raise ValueError("Python native wheel SBOM component 身份无效")
        license_expressions = []
        for license_item in licenses:
            if not isinstance(license_item, dict) or not isinstance(license_item.get("expression"), str):
                raise ValueError("Python native wheel SBOM 许可证表达式无效")
            license_expressions.append(license_item["expression"])
        sha256_values = []
        for hash_item in hashes:
            if not isinstance(hash_item, dict):
                raise ValueError("Python native wheel SBOM hash 结构无效")
            if hash_item.get("alg") == "SHA-256":
                value = str(hash_item.get("content", "")).lower()
                if not re.fullmatch(r"[0-9a-f]{64}", value):
                    raise ValueError("Python native wheel SBOM SHA-256 无效")
                sha256_values.append(value)
        identity = {
            "bom_ref": bom_ref,
            "name": name,
            "version": version,
            "scope": str(component.get("scope", "")),
            "license_expressions": license_expressions,
            "sha256": sha256_values,
        }
        if not identity["name"] or not identity["version"] or not license_expressions:
            raise ValueError("Python native wheel SBOM component 名称/版本/许可证缺失")
        identities.append(identity)
        seen_refs.add(bom_ref)
    identities.sort(key=lambda item: str(item["bom_ref"]))
    canonical = "".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for item in identities
    ).encode("utf-8")
    return identities, len(payload["dependencies"]), hashlib.sha256(canonical).hexdigest().upper()


def _python_license_corpus_identities(corpus_bytes: bytes) -> list[dict[str, object]]:
    try:
        text = corpus_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Python native license corpus 不是 UTF-8") from exc
    identities: list[dict[str, object]] = []
    expected_fields = {"bom_ref", "name", "version", "scope", "license_expressions", "sha256"}
    for line in text.splitlines():
        if not line.startswith("Component: "):
            continue
        try:
            item = json.loads(line.removeprefix("Component: "))
        except json.JSONDecodeError as exc:
            raise ValueError("Python native license corpus component 索引无法解析") from exc
        if not isinstance(item, dict) or set(item) != expected_fields:
            raise ValueError("Python native license corpus component 索引结构无效")
        bom_ref = item.get("bom_ref")
        name = item.get("name")
        version = item.get("version")
        if not isinstance(bom_ref, str) or not isinstance(name, str) or not isinstance(version, str):
            raise ValueError("Python native license corpus component identity fields are invalid")
        if _nonportable_python_bom_ref(bom_ref):
            raise ValueError("Python native license corpus contains a non-portable bom_ref")
        portable = _portable_primp_bom_ref(name, version)
        if portable is not None and bom_ref != portable:
            raise ValueError("Python native license corpus primp bom_ref is not the frozen portable source identity")
        identities.append(item)
    if not identities or identities != sorted(identities, key=lambda item: str(item["bom_ref"])):
        raise ValueError("Python native license corpus component 索引缺失或未稳定排序")
    if len({str(item["bom_ref"]) for item in identities}) != len(identities):
        raise ValueError("Python native license corpus component 索引重复")
    return identities


def _read_python_license_contract(repo: Path) -> dict[tuple[str, str], dict[str, object]]:
    boundary = repo / PYTHON_LICENSE_BOUNDARY
    manifest_path = boundary / PYTHON_LICENSE_OVERRIDE_MANIFEST
    _require_file(boundary / "README.md", "Python runtime 许可证边界说明")
    _require_file(manifest_path, "Python runtime 许可证覆盖表")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Python runtime 许可证覆盖表无法解析") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "status",
        "verified_overrides",
        "unresolved",
    }:
        raise ValueError("Python runtime 许可证覆盖表结构无效")
    unresolved = manifest.get("unresolved")
    if manifest.get("schema_version") != 2 or manifest.get("status") != "complete" or unresolved != []:
        raise ValueError("Python runtime 许可证证据尚未闭合")
    entries = manifest.get("verified_overrides")
    if not isinstance(entries, list):
        raise ValueError("Python runtime verified_overrides 必须是数组")
    expected_fields = {
        "name",
        "version",
        "spdx",
        "source_repository",
        "source_tag",
        "source_commit",
        "source_url",
        "source_sha256",
        "license_file",
        "license_sha256",
        "additional_evidence",
    }
    parsed: dict[tuple[str, str], dict[str, object]] = {}
    order: list[tuple[str, str]] = []
    destinations: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict) or set(raw) != expected_fields:
            raise ValueError("Python runtime 许可证覆盖条目结构无效")
        name = str(raw.get("name", ""))
        normalized = _normalize_python_distribution_name(name)
        version = str(raw.get("version", "")).strip()
        spdx = str(raw.get("spdx", "")).strip()
        commit = str(raw.get("source_commit", "")).strip().lower()
        source_repository = str(raw.get("source_repository", "")).strip()
        source_tag = str(raw.get("source_tag", "")).strip()
        source_url = str(raw.get("source_url", "")).strip()
        source_sha = str(raw.get("source_sha256", "")).strip().upper()
        license_sha = str(raw.get("license_sha256", "")).strip().upper()
        relative = PurePosixPath(str(raw.get("license_file", "")))
        additional = raw.get("additional_evidence")
        if (
            name != normalized
            or not version
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]*", version)
            or not spdx
            or not source_repository.startswith("https://github.com/")
            or not source_tag
            or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or not source_url.startswith("https://raw.githubusercontent.com/")
            or f"/{commit}/" not in source_url
            or not re.fullmatch(r"[0-9A-F]{64}", source_sha)
            or source_sha != license_sha
            or relative.is_absolute()
            or len(relative.parts) != 2
            or relative.parts[0] != "dependency-licenses"
            or any(part in ("", ".", "..") for part in relative.parts)
            or not isinstance(additional, list)
        ):
            raise ValueError(f"Python runtime 许可证覆盖锁无效：{name}@{version}")
        evidence = boundary / Path(*relative.parts)
        _require_file(evidence, f"Python runtime 许可证证据 {name}@{version}")
        if sha256_file(evidence) != license_sha:
            raise ValueError(f"Python runtime 许可证证据哈希不一致：{name}@{version}")
        destination = evidence.name.casefold()
        if destination in destinations:
            raise ValueError("Python runtime 许可证证据目标文件名冲突")
        destinations.add(destination)
        normalized_additional: list[dict[str, object]] = []
        additional_sources: list[dict[str, object]] = []
        additional_fields = {
            "kind",
            "source_kind",
            "bound_runtime_path",
            "bound_runtime_sha256",
            "component_count",
            "dependency_count",
            "component_identity_sha256",
            "encoded_file",
            "encoded_sha256",
            "encoding",
            "evidence_file",
            "evidence_sha256",
            "evidence_size",
        }
        for supplement in additional:
            if not isinstance(supplement, dict) or set(supplement) != additional_fields:
                raise ValueError(f"Python runtime 补充许可证证据结构无效：{name}@{version}")
            kind = str(supplement.get("kind", ""))
            source_kind = str(supplement.get("source_kind", ""))
            bound_runtime = PurePosixPath(str(supplement.get("bound_runtime_path", "")))
            bound_sha = str(supplement.get("bound_runtime_sha256", "")).strip().upper()
            component_count = supplement.get("component_count")
            dependency_count = supplement.get("dependency_count")
            identity_sha = str(supplement.get("component_identity_sha256", "")).strip().upper()
            encoded_relative = PurePosixPath(str(supplement.get("encoded_file", "")))
            encoded_sha = str(supplement.get("encoded_sha256", "")).strip().upper()
            evidence_relative = PurePosixPath(str(supplement.get("evidence_file", "")))
            evidence_sha = str(supplement.get("evidence_sha256", "")).strip().upper()
            evidence_size = supplement.get("evidence_size")
            if (
                kind not in {"license", "notice", "redistribution"}
                or source_kind != "embedded-runtime-sbom-derived-license-corpus"
                or bound_runtime.is_absolute()
                or not bound_runtime.parts
                or bound_runtime.parts[:2] != ("Lib", "site-packages")
                or any(part in ("", ".", "..") for part in bound_runtime.parts)
                or not re.fullmatch(r"[0-9A-F]{64}", bound_sha)
                or not isinstance(component_count, int)
                or isinstance(component_count, bool)
                or component_count < 1
                or not isinstance(dependency_count, int)
                or isinstance(dependency_count, bool)
                or dependency_count < 1
                or not re.fullmatch(r"[0-9A-F]{64}", identity_sha)
                or encoded_relative.is_absolute()
                or len(encoded_relative.parts) != 2
                or encoded_relative.parts[0] != "dependency-licenses"
                or any(part in ("", ".", "..") for part in encoded_relative.parts)
                or not re.fullmatch(r"[0-9A-F]{64}", encoded_sha)
                or supplement.get("encoding") != "base64+gzip"
                or evidence_relative.is_absolute()
                or len(evidence_relative.parts) != 2
                or evidence_relative.parts[0] != "dependency-licenses"
                or any(part in ("", ".", "..") for part in evidence_relative.parts)
                or not re.fullmatch(r"[0-9A-F]{64}", evidence_sha)
                or not isinstance(evidence_size, int)
                or isinstance(evidence_size, bool)
                or evidence_size < 1
            ):
                raise ValueError(f"Python runtime 补充许可证证据锁无效：{name}@{version}")
            encoded_path = boundary / Path(*encoded_relative.parts)
            _require_file(encoded_path, f"Python runtime 压缩许可证证据 {name}@{version}")
            encoded_bytes = encoded_path.read_bytes()
            if hashlib.sha256(encoded_bytes).hexdigest().upper() != encoded_sha:
                raise ValueError(f"Python runtime 压缩许可证证据哈希不一致：{name}@{version}")
            try:
                compressed = base64.b64decode(b"".join(encoded_bytes.split()), validate=True)
                evidence_bytes = gzip.decompress(compressed)
            except (ValueError, gzip.BadGzipFile, OSError) as exc:
                raise ValueError(f"Python runtime 压缩许可证证据无法解码：{name}@{version}") from exc
            if (
                len(evidence_bytes) != evidence_size
                or hashlib.sha256(evidence_bytes).hexdigest().upper() != evidence_sha
            ):
                raise ValueError(f"Python runtime 解码许可证 corpus 不一致：{name}@{version}")
            corpus_identities = _python_license_corpus_identities(evidence_bytes)
            canonical_corpus = "".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for item in corpus_identities
            ).encode("utf-8")
            if (
                len(corpus_identities) != component_count
                or hashlib.sha256(canonical_corpus).hexdigest().upper() != identity_sha
            ):
                raise ValueError(f"Python native license corpus 身份闭包不一致：{name}@{version}")
            evidence_destination = evidence_relative.name.casefold()
            if evidence_destination in destinations:
                raise ValueError("Python runtime 许可证证据目标文件名冲突")
            destinations.add(evidence_destination)
            normalized_supplement = dict(supplement)
            normalized_supplement["bound_runtime_sha256"] = bound_sha
            normalized_supplement["encoded_sha256"] = encoded_sha
            normalized_supplement["evidence_sha256"] = evidence_sha
            normalized_supplement["component_identity_sha256"] = identity_sha
            normalized_additional.append(normalized_supplement)
            additional_sources.append(
                {
                    "encoded_path": encoded_path,
                    "evidence_bytes": evidence_bytes,
                    "evidence_file": evidence_relative.name,
                    "corpus_identities": corpus_identities,
                }
            )
        if normalized_additional != sorted(
            normalized_additional, key=lambda item: str(item["evidence_file"]).casefold()
        ):
            raise ValueError(f"Python runtime 补充许可证证据未稳定排序：{name}@{version}")
        key = (normalized, version)
        if key in parsed:
            raise ValueError(f"Python runtime 许可证覆盖重复：{name}@{version}")
        item = dict(raw)
        item["source_commit"] = commit
        item["source_sha256"] = source_sha
        item["license_sha256"] = license_sha
        item["additional_evidence"] = normalized_additional
        item["source_path"] = evidence
        item["additional_evidence_sources"] = additional_sources
        parsed[key] = item
        order.append(key)
    if order != sorted(order, key=lambda item: (item[0], item[1].casefold())):
        raise ValueError("Python runtime 许可证覆盖表未稳定排序")
    return parsed


def _python_file_entry(path: Path, runtime_root: Path) -> dict[str, object]:
    relative = path.relative_to(runtime_root).as_posix()
    return {
        "path": f"runtime/python/{relative}",
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _canonical_python_files_sha256(entries: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(
            f"{entry['path']}\0{entry['size']}\0{entry['sha256']}\n".encode("utf-8")
        )
    return digest.hexdigest().upper()


def _build_python_runtime_sbom(
    runtime_root: Path,
    overrides: dict[tuple[str, str], dict[str, object]],
    moviepy_patch: dict[str, object] | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    site_packages = runtime_root / "Lib" / "site-packages"
    _require_directory(site_packages, "Python site-packages")
    dist_infos = sorted(site_packages.glob("*.dist-info"), key=lambda path: path.name.casefold())
    if not dist_infos:
        raise ValueError("Python runtime 未发现任何 dist-info distribution")
    if any(site_packages.glob("*.egg-info")):
        raise ValueError("Python runtime 不接受缺少标准 RECORD 的 egg-info distribution")
    all_actual_site_files = {
        path.relative_to(runtime_root).as_posix()
        for path in site_packages.rglob("*")
        if path.is_file()
    }
    owned_paths: dict[str, str] = {}
    distributions: list[dict[str, object]] = []
    used_overrides: dict[tuple[str, str], dict[str, object]] = {}
    normalized_keys: set[tuple[str, str]] = set()
    moviepy_modification_seen = False
    for dist_info in dist_infos:
        metadata_path = dist_info / "METADATA"
        record_path = dist_info / "RECORD"
        _require_file(metadata_path, f"Python distribution METADATA {dist_info.name}")
        _require_file(record_path, f"Python distribution RECORD {dist_info.name}")
        try:
            metadata = BytesParser().parsebytes(metadata_path.read_bytes())
        except Exception as exc:
            raise ValueError(f"Python distribution METADATA 无法解析：{dist_info.name}") from exc
        name = str(metadata.get("Name", "") or "").strip()
        version = str(metadata.get("Version", "") or "").strip()
        normalized_name = _normalize_python_distribution_name(name)
        key = (normalized_name, version)
        if not name or not normalized_name or not version or key in normalized_keys:
            raise ValueError(f"Python distribution 名称或版本无效/重复：{dist_info.name}")
        if normalized_name in PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS:
            raise ValueError(f"Python runtime 受控裁剪 distribution 仍有残留：{name}@{version}")
        normalized_keys.add(key)
        license_value, license_metadata = _python_license_metadata(metadata)
        try:
            rows = list(csv.reader(io.StringIO(record_path.read_text(encoding="utf-8-sig"))))
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise ValueError(f"Python distribution RECORD 无法解析：{dist_info.name}") from exc
        file_paths: set[str] = set()
        for row in rows:
            if len(row) != 3:
                raise ValueError(f"Python distribution RECORD 条目无效：{dist_info.name}")
            runtime_relative = _python_record_runtime_path(row[0])
            candidate = runtime_root / Path(*PurePosixPath(runtime_relative).parts)
            if not candidate.is_file():
                continue
            _require_within(runtime_root, candidate)
            if runtime_relative in owned_paths:
                raise ValueError(
                    f"Python runtime 文件被多个 distribution 声明：{runtime_relative}"
                )
            owned_paths[runtime_relative] = dist_info.name
            file_paths.add(runtime_relative)
        expected_core = {
            metadata_path.relative_to(runtime_root).as_posix(),
            record_path.relative_to(runtime_root).as_posix(),
        }
        if not expected_core.issubset(file_paths):
            raise ValueError(f"Python distribution RECORD 未覆盖自身元数据：{dist_info.name}")
        files = [
            _python_file_entry(runtime_root / Path(*PurePosixPath(relative).parts), runtime_root)
            for relative in sorted(file_paths, key=str.casefold)
        ]
        embedded_evidence = []
        embedded_has_license = False
        for file_entry in files:
            kind = _python_license_evidence_kind(str(file_entry["path"]))
            if kind is None:
                continue
            embedded_has_license = embedded_has_license or kind == "license"
            embedded_evidence.append({**file_entry, "kind": kind})
        override_payload: dict[str, object] | None = None
        evidence_source = "installed_distribution"
        license_evidence = embedded_evidence
        if not embedded_has_license:
            override = overrides.get(key)
            if override is None:
                raise ValueError(f"Python distribution 缺少许可证正文且无精确版本覆盖：{name}@{version}")
            if key in used_overrides:
                raise ValueError(f"Python runtime 许可证覆盖被重复使用：{name}@{version}")
            used_overrides[key] = override
            source_path = override["source_path"]
            assert isinstance(source_path, Path)
            destination = f"licenses/python-runtime-dependencies/{source_path.name}"
            license_evidence = [
                *embedded_evidence,
                {
                    "path": destination,
                    "size": source_path.stat().st_size,
                    "sha256": str(override["license_sha256"]),
                    "kind": "license",
                },
            ]
            additional = override["additional_evidence"]
            additional_sources = override["additional_evidence_sources"]
            assert isinstance(additional, list) and isinstance(additional_sources, list)
            for supplement, supplement_source in zip(additional, additional_sources, strict=True):
                assert isinstance(supplement, dict)
                assert isinstance(supplement_source, dict)
                bound_relative = str(supplement["bound_runtime_path"])
                bound_path = runtime_root / Path(*PurePosixPath(bound_relative).parts)
                _require_file(bound_path, f"Python runtime 补充证据绑定文件 {name}@{version}")
                _require_within(runtime_root, bound_path)
                if (
                    bound_relative not in file_paths
                    or sha256_file(bound_path) != supplement["bound_runtime_sha256"]
                ):
                    raise ValueError(f"Python runtime 补充许可证 corpus 与 wheel SBOM 不一致：{name}@{version}")
                sbom_identities, dependency_count, identity_sha = _python_native_sbom_identity(
                    bound_path.read_bytes()
                )
                if (
                    sbom_identities != supplement_source["corpus_identities"]
                    or len(sbom_identities) != supplement["component_count"]
                    or dependency_count != supplement["dependency_count"]
                    or identity_sha != supplement["component_identity_sha256"]
                ):
                    raise ValueError(f"Python native wheel SBOM 与许可证 corpus 未逐组件闭合：{name}@{version}")
                evidence_name = PurePosixPath(str(supplement["evidence_file"])).name
                license_evidence.append(
                    {
                        "path": f"licenses/python-runtime-dependencies/{evidence_name}",
                        "size": supplement["evidence_size"],
                        "sha256": supplement["evidence_sha256"],
                        "kind": supplement["kind"],
                    }
                )
            evidence_source = "project_verified_override"
            license_value = str(override["spdx"])
            override_payload = {
                field: override[field]
                for field in (
                    "name",
                    "version",
                    "spdx",
                    "source_repository",
                    "source_tag",
                    "source_commit",
                    "source_url",
                    "source_sha256",
                    "license_file",
                    "license_sha256",
                    "additional_evidence",
                )
            }
        modification: dict[str, object] | None = None
        if normalized_name == "moviepy" and moviepy_patch is not None:
            if version != MOVIEPY_DISTRIBUTION_VERSION:
                raise ValueError("MoviePy staged distribution version drifted after patching")
            moviepy_modification_seen = True
            modification = {
                "modified": True,
                "patch_manifest": MOVIEPY_PATCH_MANIFEST_RELATIVE.as_posix(),
                "patch_manifest_sha256": MOVIEPY_PATCH_MANIFEST_SHA256,
                "patch_id": MOVIEPY_PATCH_ID,
                "patch_version": MOVIEPY_PATCH_VERSION,
                "module_reported_version": MOVIEPY_MODULE_REPORTED_VERSION,
                "patcher_sha256": MOVIEPY_PATCHER_SHA256,
                "writer_sha256": MOVIEPY_PATCHED_WRITER_SHA256,
                "record_sha256": MOVIEPY_PATCHED_RECORD_SHA256,
                "record_consistent": True,
            }
        distributions.append(
            {
                "name": name,
                "normalized_name": normalized_name,
                "version": version,
                "dist_info": f"runtime/python/{dist_info.relative_to(runtime_root).as_posix()}",
                "license": license_value,
                "license_metadata": license_metadata,
                "license_evidence_source": evidence_source,
                "license_evidence": license_evidence,
                "override": override_payload,
                "modification": modification,
                "payload_sha256": _canonical_python_files_sha256(files),
                "files": files,
            }
        )
    if moviepy_patch is not None and not moviepy_modification_seen:
        raise ValueError("MoviePy Windows-MF patch contract was supplied but moviepy@2.2.1 is absent")
    unowned = sorted(all_actual_site_files - set(owned_paths), key=str.casefold)
    if unowned:
        raise ValueError("Python site-packages 存在 RECORD 未归属文件：" + ", ".join(unowned[:8]))
    if set(used_overrides) != set(overrides):
        unused = sorted(set(overrides) - set(used_overrides))
        raise ValueError(
            "Python runtime 许可证覆盖表存在未使用或版本漂移条目："
            + ", ".join(f"{name}@{version}" for name, version in unused)
        )
    distributions.sort(key=lambda item: (str(item["normalized_name"]), str(item["version"]).casefold()))
    closure = hashlib.sha256()
    for item in distributions:
        closure.update(
            f"{item['normalized_name']}\0{item['version']}\0{item['payload_sha256']}\n".encode("utf-8")
        )
    sbom: dict[str, object] = {
        "schema_version": 2,
        "runtime_kind": "python_distribution_closure",
        "platform": "win32-x64",
        "site_packages": "runtime/python/Lib/site-packages",
        "pruned_distributions": [
            {
                "name": name,
                "version": version,
                "reason": "excluded UI/LLM/Whisper roots and metadata-proven orphan dependencies",
            }
            for name, version in sorted(PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS.items())
        ],
        "distribution_count": len(distributions),
        "site_packages_file_count": len(all_actual_site_files),
        "owned_file_count": len(owned_paths),
        "project_verified_license_overrides": len(used_overrides),
        "payload_sha256": closure.hexdigest().upper(),
        "distributions": distributions,
    }
    return sbom, [used_overrides[key] for key in sorted(used_overrides)]


def _copy_motion_tree(source: Path, destination: Path) -> None:
    for path in _iter_tree_files(source, allow_node_modules=True):
        _copy_file(path, destination / path.relative_to(source))


def _read_node_package(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Node package.json 无法解析：{path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Node package.json 顶层必须是对象")
    return payload


def _read_ffmpeg_distribution_contract(
    repo: Path, *, require_formal: bool
) -> tuple[dict[str, object], Path]:
    """Bind the package to the repository-owned LGPL runtime and frozen lock."""

    lock_path = repo / FFMPEG_LOCK_RELATIVE
    runtime = repo / FFMPEG_RUNTIME_RELATIVE
    _require_file(lock_path, "FFmpeg LGPL upstream lock")
    _require_directory(runtime, "FFmpeg LGPL Windows x64 runtime")
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("FFmpeg LGPL upstream lock cannot be parsed") from exc
    if not isinstance(lock, dict):
        raise ValueError("FFmpeg LGPL upstream lock root must be an object")
    runtime_contract = lock.get("runtime")
    entries = runtime_contract.get("files") if isinstance(runtime_contract, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("FFmpeg LGPL upstream lock lacks an exact runtime file list")
    expected_names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("FFmpeg LGPL runtime lock contains a non-object file entry")
        name = entry.get("name")
        size = entry.get("bytes")
        digest = str(entry.get("sha256", "")).upper()
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or not isinstance(size, int)
            or size <= 0
            or not re.fullmatch(r"[0-9A-F]{64}", digest)
        ):
            raise ValueError("FFmpeg LGPL runtime lock contains an invalid file identity")
        path = runtime / name
        _require_file(path, f"FFmpeg LGPL runtime file {name}")
        if path.stat().st_size != size or sha256_file(path) != digest:
            raise ValueError(f"FFmpeg LGPL runtime file does not match the frozen lock: {name}")
        expected_names.append(name)
    if len(expected_names) != len(set(name.casefold() for name in expected_names)):
        raise ValueError("FFmpeg LGPL runtime lock contains duplicate Windows file names")
    actual_names = sorted(
        (path.name for path in runtime.iterdir() if path.is_file()), key=str.casefold
    )
    if sorted(expected_names, key=str.casefold) != actual_names:
        raise ValueError("FFmpeg LGPL runtime directory is not the exact frozen file set")
    if any(path.is_dir() for path in runtime.iterdir()):
        raise ValueError("FFmpeg LGPL runtime directory must not contain subdirectories")
    forbidden = re.compile(r"(?i)(?:^ffplay\.exe$|^avdevice-|\.(?:lib|h|hpp|pdb|exp)$|x26[45]|rav1e|aom|vpx)")
    if any(forbidden.search(name) for name in expected_names):
        raise ValueError("FFmpeg LGPL runtime contains a forbidden development or GPL codec file")

    for name in FFMPEG_LICENSE_FILES:
        _require_file(repo / FFMPEG_BOUNDARY_RELATIVE / "licenses" / name, f"FFmpeg license {name}")

    if require_formal:
        try:
            from tools.verify_ffmpeg_distribution import verify_ffmpeg_distribution
        except ImportError:
            from verify_ffmpeg_distribution import verify_ffmpeg_distribution

        errors = verify_ffmpeg_distribution(
            lock_path,
            runtime_dir=runtime,
            repo_root=repo,
            inspect_archives=False,
        )
        if errors:
            raise ValueError("FFmpeg LGPL formal distribution verification failed: " + "; ".join(errors))
        if (
            lock.get("schema_version") != 2
            or lock.get("distribution_status") != "release_ready_source_companion_frozen"
            or lock.get("license") != "LGPL-2.1-or-later"
            or len(expected_names) != 9
        ):
            raise ValueError("FFmpeg release package is not bound to the frozen LGPL nine-file runtime")
    return lock, runtime


def _read_hyperframes_patch_contract(repo: Path) -> dict[str, object]:
    manifest_path = repo / HYPERFRAMES_PATCH_MANIFEST_RELATIVE
    patcher_path = repo / HYPERFRAMES_PATCHER_RELATIVE
    _require_file(manifest_path, "HyperFrames Windows-MF patch manifest")
    _require_file(patcher_path, "HyperFrames Windows-MF patcher")
    manifest = _read_node_package(manifest_path)
    modified = manifest.get("modified_files")
    patcher = manifest.get("patcher")
    packaging = manifest.get("formal_packaging_contract")
    report_fields = packaging.get("report_fields") if isinstance(packaging, dict) else None
    modified_identity = []
    if isinstance(modified, list):
        modified_identity = [
            (
                item.get("path"),
                str(item.get("upstream_sha256", "")).upper(),
                str(item.get("patched_sha256", "")).upper(),
            )
            for item in modified
            if isinstance(item, dict)
        ]
    if (
        manifest.get("schema_version") != 1
        or manifest.get("patch_id") != HYPERFRAMES_PATCH_ID
        or manifest.get("patch_version") != HYPERFRAMES_PATCH_VERSION
        or manifest.get("component") != f"hyperframes@{HYPERFRAMES_VERSION}"
        or modified_identity
        != [("dist/cli.js", HYPERFRAMES_UPSTREAM_CLI_SHA256, HYPERFRAMES_PATCHED_CLI_SHA256)]
        or not isinstance(patcher, dict)
        or patcher.get("path") != HYPERFRAMES_PATCHER_RELATIVE.as_posix()
        or str(patcher.get("sha256", "")).upper() != sha256_file(patcher_path)
        or not isinstance(packaging, dict)
        or packaging.get("mpt_video_codec") != H264_CODEC_STRATEGY
        or report_fields
        != {
            "codec_strategy": H264_CODEC_STRATEGY,
            "patch_id": HYPERFRAMES_PATCH_ID,
            "patch_version": HYPERFRAMES_PATCH_VERSION,
            "patched_cli_sha256": HYPERFRAMES_PATCHED_CLI_SHA256,
        }
    ):
        raise ValueError("HyperFrames Windows-MF patch contract is not the frozen v1.2.0 identity")
    return manifest


def _read_moviepy_patch_contract(repo: Path) -> dict[str, object]:
    manifest_path = repo / MOVIEPY_PATCH_MANIFEST_RELATIVE
    patcher_path = repo / MOVIEPY_PATCHER_RELATIVE
    _require_file(manifest_path, "MoviePy Windows-MF patch manifest")
    _require_file(patcher_path, "MoviePy Windows-MF patcher")
    if sha256_file(manifest_path) != MOVIEPY_PATCH_MANIFEST_SHA256:
        raise ValueError("MoviePy Windows-MF patch manifest SHA-256 drifted")
    if sha256_file(patcher_path) != MOVIEPY_PATCHER_SHA256:
        raise ValueError("MoviePy Windows-MF patcher SHA-256 drifted")
    manifest = _read_node_package(manifest_path)
    component = manifest.get("component")
    identity_files = manifest.get("identity_files")
    modified = manifest.get("modified_files")
    codec_contract = manifest.get("codec_contract")
    packaging = manifest.get("formal_packaging_contract")
    expected_modified = [
        ("writer", MOVIEPY_UPSTREAM_WRITER_SHA256, MOVIEPY_PATCHED_WRITER_SHA256),
        ("record", MOVIEPY_UPSTREAM_RECORD_SHA256, MOVIEPY_PATCHED_RECORD_SHA256),
    ]
    actual_modified = []
    if isinstance(modified, list):
        for item in modified:
            if isinstance(item, dict):
                actual_modified.append(
                    (
                        item.get("role"),
                        str(item.get("upstream_sha256", "")).upper(),
                        str(item.get("patched_sha256", "")).upper(),
                    )
                )
    if (
        manifest.get("schema_version") != 1
        or manifest.get("patch_id") != MOVIEPY_PATCH_ID
        or manifest.get("patch_version") != MOVIEPY_PATCH_VERSION
        or not isinstance(component, dict)
        or component.get("distribution_name") != "moviepy"
        or component.get("distribution_version") != MOVIEPY_DISTRIBUTION_VERSION
        or component.get("module_reported_version") != MOVIEPY_MODULE_REPORTED_VERSION
        or component.get("license") != "MIT"
        or not isinstance(identity_files, dict)
        or actual_modified != expected_modified
        or not isinstance(codec_contract, dict)
        or codec_contract.get("strategy") != H264_CODEC_STRATEGY
        or codec_contract.get("fixed_args")
        != [
            "-c:v", "h264_mf", "-rate_control", "quality", "-quality", "72",
            "-scenario", "archive", "-hw_encoding", "0", "-bf", "0",
            "-pix_fmt", "yuv420p",
        ]
        or not isinstance(packaging, dict)
        or not isinstance(packaging.get("sbom_fields"), list)
    ):
        raise ValueError("MoviePy Windows-MF patch contract is not the frozen v1.0.0 identity")
    return manifest


def _apply_moviepy_windows_mf_patch(repo: Path, python_runtime: Path) -> dict[str, object]:
    manifest = _read_moviepy_patch_contract(repo)
    patcher = repo / MOVIEPY_PATCHER_RELATIVE
    for extra_args, label in (((), "apply"), (("--check",), "check")):
        completed = subprocess.run(
            [
                sys.executable,
                str(patcher),
                "--python-runtime",
                str(python_runtime),
                *extra_args,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise ValueError(f"MoviePy Windows-MF staging patch {label} failed: {detail}")
    site_packages = python_runtime / "Lib" / "site-packages"
    writer = site_packages / "moviepy" / "video" / "io" / "ffmpeg_writer.py"
    record = site_packages / "moviepy-2.2.1.dist-info" / "RECORD"
    if (
        sha256_file(writer) != MOVIEPY_PATCHED_WRITER_SHA256
        or sha256_file(record) != MOVIEPY_PATCHED_RECORD_SHA256
    ):
        raise ValueError("MoviePy staging writer/RECORD does not match the frozen patched identity")
    return manifest


def _read_hyperframes_license_contract(repo: Path) -> dict[str, object]:
    boundary = repo / "third_party" / "hyperframes"
    license_path = boundary / "LICENSE"
    lock_path = boundary / "upstream-lock.json"
    overrides_path = boundary / "dependency-license-overrides.json"
    for path, label in (
        (license_path, "HyperFrames 上游许可证副本"),
        (lock_path, "HyperFrames 上游锁"),
        (overrides_path, "HyperFrames 依赖许可证覆盖表"),
    ):
        _require_file(path, label)
    lock = _read_node_package(lock_path)
    expected_lock = {
        "schema_version": 1,
        "name": "HyperFrames",
        "repository": HYPERFRAMES_REPOSITORY,
        "upstream_tag": HYPERFRAMES_UPSTREAM_TAG,
        "upstream_commit": HYPERFRAMES_UPSTREAM_COMMIT,
        "npm_package": "hyperframes",
        "npm_version": HYPERFRAMES_VERSION,
        "npm_resolved": f"https://registry.npmjs.org/hyperframes/-/hyperframes-{HYPERFRAMES_VERSION}.tgz",
        "npm_integrity": "sha512-R8Vds5hY9XULMsCGUa+qynC7F0tL7KZyDaL6cgQ4xyJAATC9fOIPgRMOBkOHYd9JOntRqbR9bFSsfK7mYJjaow==",
        "license": "Apache-2.0",
        "license_source": f"https://raw.githubusercontent.com/heygen-com/hyperframes/{HYPERFRAMES_UPSTREAM_TAG}/LICENSE",
        "license_sha256": sha256_file(license_path),
        "modified_distribution": {
            "status": "patched_for_windows_media_foundation_and_offline_runtime",
            "patch_manifest": "windows-mf-patch.json",
            "patch_id": HYPERFRAMES_PATCH_ID,
            "patch_version": HYPERFRAMES_PATCH_VERSION,
            "modified_files": [
                {
                    "path": "dist/cli.js",
                    "upstream_sha256": HYPERFRAMES_UPSTREAM_CLI_SHA256,
                    "patched_sha256": HYPERFRAMES_PATCHED_CLI_SHA256,
                }
            ],
        },
    }
    if lock != expected_lock or lock["license_sha256"] != "4259155FB06F127687EE7B0A8A3682D45132DB0F2DA26CBC0B7A2D1E796436B8":
        raise ValueError("HyperFrames 固定 tag、commit、npm 版本或官方许可证锁不一致")
    overrides = _read_node_package(overrides_path)
    if set(overrides) != {"schema_version", "status", "verified_overrides", "unresolved"}:
        raise ValueError("HyperFrames 依赖许可证覆盖表结构无效")
    unresolved = overrides.get("unresolved")
    if overrides.get("schema_version") != 1 or not isinstance(unresolved, list):
        raise ValueError("HyperFrames 依赖许可证覆盖表版本无效")
    if unresolved:
        subjects = sorted(
            f"{item.get('name')}@{item.get('version')}"
            for item in unresolved
            if isinstance(item, dict)
        )
        raise ValueError("HyperFrames 依赖许可证证据尚未闭合：" + ", ".join(subjects))
    if overrides.get("status") != "complete":
        raise ValueError("HyperFrames 依赖许可证覆盖表尚未完成")
    verified = overrides.get("verified_overrides")
    if not isinstance(verified, list):
        raise ValueError("HyperFrames 依赖许可证覆盖表 verified_overrides 无效")
    parsed: dict[tuple[str, str], dict[str, object]] = {}
    evidence_destinations: dict[str, str] = {}

    def validate_evidence_file(
        relative: object,
        expected_sha256: object,
        *,
        label: str,
    ) -> Path:
        if not isinstance(relative, str) or not relative:
            raise ValueError(f"{label}缺少项目相对路径")
        source = boundary / Path(relative)
        _require_within(boundary, source)
        _require_file(source, label)
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9A-F]{64}", expected_sha256):
            raise ValueError(f"{label}缺少规范 SHA-256")
        if sha256_file(source) != expected_sha256:
            raise ValueError(f"{label}哈希不一致")
        destination_name = source.name.casefold()
        previous = evidence_destinations.setdefault(destination_name, expected_sha256)
        if previous != expected_sha256:
            raise ValueError(f"{label}与另一证据文件存在发布文件名冲突")
        return source

    for entry in verified:
        if not isinstance(entry, dict) or set(entry) != {
            "name",
            "version",
            "spdx",
            "copyright",
            "source_url",
            "source_commit",
            "source_sha256",
            "license_file",
            "license_sha256",
            "notice_files",
        }:
            raise ValueError("HyperFrames 依赖许可证覆盖条目结构无效")
        name, version, spdx = entry.get("name"), entry.get("version"), entry.get("spdx")
        copyright_notice = entry.get("copyright")
        source_url = entry.get("source_url")
        source_commit = entry.get("source_commit")
        source_sha256 = entry.get("source_sha256")
        if not all(
            isinstance(value, str) and value
            for value in (name, version, spdx, copyright_notice, source_url, source_commit, source_sha256)
        ):
            raise ValueError("HyperFrames 依赖许可证覆盖条目缺少精确身份")
        if (
            not str(source_url).startswith("https://raw.githubusercontent.com/")
            or not re.fullmatch(r"[0-9a-f]{40}", str(source_commit))
            or str(source_commit) not in str(source_url)
            or not re.fullmatch(r"[0-9A-F]{64}", str(source_sha256))
            or len(str(copyright_notice)) > 500
        ):
            raise ValueError(f"HyperFrames 依赖许可证官方来源锁无效：{name}@{version}")
        source = validate_evidence_file(
            entry.get("license_file"),
            entry.get("license_sha256"),
            label=f"HyperFrames 依赖许可证正文 {name}@{version}",
        )
        notice_entries = entry.get("notice_files")
        if not isinstance(notice_entries, list) or len(notice_entries) > 4:
            raise ValueError(f"HyperFrames 依赖 NOTICE 清单无效：{name}@{version}")
        parsed_notices: list[dict[str, object]] = []
        for notice in notice_entries:
            if not isinstance(notice, dict) or set(notice) != {
                "notice_file", "notice_sha256", "source_url", "source_sha256"
            }:
                raise ValueError(f"HyperFrames 依赖 NOTICE 条目结构无效：{name}@{version}")
            notice_url = notice.get("source_url")
            notice_source_sha = notice.get("source_sha256")
            if (
                not isinstance(notice_url, str)
                or not notice_url.startswith("https://raw.githubusercontent.com/")
                or str(source_commit) not in notice_url
                or not isinstance(notice_source_sha, str)
                or not re.fullmatch(r"[0-9A-F]{64}", notice_source_sha)
            ):
                raise ValueError(f"HyperFrames 依赖 NOTICE 官方来源锁无效：{name}@{version}")
            notice_source = validate_evidence_file(
                notice.get("notice_file"),
                notice.get("notice_sha256"),
                label=f"HyperFrames 依赖 NOTICE {name}@{version}",
            )
            parsed_notices.append({**notice, "source_path": notice_source})
        key = (str(name), str(version))
        if key in parsed:
            raise ValueError("HyperFrames 依赖许可证覆盖表存在重复包身份")
        parsed[key] = {**entry, "source_path": source, "notice_sources": parsed_notices}
    return {"license_path": license_path, "lock": lock, "overrides": parsed}


def _attach_license_evidence(
    inventory: list[dict[str, object]], contract: dict[str, object]
) -> list[dict[str, object]]:
    overrides = contract["overrides"]
    assert isinstance(overrides, dict)
    result: list[dict[str, object]] = []
    used_overrides: set[tuple[str, str]] = set()
    lock = contract["lock"]
    assert isinstance(lock, dict)
    for original in inventory:
        item = dict(original)
        key = (str(item["name"]), str(item["version"]))
        if item["name"] == "hyperframes":
            if item["license_files"]:
                raise ValueError("固定 hyperframes@0.7.86 npm 包不应伪造包内许可证文件")
            item["license_evidence"] = {
                "kind": "verified_upstream_copy",
                "spdx": "Apache-2.0",
                "path": "licenses/HyperFrames-Apache-2.0.txt",
                "sha256": lock["license_sha256"],
                "source_url": lock["license_source"],
                "source_tag": lock["upstream_tag"],
                "source_commit": lock["upstream_commit"],
            }
        elif item["license_files"]:
            item["license_evidence"] = {
                "kind": "package_files",
                "spdx": item["license"],
                "files": item["license_files"],
            }
        else:
            override = overrides.get(key)
            if not isinstance(override, dict) or override.get("spdx") != item["license"]:
                raise ValueError(f"HyperFrames 依赖缺少精确许可证覆盖证据：{key[0]}@{key[1]}")
            used_overrides.add(key)
            item["license_evidence"] = {
                "kind": "project_verified_override",
                "spdx": item["license"],
                "path": f"licenses/hyperframes-dependencies/{Path(str(override['license_file'])).name}",
                "sha256": override["license_sha256"],
                "source_url": override["source_url"],
                "source_commit": override["source_commit"],
                "source_sha256": override["source_sha256"],
                "copyright": override["copyright"],
                "notices": [
                    {
                        "path": (
                            "licenses/hyperframes-dependencies/"
                            + Path(str(notice["notice_file"])).name
                        ),
                        "sha256": notice["notice_sha256"],
                        "source_url": notice["source_url"],
                        "source_sha256": notice["source_sha256"],
                    }
                    for notice in override["notice_files"]
                    if isinstance(notice, dict)
                ],
            }
        result.append(item)
    unused = sorted(set(overrides) - used_overrides)
    if unused:
        raise ValueError("HyperFrames 依赖许可证覆盖表含未使用身份：" + ", ".join(f"{n}@{v}" for n, v in unused))
    return result


def _resolve_node_dependency(runtime_root: Path, package_root: Path, name: str) -> Path | None:
    dependency = Path(*name.split("/"))
    current = package_root
    while current == runtime_root or current.is_relative_to(runtime_root):
        candidate = current / "node_modules" / dependency
        if (candidate / "package.json").is_file():
            return candidate
        if current == runtime_root:
            break
        current = current.parent
    return None


def _collect_hyperframes_dependency_roots(runtime_root: Path) -> list[Path]:
    hyperframes_root = runtime_root / "node_modules" / "hyperframes"
    _require_file(hyperframes_root / "package.json", "HyperFrames package.json")
    queue = [hyperframes_root]
    resolved: dict[Path, Path] = {}
    while queue:
        package_root = queue.pop(0)
        canonical = package_root.resolve()
        if canonical in resolved:
            continue
        _require_within(runtime_root, package_root)
        if _is_reparse_point(package_root):
            raise ValueError("HyperFrames 依赖闭包不得包含符号链接或 Junction")
        package = _read_node_package(package_root / "package.json")
        resolved[canonical] = package_root
        dependency_names: list[tuple[str, bool]] = []
        dependencies = package.get("dependencies", {})
        if not isinstance(dependencies, dict):
            raise ValueError("HyperFrames 依赖声明结构无效")
        dependency_names.extend((str(name), True) for name in dependencies)
        peers = package.get("peerDependencies", {})
        peer_meta = package.get("peerDependenciesMeta", {})
        if not isinstance(peers, dict) or not isinstance(peer_meta, dict):
            raise ValueError("HyperFrames peer 依赖声明结构无效")
        for name in peers:
            metadata = peer_meta.get(name, {})
            required = not (isinstance(metadata, dict) and metadata.get("optional") is True)
            dependency_names.append((str(name), required))
        optional = package.get("optionalDependencies", {})
        if not isinstance(optional, dict):
            raise ValueError("HyperFrames optional 依赖声明结构无效")
        dependency_names.extend((str(name), "win32-x64" in str(name).casefold()) for name in optional)
        for dependency_name, required in dependency_names:
            dependency_root = _resolve_node_dependency(runtime_root, package_root, dependency_name)
            if dependency_root is None:
                if required:
                    raise ValueError(
                        f"HyperFrames 依赖闭包缺少传递依赖：{package.get('name')} -> {dependency_name}"
                    )
                continue
            queue.append(dependency_root)
    return sorted(resolved.values(), key=lambda path: path.relative_to(runtime_root).as_posix().casefold())


def _validate_hyperframes_dependency_closure(runtime_root: Path, expected_version: str) -> list[dict[str, object]]:
    """Validate a copied npm dependency closure without consulting ancestor node_modules."""

    if expected_version != HYPERFRAMES_VERSION:
        raise ValueError(f"HyperFrames 正式运行时仅允许固定版本 {HYPERFRAMES_VERSION}")
    node_modules = runtime_root / "node_modules"
    _require_directory(node_modules, "HyperFrames node_modules 依赖闭包")
    package_roots = _collect_hyperframes_dependency_roots(runtime_root)
    hyperframes_root = runtime_root / "node_modules" / "hyperframes"
    if hyperframes_root.resolve() not in {path.resolve() for path in package_roots}:
        raise ValueError("HyperFrames runtime 必须包含 node_modules/hyperframes")
    inventory: list[dict[str, object]] = []
    for package_root in package_roots:
        package_json = package_root / "package.json"
        package = _read_node_package(package_json)
        name = package.get("name")
        version = package.get("version")
        license_value = package.get("license")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise ValueError("HyperFrames 依赖闭包存在缺少名称或版本的包")
        if not isinstance(license_value, str) or not license_value.strip():
            raise ValueError(f"HyperFrames 依赖 {name} 缺少可审计许可证声明")
        if name == "hyperframes" and version != expected_version:
            raise ValueError("HyperFrames runtime 与显式固定版本不一致")
        for dependency_field in ("dependencies", "peerDependencies"):
            dependencies = package.get(dependency_field, {})
            if dependencies is None:
                dependencies = {}
            if not isinstance(dependencies, dict):
                raise ValueError(f"HyperFrames 依赖 {name} 的 {dependency_field} 结构无效")
            optional_peers = package.get("peerDependenciesMeta", {}) if dependency_field == "peerDependencies" else {}
            for dependency_name, requirement in dependencies.items():
                if not isinstance(dependency_name, str) or not isinstance(requirement, str):
                    raise ValueError(f"HyperFrames 依赖 {name} 含非法依赖声明")
                if requirement.startswith(("file:", "link:", "workspace:")):
                    raise ValueError(f"HyperFrames 依赖闭包不得保留本机或 workspace 依赖：{name}")
                if (
                    dependency_field == "peerDependencies"
                    and isinstance(optional_peers, dict)
                    and isinstance(optional_peers.get(dependency_name), dict)
                    and optional_peers[dependency_name].get("optional") is True
                ):
                    continue
                if _resolve_node_dependency(runtime_root, package_root, dependency_name) is None:
                    raise ValueError(f"HyperFrames 依赖闭包缺少传递依赖：{name} -> {dependency_name}")
        optional_dependencies = package.get("optionalDependencies", {})
        if isinstance(optional_dependencies, dict):
            for dependency_name in optional_dependencies:
                if "win32-x64" in str(dependency_name).casefold() and _resolve_node_dependency(
                    runtime_root, package_root, str(dependency_name)
                ) is None:
                    raise ValueError(f"HyperFrames Windows x64 依赖闭包缺少平台包：{dependency_name}")
        license_files = []
        for candidate in sorted(package_root.iterdir(), key=lambda path: path.name.casefold()):
            if candidate.is_file() and re.match(r"(?i)^(?:licen[cs]e|notice|copying)(?:[._-].*)?$", candidate.name):
                license_files.append(
                    {
                        "path": candidate.relative_to(runtime_root).as_posix(),
                        "sha256": sha256_file(candidate),
                    }
                )
        inventory.append(
            {
                "name": name,
                "version": version,
                "license": license_value.strip(),
                "package_json": package_json.relative_to(runtime_root).as_posix(),
                "package_json_sha256": sha256_file(package_json),
                "license_files": license_files,
            }
        )
    if not any(item["name"] == "esbuild" for item in inventory):
        raise ValueError("HyperFrames 依赖闭包缺少 esbuild，裸 CLI 不可作为离线 runtime")
    if not any(item["name"] == "@esbuild/win32-x64" for item in inventory):
        raise ValueError("HyperFrames 依赖闭包缺少 @esbuild/win32-x64")
    return sorted(inventory, key=lambda item: (str(item["name"]).casefold(), str(item["version"])))


def _copy_hyperframes_dependency_closure(source_root: Path, destination_root: Path) -> None:
    for package_root in _collect_hyperframes_dependency_roots(source_root):
        relative = package_root.relative_to(source_root)
        _copy_tree(package_root, destination_root / relative)


def _apply_hyperframes_windows_mf_patch(repo: Path, runtime_root: Path) -> None:
    """Patch only an exact upstream npm CLI in staging, then verify the result."""

    _read_hyperframes_patch_contract(repo)
    package_root = runtime_root / "node_modules" / "hyperframes"
    cli_path = package_root / "dist" / "cli.js"
    _require_file(cli_path, "HyperFrames upstream dist/cli.js")
    if sha256_file(cli_path) != HYPERFRAMES_UPSTREAM_CLI_SHA256:
        raise ValueError(
            "HyperFrames release input must be the exact unmodified npm CLI; a prepatched node_modules tree is forbidden"
        )
    patcher = repo / HYPERFRAMES_PATCHER_RELATIVE
    for extra_args, label in (((), "apply"), (("--check",), "check")):
        completed = subprocess.run(
            [sys.executable, str(patcher), "--package-root", str(package_root), *extra_args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise ValueError(f"HyperFrames Windows-MF staging patch {label} failed: {detail}")
    if sha256_file(cli_path) != HYPERFRAMES_PATCHED_CLI_SHA256:
        raise ValueError("HyperFrames staging CLI does not match the frozen patched SHA-256")


def _write_hyperframes_runtime_manifest(
    runtime_root: Path, expected_version: str, license_contract: dict[str, object]
) -> dict[str, object]:
    inventory = _attach_license_evidence(
        _validate_hyperframes_dependency_closure(runtime_root, expected_version), license_contract
    )
    files = []
    for path in sorted(
        (item for item in runtime_root.rglob("*") if item.is_file() and item.name != HYPERFRAMES_RUNTIME_MANIFEST),
        key=lambda item: item.relative_to(runtime_root).as_posix().casefold(),
    ):
        relative = path.relative_to(runtime_root).as_posix()
        files.append({"path": relative, "size": path.stat().st_size, "sha256": sha256_file(path)})
    digest = hashlib.sha256()
    for entry in files:
        digest.update(f"{entry['path']}\0{entry['size']}\0{entry['sha256']}\n".encode("utf-8"))
    manifest: dict[str, object] = {
        "schema_version": 2,
        "runtime_kind": "hyperframes_node_modules_closure",
        "platform": "win32-x64",
        "entry": HYPERFRAMES_CLI_RELATIVE.as_posix(),
        "hyperframes_version": expected_version,
        "codec_strategy": H264_CODEC_STRATEGY,
        "patch_id": HYPERFRAMES_PATCH_ID,
        "patch_version": HYPERFRAMES_PATCH_VERSION,
        "upstream_cli_sha256": HYPERFRAMES_UPSTREAM_CLI_SHA256,
        "patched_cli_sha256": HYPERFRAMES_PATCHED_CLI_SHA256,
        "external_node_modules_allowed": False,
        "runtime_downloads_allowed": False,
        "payload_sha256": digest.hexdigest().upper(),
        "packages": inventory,
        "files": files,
    }
    (runtime_root / HYPERFRAMES_RUNTIME_MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


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


def _validated_mpt_excluded_components(lock: dict[str, object]) -> frozenset[str]:
    raw = lock.get("excluded_components")
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        raise ValueError("MoneyPrinterTurbo 上游锁缺少无重复的 excluded_components")
    if len(raw) != len(set(raw)):
        raise ValueError("MoneyPrinterTurbo 上游锁缺少无重复的 excluded_components")
    normalized: list[str] = []
    for value in raw:
        if not value or "\\" in value:
            raise ValueError("MoneyPrinterTurbo excluded_components 路径无效")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("MoneyPrinterTurbo excluded_components 路径无效")
        normalized.append(path.as_posix())
    result = frozenset(normalized)
    if result != EXPECTED_MPT_EXCLUDED_COMPONENTS:
        raise ValueError("MoneyPrinterTurbo 上游锁的 excluded_components 与正式精简边界不一致")
    return result


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


def _sanitized_node_environment(package: Path, motion: MotionRuntimeInputs, temp_root: Path) -> dict[str, str]:
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
    path_entries = [str(package / "runtime" / "node"), str(package / "runtime" / "ffmpeg")]
    environment: dict[str, str] = {
        "PATH": os.pathsep.join(path_entries),
        "TEMP": str(temp_root),
        "TMP": str(temp_root),
        "HYPERFRAMES_FFMPEG_PATH": str(package / "runtime" / "ffmpeg" / "ffmpeg.exe"),
        "HYPERFRAMES_FFPROBE_PATH": str(package / "runtime" / "ffmpeg" / "ffprobe.exe"),
        "HYPERFRAMES_NO_UPDATE_CHECK": "1",
        "HYPERFRAMES_NO_AUTO_INSTALL": "1",
        "HYPERFRAMES_NO_TELEMETRY": "1",
        "HYPERFRAMES_SKIP_SKILLS": "1",
        "DO_NOT_TRACK": "1",
        "NO_UPDATE_NOTIFIER": "1",
        "NPM_CONFIG_OFFLINE": "true",
        "NPM_CONFIG_PREFER_OFFLINE": "true",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "NPM_CONFIG_AUDIT": "false",
        "NPM_CONFIG_FUND": "false",
        "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
        "PUPPETEER_SKIP_DOWNLOAD": "true",
    }
    if system_root:
        environment["SYSTEMROOT"] = system_root
        environment["WINDIR"] = system_root
        environment["PATH"] += os.pathsep + str(Path(system_root) / "System32")
    return environment


def _verify_motion_executables(package: Path, motion: MotionRuntimeInputs) -> None:
    node = package / "runtime" / "node" / "node.exe"
    cli = package / "runtime" / "hyperframes" / HYPERFRAMES_CLI_RELATIVE
    probes = (
        ([str(node), "--version"], motion.node_version, "Node"),
        ([str(node), str(cli), "--version"], motion.hyperframes_version, "HyperFrames"),
    )
    with tempfile.TemporaryDirectory(prefix=".motion-probe-", dir=package.parent) as temp_dir:
        environment = _sanitized_node_environment(package, motion, Path(temp_dir))
        for command, expected, label in probes:
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(package / "runtime" / "hyperframes"),
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ValueError(f"固定 {label} 无法从 staging 便携树执行离线版本探针") from exc
            version_text = completed.stdout + "\n" + completed.stderr
            if completed.returncode != 0 or not re.search(
                rf"(?<![0-9.]){re.escape(expected)}(?![0-9.])", version_text
            ):
                raise ValueError(f"固定 {label} staging 版本探针与声明版本不一致")


def _verify_mpt_offline_subset_executable(package: Path) -> None:
    python = package / "runtime" / "python" / "python.exe"
    mpt_root = package / "engine" / "MoneyPrinterTurbo"
    ffmpeg = package / "runtime" / "ffmpeg" / "ffmpeg.exe"
    import_contract = _read_python_pruned_import_contract(package)
    _validate_formal_python_import_boundary(package, import_contract)
    module_contract = import_contract["modules"]
    assert isinstance(module_contract, dict)
    pruned_modules = tuple(
        sorted({module for modules in module_contract.values() for module in modules})
    )
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
    path_entries = [str(ffmpeg.parent)]
    probe = (
        "import hashlib\n"
        "import importlib.util\n"
        "import json\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "from app.asgi import app\n"
        "from app.config import config\n"
        "from app.services import subtitle, task, video\n"
        "paths = {route.path for route in app.routes}\n"
        "assert '/api/v1/videos' in paths\n"
        "assert not {'/api/v1/scripts', '/api/v1/terms', '/api/v1/social-metadata'} & paths\n"
        "assert config.app.get('subtitle_provider') == 'edge'\n"
        "assert config.app.get('video_codec') == 'h264_mf'\n"
        "assert subtitle.WhisperModel is None\n"
        "assert task.upload_post.upload_post_service.is_configured() is False\n"
        "assert task._VIDEO_MUSIC_PROVIDERS == {}\n"
        "assert video._DEFAULT_VIDEO_CODEC == 'h264_mf'\n"
        "assert video._SUPPORTED_VIDEO_CODECS == ('h264_mf',)\n"
        "assert video._get_configured_video_codec() == 'h264_mf'\n"
        "assert video._get_effective_video_codec() == 'h264_mf'\n"
        "params = SimpleNamespace(video_script='approved', video_terms=['ordered'], match_materials_to_script=True)\n"
        "assert task.generate_script('subset-probe', params) == 'approved'\n"
        "assert task.generate_terms('subset-probe', params, 'approved') == ['ordered']\n"
        f"for removed in {pruned_modules!r}:\n"
        "    try:\n"
        "        removed_spec = importlib.util.find_spec(removed)\n"
        "    except ModuleNotFoundError:\n"
        "        removed_spec = None\n"
        "    assert removed_spec is None, removed\n"
        "import primp\n"
        "from ddgs import DDGS\n"
        "import multipart, openai, toml, uvicorn\n"
        "from uvicorn.protocols.http.auto import AutoHTTPProtocol\n"
        "assert AutoHTTPProtocol.__module__ == 'uvicorn.protocols.http.h11_impl'\n"
        f"package_root = Path({str(package)!r})\n"
        "sys.path.insert(0, str(package_root))\n"
        "spec = importlib.util.spec_from_file_location('shiyi_workbench_app', package_root / 'app.py')\n"
        "assert spec is not None and spec.loader is not None\n"
        "workbench = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(workbench)\n"
        "from scripts import launch_combined as combined_launcher\n"
        "assert callable(combined_launcher.main)\n"
        "import numpy as np\n"
        "from moviepy import AudioClip, ColorClip\n"
        "from moviepy.video.io import ffmpeg_writer\n"
        f"assert hashlib.sha256(Path(ffmpeg_writer.__file__).read_bytes()).hexdigest().upper() == {MOVIEPY_PATCHED_WRITER_SHA256!r}\n"
        "canary_root = Path(os.environ['TEMP']) / 'shiyi-codec-canary'\n"
        "canary_root.mkdir(parents=True, exist_ok=True)\n"
        "clip_path = canary_root / 'moviepy-h264-mf.mp4'\n"
        "concat_path = canary_root / 'mpt-concat-h264-mf.mp4'\n"
        "audio = AudioClip(lambda t: 0.02 * np.sin(2 * np.pi * 440 * t), duration=0.5, fps=48000)\n"
        "clip = ColorClip(size=(128, 128), color=(24, 132, 196), duration=0.5).with_audio(audio)\n"
        "try:\n"
        "    clip.write_videofile(str(clip_path), fps=10, codec='h264_mf', audio_codec='aac', audio_bitrate='64k', logger=None)\n"
        "finally:\n"
        "    clip.close()\n"
        "    audio.close()\n"
        "assert clip_path.is_file() and clip_path.stat().st_size > 0\n"
        "assert video.concat_video_clips_with_ffmpeg([str(clip_path), str(clip_path)], str(concat_path), 1, str(canary_root)) == 'h264_mf'\n"
        "assert concat_path.is_file() and concat_path.stat().st_size > 0\n"
        "ffprobe = package_root / 'runtime' / 'ffmpeg' / 'ffprobe.exe'\n"
        "probe_result = subprocess.run([str(ffprobe), '-v', 'error', '-show_entries', 'stream=codec_type,codec_name,pix_fmt', '-of', 'json', str(concat_path)], check=True, capture_output=True, text=True, encoding='utf-8')\n"
        "streams = json.loads(probe_result.stdout)['streams']\n"
        "video_stream = next(stream for stream in streams if stream['codec_type'] == 'video')\n"
        "audio_stream = next(stream for stream in streams if stream['codec_type'] == 'audio')\n"
        "assert video_stream['codec_name'] == 'h264' and video_stream['pix_fmt'] == 'yuv420p'\n"
        "assert audio_stream['codec_name'] == 'aac'\n"
    )
    try:
        with tempfile.TemporaryDirectory(prefix=".python-mpt-probe-", dir=package.parent) as temp_dir:
            probe_root = Path(temp_dir)
            user_profile = probe_root / "UserProfile"
            app_data = user_profile / "AppData" / "Roaming"
            local_app_data = user_profile / "AppData" / "Local"
            temp_path = probe_root / "Temp"
            runtime_state = probe_root / "RuntimeState"
            for directory in (user_profile, app_data, local_app_data, temp_path, runtime_state):
                directory.mkdir(parents=True, exist_ok=True)
            environment: dict[str, str] = {
                "APPDATA": str(app_data),
                "LOCALAPPDATA": str(local_app_data),
                "PATH": os.pathsep.join(path_entries),
                "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                "SHIYI_RUNTIME_DIR": str(runtime_state),
                "TEMP": str(temp_path),
                "TMP": str(temp_path),
                "USERPROFILE": str(user_profile),
                "FFMPEG_PATH": str(ffmpeg),
                "IMAGEIO_FFMPEG_EXE": str(ffmpeg),
            }
            if system_root:
                environment["SYSTEMROOT"] = system_root
                environment["WINDIR"] = system_root
                environment["PATH"] += os.pathsep + str(Path(system_root) / "System32")
            completed = subprocess.run(
                [str(python), "-E", "-s", "-B", "-X", "utf8", "-c", probe],
                cwd=str(mpt_root),
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("MoneyPrinterTurbo 离线精简子集无法从 staging 便携树执行启动探针") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()[-1:]
        suffix = f"：{detail[0]}" if detail else ""
        raise ValueError("MoneyPrinterTurbo 离线精简子集启动探针失败" + suffix)


def _validate_inputs(inputs: BuildInputs) -> tuple[str, Path, frozenset[str]]:
    repo = inputs.repo.resolve()
    mpt = inputs.mpt_source.resolve()
    runtime = inputs.python_runtime.resolve()
    if inputs.package_profile not in {LEGACY_PACKAGE_PROFILE, MOTION_PACKAGE_PROFILE}:
        raise ValueError("未知便携包 profile")
    motion = inputs.motion_runtime
    if inputs.package_profile == MOTION_PACKAGE_PROFILE and motion is None:
        raise ValueError("motion_primary v0.3 便携包必须显式提供离线动画运行时")
    if inputs.package_profile == LEGACY_PACKAGE_PROFILE and motion is not None:
        raise ValueError("legacy_combined 不得混入未声明的动画运行时")
    _ffmpeg_lock, ffmpeg_runtime = _read_ffmpeg_distribution_contract(
        repo, require_formal=inputs.package_profile == MOTION_PACKAGE_PROFILE
    )
    output = inputs.output.resolve()
    zip_path = inputs.zip_path.resolve()

    _require_directory(repo, "项目根目录")
    _require_directory(mpt, "MoneyPrinterTurbo 源码")
    _require_directory(runtime, "便携 Python")
    _require_directory(ffmpeg_runtime, "完整 FFmpeg runtime")
    _require_file(runtime / "python.exe", "便携 Python 入口")
    python_license = _python_license(runtime)
    _read_python_license_contract(repo)
    pruned_runtime_paths = _python_pruned_runtime_paths(runtime)
    _validate_retained_python_import_boundary(
        runtime,
        _read_python_pruned_import_contract(repo),
        excluded_paths=pruned_runtime_paths,
    )
    _require_file(ffmpeg_runtime / "ffmpeg.exe", "FFmpeg")
    _require_file(ffmpeg_runtime / "ffprobe.exe", "FFprobe")
    if inputs.verify_runtime_executables:
        _verify_python_executable(runtime / "python.exe")
        _verify_ffmpeg_executable(ffmpeg_runtime / "ffmpeg.exe", "ffmpeg")
        _verify_ffmpeg_executable(ffmpeg_runtime / "ffprobe.exe", "ffprobe")
    if motion is not None:
        for root, label in (
            (motion.node_runtime, "固定 Node runtime"),
            (motion.hyperframes_runtime, "固定 HyperFrames runtime"),
        ):
            _require_directory(root.resolve(), label)
        _require_file(motion.node_runtime.resolve() / "node.exe", "固定 Node 入口")
        _require_file(motion.node_runtime.resolve() / "LICENSE", "Node 许可证")
        _require_file(motion.hyperframes_runtime.resolve() / HYPERFRAMES_PACKAGE_RELATIVE, "HyperFrames package.json")
        _require_file(motion.hyperframes_runtime.resolve() / HYPERFRAMES_CLI_RELATIVE, "HyperFrames CLI")
        if not all(
            re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", value)
            for value in (motion.node_version, motion.hyperframes_version)
        ):
            raise ValueError("动画运行时版本必须是显式的数字版本")
        if motion.hyperframes_version != HYPERFRAMES_VERSION:
            raise ValueError(f"HyperFrames 正式运行时仅允许固定版本 {HYPERFRAMES_VERSION}")
        if int(motion.node_version.split(".", 1)[0]) < NODE_MINIMUM_MAJOR:
            raise ValueError(f"HyperFrames 离线运行时要求 Node {NODE_MINIMUM_MAJOR} 或更高版本")
        try:
            hyperframes_package = json.loads(
                (motion.hyperframes_runtime.resolve() / HYPERFRAMES_PACKAGE_RELATIVE).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("HyperFrames package.json 无法解析") from exc
        if not isinstance(hyperframes_package, dict) or hyperframes_package.get("version") != motion.hyperframes_version:
            raise ValueError("HyperFrames runtime 与显式固定版本不一致")
        upstream_cli = motion.hyperframes_runtime.resolve() / "node_modules" / "hyperframes" / "dist" / "cli.js"
        _require_file(upstream_cli, "HyperFrames upstream dist/cli.js")
        if sha256_file(upstream_cli) != HYPERFRAMES_UPSTREAM_CLI_SHA256:
            raise ValueError(
                "HyperFrames 正式构建输入必须是精确、未修改的 npm CLI；拒绝预补丁或漂移的 node_modules"
            )
        _validate_hyperframes_dependency_closure(motion.hyperframes_runtime.resolve(), motion.hyperframes_version)
        _read_hyperframes_license_contract(repo)
        _read_hyperframes_patch_contract(repo)
        _read_moviepy_patch_contract(repo)
        moviepy_site_packages = runtime / "Lib" / "site-packages"
        moviepy_writer = moviepy_site_packages / "moviepy" / "video" / "io" / "ffmpeg_writer.py"
        moviepy_record = moviepy_site_packages / "moviepy-2.2.1.dist-info" / "RECORD"
        _require_file(moviepy_writer, "frozen upstream MoviePy writer")
        _require_file(moviepy_record, "frozen upstream MoviePy RECORD")
        if (
            sha256_file(moviepy_writer) != MOVIEPY_UPSTREAM_WRITER_SHA256
            or sha256_file(moviepy_record) != MOVIEPY_UPSTREAM_RECORD_SHA256
        ):
            raise ValueError(
                "MoviePy formal build input must contain the exact upstream 2.2.1 writer/RECORD pair"
            )

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
    if lock.get("portable_subset") != {
        "id": MPT_OFFLINE_SUBSET_MARKER,
        "mode": "video_only_adapted_runtime_dependency_closure",
        "deterministic_modifications": EXPECTED_MPT_DETERMINISTIC_MODIFICATIONS,
        "required_probe": EXPECTED_MPT_REQUIRED_PROBE,
    }:
        raise ValueError("MoneyPrinterTurbo 上游锁未精确声明三处确定性适配与真实编码探针")
    excluded_components = _validated_mpt_excluded_components(lock)
    license_sha = str(lock.get("license_sha256", "")).upper()
    if not re.fullmatch(r"[0-9A-F]{64}", license_sha) or license_sha != sha256_file(mpt / "LICENSE"):
        raise ValueError("MoneyPrinterTurbo 许可证与上游锁不一致")
    if sha256_file(repo / "third_party" / "moneyprinterturbo" / "LICENSE") != license_sha:
        raise ValueError("项目内 MoneyPrinterTurbo 许可证副本与上游锁不一致")
    return repo_commit, python_license, excluded_components


def _copy_repository_payload(repo: Path, package: Path) -> None:
    for relative in REPO_FILE_ALLOWLIST:
        _copy_file(repo / Path(relative), package / Path(relative))
    for relative, suffixes in REPO_TREE_ALLOWLIST.items():
        _copy_tree(repo / relative, package / relative, suffixes)


def _mpt_component_is_excluded(relative: str, excluded_components: frozenset[str]) -> bool:
    normalized = PurePosixPath(relative).as_posix().casefold()
    return any(
        normalized == component.casefold() or normalized.startswith(component.casefold() + "/")
        for component in excluded_components
    )


def _replace_exact_once(text: str, old: str, new: str, *, label: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"MoneyPrinterTurbo 固定源码无法安全净化：{label}")
    return text.replace(old, new, 1)


def _replace_region_once(
    text: str, start_marker: str, end_marker: str, replacement: str, *, label: str
) -> str:
    if text.count(start_marker) != 1 or text.count(end_marker) != 1:
        raise ValueError(f"MoneyPrinterTurbo pinned source cannot be safely adapted: {label}")
    start = text.index(start_marker)
    end = text.index(end_marker, start + len(start_marker))
    return text[:start] + replacement + text[end:]


def _adapt_mpt_h264_mf_video_service(target: Path) -> None:
    video_path = target / "app" / "services" / "video.py"
    _require_file(video_path, "MoneyPrinterTurbo video service")
    video = video_path.read_text(encoding="utf-8")
    video = _replace_exact_once(
        video,
        '''_DEFAULT_VIDEO_CODEC = "libx264"
_SUPPORTED_VIDEO_CODECS = (
    "libx264",
    "h264_nvenc",
    "h264_amf",
    "h264_qsv",
    "h264_mf",
    "h264_videotoolbox",
)
_runtime_disabled_video_codecs = set()
''',
        f'''# {MPT_H264_MF_CODEC_MARKER}: this portable subset has one fail-closed
# encoder contract.  The bundled LGPL FFmpeg exposes only the reviewed H.264 path.
_DEFAULT_VIDEO_CODEC = "{H264_CODEC_STRATEGY}"
_SUPPORTED_VIDEO_CODECS = ("{H264_CODEC_STRATEGY}",)
_SHIYI_H264_MF_FFMPEG_PARAMS = (
    "-rate_control", "quality",
    "-quality", "{H264_MF_QUALITY}",
    "-scenario", "archive",
    "-hw_encoding", "0",
    "-bf", "0",
    "-pix_fmt", "yuv420p",
)
''',
        label="video codec allowlist",
    )
    video = _replace_region_once(
        video,
        "def _get_configured_video_codec() -> str:\n",
        "@lru_cache(maxsize=16)\n",
        f'''def _get_configured_video_codec() -> str:
    configured_codec = str(
        config.app.get("video_codec", _DEFAULT_VIDEO_CODEC) or _DEFAULT_VIDEO_CODEC
    ).strip()
    if configured_codec != "{H264_CODEC_STRATEGY}":
        raise RuntimeError(
            "bundled MPT permits only the reviewed h264_mf encoder; "
            f"configured={{configured_codec!r}}"
        )
    return "{H264_CODEC_STRATEGY}"


''',
        label="configured video codec",
    )
    video = _replace_region_once(
        video,
        "def _get_effective_video_codec(preferred_codec: str | None = None) -> str:\n",
        "def _get_temp_audio_dir(output_dir: str) -> str:\n",
        f'''def _get_effective_video_codec(preferred_codec: str | None = None) -> str:
    selected_codec = preferred_codec or _get_configured_video_codec()
    if selected_codec != "{H264_CODEC_STRATEGY}":
        raise RuntimeError(
            "bundled MPT refuses an encoder outside the reviewed h264_mf contract"
        )
    ffmpeg_binary = utils.get_ffmpeg_binary()
    if not _ffmpeg_encoder_exists(ffmpeg_binary, "{H264_CODEC_STRATEGY}"):
        raise RuntimeError("bundled FFmpeg does not expose the required h264_mf encoder")
    return "{H264_CODEC_STRATEGY}"


''',
        label="effective codec and fallback state",
    )
    video = _replace_exact_once(
        video,
        "    一定可用。因此实际编码失败时仍会再回退到 libx264。\n",
        "    一定可用；正式便携子集在真实写出失败时直接报错。\n",
        label="encoder probe fallback documentation",
    )
    video = _replace_region_once(
        video,
        "def _fallback_write_videofile(clip, output_file: str, failed_codec: str, reason: str, **kwargs):\n",
        "def _escape_ffmpeg_concat_path(file_path: str) -> str:\n",
        '''def _write_videofile_with_codec_fallback(clip, output_file: str, codec: str, **kwargs):
    # The historical function name is retained for upstream call-site stability,
    # but this distribution deliberately has no codec fallback.
    effective_codec = _get_effective_video_codec(codec)
    caller_params = kwargs.pop("ffmpeg_params", None)
    if caller_params not in (None, [], ()):
        raise RuntimeError("bundled MPT does not accept caller-supplied FFmpeg video parameters")
    clip.write_videofile(output_file, codec=effective_codec, **kwargs)
    return effective_codec


''',
        label="MoviePy codec fallback",
    )
    video = _replace_exact_once(
        video,
        '''            "-c:v",
            codec,
            "-threads",
            str(threads or 2),
            "-pix_fmt",
            "yuv420p",
''',
        '''            "-c:v",
            codec,
            *_SHIYI_H264_MF_FFMPEG_PARAMS,
            "-threads",
            str(threads or 2),
''',
        label="concat h264_mf arguments",
    )
    video = _replace_exact_once(
        video,
        '''    try:
        effective_codec = _get_effective_video_codec()
        try:
            return run_concat(effective_codec)
        except Exception as exc:
            if effective_codec == _DEFAULT_VIDEO_CODEC:
                raise
            result_codec = run_concat(_DEFAULT_VIDEO_CODEC)
            _disable_runtime_video_codec(effective_codec, str(exc))
            return result_codec
    finally:
''',
        '''    try:
        return run_concat(_get_effective_video_codec())
    finally:
''',
        label="concat codec fallback",
    )
    video = _replace_exact_once(
        video,
        "                final_clip.write_videofile(video_file, fps=30, logger=None)\n",
        '''                _write_videofile_with_codec_fallback(
                    final_clip,
                    video_file,
                    codec=_get_configured_video_codec(),
                    fps=30,
                    logger=None,
                )
''',
        label="image material bare MoviePy writer",
    )
    if "libx264" in video:
        raise ValueError("adapted MoneyPrinterTurbo video service still contains libx264")
    if video.count(".write_videofile(") != 1:
        raise ValueError("adapted MoneyPrinterTurbo video service retains a bare MoviePy writer")
    video_path.write_text(video, encoding="utf-8")


def _adapt_mpt_offline_subset(target: Path) -> None:
    router_path = target / "app" / "router.py"
    task_path = target / "app" / "services" / "task.py"
    _require_file(router_path, "MoneyPrinterTurbo 路由入口")
    _require_file(task_path, "MoneyPrinterTurbo 视频任务服务")

    router = router_path.read_text(encoding="utf-8")
    router = _replace_exact_once(
        router,
        "from app.controllers.v1 import llm, video",
        "from app.controllers.v1 import video",
        label="LLM 路由导入",
    )
    router = _replace_exact_once(
        router,
        "root_api_router.include_router(llm.router)\n",
        "",
        label="LLM 路由注册",
    )
    router_path.write_text(router, encoding="utf-8")

    task = task_path.read_text(encoding="utf-8")
    task = _replace_exact_once(
        task, "    elevenlabs_music,\n", "", label="ElevenLabs music provider import"
    )
    task = _replace_exact_once(task, "    llm,\n", "", label="LLM 服务导入")
    task = _replace_exact_once(
        task, "    sonilo,\n", "", label="Sonilo music provider import"
    )
    task = _replace_exact_once(
        task,
        "from app.services import upload_post\n",
        "",
        label="跨平台发布服务导入",
    )
    anchor = "from app.utils import file_security, utils\n\n\n"
    disabled_features = f'''from app.utils import file_security, utils


# {MPT_OFFLINE_SUBSET_MARKER}: the portable product supplies a reviewed script
# and ordered terms.  Upstream LLM generation and social publishing are outside
# this local rendering boundary and remain deterministically disabled.
class _ShiyiDisabledLanguageModel:
    @staticmethod
    def _disabled(*_args, **_kwargs):
        raise RuntimeError("bundled MPT requires a pre-approved script and terms")

    generate_script = _disabled
    generate_terms = _disabled
    generate_social_metadata = _disabled


class _ShiyiDisabledUploadService:
    auto_upload = False
    platforms = ()
    youtube_privacy_status = "private"

    @staticmethod
    def is_configured():
        return False


class _ShiyiDisabledUploadPost:
    upload_post_service = _ShiyiDisabledUploadService()

    @staticmethod
    def cross_post_video(*_args, **_kwargs):
        raise RuntimeError("social publishing is excluded from the bundled MPT subset")


llm = _ShiyiDisabledLanguageModel()
upload_post = _ShiyiDisabledUploadPost()


'''
    task = _replace_exact_once(task, anchor, disabled_features, label="离线子集适配锚点")
    task = _replace_region_once(
        task,
        "_VIDEO_MUSIC_PROVIDERS = {\n",
        "def _get_video_music_prompt(params: VideoParams) -> str:\n",
        "_VIDEO_MUSIC_PROVIDERS = {}\n\n\n",
        label="external video music provider registry",
    )
    task_path.write_text(task, encoding="utf-8")
    _adapt_mpt_h264_mf_video_service(target)


def _copy_mpt_payload(
    source: Path,
    target: Path,
    regular_font: Path,
    excluded_components: frozenset[str],
) -> None:
    for path in _iter_tree_files(source / "app", MPT_APP_SUFFIX_ALLOWLIST):
        relative = path.relative_to(source).as_posix()
        if (
            _mpt_component_is_excluded(relative, excluded_components)
            or relative in MPT_DISABLED_MUSIC_PROVIDER_FILES
        ):
            continue
        _copy_file(path, target / Path(relative))
    _adapt_mpt_offline_subset(target)
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
        'bgm_volume = 0.0\n'
        'video_codec = "h264_mf"\n',
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
        "echo Verifying the offline package once, then starting the local workbench. Please wait...\r\n"
        "\"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe\" -NoLogo -NoProfile -ExecutionPolicy Bypass -File \"%~dp0scripts\\launch_combined.ps1\" "
        "-MptRoot \"%~dp0engine\\MoneyPrinterTurbo\" "
        "-MptPython \"%~dp0runtime\\python\\python.exe\" "
        "-AppPython \"%~dp0runtime\\python\\python.exe\" "
        "-Ffmpeg \"%~dp0runtime\\ffmpeg\\ffmpeg.exe\" "
        "-Ffprobe \"%~dp0runtime\\ffmpeg\\ffprobe.exe\" "
        "-MaterialRoot \"%~dp0engine\\MoneyPrinterTurbo\\storage\\local_videos\" "
        "-MechanicalReview\r\n"
        "set \"SHIYI_EXIT_CODE=%ERRORLEVEL%\"\r\n"
        "if not \"%SHIYI_EXIT_CODE%\"==\"0\" pause\r\n"
        "exit /b %SHIYI_EXIT_CODE%\r\n",
        encoding="ascii",
        newline="",
    )
    (package / STOP_LAUNCHER_NAME).write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n"
        "set \"SHIYI_LAUNCHER_PYTHON=%~dp0runtime\\python\\python.exe\"\r\n"
        "echo Checking recorded process identity, then stopping only this product process tree...\r\n"
        "\"%SHIYI_LAUNCHER_PYTHON%\" -I -S -B -X utf8 \"%~dp0scripts\\launch_combined.py\" --project-root \"%~dp0.\" --stop\r\n"
        "set \"SHIYI_EXIT_CODE=%ERRORLEVEL%\"\r\n"
        "if not \"%SHIYI_EXIT_CODE%\"==\"0\" pause\r\n"
        "exit /b %SHIYI_EXIT_CODE%\r\n",
        encoding="ascii",
        newline="",
    )
    (package / MIGRATION_LAUNCHER_NAME).write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n"
        "set \"OLD_RUNTIME=%~1\"\r\n"
        "if not defined OLD_RUNTIME set /p \"OLD_RUNTIME=Old package runtime full path: \"\r\n"
        "if not defined OLD_RUNTIME (\r\n"
        "  echo [ERROR] No old runtime path was provided.\r\n"
        "  pause\r\n"
        "  exit /b 2\r\n"
        ")\r\n"
        "set \"SHIYI_LAUNCHER_PYTHON=%~dp0runtime\\python\\python.exe\"\r\n"
        "echo Validating old user data, then copying it without deleting the source...\r\n"
        "\"%SHIYI_LAUNCHER_PYTHON%\" -I -S -B -X utf8 \"%~dp0scripts\\launch_combined.py\" --project-root \"%~dp0.\" --import-runtime \"%OLD_RUNTIME%\"\r\n"
        "set \"SHIYI_EXIT_CODE=%ERRORLEVEL%\"\r\n"
        "pause\r\n"
        "exit /b %SHIYI_EXIT_CODE%\r\n",
        encoding="ascii",
        newline="",
    )
    (package / USAGE_NAME).write_text(
        "时宜 Agent 内容工厂 v0.3 Windows 纯动画主线便携版\n\n"
        "【运行前】\n"
        "1. 需要 Windows 10/11 x64，并安装机器级 Microsoft Edge 151 或更高版本。ZIP 约 0.27GB，完整解压约 0.8GB；建议至少预留 1.5GB。\n"
        "2. 将整个目录解压到可写入的短路径，不要直接在 ZIP 内运行，也不要拆散目录。\n"
        "3. API Key 不在包内；无 Key 仍可使用本地安全候选。默认自然配音和联网研究需要网络；网络不可用时会明确停止，不会降级成诊断语音或发布无声成片。\n\n"
        "【启动与制作】\n"
        f"4. 双击“{ROOT_LAUNCHER_NAME}”。每次启动都会做一遍完整性校验（不会重复校验），复算两万余项文件哈希可能需要几分钟；请等待浏览器自动打开。校验期间也可用关闭入口终止本次启动。\n"
        "5. 默认生产方式是离线纯动画 HyperFrames；MoneyPrinterTurbo 只是可选实拍支线。运行时不会安装或下载依赖。\n"
        "6. 选择角度即授权本次内部候选生产；研究、反向机械证据审核、脚本、合规、配音和成片会自动推进，不要求中途人工审查或改稿。未通过时系统会安全停止并显示诊断，不会发布不合格结果。\n"
        "7. 成片完成后可在首页播放并下载 final.mp4；证据清单用于核对脚本、审核记录和 SHA-256。\n\n"
        "【数据、关闭与故障】\n"
        "8. 新版任务、历史成片和本机加密 Key 固定保存在当前 Windows 用户的 LocalAppData\\ShiyiContentFactory\\UserData，今后更换解压目录仍会沿用。\n"
        f"9. 从旧便携版升级时，请在第一次启动新版前，把旧包的 runtime 文件夹拖到“{MIGRATION_LAUNCHER_NAME}”上；它只复制任务、配置和 DPAPI 加密 Key，不删除旧目录。若新版已产生不同数据会拒绝覆盖。\n"
        f"10. 使用完毕请双击“{STOP_LAUNCHER_NAME}”。关闭浏览器本身不会停止本地服务。\n"
        "11. 启动失败时先查看 %LOCALAPPDATA%\\ShiyiContentFactory\\Launcher\\mpt-api.log 和控制台中的结构化错误；不要重复双击多个实例。\n"
        "12. 工作台和可选实拍引擎只监听 127.0.0.1，不提供公网云服务；对外发布仍由企业负责人最终确认。\n",
        encoding="utf-8",
    )


def _copy_runtime_and_licenses(inputs: BuildInputs, package: Path, python_license: Path) -> None:
    repo = inputs.repo.resolve()
    _copy_python_runtime(inputs.python_runtime.resolve(), package / "runtime" / "python")
    _validate_retained_python_import_boundary(
        package / "runtime" / "python",
        _read_python_pruned_import_contract(repo),
    )
    moviepy_patch = (
        _apply_moviepy_windows_mf_patch(repo, package / "runtime" / "python")
        if inputs.motion_runtime is not None
        else None
    )
    python_license_contract = _read_python_license_contract(repo)
    python_sbom, python_overrides = _build_python_runtime_sbom(
        package / "runtime" / "python",
        python_license_contract,
        moviepy_patch,
    )
    ffmpeg_contract, ffmpeg_source = _read_ffmpeg_distribution_contract(
        repo, require_formal=inputs.package_profile == MOTION_PACKAGE_PROFILE
    )
    ffmpeg_entries = ffmpeg_contract["runtime"]["files"]
    assert isinstance(ffmpeg_entries, list)
    for entry in ffmpeg_entries:
        assert isinstance(entry, dict) and isinstance(entry.get("name"), str)
        _copy_file(
            ffmpeg_source / str(entry["name"]),
            package / "runtime" / "ffmpeg" / str(entry["name"]),
        )
    if inputs.motion_runtime is not None:
        motion = inputs.motion_runtime
        hyperframes_license_contract = _read_hyperframes_license_contract(repo)
        _copy_file(motion.node_runtime.resolve() / "node.exe", package / "runtime" / "node" / "node.exe")
        _copy_file(motion.node_runtime.resolve() / "LICENSE", package / "runtime" / "node" / "LICENSE")
        _copy_hyperframes_dependency_closure(
            motion.hyperframes_runtime.resolve(),
            package / "runtime" / "hyperframes",
        )
        _apply_hyperframes_windows_mf_patch(repo, package / "runtime" / "hyperframes")
        hyperframes_sbom = _write_hyperframes_runtime_manifest(
            package / "runtime" / "hyperframes",
            motion.hyperframes_version,
            hyperframes_license_contract,
        )
    else:
        hyperframes_sbom = None

    license_dir = package / "licenses"
    _copy_file(repo / "LICENSE", license_dir / "PRODUCT-MIT.txt")
    _copy_file(inputs.mpt_source.resolve() / "LICENSE", license_dir / "MoneyPrinterTurbo-MIT.txt")
    _copy_file(repo / "docs" / "fonts" / "OFL.txt", license_dir / "NotoSansSC-OFL.txt")
    _copy_file(repo / FFMPEG_LOCK_RELATIVE, package / FFMPEG_RUNTIME_LOCK_COPY)
    for name in FFMPEG_LICENSE_FILES:
        _copy_file(
            repo / FFMPEG_BOUNDARY_RELATIVE / "licenses" / name,
            license_dir / name,
        )
    _copy_file(python_license, license_dir / "Python-license.txt")
    for override in python_overrides:
        source_path = override["source_path"]
        assert isinstance(source_path, Path)
        _copy_file(
            source_path,
            license_dir / "python-runtime-dependencies" / source_path.name,
        )
        additional_sources = override["additional_evidence_sources"]
        assert isinstance(additional_sources, list)
        for supplement in additional_sources:
            assert isinstance(supplement, dict)
            evidence_bytes = supplement["evidence_bytes"]
            evidence_name = supplement["evidence_file"]
            assert isinstance(evidence_bytes, bytes) and isinstance(evidence_name, str)
            destination = license_dir / "python-runtime-dependencies" / evidence_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(evidence_bytes)
    (license_dir / PYTHON_RUNTIME_SBOM).write_text(
        json.dumps(python_sbom, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if inputs.motion_runtime is not None:
        _copy_file(inputs.motion_runtime.node_runtime.resolve() / "LICENSE", license_dir / "Node-license.txt")
        assert isinstance(hyperframes_license_contract["license_path"], Path)
        _copy_file(
            hyperframes_license_contract["license_path"],
            license_dir / "HyperFrames-Apache-2.0.txt",
        )
        overrides = hyperframes_license_contract["overrides"]
        assert isinstance(overrides, dict)
        copied_dependency_evidence: set[str] = set()
        for override in overrides.values():
            assert isinstance(override, dict) and isinstance(override["source_path"], Path)
            evidence_sources = [override["source_path"]]
            notice_sources = override.get("notice_sources", [])
            if not isinstance(notice_sources, list):
                raise ValueError("HyperFrames 依赖 NOTICE 解析结果无效")
            evidence_sources.extend(
                notice["source_path"]
                for notice in notice_sources
                if isinstance(notice, dict) and isinstance(notice.get("source_path"), Path)
            )
            for evidence_source in evidence_sources:
                destination_name = evidence_source.name.casefold()
                if destination_name in copied_dependency_evidence:
                    continue
                copied_dependency_evidence.add(destination_name)
                _copy_file(
                    evidence_source,
                    license_dir / "hyperframes-dependencies" / evidence_source.name,
                )
        assert hyperframes_sbom is not None
        (license_dir / "HyperFrames-third-party-SBOM.json").write_text(
            json.dumps(hyperframes_sbom, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    motion_label = "、Node 与 HyperFrames" if inputs.motion_runtime is not None else ""
    (license_dir / "README.txt").write_text(
        f"本目录集中保存产品、MoneyPrinterTurbo、Noto Sans SC、FFmpeg、Python{motion_label} 的许可证副本。\n"
        f"FFmpeg 对象码仅来自项目内 {FFMPEG_RUNTIME_RELATIVE.as_posix()}；完整 LGPL 构建、精确九文件哈希、"
        f"源代码伴随包身份及发布配对规则见 {FFMPEG_RUNTIME_LOCK_COPY.name}。\n"
        f"Python 第三方 distribution 的逐文件哈希、许可证声明与正文证据见 {PYTHON_RUNTIME_SBOM}；"
        "缺正文 wheel 仅允许使用 third_party/python_runtime 中的精确版本覆盖。\n"
        + (
            "HyperFrames 的传递依赖版本、SPDX 声明及实际存在的 LICENSE/NOTICE 文件见 "
            "HyperFrames-third-party-SBOM.json；并不宣称每个二进制包都附有独立许可证正文。\n"
            "Microsoft Edge 是受信 Program Files 系统前置条件，不随本包再分发浏览器二进制或许可证。\n"
            if inputs.motion_runtime is not None
            else ""
        ),
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


def _write_manifests(package: Path, repo_commit: str, inputs: BuildInputs) -> dict[str, object]:
    files = []
    for path in _payload_files(package):
        relative = path.relative_to(package).as_posix()
        files.append(
            {"path": relative, "size": path.stat().st_size, "sha256": sha256_file(path), "role": _role_for(relative)}
        )
    manifest: dict[str, object] = {
        "schema_version": 2 if inputs.motion_runtime is not None else 1,
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
            "user_data_root": "%LOCALAPPDATA%/ShiyiContentFactory/UserData",
            "launcher_state_root": "%LOCALAPPDATA%/ShiyiContentFactory/Launcher",
            "package_runtime_mutable": False,
            "moneyprinterturbo_root": "engine/MoneyPrinterTurbo/storage",
            "moneyprinterturbo_immutable_children": ["local_videos"],
            "executable_files_allowed": False,
        },
        "network": {"listen_host": "127.0.0.1", "public_cloud_service": False},
        "materials": {"root": "engine/MoneyPrinterTurbo/storage/local_videos", "count": len([p for p in files if p["path"].endswith(".mp4")])},
        "files": files,
    }
    if inputs.motion_runtime is not None:
        motion = inputs.motion_runtime
        ffmpeg_lock, _ffmpeg_runtime = _read_ffmpeg_distribution_contract(
            inputs.repo.resolve(), require_formal=True
        )
        ffmpeg_build = ffmpeg_lock["build"]
        ffmpeg_source_companion = ffmpeg_lock["source_companion"]
        ffmpeg_runtime_contract = ffmpeg_lock["runtime"]
        assert isinstance(ffmpeg_build, dict)
        assert isinstance(ffmpeg_source_companion, dict)
        assert isinstance(ffmpeg_runtime_contract, dict)
        runtime_manifest = manifest["runtime"]
        assert isinstance(runtime_manifest, dict)
        runtime_manifest["video_codec"] = H264_CODEC_STRATEGY
        runtime_manifest["moviepy_patch"] = {
            "manifest": MOVIEPY_PATCH_MANIFEST_RELATIVE.as_posix(),
            "manifest_sha256": MOVIEPY_PATCH_MANIFEST_SHA256,
            "patch_id": MOVIEPY_PATCH_ID,
            "patch_version": MOVIEPY_PATCH_VERSION,
            "distribution_version": MOVIEPY_DISTRIBUTION_VERSION,
            "module_reported_version": MOVIEPY_MODULE_REPORTED_VERSION,
            "writer_sha256": MOVIEPY_PATCHED_WRITER_SHA256,
            "record_sha256": MOVIEPY_PATCHED_RECORD_SHA256,
            "record_consistent": True,
        }
        runtime_manifest["ffmpeg_distribution"] = {
            "lock": FFMPEG_RUNTIME_LOCK_COPY.as_posix(),
            "license": ffmpeg_lock["license"],
            "runtime_file_count": len(ffmpeg_runtime_contract["files"]),
            "ffmpeg_version": ffmpeg_build["ffmpeg_version"],
            "ffmpeg_commit": ffmpeg_build["ffmpeg_commit"],
            "source_companion_name": ffmpeg_source_companion["name"],
            "source_companion_sha256": str(ffmpeg_source_companion["sha256"]).upper(),
        }
        manifest["package_profile"] = MOTION_PACKAGE_PROFILE
        manifest["motion_runtime"] = {
            "mode": "offline_bundled_with_system_browser",
            "node": "runtime/node/node.exe",
            "node_version": motion.node_version,
            "hyperframes_cli": f"runtime/hyperframes/{HYPERFRAMES_CLI_RELATIVE.as_posix()}",
            "hyperframes_version": motion.hyperframes_version,
            "closure_manifest": f"runtime/hyperframes/{HYPERFRAMES_RUNTIME_MANIFEST}",
            "codec_strategy": H264_CODEC_STRATEGY,
            "hyperframes_patch_id": HYPERFRAMES_PATCH_ID,
            "hyperframes_patch_version": HYPERFRAMES_PATCH_VERSION,
            "hyperframes_patched_cli_sha256": HYPERFRAMES_PATCHED_CLI_SHA256,
            "browser_strategy": SYSTEM_EDGE_BROWSER_STRATEGY,
            "browser_minimum_major": SYSTEM_EDGE_MINIMUM_MAJOR,
            "system_browser_required": True,
            "runtime_downloads_allowed": False,
            "startup_canary_required": True,
            "payload_sha256": _canonical_payload_sha256(
                files, ("runtime/node/", "runtime/hyperframes/")
            ),
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
    repo_commit, python_license, mpt_excluded_components = _validate_inputs(inputs)
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
            mpt_excluded_components,
        )
        _copy_materials(inputs.materials, package / "engine" / "MoneyPrinterTurbo" / "storage" / "local_videos")
        _copy_runtime_and_licenses(inputs, package, python_license)
        if inputs.verify_runtime_executables:
            _verify_mpt_offline_subset_executable(package)
        if inputs.motion_runtime is not None and inputs.verify_runtime_executables:
            _verify_motion_executables(package, inputs.motion_runtime)
        _write_root_launcher(package)
        manifest = _write_manifests(package, repo_commit, inputs)

        verify_folder, verify_zip = _load_portable_verifiers()

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
    parser.add_argument("--node-runtime", type=Path, required=True)
    parser.add_argument("--hyperframes-runtime", type=Path, required=True)
    parser.add_argument("--node-version", required=True)
    parser.add_argument("--hyperframes-version", required=True)
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
                materials=tuple(args.material),
                output=args.output,
                zip_path=args.zip_path,
                package_profile=MOTION_PACKAGE_PROFILE,
                motion_runtime=MotionRuntimeInputs(
                    node_runtime=args.node_runtime,
                    hyperframes_runtime=args.hyperframes_runtime,
                    node_version=args.node_version,
                    hyperframes_version=args.hyperframes_version,
                ),
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
