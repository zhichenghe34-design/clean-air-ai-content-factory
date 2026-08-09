from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tomllib
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable


# This module is intentionally self-contained: it is copied into the portable
# package and runs before application imports, so integrity verification cannot
# depend on the source-tree-only builder.
HYPERFRAMES_VERSION = "0.7.86"
NODE_MINIMUM_MAJOR = 22
CHECKSUMS_FILE = "SHA256SUMS.txt"
EXPECTED_MPT_COMMIT = "254cd028906ee657eab844dc94087cdbea2a7aa8"
EXPECTED_MPT_VERSION = "1.3.3"
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
MPT_APP_SUFFIX_ALLOWLIST = frozenset({".py", ".json"})
PACKAGE_MANIFEST = "PACKAGE-MANIFEST.json"
PACKAGE_ROOT_NAME = "Shiyi"
PACKAGE_VERSION = "0.3.0"
HYPERFRAMES_RUNTIME_MANIFEST = "runtime/hyperframes/RUNTIME-MANIFEST.json"
HYPERFRAMES_CLI = "runtime/hyperframes/node_modules/hyperframes/bin/hyperframes.mjs"
HYPERFRAMES_PACKAGE = "runtime/hyperframes/node_modules/hyperframes/package.json"
HYPERFRAMES_UPSTREAM_LICENSE = "third_party/hyperframes/LICENSE"
HYPERFRAMES_UPSTREAM_LOCK = "third_party/hyperframes/upstream-lock.json"
HYPERFRAMES_UPSTREAM_COMMIT = "1a52351f05237433006e6ca92db18feafed16fed"
WINDOWS_TYPICAL_EXTRACT_ROOT = "\\".join(("C:", "Users", "Default", "Downloads"))
WINDOWS_PORTABLE_PATH_BUDGET = 248
ROOT_LAUNCHER_NAME = "启动时宜Agent内容工厂.bat"
USAGE_NAME = "使用说明.txt"
SKIPPED_DIRECTORY_NAMES = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__", "node_modules"}
)
REPO_TREE_ALLOWLIST: dict[str, frozenset[str]] = {
    "core": frozenset({".py", ".ps1"}),
    "static": frozenset(
        {".css", ".html", ".js", ".json", ".jpeg", ".jpg", ".lucide", ".png", ".svg", ".webp", ".woff", ".woff2"}
    ),
    "catalog": frozenset({".json", ".md", ".txt", ".yaml", ".yml"}),
    "agent-skills": frozenset({".css", ".html", ".js", ".json", ".md", ".py", ".txt", ".yaml", ".yml"}),
}


ROOT_FILES = frozenset({"app.py", "LICENSE", ROOT_LAUNCHER_NAME, USAGE_NAME, PACKAGE_MANIFEST, CHECKSUMS_FILE})
ROOT_DIRECTORIES = frozenset(
    {"agent-skills", "catalog", "core", "docs", "engine", "licenses", "runtime", "scripts", "static", "third_party", "tools"}
)
EXACT_FILES = frozenset(
    {
        "docs/fonts/NotoSansSC-Regular.ttf",
        "docs/fonts/NotoSansSC-Bold.ttf",
        "docs/fonts/OFL.txt",
        "docs/fonts/SOURCE.md",
        "scripts/launch_combined.py",
    "scripts/launch_combined.ps1",
        "tools/verify_combined_portable.py",
        "third_party/moneyprinterturbo/LICENSE",
        "third_party/moneyprinterturbo/README.md",
        "third_party/moneyprinterturbo/upstream-lock.json",
        HYPERFRAMES_UPSTREAM_LICENSE,
        "third_party/hyperframes/README.md",
        HYPERFRAMES_UPSTREAM_LOCK,
        "third_party/hyperframes/dependency-license-overrides.json",
        "runtime/python/python.exe",
        "runtime/ffmpeg/ffmpeg.exe",
        "runtime/ffmpeg/ffprobe.exe",
        "licenses/PRODUCT-MIT.txt",
        "licenses/MoneyPrinterTurbo-MIT.txt",
        "licenses/NotoSansSC-OFL.txt",
        "licenses/FFmpeg-license.txt",
        "licenses/Python-license.txt",
        "licenses/README.txt",
        "engine/MoneyPrinterTurbo/pyproject.toml",
        "engine/MoneyPrinterTurbo/uv.lock",
        "engine/MoneyPrinterTurbo/LICENSE",
        "engine/MoneyPrinterTurbo/UPSTREAM_COMMIT",
        "engine/MoneyPrinterTurbo/config.toml",
        "engine/MoneyPrinterTurbo/resource/public/index.html",
        "engine/MoneyPrinterTurbo/resource/fonts/NotoSansSC-Regular.ttf",
        "engine/MoneyPrinterTurbo/storage/local_videos/MATERIALS.json",
    }
)
OPTIONAL_EXACT_FILES = frozenset({"licenses/FFmpeg-build-info.txt"})
MOTION_EXACT_FILES = frozenset(
    {
        "runtime/node/node.exe",
        "runtime/node/LICENSE",
        HYPERFRAMES_RUNTIME_MANIFEST,
        HYPERFRAMES_PACKAGE,
        HYPERFRAMES_CLI,
        "runtime/browser/chrome-headless-shell.exe",
        "runtime/browser/LICENSE.headless_shell",
        "licenses/Node-license.txt",
        "licenses/HyperFrames-Apache-2.0.txt",
        "licenses/HyperFrames-third-party-SBOM.json",
        "licenses/Chrome-Headless-Shell-license.txt",
    }
)
TEXT_SUFFIXES = frozenset(
    {"", "._pth", ".bat", ".cfg", ".css", ".html", ".ini", ".js", ".json", ".md", ".ps1", ".pth", ".py", ".toml", ".txt", ".yaml", ".yml"}
)
SECRET_FILE_NAMES = frozenset({".env", ".env.local", ".env.production", "cookies.json", "secrets.json"})
WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?P<path>[A-Za-z]:[\\/][^\r\n\"'<>|]*)")
UNC_PATH_RE = re.compile(r"(?<![\\A-Za-z0-9])(?P<path>\\\\[A-Za-z0-9_.-]+\\[A-Za-z0-9$_.-]+(?:\\[^\r\n\"'<>|]*)?)")
SECRET_PATTERNS = (
    # Exclude BCP-47 voice locales such as ``sk-SK-ViktoriaNeural``.
    re.compile(r"\bsk-(?![A-Z]{2}-)[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|password|client[_-]?secret)\b\s*[:=]\s*[\"'](?P<value>[^\"'\r\n]{8,})[\"']"
    ),
)
DOWNLOAD_COMMAND_RE = re.compile(
    r"(?i)(?:\bpip(?:3)?\s+install\b|\buv\s+(?:sync|pip\s+install)\b|\bnpx\s+(?:--yes|-y)\b|\bInvoke-WebRequest\b|\bStart-BitsTransfer\b|\bcurl(?:\.exe)?\s+https?://)"
)
MUTABLE_STATE_SUFFIX_ALLOWLIST = frozenset(
    {
        ".aac",
        ".ass",
        ".db",
        ".flac",
        ".gif",
        ".jpeg",
        ".jpg",
        ".json",
        ".jsonl",
        ".lock",
        ".log",
        ".m4a",
        ".md",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".ogg",
        ".part",
        ".pid",
        ".png",
        ".srt",
        ".sqlite",
        ".sqlite3",
        ".tmp",
        ".txt",
        ".vtt",
        ".wav",
        ".webm",
        ".webp",
    }
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


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


def _safe_relative(value: str) -> bool:
    if not value or "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and all(part not in ("", ".", "..") for part in path.parts)
        and all(_safe_windows_component(part) for part in path.parts)
    )


def _safe_windows_component(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value)
    if not normalized or normalized.endswith((" ", ".")):
        return False
    if any(ord(character) < 32 or character in '<>:"/\\|?*' for character in normalized):
        return False
    stem = normalized.split(".", 1)[0].casefold()
    return not re.fullmatch(r"con|prn|aux|nul|com[1-9]|lpt[1-9]", stem)


def _windows_path_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _is_mutable_runtime_path(relative: str) -> bool:
    if relative.startswith("runtime/"):
        return not relative.startswith(
            ("runtime/python/", "runtime/ffmpeg/", "runtime/node/", "runtime/hyperframes/", "runtime/browser/")
        )
    if relative.startswith("engine/MoneyPrinterTurbo/storage/"):
        return not relative.startswith("engine/MoneyPrinterTurbo/storage/local_videos/")
    return False


def _verify_mutable_runtime_paths(names: Iterable[str]) -> list[str]:
    errors: list[str] = []
    for relative in names:
        if not _safe_relative(relative):
            errors.append(f"可变运行状态路径不安全：{relative}")
        if PurePosixPath(relative).suffix.casefold() not in MUTABLE_STATE_SUFFIX_ALLOWLIST:
            errors.append(f"可变运行状态不得新增未声明类型或可执行文件：{relative}")
    return errors


def _allowed_file(relative: str) -> bool:
    if relative in ROOT_FILES or relative in EXACT_FILES or relative in OPTIONAL_EXACT_FILES:
        return True
    path = PurePosixPath(relative)
    parts = path.parts
    if not parts:
        return False
    if parts[0] in REPO_TREE_ALLOWLIST and len(parts) >= 2:
        return path.suffix.casefold() in REPO_TREE_ALLOWLIST[parts[0]]
    if relative.startswith("engine/MoneyPrinterTurbo/app/"):
        return path.suffix.casefold() in MPT_APP_SUFFIX_ALLOWLIST
    if re.fullmatch(r"engine/MoneyPrinterTurbo/storage/local_videos/material-\d{2}\.mp4", relative):
        return True
    if relative.startswith("runtime/python/") and len(parts) >= 3:
        return True
    if relative.startswith("runtime/ffmpeg/") and len(parts) >= 3:
        return True
    if relative.startswith(("runtime/hyperframes/", "runtime/browser/")) and len(parts) >= 3:
        return True
    if relative.startswith("licenses/hyperframes-dependencies/") and len(parts) == 3:
        return path.suffix.casefold() in {".txt", ".md"}
    if relative in MOTION_EXACT_FILES:
        return True
    return False


def _decode_text(value: bytes) -> str:
    if value.startswith(b"\xef\xbb\xbf"):
        return value.decode("utf-8-sig", errors="ignore")
    return value.decode("utf-8", errors="ignore")


def _scan_text(relative: str, text: str) -> list[str]:
    errors: list[str] = []
    for match in WINDOWS_PATH_RE.finditer(text):
        candidate = match.group("path")
        normalized_path = candidate.replace("\\\\", "\\").replace("/", "\\").casefold()
        is_public_system_path = normalized_path.startswith("c:\\windows\\")
        if not is_public_system_path and "..." not in candidate and "%~dp0" not in candidate and "<" not in candidate:
            errors.append(f"{relative} 含本机 Windows 绝对路径")
            break
    if UNC_PATH_RE.search(text):
        errors.append(f"{relative} 含本机 UNC 路径")
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            value = match.groupdict().get("value", match.group(0)).strip()
            normalized = value.casefold()
            if normalized in {"change-me", "changeme", "example-key", "placeholder", "your-api-key"}:
                continue
            if any(token in value for token in ("${", "%", "{token}", "<")):
                continue
            errors.append(f"{relative} 含疑似 Key、Cookie 或授权值")
            return errors
    return errors


def _parse_json(read: Callable[[str], bytes], relative: str, errors: list[str]) -> object | None:
    try:
        return json.loads(read(relative).decode("utf-8-sig"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{relative} 无法解析：{exc}")
        return None


def _verify_hyperframes_closure_manifest(
    names: list[str],
    read: Callable[[str], bytes],
    size: Callable[[str], int],
    digest: Callable[[str], str],
    expected_version: object,
) -> list[str]:
    errors: list[str] = []
    manifest = _parse_json(read, HYPERFRAMES_RUNTIME_MANIFEST, errors)
    sbom = _parse_json(read, "licenses/HyperFrames-third-party-SBOM.json", errors)
    expected_keys = {
        "schema_version",
        "runtime_kind",
        "platform",
        "entry",
        "hyperframes_version",
        "external_node_modules_allowed",
        "runtime_downloads_allowed",
        "payload_sha256",
        "packages",
        "files",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected_keys:
        return errors + ["HyperFrames 依赖闭包清单结构无效"]
    if sbom != manifest:
        errors.append("HyperFrames 第三方 SBOM 与运行时闭包清单不一致")
    fixed = {
        "schema_version": 1,
        "runtime_kind": "hyperframes_node_modules_closure",
        "platform": "win32-x64",
        "entry": "node_modules/hyperframes/bin/hyperframes.mjs",
        "hyperframes_version": expected_version,
        "external_node_modules_allowed": False,
        "runtime_downloads_allowed": False,
    }
    if any(manifest.get(key) != value for key, value in fixed.items()):
        errors.append("HyperFrames 依赖闭包固定边界或版本不正确")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        return errors + ["HyperFrames 依赖闭包文件清单不是数组"]
    entry_paths: list[str] = []
    digest_builder = hashlib.sha256()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            errors.append("HyperFrames 依赖闭包含非法文件条目")
            continue
        relative = str(entry.get("path", ""))
        full = f"runtime/hyperframes/{relative}"
        entry_paths.append(relative)
        if not _safe_relative(relative) or not full.startswith("runtime/hyperframes/node_modules/"):
            errors.append(f"HyperFrames 依赖闭包文件路径不安全：{relative}")
            continue
        if full not in names:
            errors.append(f"HyperFrames 依赖闭包引用不存在文件：{relative}")
            continue
        if entry.get("size") != size(full) or str(entry.get("sha256", "")).upper() != digest(full):
            errors.append(f"HyperFrames 依赖闭包文件哈希或大小不一致：{relative}")
        digest_builder.update(
            f"{relative}\0{entry.get('size')}\0{str(entry.get('sha256', '')).upper()}\n".encode("utf-8")
        )
    actual = sorted(
        (
            name.removeprefix("runtime/hyperframes/")
            for name in names
            if name.startswith("runtime/hyperframes/") and name != HYPERFRAMES_RUNTIME_MANIFEST
        ),
        key=str.casefold,
    )
    if entry_paths != sorted(entry_paths, key=str.casefold) or len(entry_paths) != len(set(entry_paths)):
        errors.append("HyperFrames 依赖闭包文件清单未稳定排序或重复")
    if set(entry_paths) != set(actual):
        errors.append("HyperFrames 依赖闭包清单与实际文件集合不一致")
    if manifest.get("payload_sha256") != digest_builder.hexdigest().upper():
        errors.append("HyperFrames 依赖闭包 payload SHA-256 不一致")
    packages = manifest.get("packages")
    package_names: set[str] = set()
    referenced_dependency_evidence: set[str] = set()
    if not isinstance(packages, list):
        errors.append("HyperFrames 第三方包清单不是数组")
    else:
        for package in packages:
            if not isinstance(package, dict) or set(package) != {
                "name", "version", "license", "package_json", "package_json_sha256", "license_files",
                "license_evidence",
            }:
                errors.append("HyperFrames 第三方包清单含非法条目")
                continue
            name = package.get("name")
            package_json = str(package.get("package_json", ""))
            if not isinstance(name, str) or not isinstance(package.get("version"), str) or not isinstance(
                package.get("license"), str
            ):
                errors.append("HyperFrames 第三方包缺少名称、版本或 SPDX 声明")
                continue
            package_names.add(name)
            full_package = f"runtime/hyperframes/{package_json}"
            if full_package not in names or package.get("package_json_sha256") != digest(full_package):
                errors.append(f"HyperFrames 第三方包元数据不一致：{name}")
            license_files = package.get("license_files")
            if not isinstance(license_files, list):
                errors.append(f"HyperFrames 第三方包许可证索引无效：{name}")
                continue
            for license_entry in license_files:
                if not isinstance(license_entry, dict) or set(license_entry) != {"path", "sha256"}:
                    errors.append(f"HyperFrames 第三方包许可证条目无效：{name}")
                    continue
                full_license = f"runtime/hyperframes/{license_entry.get('path', '')}"
                if full_license not in names or license_entry.get("sha256") != digest(full_license):
                    errors.append(f"HyperFrames 第三方包许可证哈希不一致：{name}")
            evidence = package.get("license_evidence")
            if not isinstance(evidence, dict) or evidence.get("spdx") != package.get("license"):
                errors.append(f"HyperFrames 第三方包许可证证据无效：{name}")
                continue
            kind = evidence.get("kind")
            if kind == "package_files":
                if set(evidence) != {"kind", "spdx", "files"} or evidence.get("files") != license_files or not license_files:
                    errors.append(f"HyperFrames 包内许可证证据不一致：{name}")
            elif kind == "verified_upstream_copy":
                expected = {
                    "kind": "verified_upstream_copy",
                    "spdx": "Apache-2.0",
                    "path": "licenses/HyperFrames-Apache-2.0.txt",
                    "sha256": "4259155FB06F127687EE7B0A8A3682D45132DB0F2DA26CBC0B7A2D1E796436B8",
                    "source_url": "https://raw.githubusercontent.com/heygen-com/hyperframes/v0.7.86/LICENSE",
                    "source_tag": "v0.7.86",
                    "source_commit": HYPERFRAMES_UPSTREAM_COMMIT,
                }
                if name != "hyperframes" or evidence != expected or license_files:
                    errors.append("HyperFrames 主包上游许可证证据不一致")
            elif kind == "project_verified_override":
                if set(evidence) != {
                    "kind",
                    "spdx",
                    "path",
                    "sha256",
                    "source_url",
                    "source_commit",
                    "source_sha256",
                    "copyright",
                    "notices",
                } or license_files:
                    errors.append(f"HyperFrames 依赖许可证覆盖证据结构无效：{name}")
                    continue
                evidence_path = str(evidence.get("path", ""))
                source_url = str(evidence.get("source_url", ""))
                source_commit = str(evidence.get("source_commit", ""))
                source_sha256 = str(evidence.get("source_sha256", ""))
                copyright_notice = evidence.get("copyright")
                if (
                    not evidence_path.startswith("licenses/hyperframes-dependencies/")
                    or evidence_path not in names
                    or evidence.get("sha256") != digest(evidence_path)
                    or not source_url.startswith("https://raw.githubusercontent.com/")
                    or not re.fullmatch(r"[0-9a-f]{40}", source_commit)
                    or source_commit not in source_url
                    or not re.fullmatch(r"[0-9A-F]{64}", source_sha256)
                    or not isinstance(copyright_notice, str)
                    or not copyright_notice
                    or len(copyright_notice) > 500
                ):
                    errors.append(f"HyperFrames 依赖许可证覆盖正文或来源无效：{name}")
                    continue
                referenced_dependency_evidence.add(evidence_path)
                notices = evidence.get("notices")
                if not isinstance(notices, list) or len(notices) > 4:
                    errors.append(f"HyperFrames 依赖 NOTICE 清单无效：{name}")
                    continue
                for notice in notices:
                    if not isinstance(notice, dict) or set(notice) != {
                        "path", "sha256", "source_url", "source_sha256"
                    }:
                        errors.append(f"HyperFrames 依赖 NOTICE 条目结构无效：{name}")
                        continue
                    notice_path = str(notice.get("path", ""))
                    notice_url = str(notice.get("source_url", ""))
                    if (
                        not notice_path.startswith("licenses/hyperframes-dependencies/")
                        or notice_path not in names
                        or notice.get("sha256") != digest(notice_path)
                        or not notice_url.startswith("https://raw.githubusercontent.com/")
                        or source_commit not in notice_url
                        or not re.fullmatch(r"[0-9A-F]{64}", str(notice.get("source_sha256", "")))
                    ):
                        errors.append(f"HyperFrames 依赖 NOTICE 正文或来源无效：{name}")
                        continue
                    referenced_dependency_evidence.add(notice_path)
            else:
                errors.append(f"HyperFrames 第三方包许可证证据类型无效：{name}")
    actual_dependency_evidence = {
        path for path in names if path.startswith("licenses/hyperframes-dependencies/")
    }
    if actual_dependency_evidence != referenced_dependency_evidence:
        errors.append("HyperFrames 依赖许可证证据目录与 SBOM 引用不一致")
    if not {"hyperframes", "esbuild", "@esbuild/win32-x64"}.issubset(package_names):
        errors.append("HyperFrames 依赖闭包缺少主包、esbuild 或 Windows x64 平台包")
    return errors


def _verify_layout(names: list[str]) -> list[str]:
    errors: list[str] = []
    if names != sorted(names, key=str.casefold):
        errors.append("包内文件路径未按稳定顺序排列")
    if len(names) != len(set(names)):
        errors.append("包内存在重复文件路径")
    if len(names) != len({_windows_path_key(name) for name in names}):
        errors.append("包内存在 Windows 大小写或 Unicode 折叠冲突路径")
    over_budget = [
        name
        for name in names
        if len(f"{WINDOWS_TYPICAL_EXTRACT_ROOT}\\{PACKAGE_ROOT_NAME}\\{name.replace('/', '\\')}")
        > WINDOWS_PORTABLE_PATH_BUDGET
    ]
    if over_budget:
        errors.append("包内路径超过普通 Downloads 解压位置的 Windows 兼容预算")
    for relative in names:
        if not _safe_relative(relative):
            errors.append(f"包内路径不安全：{relative}")
            continue
        parts = PurePosixPath(relative).parts
        if parts[0] not in ROOT_FILES and parts[0] not in ROOT_DIRECTORIES:
            errors.append(f"包内出现非白名单顶层路径：{relative}")
        forbidden_parts = SKIPPED_DIRECTORY_NAMES - (
            {"node_modules"} if relative.startswith("runtime/hyperframes/") else set()
        )
        if any(part.casefold() in forbidden_parts for part in parts):
            errors.append(f"包内出现缓存、测试依赖或版本库目录：{relative}")
        if PurePosixPath(relative).name.casefold() in SECRET_FILE_NAMES:
            errors.append(f"包内出现秘密或 Cookie 文件：{relative}")
        if not _allowed_file(relative):
            errors.append(f"包内出现非白名单文件：{relative}")

    required = ROOT_FILES | EXACT_FILES
    missing = sorted(required - set(names), key=str.casefold)
    if missing:
        errors.append(f"包内缺少必需文件：{missing}")
    ffmpeg_dll_names = {PurePosixPath(name).name.casefold() for name in names if name.startswith("runtime/ffmpeg/")}
    for prefix in ("avcodec", "avfilter", "avformat", "avutil", "swresample", "swscale"):
        if not any(name.startswith(prefix) and name.endswith(".dll") for name in ffmpeg_dll_names):
            errors.append(f"完整 FFmpeg runtime 缺少 {prefix} DLL")
    if any(name.casefold().endswith("/shiyicontentfactory.exe") for name in names):
        errors.append("组合包不得复用旧 v0.2 EXE")
    forbidden_mpt_prefixes = (
        "engine/MoneyPrinterTurbo/.git/",
        "engine/MoneyPrinterTurbo/docs/",
        "engine/MoneyPrinterTurbo/test/",
        "engine/MoneyPrinterTurbo/tests/",
        "engine/MoneyPrinterTurbo/webui/",
        "engine/MoneyPrinterTurbo/resource/songs/",
    )
    for name in names:
        if name.startswith(forbidden_mpt_prefixes):
            errors.append(f"MoneyPrinterTurbo 包含明确排除的上游目录：{name}")
        if name.startswith("engine/MoneyPrinterTurbo/resource/fonts/") and name != "engine/MoneyPrinterTurbo/resource/fonts/NotoSansSC-Regular.ttf":
            errors.append(f"MoneyPrinterTurbo 包含未授权字体：{name}")
    return errors


def _verify_manifest(
    names: list[str],
    read: Callable[[str], bytes],
    size: Callable[[str], int],
    digest: Callable[[str], str],
) -> tuple[list[str], dict[str, object] | None]:
    errors: list[str] = []
    raw = _parse_json(read, PACKAGE_MANIFEST, errors)
    if not isinstance(raw, dict):
        return errors or ["PACKAGE-MANIFEST.json 结构无效"], None
    manifest = raw
    legacy_keys = {
        "schema_version",
        "product",
        "version",
        "package_kind",
        "source",
        "runtime",
        "mutable_state",
        "network",
        "materials",
        "files",
    }
    motion_package = manifest.get("schema_version") == 2
    expected_keys = legacy_keys | ({"package_profile", "motion_runtime"} if motion_package else set())
    if set(manifest) != expected_keys:
        errors.append("PACKAGE-MANIFEST.json 顶层字段不符合固定协议")
    if manifest.get("schema_version") not in {1, 2} or manifest.get("version") != PACKAGE_VERSION:
        errors.append("PACKAGE-MANIFEST.json 版本不正确")
    if manifest.get("package_kind") != "windows_x64_combined_portable":
        errors.append("PACKAGE-MANIFEST.json 包类型不正确")
    source = manifest.get("source")
    if not isinstance(source, dict) or set(source) != {
        "repository_commit",
        "moneyprinterturbo_version",
        "moneyprinterturbo_commit",
        "mpt_payload_sha256",
    }:
        errors.append("PACKAGE-MANIFEST.json 来源结构不正确")
    else:
        if not re.fullmatch(r"[0-9a-f]{40}", str(source.get("repository_commit", ""))):
            errors.append("PACKAGE-MANIFEST.json 缺少完整项目提交哈希")
        if source.get("moneyprinterturbo_version") != EXPECTED_MPT_VERSION or source.get("moneyprinterturbo_commit") != EXPECTED_MPT_COMMIT:
            errors.append("PACKAGE-MANIFEST.json 的 MoneyPrinterTurbo 锁不正确")
    expected_runtime = {
        "shared_python": "runtime/python/python.exe",
        "workbench_entry": "app.py",
        "moneyprinterturbo_entry": "engine/MoneyPrinterTurbo/app/asgi.py",
        "ffmpeg": "runtime/ffmpeg/ffmpeg.exe",
        "ffprobe": "runtime/ffmpeg/ffprobe.exe",
        "runtime_downloads_allowed": False,
        "payload_sha256": "",
    }
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict) or set(runtime) != set(expected_runtime) or any(
        runtime.get(key) != value for key, value in expected_runtime.items() if key != "payload_sha256"
    ):
        errors.append("PACKAGE-MANIFEST.json 运行时合同不正确")
    expected_mutable = {
        "workbench_root": "runtime",
        "workbench_immutable_children": ["python", "ffmpeg"]
        + (["node", "hyperframes", "browser"] if motion_package else []),
        "moneyprinterturbo_root": "engine/MoneyPrinterTurbo/storage",
        "moneyprinterturbo_immutable_children": ["local_videos"],
        "executable_files_allowed": False,
    }
    if manifest.get("mutable_state") != expected_mutable:
        errors.append("PACKAGE-MANIFEST.json 可变运行状态边界不正确")
    if manifest.get("network") != {"listen_host": "127.0.0.1", "public_cloud_service": False}:
        errors.append("PACKAGE-MANIFEST.json 本机监听合同不正确")
    motion_runtime = manifest.get("motion_runtime")
    if motion_package:
        expected_motion_keys = {
            "mode",
            "node",
            "node_version",
            "hyperframes_cli",
            "hyperframes_version",
            "closure_manifest",
            "browser",
            "browser_version",
            "runtime_downloads_allowed",
            "system_fallback_allowed",
            "payload_sha256",
        }
        if manifest.get("package_profile") != "motion_primary":
            errors.append("motion schema 必须声明 motion_primary profile")
        if not isinstance(motion_runtime, dict) or set(motion_runtime) != expected_motion_keys:
            errors.append("PACKAGE-MANIFEST.json 离线动画运行时合同不正确")
        elif isinstance(motion_runtime, dict):
            fixed = {
                "mode": "offline_bundled_required",
                "node": "runtime/node/node.exe",
                "hyperframes_cli": HYPERFRAMES_CLI,
                "closure_manifest": HYPERFRAMES_RUNTIME_MANIFEST,
                "browser": "runtime/browser/chrome-headless-shell.exe",
                "runtime_downloads_allowed": False,
                "system_fallback_allowed": False,
            }
            if any(motion_runtime.get(key) != value for key, value in fixed.items()):
                errors.append("离线动画运行时路径或禁止下载/回退合同不正确")
            for key in ("node_version", "hyperframes_version", "browser_version"):
                if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", str(motion_runtime.get(key, ""))):
                    errors.append("离线动画运行时缺少固定版本")
                    break
            if motion_runtime.get("hyperframes_version") != HYPERFRAMES_VERSION:
                errors.append(f"HyperFrames 正式运行时版本必须固定为 {HYPERFRAMES_VERSION}")
            node_version = str(motion_runtime.get("node_version", ""))
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", node_version) and int(
                node_version.split(".", 1)[0]
            ) < NODE_MINIMUM_MAJOR:
                errors.append(f"Node 正式运行时主版本不得低于 {NODE_MINIMUM_MAJOR}")
        missing_motion = sorted(MOTION_EXACT_FILES - set(names), key=str.casefold)
        if missing_motion:
            errors.append(f"motion_primary 包缺少离线动画运行时文件：{missing_motion}")
        else:
            hyperframes_lock = _parse_json(read, HYPERFRAMES_UPSTREAM_LOCK, errors)
            expected_hyperframes_lock = {
                "schema_version": 1,
                "name": "HyperFrames",
                "repository": "https://github.com/heygen-com/hyperframes",
                "upstream_tag": "v0.7.86",
                "upstream_commit": HYPERFRAMES_UPSTREAM_COMMIT,
                "npm_package": "hyperframes",
                "npm_version": HYPERFRAMES_VERSION,
                "npm_resolved": f"https://registry.npmjs.org/hyperframes/-/hyperframes-{HYPERFRAMES_VERSION}.tgz",
                "npm_integrity": "sha512-R8Vds5hY9XULMsCGUa+qynC7F0tL7KZyDaL6cgQ4xyJAATC9fOIPgRMOBkOHYd9JOntRqbR9bFSsfK7mYJjaow==",
                "license": "Apache-2.0",
                "license_source": "https://raw.githubusercontent.com/heygen-com/hyperframes/v0.7.86/LICENSE",
                "license_sha256": "4259155FB06F127687EE7B0A8A3682D45132DB0F2DA26CBC0B7A2D1E796436B8",
            }
            if hyperframes_lock != expected_hyperframes_lock:
                errors.append("HyperFrames 固定 tag、commit、npm 或许可证锁不一致")
            hyperframes_package = _parse_json(read, HYPERFRAMES_PACKAGE, errors)
            if not isinstance(hyperframes_package, dict) or hyperframes_package.get("version") != motion_runtime.get(
                "hyperframes_version"
            ):
                errors.append("HyperFrames package.json 与动画运行时版本锁不一致")
            errors.extend(
                _verify_hyperframes_closure_manifest(
                    names, read, size, digest, motion_runtime.get("hyperframes_version")
                )
            )
            for source_path, license_path in (
                ("runtime/node/LICENSE", "licenses/Node-license.txt"),
                (HYPERFRAMES_UPSTREAM_LICENSE, "licenses/HyperFrames-Apache-2.0.txt"),
                ("runtime/browser/LICENSE.headless_shell", "licenses/Chrome-Headless-Shell-license.txt"),
            ):
                if digest(source_path) != digest(license_path):
                    errors.append(f"离线动画运行时许可证副本不一致：{license_path}")
            if digest(HYPERFRAMES_UPSTREAM_LICENSE) != expected_hyperframes_lock["license_sha256"]:
                errors.append("HyperFrames 官方 tag 许可证 SHA-256 不一致")
    elif "motion_runtime" in manifest or any(name.startswith(("runtime/node/", "runtime/hyperframes/", "runtime/browser/")) for name in names):
        errors.append("legacy schema 不得混入未声明动画运行时")

    entries = manifest.get("files")
    if not isinstance(entries, list):
        errors.append("PACKAGE-MANIFEST.json files 不是数组")
        return errors, manifest
    manifest_paths: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256", "role"}:
            errors.append("PACKAGE-MANIFEST.json 存在非法文件条目")
            continue
        relative = str(entry.get("path", ""))
        manifest_paths.append(relative)
        if not _safe_relative(relative) or relative in {PACKAGE_MANIFEST, CHECKSUMS_FILE}:
            errors.append(f"PACKAGE-MANIFEST.json 文件路径不安全：{relative}")
            continue
        if relative not in names:
            errors.append(f"PACKAGE-MANIFEST.json 引用了不存在的文件：{relative}")
            continue
        if entry.get("size") != size(relative):
            errors.append(f"{relative} 文件大小与 PACKAGE-MANIFEST.json 不一致")
        if str(entry.get("sha256", "")).upper() != digest(relative):
            errors.append(f"{relative} SHA-256 与 PACKAGE-MANIFEST.json 不一致")
        if entry.get("role") != _role_for(relative):
            errors.append(f"{relative} role 与固定白名单不一致")
    if manifest_paths != sorted(manifest_paths, key=str.casefold) or len(manifest_paths) != len(set(manifest_paths)):
        errors.append("PACKAGE-MANIFEST.json 文件列表未稳定排序或存在重复")
    actual_payload = [name for name in names if name not in {PACKAGE_MANIFEST, CHECKSUMS_FILE}]
    if set(manifest_paths) != set(actual_payload):
        errors.append("PACKAGE-MANIFEST.json 与实际文件集合不一致")

    valid_entries = [entry for entry in entries if isinstance(entry, dict) and set(entry) == {"path", "size", "sha256", "role"}]
    if isinstance(source, dict) and source.get("mpt_payload_sha256") != _canonical_payload_sha256(
        valid_entries, ("engine/MoneyPrinterTurbo/",)
    ):
        errors.append("source.mpt_payload_sha256 与规范化 MPT 文件集不一致")
    if isinstance(runtime, dict) and runtime.get("payload_sha256") != _canonical_payload_sha256(
        valid_entries, ("runtime/python/", "runtime/ffmpeg/")
    ):
        errors.append("runtime.payload_sha256 与规范化运行时文件集不一致")
    if motion_package and isinstance(motion_runtime, dict) and motion_runtime.get(
        "payload_sha256"
    ) != _canonical_payload_sha256(valid_entries, ("runtime/node/", "runtime/hyperframes/", "runtime/browser/")):
        errors.append("motion_runtime.payload_sha256 与规范化动画运行时文件集不一致")

    if motion_package:
        for relative in names:
            if not relative.startswith("agent-skills/") or PurePosixPath(relative).suffix.casefold() not in {
                ".css", ".html", ".js", ".json"
            }:
                continue
            if re.search(r"(?i)\bhttps?://", _decode_text(read(relative))):
                errors.append(f"motion_primary 动画资产含网络资源：{relative}")
                break

    materials = manifest.get("materials")
    material_names = [name for name in names if re.fullmatch(r"engine/MoneyPrinterTurbo/storage/local_videos/material-\d{2}\.mp4", name)]
    if materials != {"root": "engine/MoneyPrinterTurbo/storage/local_videos", "count": len(material_names)}:
        errors.append("PACKAGE-MANIFEST.json 素材根目录或数量不正确")
    return errors, manifest


def _verify_checksums(
    read: Callable[[str], bytes], manifest: dict[str, object] | None, digest: Callable[[str], str]
) -> list[str]:
    if manifest is None or not isinstance(manifest.get("files"), list):
        return ["无法在清单无效时验证 SHA256SUMS.txt"]
    try:
        text = read(CHECKSUMS_FILE).decode("utf-8")
    except (KeyError, UnicodeDecodeError) as exc:
        return [f"SHA256SUMS.txt 无法读取：{exc}"]
    actual = text.splitlines()
    paths = [str(entry.get("path", "")) for entry in manifest["files"] if isinstance(entry, dict)]
    paths.append(PACKAGE_MANIFEST)
    expected = [f"{digest(path)}  {path}" for path in paths]
    return [] if actual == expected else ["SHA256SUMS.txt 顺序、集合或 SHA-256 与实际文件不一致"]


def _verify_mpt_and_launchers(
    names: list[str], read: Callable[[str], bytes], size: Callable[[str], int], digest: Callable[[str], str]
) -> list[str]:
    errors: list[str] = []
    prefix = "engine/MoneyPrinterTurbo/"
    if _decode_text(read(prefix + "UPSTREAM_COMMIT")).strip() != EXPECTED_MPT_COMMIT:
        errors.append("MoneyPrinterTurbo UPSTREAM_COMMIT 不正确")
    try:
        pyproject = tomllib.loads(_decode_text(read(prefix + "pyproject.toml")))
    except tomllib.TOMLDecodeError:
        pyproject = {}
        errors.append("MoneyPrinterTurbo pyproject.toml 无法解析")
    project = pyproject.get("project", {}) if isinstance(pyproject, dict) else {}
    if not isinstance(project, dict) or project.get("version") != EXPECTED_MPT_VERSION:
        errors.append("MoneyPrinterTurbo pyproject.toml 版本不正确")
    try:
        config = tomllib.loads(_decode_text(read(prefix + "config.toml")))
    except tomllib.TOMLDecodeError:
        config = {}
        errors.append("MoneyPrinterTurbo config.toml 无法解析")
    expected_app = {
        "endpoint": "",
        "hide_config": True,
        "edge_tts_timeout": 30,
        "tls_verify": True,
        "video_source": "local",
        "subtitle_provider": "edge",
        "bgm_type": "",
        "bgm_volume": 0.0,
    }
    if config != {"log_level": "WARNING", "listen_host": "127.0.0.1", "listen_port": 8080, "app": expected_app}:
        errors.append("MoneyPrinterTurbo 安全 config.toml 与固定合同不一致")

    lock = _parse_json(read, "third_party/moneyprinterturbo/upstream-lock.json", errors)
    if not isinstance(lock, dict) or (
        lock.get("upstream_commit") != EXPECTED_MPT_COMMIT
        or lock.get("upstream_version") != EXPECTED_MPT_VERSION
        or lock.get("license") != "MIT"
    ):
        errors.append("third_party MoneyPrinterTurbo 上游锁不正确")
    else:
        expected_license_sha = str(lock.get("license_sha256", "")).upper()
        license_paths = (
            prefix + "LICENSE",
            "third_party/moneyprinterturbo/LICENSE",
            "licenses/MoneyPrinterTurbo-MIT.txt",
        )
        if not re.fullmatch(r"[0-9A-F]{64}", expected_license_sha) or any(digest(path) != expected_license_sha for path in license_paths):
            errors.append("MoneyPrinterTurbo MIT 许可证副本与上游锁不一致")
    if digest("docs/fonts/NotoSansSC-Regular.ttf") != digest(prefix + "resource/fonts/NotoSansSC-Regular.ttf"):
        errors.append("MoneyPrinterTurbo Noto 字体与项目锁定字体不一致")
    if digest("docs/fonts/OFL.txt") != digest("licenses/NotoSansSC-OFL.txt"):
        errors.append("Noto Sans SC 许可证副本不一致")
    if digest("LICENSE") != digest("licenses/PRODUCT-MIT.txt"):
        errors.append("产品许可证副本不一致")

    material_names = sorted(
        (name for name in names if re.fullmatch(r"engine/MoneyPrinterTurbo/storage/local_videos/material-\d{2}\.mp4", name)),
        key=str.casefold,
    )
    expected_names = [f"{prefix}storage/local_videos/material-{index:02d}.mp4" for index in range(1, len(material_names) + 1)]
    if not 1 <= len(material_names) <= 24 or material_names != expected_names:
        errors.append("本地 MP4 素材必须为连续编号的 1 到 24 个文件")
    materials = _parse_json(read, prefix + "storage/local_videos/MATERIALS.json", errors)
    expected_entries = [
        {"name": PurePosixPath(name).name, "size": size(name), "sha256": digest(name)} for name in material_names
    ]
    if materials != {"schema_version": 1, "files": expected_entries}:
        errors.append("MATERIALS.json 与本地 MP4 文件不一致")

    launcher = _decode_text(read(ROOT_LAUNCHER_NAME))
    shared_python = "%~dp0runtime\\python\\python.exe"
    required_launcher_fragments = (
        f'set "SHIYI_LAUNCHER_PYTHON={shared_python}"',
        '"%SHIYI_LAUNCHER_PYTHON%" -I -S -B -X utf8 "%~dp0tools\\verify_combined_portable.py" "%~dp0." --startup',
        '"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"',
        f'-MptPython "{shared_python}"',
        f'-AppPython "{shared_python}"',
        '-MptRoot "%~dp0engine\\MoneyPrinterTurbo"',
        '-Ffmpeg "%~dp0runtime\\ffmpeg\\ffmpeg.exe"',
        '-Ffprobe "%~dp0runtime\\ffmpeg\\ffprobe.exe"',
        '-MaterialRoot "%~dp0engine\\MoneyPrinterTurbo\\storage\\local_videos"',
    )
    if any(fragment not in launcher for fragment in required_launcher_fragments):
        errors.append("根启动 BAT 未把工作台和 MPT 固定到同一便携 Python 或固定素材目录")
    powershell_launcher = _decode_text(read("scripts/launch_combined.ps1"))
    required_powershell_fragments = (
        '$packageManifest = Join-Path $projectRoot "PACKAGE-MANIFEST.json"',
        '$launcherPython = Join-Path $projectRoot "runtime\\python\\python.exe"',
        '@("-I", "-S", "-B", "-X", "utf8", $launcher',
    )
    if any(fragment not in powershell_launcher for fragment in required_powershell_fragments):
        errors.append("PowerShell 启动器未固定便携 Python 或隔离解释器启动参数。")
    for relative in (ROOT_LAUNCHER_NAME, "scripts/launch_combined.ps1", "scripts/launch_combined.py"):
        if DOWNLOAD_COMMAND_RE.search(_decode_text(read(relative))):
            errors.append(f"{relative} 含运行时下载或安装命令")
    return errors


def _verify_scans(names: Iterable[str], read: Callable[[str], bytes], size: Callable[[str], int]) -> list[str]:
    errors: list[str] = []
    for relative in names:
        # The vendored Python/FFmpeg trees are immutable and hash-pinned. Their
        # standard-library and dependency sources legitimately contain example
        # Windows paths and credential *field names*, so treating all of that as
        # user data creates hundreds of false positives. Relocation-sensitive
        # runtime metadata is rejected separately below.
        if relative.startswith(
            ("runtime/python/", "runtime/ffmpeg/", "runtime/node/", "runtime/hyperframes/", "runtime/browser/")
        ):
            continue
        suffix = PurePosixPath(relative).suffix.casefold()
        if suffix not in TEXT_SUFFIXES or size(relative) > 4 * 1024 * 1024:
            continue
        errors.extend(_scan_text(relative, _decode_text(read(relative))))
    return errors


def _verify_runtime_relocation_metadata(names: Iterable[str]) -> list[str]:
    errors: list[str] = []
    for relative in names:
        if not relative.startswith("runtime/python/"):
            continue
        path = PurePosixPath(relative)
        lowered_parts = tuple(part.casefold() for part in path.parts)
        lowered_name = path.name.casefold()
        hook_names = {"sitecustomize", "usercustomize"}
        if any(
            part in hook_names or any(part.startswith(f"{hook}.") for hook in hook_names)
            for part in lowered_parts[2:]
        ):
            errors.append(f"便携 Python 不得包含解释器自动启动钩子：{relative}")
            continue
        if len(lowered_parts) >= 3 and lowered_parts[2] == "scripts":
            errors.append(f"便携 Python 不得包含不可迁移的 Scripts 入口：{relative}")
            continue
        if (
            lowered_name in {"pyvenv.cfg", "direct_url.json"}
            or path.suffix.casefold() in {".pth", ".egg-link", "._pth"}
        ):
            errors.append(f"便携 Python 含不可迁移或可注入路径元数据：{relative}")
    return errors


def _verify_package(
    names: list[str], read: Callable[[str], bytes], size: Callable[[str], int], digest: Callable[[str], str]
) -> list[str]:
    errors = _verify_layout(names)
    if any(item.startswith("包内路径不安全") for item in errors):
        return errors
    manifest_errors, manifest = _verify_manifest(names, read, size, digest)
    errors.extend(manifest_errors)
    errors.extend(_verify_checksums(read, manifest, digest))
    try:
        errors.extend(_verify_mpt_and_launchers(names, read, size, digest))
        errors.extend(_verify_runtime_relocation_metadata(names))
        errors.extend(_verify_scans(names, read, size))
    except KeyError as exc:
        errors.append(f"缺少验证所需文件：{exc}")
    return errors


def verify_folder(folder: Path, allow_runtime_state: bool = False) -> list[str]:
    if not folder.is_dir():
        return [f"组合便携目录不存在：{folder}"]
    if _is_reparse_point(folder):
        return ["组合便携目录根不得是符号链接、Junction 或其他重解析点"]
    folder = folder.resolve()
    errors: list[str] = []
    paths: dict[str, Path] = {}
    for current, directory_names, file_names in os.walk(folder, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in sorted(directory_names, key=str.casefold):
            path = current_path / name
            relative = path.relative_to(folder).as_posix()
            if _is_reparse_point(path):
                errors.append(f"包内不得包含符号链接、Junction 或其他重解析目录：{relative}")
                continue
            try:
                if not path.resolve(strict=True).is_relative_to(folder):
                    errors.append(f"包内目录越过发布根：{relative}")
                    continue
            except OSError:
                errors.append(f"包内目录无法安全解析：{relative}")
                continue
            kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in sorted(file_names, key=str.casefold):
            path = current_path / name
            relative = path.relative_to(folder).as_posix()
            if _is_reparse_point(path):
                errors.append(f"包内不得包含符号链接、Junction 或其他重解析文件：{relative}")
                continue
            try:
                if not path.resolve(strict=True).is_relative_to(folder):
                    errors.append(f"包内文件越过发布根：{relative}")
                    continue
            except OSError:
                errors.append(f"包内文件无法安全解析：{relative}")
                continue
            paths[relative] = path
    if allow_runtime_state:
        mutable_names = [name for name in paths if _is_mutable_runtime_path(name)]
        errors.extend(_verify_mutable_runtime_paths(mutable_names))
        ignored = set(mutable_names)
        paths = {name: path for name, path in paths.items() if name not in ignored}
    names = sorted(paths, key=str.casefold)

    def read(relative: str) -> bytes:
        return paths[relative].read_bytes()

    def size(relative: str) -> int:
        return paths[relative].stat().st_size

    digest_cache: dict[str, str] = {}

    def digest(relative: str) -> str:
        if relative in digest_cache:
            return digest_cache[relative]
        value = hashlib.sha256()
        with paths[relative].open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                value.update(chunk)
        digest_cache[relative] = value.hexdigest().upper()
        return digest_cache[relative]

    errors.extend(_verify_package(names, read, size, digest))
    return errors


def verify_zip(path: Path) -> list[str]:
    path = path.resolve()
    if not path.is_file():
        return [f"组合便携 ZIP 不存在：{path}"]
    errors: list[str] = []
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"组合便携 ZIP 无法打开：{exc}"]
    with archive:
        infos = archive.infolist()
        raw_names = [info.filename for info in infos]
        prefix = PACKAGE_ROOT_NAME + "/"
        if any(not name.startswith(prefix) for name in raw_names):
            return ["ZIP 必须只包含固定根目录"]
        names = [name[len(prefix) :] for name in raw_names]
        if any(not name or not _safe_relative(name) for name in names):
            return ["ZIP 含 Windows 非法、空、绝对或目录穿越路径"]
        if any(info.is_dir() for info in infos):
            errors.append("ZIP 不应包含可变目录条目")
        if names != sorted(names, key=str.casefold) or len(names) != len(set(names)):
            errors.append("ZIP 文件顺序不固定或存在重复路径")
        if len(names) != len({_windows_path_key(name) for name in names}):
            errors.append("ZIP 存在 Windows 大小写或 Unicode 折叠冲突路径")
        for info in infos:
            if info.date_time != FIXED_ZIP_TIME:
                errors.append(f"ZIP 时间戳不固定：{info.filename}")
            if info.compress_type != zipfile.ZIP_DEFLATED:
                errors.append(f"ZIP 压缩方式不正确：{info.filename}")
            if info.external_attr != (0o100644 << 16):
                errors.append(f"ZIP 条目不是固定权限的普通文件：{info.filename}")
        info_by_name = {name: info for name, info in zip(names, infos)}

        def read(relative: str) -> bytes:
            return archive.read(info_by_name[relative])

        def size(relative: str) -> int:
            return info_by_name[relative].file_size

        digest_cache: dict[str, str] = {}

        def digest(relative: str) -> str:
            if relative in digest_cache:
                return digest_cache[relative]
            value = hashlib.sha256()
            with archive.open(info_by_name[relative]) as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    value.update(chunk)
            digest_cache[relative] = value.hexdigest().upper()
            return digest_cache[relative]

        errors.extend(_verify_package(names, read, size, digest))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="复算 v0.3 Windows 组合便携目录或 ZIP。")
    parser.add_argument("path", type=Path)
    parser.add_argument("--zip", action="store_true", dest="is_zip")
    parser.add_argument("--startup", action="store_true", help="允许已声明的运行状态目录，不放宽静态代码与运行时文件")
    args = parser.parse_args()
    errors = (
        verify_zip(args.path)
        if args.is_zip or args.path.suffix.casefold() == ".zip"
        else verify_folder(args.path, allow_runtime_state=args.startup)
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(json.dumps({"status": "COMBINED_PORTABLE_OK", "path": str(args.path.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
