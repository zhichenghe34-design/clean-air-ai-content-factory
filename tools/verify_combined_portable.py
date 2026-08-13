from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import json
import os
import re
import stat
import sys
import tomllib
import unicodedata
import warnings
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable


# This module is intentionally self-contained: it is copied into the portable
# package and runs before application imports, so integrity verification cannot
# depend on the source-tree-only builder.
HYPERFRAMES_VERSION = "0.7.86"
NODE_MINIMUM_MAJOR = 22
HYPERFRAMES_PATCH_ID = "shiyi-hyperframes-windows-mf"
HYPERFRAMES_PATCH_VERSION = "1.2.0"
HYPERFRAMES_UPSTREAM_CLI_SHA256 = "B89672986C4487A133B241261AC610EA9F9CCDE467F206E18A60BEFFACAB6CB8"
HYPERFRAMES_PATCHED_CLI_SHA256 = "86DA751BA397FF551355BA0C90370D732A297C3DC4652C981E9A8146D8EAC108"
SYSTEM_EDGE_BROWSER_STRATEGY = "trusted_system_edge"
SYSTEM_EDGE_MINIMUM_MAJOR = 151
CHECKSUMS_FILE = "SHA256SUMS.txt"
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
H264_CODEC_STRATEGY = "h264_mf"
H264_CODEC_TOKEN_RE = re.compile(
    r"(?i)\b(?:libx264|openh264|x264|h264(?:[_-][a-z0-9]+)+)\b"
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
MPT_DISABLED_MUSIC_PROVIDER_FILES = frozenset(
    {
        "engine/MoneyPrinterTurbo/app/services/elevenlabs_music.py",
        "engine/MoneyPrinterTurbo/app/services/sonilo.py",
    }
)
FFMPEG_UPSTREAM_LOCK = "third_party/ffmpeg/upstream-lock.json"
FFMPEG_RUNTIME_LOCK_COPY = "licenses/FFmpeg-runtime-lock.json"
FFMPEG_RUNTIME_FILES = {
    "avcodec-63.dll": (14030848, "6C4C5A12A84C940BCF12CA729F26F1629A97BA94B8159E6349ECBF176E7A2987"),
    "avfilter-12.dll": (4350976, "797CFB6FB27DA31CA88F9A59508440CD98CB47290BBBDE54F7C1089077751132"),
    "avformat-63.dll": (2473472, "244505AAFB3B37AE192B15B03FE871965FBE981082379E0C33BC43F3A7611758"),
    "avutil-61.dll": (1143808, "37D6FEC1955060F1FE867A86BDCA968DCFDDAE76E0456C79DD63633E7BEFA2AE"),
    "ffmpeg.exe": (592384, "53CC924AFEFFBE48BD94E569D0081B2EEF64EBAEA18B7966F7686947B0EBC1DC"),
    "ffprobe.exe": (335872, "12324DCCEE8985B2D7F83F59C6BE0617046DF44DD8F25723BB1EECC20ABE7A4A"),
    "swresample-7.dll": (231424, "F7C0DF579F8464534020F4DCA1D870B5BA359C260BBC2450A272C159B16D8772"),
    "swscale-10.dll": (1282560, "C8AD8C480404C68BB7E82A9EEEF35D139FBD65AB632AEB4CE1ADEFEEE8F3281A"),
    "zlib1.dll": (226816, "AB4485B6302F4DEBABA78D70552445CD0A931610C664E7FF1C9639001D9062E3"),
}
FFMPEG_LICENSE_IDENTITIES = {
    "FFmpeg-COPYING.LGPLv2.1": (26517, "246041B6ECF9BC32D718A62C57877C78B5EB397B6467E74ED7AE2626AB189C30"),
    "FFmpeg-COPYING.LGPLv3": (7651, "DA7EABB7BAFDF7D3AE5E9F223AA5BDC1EECE45AC569DC21B3B037520B4464768"),
    "FFmpeg-LICENSE.md": (4346, "2E1D16C72FD74E12063776371DA757322F8B77589386532F4FD8634BDE7DE1AF"),
    "zlib-LICENSE": (1002, "E32FF4E00D9D94930537635291DA39E7E612703334BF6FDE8C7F1686FE8A45A2"),
}
MOVIEPY_PATCH_MANIFEST = "third_party/python_runtime/moviepy-windows-mf-patch.json"
MOVIEPY_PATCHER = "tools/apply_moviepy_windows_mf_patch.py"
MOVIEPY_PATCH_ID = "shiyi-moviepy-windows-mf"
MOVIEPY_PATCH_VERSION = "1.0.0"
MOVIEPY_PATCH_MANIFEST_SHA256 = "A3F347C932956534C746AF7FEDC2C87AFCA55FA3BCAF4C8E4C101DAD8CABE763"
MOVIEPY_PATCHER_SHA256 = "6D307A0D90FDD2D62652704D015D1F4BC42AEE2CF23C115834B91606E464E0C4"
MOVIEPY_DISTRIBUTION_VERSION = "2.2.1"
MOVIEPY_MODULE_REPORTED_VERSION = "2.1.2"
MOVIEPY_PATCHED_WRITER_SHA256 = "DFE76CD8AED151B99881DD01FA2BC1E040D0788EC364A8C6EF14020F2009D8B9"
MOVIEPY_PATCHED_RECORD_SHA256 = "4D836329F0D7804F389AED64FB102A48A614821793F5D7C7E419D11EB7A574C5"
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
PYTHON_RUNTIME_SBOM = "licenses/Python-runtime-SBOM.json"
PYTHON_LICENSE_OVERRIDE_MANIFEST = "third_party/python_runtime/dependency-license-overrides.json"
PYTHON_PRUNED_IMPORT_CONTRACT = "third_party/python_runtime/pruned-import-boundary.json"
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
WINDOWS_TYPICAL_EXTRACT_ROOT = "\\".join(("C:", "Users", "Default", "Downloads"))
WINDOWS_PORTABLE_PATH_BUDGET = 248
ROOT_LAUNCHER_NAME = "启动时宜Agent内容工厂.bat"
STOP_LAUNCHER_NAME = "关闭时宜Agent内容工厂.bat"
MIGRATION_LAUNCHER_NAME = "迁移旧版数据.bat"
INSTALLER_LAUNCHER_NAME = "安装到D盘.bat"
USAGE_NAME = "使用说明.txt"
SKIPPED_DIRECTORY_NAMES = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__", "node_modules"}
)
DEPENDENCY_JUNK_DIRECTORY_NAMES = frozenset({".github", "__tests__", "_tests", "test", "tests"})
DEPENDENCY_LOOSE_TEST_PATHS = frozenset(
    {
        "runtime/python/Lib/site-packages/aiohttp/test_utils.py",
        "runtime/python/Lib/site-packages/annotated_types/test_cases.py",
    }
)
DEPENDENCY_LOOSE_TEST_PATHS_CASEFOLDED = frozenset(
    item.casefold() for item in DEPENDENCY_LOOSE_TEST_PATHS
)
REPO_TREE_ALLOWLIST: dict[str, frozenset[str]] = {
    "core": frozenset({".pyc"}),
    "static": frozenset(
        {".css", ".html", ".js", ".json", ".jpeg", ".jpg", ".lucide", ".png", ".svg", ".webp", ".woff", ".woff2"}
    ),
}


ROOT_FILES = frozenset(
    {
        "app.pyc",
        "LICENSE",
        ROOT_LAUNCHER_NAME,
        STOP_LAUNCHER_NAME,
        MIGRATION_LAUNCHER_NAME,
        INSTALLER_LAUNCHER_NAME,
        USAGE_NAME,
        PACKAGE_MANIFEST,
        CHECKSUMS_FILE,
    }
)
ROOT_DIRECTORIES = frozenset(
    {"core", "docs", "engine", "licenses", "product-assets", "product-tools", "runtime", "scripts", "static", "third_party", "tools"}
)
EXACT_FILES = frozenset(
    {
        "docs/fonts/NotoSansSC-Regular.ttf",
        "docs/fonts/NotoSansSC-Bold.ttf",
        "docs/fonts/OFL.txt",
        "docs/fonts/SOURCE.md",
        "scripts/install_combined.pyc",
        "scripts/launch_combined.pyc",
        "tools/verify_combined_portable.pyc",
        "tools/build_public_evidence.pyc",
        "tools/verify_public_evidence.pyc",
        HYPERFRAMES_UPSTREAM_LICENSE,
        "third_party/hyperframes/README.md",
        HYPERFRAMES_UPSTREAM_LOCK,
        "third_party/hyperframes/windows-mf-patch.json",
        "third_party/hyperframes/dependency-license-overrides.json",
        FFMPEG_UPSTREAM_LOCK,
        "third_party/ffmpeg/licenses/FFmpeg-COPYING.LGPLv2.1",
        "third_party/ffmpeg/licenses/FFmpeg-COPYING.LGPLv3",
        "third_party/ffmpeg/licenses/FFmpeg-LICENSE.md",
        "third_party/ffmpeg/licenses/zlib-LICENSE",
        "third_party/python_runtime/README.md",
        MOVIEPY_PATCH_MANIFEST,
        PYTHON_LICENSE_OVERRIDE_MANIFEST,
        PYTHON_PRUNED_IMPORT_CONTRACT,
        "runtime/python/python.exe",
        "runtime/ffmpeg/ffmpeg.exe",
        "runtime/ffmpeg/ffprobe.exe",
        "licenses/PRODUCT-MIT.txt",
        "licenses/NotoSansSC-OFL.txt",
        FFMPEG_RUNTIME_LOCK_COPY,
        "licenses/FFmpeg-COPYING.LGPLv2.1",
        "licenses/FFmpeg-COPYING.LGPLv3",
        "licenses/FFmpeg-LICENSE.md",
        "licenses/zlib-LICENSE",
        "licenses/Python-license.txt",
        PYTHON_RUNTIME_SBOM,
        "licenses/README.txt",
    }
)
LEGACY_MPT_EXACT_FILES = frozenset(
    {
        "third_party/moneyprinterturbo/LICENSE",
        "third_party/moneyprinterturbo/README.md",
        "third_party/moneyprinterturbo/upstream-lock.json",
        "licenses/MoneyPrinterTurbo-MIT.txt",
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
CUSTOMER_ASSETS = frozenset(
    {
        "product-assets/motion/animation-pack-v1.json",
        "product-assets/motion/composition-template.html",
        "product-assets/motion/composition-template-clean-air-explainer.html",
        "product-assets/motion/composition-template-cinematic.html",
        "product-assets/motion/media/clean-air-device-neutral-v1.png",
        "product-tools/extract_url.pyc",
    }
)
OPTIONAL_EXACT_FILES = frozenset()
MOTION_EXACT_FILES = frozenset(
    {
        "runtime/node/node.exe",
        "runtime/node/LICENSE",
        HYPERFRAMES_RUNTIME_MANIFEST,
        HYPERFRAMES_PACKAGE,
        HYPERFRAMES_CLI,
        "licenses/Node-license.txt",
        "licenses/HyperFrames-Apache-2.0.txt",
        "licenses/HyperFrames-third-party-SBOM.json",
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
        "product-assets": "product_assets",
        "product-tools": "product_runtime_helpers",
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


def _is_redundant_imageio_ffmpeg_executable(relative: str) -> bool:
    path = PurePosixPath(relative)
    parts = tuple(part.casefold() for part in path.parts)
    return path.suffix.casefold() == ".exe" and any(
        parts[index : index + 2] == ("imageio_ffmpeg", "binaries")
        for index in range(max(0, len(parts) - 1))
    )


def _is_locked_mpt_component(relative: str) -> bool:
    prefix = "engine/MoneyPrinterTurbo/"
    if not relative.casefold().startswith(prefix.casefold()):
        return False
    upstream_relative = relative[len(prefix) :].casefold()
    for component in EXPECTED_MPT_EXCLUDED_COMPONENTS:
        normalized_component = component.casefold()
        if upstream_relative != normalized_component and not upstream_relative.startswith(normalized_component + "/"):
            continue
        # The upstream font directory is excluded; this one separately licensed
        # product-owned replacement is the only permitted file at that path.
        if upstream_relative == "resource/fonts/notosanssc-regular.ttf".casefold():
            return False
        return True
    return False


def _is_forbidden_browser_payload(relative: str) -> bool:
    """Reject redistributed Chrome/CfT/Edge payloads in every package profile."""

    path = PurePosixPath(relative)
    parts = tuple(part.casefold() for part in path.parts)
    if len(parts) >= 2 and parts[0] == "runtime" and parts[1] in {
        "browser",
        "chrome",
        "chrome-for-testing",
        "chrome-headless-shell",
        "edge",
        "msedge",
    }:
        return True
    if path.name.casefold() in {
        "chrome.exe",
        "chrome-headless-shell.exe",
        "chromedriver.exe",
        "msedge.exe",
        "msedgedriver.exe",
        "license.headless_shell",
        "chrome-headless-shell-license.txt",
        "chrome-for-testing-license.txt",
    }:
        return True
    return any(
        part in {"chrome-for-testing", "chrome-headless-shell"}
        for part in parts
    )


def _is_mutable_runtime_path(relative: str) -> bool:
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
    if (
        _is_redundant_imageio_ffmpeg_executable(relative)
        or _is_locked_mpt_component(relative)
        or _is_forbidden_browser_payload(relative)
    ):
        return False
    if relative in ROOT_FILES or relative in EXACT_FILES or relative in LEGACY_MPT_EXACT_FILES or relative in CUSTOMER_ASSETS or relative in OPTIONAL_EXACT_FILES:
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
    if relative.startswith("runtime/hyperframes/") and len(parts) >= 3:
        return True
    if relative.startswith("licenses/hyperframes-dependencies/") and len(parts) == 3:
        return path.suffix.casefold() in {".txt", ".md"}
    if relative.startswith("licenses/python-runtime-dependencies/") and len(parts) == 3:
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


def _python_record_package_path(raw: str) -> str:
    value = raw.replace("\\", "/").strip()
    if not value or "\x00" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ValueError(f"Python distribution RECORD 路径不安全：{raw!r}")
    parts = ["runtime", "python", "Lib", "site-packages"]
    for part in PurePosixPath(value).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if len(parts) <= 2:
                raise ValueError(f"Python distribution RECORD 越过运行时根：{raw!r}")
            parts.pop()
            continue
        if ":" in part:
            raise ValueError(f"Python distribution RECORD 含 Windows 绝对路径：{raw!r}")
        parts.append(part)
    relative = "/".join(parts)
    if not _safe_relative(relative) or not relative.startswith("runtime/python/"):
        raise ValueError(f"Python distribution RECORD 路径无效：{raw!r}")
    return relative


def _python_pruned_import_contract(
    read: Callable[[str], bytes],
    errors: list[str],
) -> dict[str, object] | None:
    raw = _parse_json(read, PYTHON_PRUNED_IMPORT_CONTRACT, errors)
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "status",
        "scope",
        "pruned_distributions",
        "retained_optional_references",
        "formal_exceptions",
        "unresolved",
    }:
        errors.append("Python runtime 裁剪静态 import 合同结构无效")
        return None
    if (
        raw.get("schema_version") != 1
        or raw.get("status") != "complete"
        or raw.get("scope") != "portable_python_static_import_boundary"
        or raw.get("unresolved") != []
    ):
        errors.append("Python runtime 裁剪静态 import 合同尚未闭合")

    pruned = raw.get("pruned_distributions")
    if not isinstance(pruned, list):
        errors.append("Python runtime 裁剪 import 模块表不是数组")
        return None
    modules: dict[str, tuple[str, ...]] = {}
    order: list[str] = []
    versions: dict[str, str] = {}
    for item in pruned:
        if not isinstance(item, dict) or set(item) != {"name", "version", "modules"}:
            errors.append("Python runtime 裁剪 import 模块条目无效")
            return None
        name = str(item.get("name", ""))
        version = str(item.get("version", ""))
        values = item.get("modules")
        if (
            name != _normalize_python_distribution_name(name)
            or not isinstance(values, list)
            or not values
            or values != sorted(values)
            or len(values) != len(set(values))
            or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", str(value)) for value in values)
            or name in modules
        ):
            errors.append(f"Python runtime 裁剪 import 模块锁无效：{name}@{version}")
            return None
        modules[name] = tuple(str(value) for value in values)
        versions[name] = version
        order.append(name)
    if order != sorted(order) or versions != PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS:
        errors.append("Python runtime 裁剪 import 模块表与 distribution 锁不一致")
        return None

    retained_raw = raw.get("retained_optional_references")
    if not isinstance(retained_raw, list):
        errors.append("Python runtime 保留包可选 import 合同不是数组")
        return None
    retained: dict[tuple[str, str], dict[str, object]] = {}
    retained_order: list[tuple[str, str]] = []
    reference_fields = {
        "distribution",
        "owner",
        "owner_version",
        "path",
        "imports",
        "reason",
    }
    for item in retained_raw:
        if not isinstance(item, dict) or set(item) != reference_fields:
            errors.append("Python runtime 保留包可选 import 合同条目无效")
            return None
        distribution = str(item.get("distribution", ""))
        owner = str(item.get("owner", ""))
        owner_version = str(item.get("owner_version", ""))
        relative = PurePosixPath(str(item.get("path", "")))
        imports = item.get("imports")
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
            or len(str(item.get("reason", "")).strip()) < 24
            or key in retained
        ):
            errors.append(f"Python runtime 保留包可选 import 合同锁无效：{key}")
            return None
        retained[key] = {
            "owner": owner,
            "owner_version": owner_version,
            "imports": frozenset(str(value) for value in imports),
        }
        retained_order.append(key)
    if retained_order != sorted(retained_order):
        errors.append("Python runtime 保留包可选 import 合同未稳定排序")
        return None

    formal_raw = raw.get("formal_exceptions")
    if not isinstance(formal_raw, list):
        errors.append("Python runtime 正式链 import 例外合同不是数组")
        return None
    formal: dict[tuple[str, str], frozenset[str]] = {}
    formal_order: list[tuple[str, str]] = []
    for item in formal_raw:
        if not isinstance(item, dict) or set(item) != {"distribution", "path", "imports", "reason"}:
            errors.append("Python runtime 正式链 import 例外合同条目无效")
            return None
        distribution = str(item.get("distribution", ""))
        relative = PurePosixPath(str(item.get("path", "")))
        imports = item.get("imports")
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
            or len(str(item.get("reason", "")).strip()) < 24
            or key in formal
        ):
            errors.append(f"Python runtime 正式链 import 例外合同锁无效：{key}")
            return None
        formal[key] = frozenset(str(value) for value in imports)
        formal_order.append(key)
    if formal_order != sorted(formal_order):
        errors.append("Python runtime 正式链 import 例外合同未稳定排序")
        return None
    return {"modules": modules, "retained": retained, "formal": formal}


def _python_static_import_names(
    value: bytes,
    relative: str,
    errors: list[str],
) -> frozenset[str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(value.decode("utf-8-sig"), filename=relative)
    except (UnicodeDecodeError, SyntaxError) as exc:
        errors.append(f"Python 静态 import 扫描无法解析：{relative}: {exc}")
        return frozenset()
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


def _verify_python_pruned_import_boundary(
    names: list[str],
    read: Callable[[str], bytes],
    ownership: dict[str, tuple[str, str]],
    errors: list[str],
) -> None:
    contract = _python_pruned_import_contract(read, errors)
    if contract is None:
        return
    modules = contract["modules"]
    retained = contract["retained"]
    formal = contract["formal"]
    assert isinstance(modules, dict) and isinstance(retained, dict) and isinstance(formal, dict)

    def matches(import_name: str) -> list[str]:
        return [
            distribution
            for distribution, prefixes in modules.items()
            if any(import_name == prefix or import_name.startswith(prefix + ".") for prefix in prefixes)
        ]

    site_prefix = "runtime/python/Lib/site-packages/"
    for relative in sorted(
        (name for name in names if name.startswith(site_prefix) and name.casefold().endswith(".py")),
        key=str.casefold,
    ):
        site_relative = relative.removeprefix(site_prefix)
        for imported in _python_static_import_names(read(relative), relative, errors):
            for distribution in matches(imported):
                expected = retained.get((distribution, site_relative))
                if not isinstance(expected, dict) or imported not in expected["imports"]:
                    errors.append(
                        f"Python 保留包出现未审批的已裁依赖 import：{site_relative} -> {imported}"
                    )
                    continue
                if ownership.get(relative) != (expected["owner"], expected["owner_version"]):
                    errors.append(
                        f"Python 保留包可选 import 的 RECORD 所有者/版本漂移：{site_relative}"
                    )

    for relative in sorted(
        (
            name
            for name in names
            if name.casefold().endswith(".py") and not name.startswith("runtime/python/")
        ),
        key=str.casefold,
    ):
        for imported in _python_static_import_names(read(relative), relative, errors):
            for distribution in matches(imported):
                approved = formal.get((distribution, relative))
                if not isinstance(approved, frozenset) or imported not in approved:
                    errors.append(
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


def _canonical_python_files_sha256(entries: list[dict[str, object]]) -> str:
    value = hashlib.sha256()
    for entry in entries:
        value.update(
            f"{entry['path']}\0{entry['size']}\0{str(entry['sha256']).upper()}\n".encode("utf-8")
        )
    return value.hexdigest().upper()


def _verify_python_license_contract(
    read: Callable[[str], bytes],
    errors: list[str],
) -> dict[tuple[str, str], dict[str, object]]:
    raw = _parse_json(read, PYTHON_LICENSE_OVERRIDE_MANIFEST, errors)
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "status",
        "verified_overrides",
        "unresolved",
    }:
        errors.append("Python runtime 许可证覆盖表结构无效")
        return {}
    if raw.get("schema_version") != 2 or raw.get("status") != "complete" or raw.get("unresolved") != []:
        errors.append("Python runtime 许可证覆盖表尚未闭合")
    entries = raw.get("verified_overrides")
    if not isinstance(entries, list):
        errors.append("Python runtime verified_overrides 不是数组")
        return {}
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
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != expected_fields:
            errors.append("Python runtime 许可证覆盖条目结构无效")
            continue
        name = str(entry.get("name", ""))
        normalized = _normalize_python_distribution_name(name)
        version = str(entry.get("version", "")).strip()
        spdx = str(entry.get("spdx", "")).strip()
        commit = str(entry.get("source_commit", "")).strip().lower()
        source_url = str(entry.get("source_url", "")).strip()
        source_sha = str(entry.get("source_sha256", "")).strip().upper()
        license_sha = str(entry.get("license_sha256", "")).strip().upper()
        license_file = PurePosixPath(str(entry.get("license_file", "")))
        destination = f"licenses/python-runtime-dependencies/{license_file.name}"
        additional = entry.get("additional_evidence")
        key = (normalized, version)
        if (
            name != normalized
            or not version
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]*", version)
            or not spdx
            or not str(entry.get("source_repository", "")).startswith("https://github.com/")
            or not str(entry.get("source_tag", "")).strip()
            or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or not source_url.startswith("https://raw.githubusercontent.com/")
            or f"/{commit}/" not in source_url
            or not re.fullmatch(r"[0-9A-F]{64}", source_sha)
            or source_sha != license_sha
            or license_file.is_absolute()
            or len(license_file.parts) != 2
            or license_file.parts[0] != "dependency-licenses"
            or any(part in ("", ".", "..") for part in license_file.parts)
            or key in parsed
            or destination.casefold() in destinations
            or not isinstance(additional, list)
        ):
            errors.append(f"Python runtime 许可证覆盖锁无效：{name}@{version}")
            continue
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
        normalized_additional: list[dict[str, object]] = []
        valid_additional = True
        for supplement in additional:
            if not isinstance(supplement, dict) or set(supplement) != additional_fields:
                valid_additional = False
                break
            bound_runtime = PurePosixPath(str(supplement.get("bound_runtime_path", "")))
            encoded_file = PurePosixPath(str(supplement.get("encoded_file", "")))
            evidence_file = PurePosixPath(str(supplement.get("evidence_file", "")))
            bound_sha = str(supplement.get("bound_runtime_sha256", "")).strip().upper()
            encoded_sha = str(supplement.get("encoded_sha256", "")).strip().upper()
            evidence_sha = str(supplement.get("evidence_sha256", "")).strip().upper()
            identity_sha = str(supplement.get("component_identity_sha256", "")).strip().upper()
            component_count = supplement.get("component_count")
            dependency_count = supplement.get("dependency_count")
            evidence_size = supplement.get("evidence_size")
            evidence_destination = f"licenses/python-runtime-dependencies/{evidence_file.name}"
            if (
                supplement.get("kind") not in {"license", "notice", "redistribution"}
                or supplement.get("source_kind") != "embedded-runtime-sbom-derived-license-corpus"
                or bound_runtime.is_absolute()
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
                or encoded_file.is_absolute()
                or len(encoded_file.parts) != 2
                or encoded_file.parts[0] != "dependency-licenses"
                or any(part in ("", ".", "..") for part in encoded_file.parts)
                or not re.fullmatch(r"[0-9A-F]{64}", encoded_sha)
                or supplement.get("encoding") != "base64+gzip"
                or evidence_file.is_absolute()
                or len(evidence_file.parts) != 2
                or evidence_file.parts[0] != "dependency-licenses"
                or any(part in ("", ".", "..") for part in evidence_file.parts)
                or not re.fullmatch(r"[0-9A-F]{64}", evidence_sha)
                or not isinstance(evidence_size, int)
                or isinstance(evidence_size, bool)
                or evidence_size < 1
                or evidence_destination.casefold() == destination.casefold()
                or evidence_destination.casefold() in destinations
            ):
                valid_additional = False
                break
            normalized_supplement = dict(supplement)
            normalized_supplement["bound_runtime_sha256"] = bound_sha
            normalized_supplement["encoded_sha256"] = encoded_sha
            normalized_supplement["evidence_sha256"] = evidence_sha
            normalized_supplement["component_identity_sha256"] = identity_sha
            normalized_additional.append(normalized_supplement)
            destinations.add(evidence_destination.casefold())
        if not valid_additional or normalized_additional != sorted(
            normalized_additional, key=lambda item: str(item["evidence_file"]).casefold()
        ):
            errors.append(f"Python runtime 补充许可证证据锁无效：{name}@{version}")
            continue
        normalized_entry = dict(entry)
        normalized_entry["source_commit"] = commit
        normalized_entry["source_sha256"] = source_sha
        normalized_entry["license_sha256"] = license_sha
        normalized_entry["additional_evidence"] = normalized_additional
        parsed[key] = normalized_entry
        order.append(key)
        destinations.add(destination.casefold())
    if order != sorted(order, key=lambda item: (item[0], item[1].casefold())):
        errors.append("Python runtime 许可证覆盖表未稳定排序")
    return parsed


def _verify_python_runtime_sbom(
    names: list[str],
    read: Callable[[str], bytes],
    size: Callable[[str], int],
    digest: Callable[[str], str],
    *,
    formal: bool,
) -> list[str]:
    errors: list[str] = []
    sbom = _parse_json(read, PYTHON_RUNTIME_SBOM, errors)
    overrides = _verify_python_license_contract(read, errors)
    expected_top_fields = {
        "schema_version",
        "runtime_kind",
        "platform",
        "site_packages",
        "pruned_distributions",
        "distribution_count",
        "site_packages_file_count",
        "owned_file_count",
        "project_verified_license_overrides",
        "payload_sha256",
        "distributions",
    }
    if not isinstance(sbom, dict) or set(sbom) != expected_top_fields:
        return errors + ["Python runtime SBOM 结构无效"]
    fixed = {
        "schema_version": 2,
        "runtime_kind": "python_distribution_closure",
        "platform": "win32-x64",
        "site_packages": "runtime/python/Lib/site-packages",
    }
    if any(sbom.get(key) != value for key, value in fixed.items()):
        errors.append("Python runtime SBOM 固定边界无效")
    expected_pruned = [
        {
            "name": name,
            "version": version,
            "reason": "excluded UI/LLM/Whisper roots and metadata-proven orphan dependencies",
        }
        for name, version in sorted(PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS.items())
    ]
    if sbom.get("pruned_distributions") != expected_pruned:
        errors.append("Python runtime SBOM 受控裁剪锁不一致")
    distributions = sbom.get("distributions")
    if not isinstance(distributions, list):
        return errors + ["Python runtime SBOM distributions 不是数组"]
    expected_distribution_fields = {
        "name",
        "normalized_name",
        "version",
        "dist_info",
        "license",
        "license_metadata",
        "license_evidence_source",
        "license_evidence",
        "override",
        "modification",
        "payload_sha256",
        "files",
    }
    actual_metadata_paths = sorted(
        (
            name
            for name in names
            if re.fullmatch(r"runtime/python/Lib/site-packages/[^/]+\.dist-info/METADATA", name)
        ),
        key=str.casefold,
    )
    actual_site_files = {
        name for name in names if name.startswith("runtime/python/Lib/site-packages/")
    }
    for metadata_path in actual_metadata_paths:
        try:
            metadata = BytesParser().parsebytes(read(metadata_path))
            actual_name = _normalize_python_distribution_name(str(metadata.get("Name", "") or ""))
        except Exception:
            continue
        if actual_name in PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS:
            errors.append(f"Python runtime 受控裁剪 distribution 元数据仍有残留：{actual_name}")
    seen_keys: set[tuple[str, str]] = set()
    seen_dist_info: set[str] = set()
    owned_paths: dict[str, tuple[str, str]] = {}
    used_overrides: set[tuple[str, str]] = set()
    override_evidence_paths: set[str] = set()
    closure_items: list[tuple[str, str, str]] = []
    moviepy_modification_seen = False
    for item in distributions:
        if not isinstance(item, dict) or set(item) != expected_distribution_fields:
            errors.append("Python runtime SBOM distribution 条目结构无效")
            continue
        name = str(item.get("name", ""))
        normalized_name = str(item.get("normalized_name", ""))
        version = str(item.get("version", ""))
        key = (normalized_name, version)
        dist_info = str(item.get("dist_info", ""))
        metadata_path = f"{dist_info}/METADATA"
        record_path = f"{dist_info}/RECORD"
        if (
            normalized_name != _normalize_python_distribution_name(name)
            or not name
            or not version
            or key in seen_keys
            or dist_info in seen_dist_info
            or not re.fullmatch(r"runtime/python/Lib/site-packages/[^/]+\.dist-info", dist_info)
            or metadata_path not in names
            or record_path not in names
        ):
            errors.append(f"Python runtime SBOM distribution 身份无效：{name}@{version}")
            continue
        if normalized_name in PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS:
            errors.append(f"Python runtime 受控裁剪 distribution 仍有残留：{name}@{version}")
            continue
        seen_keys.add(key)
        seen_dist_info.add(dist_info)
        try:
            metadata = BytesParser().parsebytes(read(metadata_path))
            actual_name = str(metadata.get("Name", "") or "").strip()
            actual_version = str(metadata.get("Version", "") or "").strip()
            metadata_license, license_metadata = _python_license_metadata(metadata)
        except Exception as exc:
            errors.append(f"Python distribution METADATA 无法验证：{name}@{version}: {exc}")
            continue
        if (actual_name, actual_version) != (name, version) or item.get("license_metadata") != license_metadata:
            errors.append(f"Python runtime SBOM 与 METADATA 身份/许可证字段不一致：{name}@{version}")
        try:
            rows = list(csv.reader(io.StringIO(_decode_text(read(record_path)))))
        except csv.Error as exc:
            errors.append(f"Python distribution RECORD 无法解析：{name}@{version}: {exc}")
            continue
        record_present: set[str] = set()
        for row in rows:
            if len(row) != 3:
                errors.append(f"Python distribution RECORD 条目无效：{name}@{version}")
                continue
            try:
                relative = _python_record_package_path(row[0])
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if relative not in names:
                continue
            if relative in owned_paths:
                errors.append(f"Python runtime 文件被多个 distribution 声明：{relative}")
                continue
            owned_paths[relative] = key
            record_present.add(relative)
        files = item.get("files")
        if not isinstance(files, list):
            errors.append(f"Python runtime SBOM files 不是数组：{name}@{version}")
            continue
        file_paths: list[str] = []
        valid_files: list[dict[str, object]] = []
        for entry in files:
            if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
                errors.append(f"Python runtime SBOM 含非法文件条目：{name}@{version}")
                continue
            relative = str(entry.get("path", ""))
            file_paths.append(relative)
            valid_files.append(entry)
            if not _safe_relative(relative) or not relative.startswith("runtime/python/") or relative not in names:
                errors.append(f"Python runtime SBOM 引用不存在或不安全文件：{relative}")
                continue
            if entry.get("size") != size(relative) or str(entry.get("sha256", "")).upper() != digest(relative):
                errors.append(f"Python runtime SBOM 文件哈希或大小不一致：{relative}")
        if file_paths != sorted(file_paths, key=str.casefold) or len(file_paths) != len(set(file_paths)):
            errors.append(f"Python runtime SBOM 文件列表未排序或重复：{name}@{version}")
        if set(file_paths) != record_present:
            errors.append(f"Python runtime SBOM 与实际 RECORD 文件集合不一致：{name}@{version}")
        payload_sha = _canonical_python_files_sha256(valid_files)
        if item.get("payload_sha256") != payload_sha:
            errors.append(f"Python runtime distribution payload SHA-256 不一致：{name}@{version}")
        modification = item.get("modification")
        if normalized_name == "moviepy" and formal:
            moviepy_modification_seen = True
            expected_modification = {
                "modified": True,
                "patch_manifest": MOVIEPY_PATCH_MANIFEST,
                "patch_manifest_sha256": MOVIEPY_PATCH_MANIFEST_SHA256,
                "patch_id": MOVIEPY_PATCH_ID,
                "patch_version": MOVIEPY_PATCH_VERSION,
                "module_reported_version": MOVIEPY_MODULE_REPORTED_VERSION,
                "patcher_sha256": MOVIEPY_PATCHER_SHA256,
                "writer_sha256": MOVIEPY_PATCHED_WRITER_SHA256,
                "record_sha256": MOVIEPY_PATCHED_RECORD_SHA256,
                "record_consistent": True,
            }
            writer_path = "runtime/python/Lib/site-packages/moviepy/video/io/ffmpeg_writer.py"
            if (
                version != MOVIEPY_DISTRIBUTION_VERSION
                or modification != expected_modification
                or writer_path not in names
                or digest(writer_path) != MOVIEPY_PATCHED_WRITER_SHA256
                or digest(record_path) != MOVIEPY_PATCHED_RECORD_SHA256
                or [row for row in rows if row and row[0] == "moviepy/video/io/ffmpeg_writer.py"]
                != [
                    [
                        "moviepy/video/io/ffmpeg_writer.py",
                        "sha256=3-ds2K7RUbmYgd0B-ivB4EDQeI7DZKjG7xQCDyAJ2Lk",
                        "12362",
                    ]
                ]
            ):
                errors.append("MoviePy distribution SBOM、writer 与 RECORD 补丁身份不一致")
        elif modification is not None:
            errors.append(f"Python runtime 非正式或非 MoviePy distribution 含未授权修改声明：{name}@{version}")
        embedded_evidence = []
        embedded_has_license = False
        for entry in valid_files:
            kind = _python_license_evidence_kind(str(entry.get("path", "")))
            if kind is None:
                continue
            embedded_has_license = embedded_has_license or kind == "license"
            embedded_evidence.append({**entry, "kind": kind})
        evidence = item.get("license_evidence")
        if not isinstance(evidence, list):
            errors.append(f"Python runtime license_evidence 不是数组：{name}@{version}")
            evidence = []
        for entry in evidence:
            if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256", "kind"}:
                errors.append(f"Python runtime 许可证证据条目无效：{name}@{version}")
                continue
            relative = str(entry.get("path", ""))
            if (
                entry.get("kind") not in {"license", "notice", "redistribution"}
                or relative not in names
                or entry.get("size") != size(relative)
                or str(entry.get("sha256", "")).upper() != digest(relative)
            ):
                errors.append(f"Python runtime 许可证证据不存在或哈希不一致：{relative}")
        if embedded_has_license:
            expected_evidence = embedded_evidence
            if (
                item.get("license_evidence_source") != "installed_distribution"
                or item.get("override") is not None
                or item.get("license") != metadata_license
            ):
                errors.append(f"Python runtime 内嵌许可证来源声明不一致：{name}@{version}")
        else:
            override = overrides.get(key)
            source = item.get("override")
            if override is None or source != override or key in used_overrides:
                errors.append(f"Python runtime 缺正文 distribution 的精确覆盖无效：{name}@{version}")
                expected_evidence = embedded_evidence
            else:
                used_overrides.add(key)
                evidence_name = PurePosixPath(str(override["license_file"])).name
                evidence_path = f"licenses/python-runtime-dependencies/{evidence_name}"
                override_evidence_paths.add(evidence_path)
                expected_evidence = [
                    *embedded_evidence,
                    {
                        "path": evidence_path,
                        "size": size(evidence_path) if evidence_path in names else -1,
                        "sha256": str(override["license_sha256"]),
                        "kind": "license",
                    },
                ]
                additional = override.get("additional_evidence", [])
                if not isinstance(additional, list):
                    additional = []
                    errors.append(f"Python runtime 补充许可证证据不是数组：{name}@{version}")
                for supplement in additional:
                    if not isinstance(supplement, dict):
                        errors.append(f"Python runtime 补充许可证证据结构无效：{name}@{version}")
                        continue
                    supplemental_name = PurePosixPath(str(supplement["evidence_file"])).name
                    supplemental_path = f"licenses/python-runtime-dependencies/{supplemental_name}"
                    bound_path = f"runtime/python/{supplement['bound_runtime_path']}"
                    override_evidence_paths.add(supplemental_path)
                    expected_evidence.append(
                        {
                            "path": supplemental_path,
                            "size": supplement["evidence_size"],
                            "sha256": supplement["evidence_sha256"],
                            "kind": supplement["kind"],
                        }
                    )
                    if (
                        bound_path not in record_present
                        or bound_path not in names
                        or digest(bound_path) != supplement["bound_runtime_sha256"]
                        or supplemental_path not in names
                        or size(supplemental_path) != supplement["evidence_size"]
                        or digest(supplemental_path) != supplement["evidence_sha256"]
                    ):
                        errors.append(f"Python runtime 补充许可证证据文件/绑定哈希不一致：{name}@{version}")
                        continue
                    try:
                        sbom_identities, dependency_count, identity_sha = _python_native_sbom_identity(
                            read(bound_path)
                        )
                        corpus_identities = _python_license_corpus_identities(read(supplemental_path))
                    except ValueError as exc:
                        errors.append(f"Python runtime native SBOM/corpus 无法验证：{name}@{version}: {exc}")
                        continue
                    if (
                        sbom_identities != corpus_identities
                        or len(sbom_identities) != supplement["component_count"]
                        or dependency_count != supplement["dependency_count"]
                        or identity_sha != supplement["component_identity_sha256"]
                    ):
                        errors.append(f"Python runtime native SBOM 与许可证 corpus 未逐组件闭合：{name}@{version}")
                if (
                    item.get("license_evidence_source") != "project_verified_override"
                    or item.get("license") != override.get("spdx")
                    or evidence_path not in names
                    or digest(evidence_path) != override.get("license_sha256")
                ):
                    errors.append(f"Python runtime 精确版本许可证证据不一致：{name}@{version}")
        if evidence != expected_evidence:
            errors.append(f"Python runtime 许可证/NOTICE/REDIST 证据集合不完整：{name}@{version}")
        closure_items.append((normalized_name, version, payload_sha))
    if formal:
        if not moviepy_modification_seen:
            errors.append("motion_primary Python runtime 缺少正式 MoviePy Windows-MF 修改身份")
        if (
            digest(MOVIEPY_PATCH_MANIFEST) != MOVIEPY_PATCH_MANIFEST_SHA256
        ):
            errors.append("MoviePy Windows-MF patch manifest 或 patcher SHA-256 不一致")
    expected_order = sorted(seen_keys, key=lambda value: (value[0], value[1].casefold()))
    actual_order = [
        (str(item.get("normalized_name", "")), str(item.get("version", "")))
        for item in distributions
        if isinstance(item, dict)
    ]
    if actual_order != expected_order:
        errors.append("Python runtime SBOM distributions 未稳定排序")
    expected_metadata_paths = sorted((f"{path}/METADATA" for path in seen_dist_info), key=str.casefold)
    if actual_metadata_paths != expected_metadata_paths:
        errors.append("Python runtime SBOM 与实际 dist-info 集合不一致")
    if actual_site_files != {path for path in owned_paths if path.startswith("runtime/python/Lib/site-packages/")}:
        errors.append("Python site-packages 存在 SBOM/RECORD 未归属文件")
    actual_override_evidence = {
        name for name in names if name.startswith("licenses/python-runtime-dependencies/")
    }
    if actual_override_evidence != override_evidence_paths or used_overrides != set(overrides):
        errors.append("Python runtime 精确版本覆盖与实际许可证证据未双向闭合")
    _verify_python_pruned_import_boundary(names, read, owned_paths, errors)
    closure = hashlib.sha256()
    for normalized_name, version, payload_sha in closure_items:
        closure.update(f"{normalized_name}\0{version}\0{payload_sha}\n".encode("utf-8"))
    if (
        sbom.get("distribution_count") != len(distributions)
        or sbom.get("site_packages_file_count") != len(actual_site_files)
        or sbom.get("owned_file_count") != len(owned_paths)
        or sbom.get("project_verified_license_overrides") != len(used_overrides)
        or sbom.get("payload_sha256") != closure.hexdigest().upper()
    ):
        errors.append("Python runtime SBOM 计数或闭包哈希不一致")
    return errors


def _is_amd64_pe(payload: bytes) -> bool:
    if len(payload) < 0x100 or payload[:2] != b"MZ":
        return False
    pe_offset = int.from_bytes(payload[0x3C:0x40], "little")
    if pe_offset < 0x40 or pe_offset + 26 > len(payload):
        return False
    return (
        payload[pe_offset : pe_offset + 4] == b"PE\0\0"
        and int.from_bytes(payload[pe_offset + 4 : pe_offset + 6], "little") == 0x8664
        and int.from_bytes(payload[pe_offset + 24 : pe_offset + 26], "little") == 0x20B
    )


def _verify_ffmpeg_distribution(
    names: list[str],
    read: Callable[[str], bytes],
    size: Callable[[str], int],
    digest: Callable[[str], str],
    *,
    formal: bool,
) -> list[str]:
    errors: list[str] = []
    lock = _parse_json(read, FFMPEG_UPSTREAM_LOCK, errors)
    lock_copy = _parse_json(read, FFMPEG_RUNTIME_LOCK_COPY, errors)
    if lock != lock_copy:
        errors.append("FFmpeg 发布锁副本与项目上游锁不一致")
    if not isinstance(lock, dict):
        return errors

    runtime_files = {
        name.removeprefix("runtime/ffmpeg/")
        for name in names
        if name.startswith("runtime/ffmpeg/")
    }
    if formal and runtime_files != set(FFMPEG_RUNTIME_FILES):
        errors.append("FFmpeg 正式 runtime 不是精确九文件集合")
    locked_runtime = lock.get("runtime")
    locked_entries = locked_runtime.get("files") if isinstance(locked_runtime, dict) else None
    parsed_entries: dict[str, tuple[int, str]] = {}
    if not isinstance(locked_entries, list):
        errors.append("FFmpeg 发布锁缺少 runtime.files")
    else:
        for entry in locked_entries:
            if not isinstance(entry, dict):
                errors.append("FFmpeg 发布锁 runtime.files 含非法条目")
                continue
            file_name = entry.get("name")
            byte_count = entry.get("bytes")
            sha256 = str(entry.get("sha256", "")).upper()
            if (
                not isinstance(file_name, str)
                or PurePosixPath(file_name).name != file_name
                or not isinstance(byte_count, int)
                or not re.fullmatch(r"[0-9A-F]{64}", sha256)
                or file_name in parsed_entries
            ):
                errors.append("FFmpeg 发布锁 runtime.files 身份无效或重复")
                continue
            parsed_entries[file_name] = (byte_count, sha256)
    if formal and parsed_entries != FFMPEG_RUNTIME_FILES:
        errors.append("FFmpeg 发布锁未固定到审核通过的九文件哈希")

    if formal:
        for file_name, (expected_size, expected_sha) in FFMPEG_RUNTIME_FILES.items():
            relative = f"runtime/ffmpeg/{file_name}"
            if relative not in names:
                continue
            payload = read(relative)
            if size(relative) != expected_size or digest(relative) != expected_sha:
                errors.append(f"FFmpeg runtime 文件大小或 SHA-256 不一致：{file_name}")
            if not _is_amd64_pe(payload):
                errors.append(f"FFmpeg runtime 文件不是 AMD64 PE32+：{file_name}")

        for file_name, (expected_size, expected_sha) in FFMPEG_LICENSE_IDENTITIES.items():
            source = f"third_party/ffmpeg/licenses/{file_name}"
            copied = f"licenses/{file_name}"
            for relative in (source, copied):
                if relative in names and (size(relative) != expected_size or digest(relative) != expected_sha):
                    errors.append(f"FFmpeg/zlib 许可证正文身份不一致：{relative}")
            if source in names and copied in names and digest(source) != digest(copied):
                errors.append(f"FFmpeg/zlib 许可证副本不一致：{copied}")

    build = lock.get("build")
    source_companion = lock.get("source_companion")
    capabilities = lock.get("capabilities")
    release_contract = lock.get("release_contract")
    configure_flags = build.get("configure_flags") if isinstance(build, dict) else None
    source_entries = build.get("sources") if isinstance(build, dict) else None
    formal_identity_ok = (
        lock.get("schema_version") == 2
        and lock.get("distribution_status") == "release_ready_source_companion_frozen"
        and lock.get("license") == "LGPL-2.1-or-later"
        and isinstance(build, dict)
        and build.get("ffmpeg_version") == "8.0.git"
        and build.get("ffmpeg_commit") == "d3ad8a7fee6a647c6362e4a105d949282d50a98f"
        and build.get("target") == "windows-x86_64-msvc-shared"
        and isinstance(configure_flags, list)
        and "--enable-encoder=h264_mf" in configure_flags
        and "--disable-network" in configure_flags
        and "--disable-avdevice" in configure_flags
        and not any(re.search(r"(?i)(?:enable-gpl|x264|x265|openh264)", str(flag)) for flag in configure_flags)
        and isinstance(source_entries, list)
        and [
            (item.get("id"), item.get("bytes"), str(item.get("sha256", "")).upper())
            for item in source_entries
            if isinstance(item, dict)
        ]
        == [
            ("ffmpeg", 17492386, "6F7B70D14DBF30B14C2DD78423B289FDCEEF04E22D3BA7201FFB12066A6EC53B"),
            ("zlib", 1502830, "BB329A0A2CD0274D05519D61C667C062E06990D72E125EE2DFA8DE64F0119D16"),
        ]
        and isinstance(source_companion, dict)
        and source_companion.get("status") == "frozen"
        and source_companion.get("name") == "ShiyiContentFactory-v0.3.0-FFmpeg-LGPL-source-d3ad8a7.zip"
        and source_companion.get("bytes") == 19314160
        and str(source_companion.get("sha256", "")).upper()
        == "A09A28824F6C5EBBFC8CF724136701FA6ADFE7F35BA58670A85F48A9CA856C08"
        and isinstance(capabilities, dict)
        and capabilities.get("status") == "passed"
        and isinstance(capabilities.get("canary_contract"), dict)
        and capabilities["canary_contract"].get("encoder") == H264_CODEC_STRATEGY
        and isinstance(release_contract, dict)
        and release_contract.get("required_source_asset") == source_companion.get("name")
    )
    if formal and not formal_identity_ok:
        errors.append("FFmpeg LGPL 构建、源码伴随包或能力身份未匹配正式锁")
    return errors


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
        "codec_strategy",
        "patch_id",
        "patch_version",
        "upstream_cli_sha256",
        "patched_cli_sha256",
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
        "schema_version": 2,
        "runtime_kind": "hyperframes_node_modules_closure",
        "platform": "win32-x64",
        "entry": "node_modules/hyperframes/bin/hyperframes.mjs",
        "hyperframes_version": expected_version,
        "codec_strategy": H264_CODEC_STRATEGY,
        "patch_id": HYPERFRAMES_PATCH_ID,
        "patch_version": HYPERFRAMES_PATCH_VERSION,
        "upstream_cli_sha256": HYPERFRAMES_UPSTREAM_CLI_SHA256,
        "patched_cli_sha256": HYPERFRAMES_PATCHED_CLI_SHA256,
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
    patched_cli = "runtime/hyperframes/node_modules/hyperframes/dist/cli.js"
    if patched_cli not in names or digest(patched_cli) != HYPERFRAMES_PATCHED_CLI_SHA256:
        errors.append("HyperFrames staging CLI 未匹配固定 Windows-MF 补丁哈希")
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
        if _is_redundant_imageio_ffmpeg_executable(relative):
            errors.append(f"便携 Python 含多余的 imageio_ffmpeg 内置 FFmpeg 可执行文件：{relative}")
        if _is_locked_mpt_component(relative):
            errors.append(f"MoneyPrinterTurbo 包含上游锁明确排除的组件：{relative}")
        if _is_forbidden_browser_payload(relative):
            errors.append(f"便携包不得再分发 Chrome、Chrome for Testing 或 Edge 浏览器 payload：{relative}")
        parts = PurePosixPath(relative).parts
        if parts[0] not in ROOT_FILES and parts[0] not in ROOT_DIRECTORIES:
            errors.append(f"包内出现非白名单顶层路径：{relative}")
        forbidden_parts = SKIPPED_DIRECTORY_NAMES - (
            {"node_modules"} if relative.startswith("runtime/hyperframes/") else set()
        )
        if any(part.casefold() in forbidden_parts for part in parts):
            errors.append(f"包内出现缓存、测试依赖或版本库目录：{relative}")
        if relative.startswith(("runtime/python/", "runtime/hyperframes/")) and any(
            part.casefold() in DEPENDENCY_JUNK_DIRECTORY_NAMES for part in parts[2:-1]
        ):
            errors.append(f"customer runtime contains upstream development/test directory: {relative}")
        if relative.startswith(("runtime/python/", "runtime/hyperframes/")):
            dependency_name = PurePosixPath(relative).name.casefold()
            if (
                relative.casefold() in DEPENDENCY_LOOSE_TEST_PATHS_CASEFOLDED
                or dependency_name == "conftest.py"
                or re.fullmatch(r".+\.(?:test|spec)\.(?:js|cjs|mjs|jsx|ts|tsx)", dependency_name)
                or re.fullmatch(r"test[-_].+\.(?:js|cjs|mjs|ts)", dependency_name)
            ):
                errors.append(f"customer runtime contains upstream loose test file: {relative}")
        if relative.startswith(("app.", "core/", "scripts/", "tools/", "product-tools/")) and PurePosixPath(relative).suffix.casefold() == ".py":
            errors.append(f"customer package contains first-party Python source: {relative}")
        if PurePosixPath(relative).name.casefold() in SECRET_FILE_NAMES:
            errors.append(f"包内出现秘密或 Cookie 文件：{relative}")
        if not _allowed_file(relative):
            errors.append(f"包内出现非白名单文件：{relative}")

    required = ROOT_FILES | EXACT_FILES | CUSTOMER_ASSETS
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
    schema_version = manifest.get("schema_version")
    motion_package = schema_version in {2, 3}
    customer_motion = schema_version == 3
    expected_keys = legacy_keys | ({"package_profile", "motion_runtime"} if motion_package else set())
    if customer_motion:
        expected_keys = (expected_keys - {"materials"}) | {"customer_distribution"}
    if set(manifest) != expected_keys:
        errors.append("PACKAGE-MANIFEST.json 顶层字段不符合固定协议")
    if not customer_motion:
        missing_legacy = sorted(LEGACY_MPT_EXACT_FILES - set(names), key=str.casefold)
        if missing_legacy:
            errors.append(f"组合包缺少旧式实拍引擎合同文件：{missing_legacy}")
    if schema_version not in {1, 2, 3} or manifest.get("version") != PACKAGE_VERSION:
        errors.append("PACKAGE-MANIFEST.json 版本不正确")
    if manifest.get("package_kind") != "windows_x64_combined_portable":
        errors.append("PACKAGE-MANIFEST.json 包类型不正确")
    source = manifest.get("source")
    expected_source_keys = (
        {"repository_commit"}
        if customer_motion
        else {
            "repository_commit",
            "moneyprinterturbo_version",
            "moneyprinterturbo_commit",
            "mpt_payload_sha256",
        }
    )
    if not isinstance(source, dict) or set(source) != expected_source_keys:
        errors.append("PACKAGE-MANIFEST.json 来源结构不正确")
    else:
        if not re.fullmatch(r"[0-9a-f]{40}", str(source.get("repository_commit", ""))):
            errors.append("PACKAGE-MANIFEST.json 缺少完整项目提交哈希")
        if not customer_motion and (source.get("moneyprinterturbo_version") != EXPECTED_MPT_VERSION or source.get("moneyprinterturbo_commit") != EXPECTED_MPT_COMMIT):
            errors.append("PACKAGE-MANIFEST.json 的 MoneyPrinterTurbo 锁不正确")
    expected_runtime = {
        "shared_python": "runtime/python/python.exe",
        "workbench_entry": "app.pyc",
        "ffmpeg": "runtime/ffmpeg/ffmpeg.exe",
        "ffprobe": "runtime/ffmpeg/ffprobe.exe",
        "runtime_downloads_allowed": False,
        "payload_sha256": "",
    }
    if not customer_motion:
        expected_runtime["moneyprinterturbo_entry"] = "engine/MoneyPrinterTurbo/app/asgi.py"
    if motion_package:
        expected_runtime.update(
            {
                "video_codec": H264_CODEC_STRATEGY,
                "moviepy_patch": {
                    "manifest": MOVIEPY_PATCH_MANIFEST,
                    "manifest_sha256": MOVIEPY_PATCH_MANIFEST_SHA256,
                    "patch_id": MOVIEPY_PATCH_ID,
                    "patch_version": MOVIEPY_PATCH_VERSION,
                    "distribution_version": MOVIEPY_DISTRIBUTION_VERSION,
                    "module_reported_version": MOVIEPY_MODULE_REPORTED_VERSION,
                    "writer_sha256": MOVIEPY_PATCHED_WRITER_SHA256,
                    "record_sha256": MOVIEPY_PATCHED_RECORD_SHA256,
                    "record_consistent": True,
                },
                "ffmpeg_distribution": {
                    "lock": FFMPEG_RUNTIME_LOCK_COPY,
                    "license": "LGPL-2.1-or-later",
                    "runtime_file_count": 9,
                    "ffmpeg_version": "8.0.git",
                    "ffmpeg_commit": "d3ad8a7fee6a647c6362e4a105d949282d50a98f",
                    "source_companion_name": "ShiyiContentFactory-v0.3.0-FFmpeg-LGPL-source-d3ad8a7.zip",
                    "source_companion_sha256": "A09A28824F6C5EBBFC8CF724136701FA6ADFE7F35BA58670A85F48A9CA856C08",
                },
            }
        )
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict) or set(runtime) != set(expected_runtime) or any(
        runtime.get(key) != value for key, value in expected_runtime.items() if key != "payload_sha256"
    ):
        errors.append("PACKAGE-MANIFEST.json 运行时合同不正确")
    expected_mutable = {
        "user_data_root": "%LOCALAPPDATA%/ShiyiContentFactory/UserData",
        "launcher_state_root": "%LOCALAPPDATA%/ShiyiContentFactory/Launcher",
        "package_runtime_mutable": False,
        "executable_files_allowed": False,
    }
    if not customer_motion:
        expected_mutable.update(
            {
                "moneyprinterturbo_root": "engine/MoneyPrinterTurbo/storage",
                "moneyprinterturbo_immutable_children": ["local_videos"],
            }
        )
    if manifest.get("mutable_state") != expected_mutable:
        errors.append("PACKAGE-MANIFEST.json 可变运行状态边界不正确")
    if manifest.get("network") != {"listen_host": "127.0.0.1", "public_cloud_service": False}:
        errors.append("PACKAGE-MANIFEST.json 本机监听合同不正确")
    if customer_motion:
        expected_distribution = {
            "audience": "external_customer",
            "first_party_python_source_included": False,
            "internal_diagnostics_included": False,
            "footage_engine_included": False,
            "runtime_downloads_allowed": False,
        }
        if manifest.get("customer_distribution") != expected_distribution:
            errors.append("客户发行净化合同不正确")
        forbidden_prefixes = (
            "agent-skills/",
            "catalog/",
            "examples/",
            "engine/",
            "third_party/moneyprinterturbo/",
        )
        if any(name.startswith(forbidden_prefixes) for name in names):
            errors.append("客户纯动画包混入内部能力目录、示例或实拍引擎")
        if "licenses/MoneyPrinterTurbo-MIT.txt" in names:
            errors.append("客户纯动画包混入未分发实拍引擎许可证")
        customer_static_tokens = (
            "CUSTOMER_BUILD_STRIP_",
            "agent_test",
            "Codex",
            "agentTestReview",
            "internalReviewUi",
            "自动化测试代理",
            "测试代理",
            "测试成片",
            "内部诊断",
            "内部能力目录",
        )
        for relative in ("static/index.html", "static/app.js"):
            text = _decode_text(read(relative))
            if any(token.casefold() in text.casefold() for token in customer_static_tokens):
                errors.append(f"客户静态界面仍包含内部测试或构建内容：{relative}")
    motion_runtime = manifest.get("motion_runtime")
    if motion_package:
        expected_motion_keys = {
            "mode",
            "node",
            "node_version",
            "hyperframes_cli",
            "hyperframes_version",
            "closure_manifest",
            "codec_strategy",
            "hyperframes_patch_id",
            "hyperframes_patch_version",
            "hyperframes_patched_cli_sha256",
            "browser_strategy",
            "browser_minimum_major",
            "system_browser_required",
            "runtime_downloads_allowed",
            "startup_canary_required",
            "payload_sha256",
        }
        if manifest.get("package_profile") != "motion_primary":
            errors.append("motion schema 必须声明 motion_primary profile")
        if not isinstance(motion_runtime, dict) or set(motion_runtime) != expected_motion_keys:
            errors.append("PACKAGE-MANIFEST.json 离线动画运行时合同不正确")
        elif isinstance(motion_runtime, dict):
            fixed = {
                "mode": "offline_bundled_with_system_browser",
                "node": "runtime/node/node.exe",
                "hyperframes_cli": HYPERFRAMES_CLI,
                "closure_manifest": HYPERFRAMES_RUNTIME_MANIFEST,
                "codec_strategy": H264_CODEC_STRATEGY,
                "hyperframes_patch_id": HYPERFRAMES_PATCH_ID,
                "hyperframes_patch_version": HYPERFRAMES_PATCH_VERSION,
                "hyperframes_patched_cli_sha256": HYPERFRAMES_PATCHED_CLI_SHA256,
                "browser_strategy": SYSTEM_EDGE_BROWSER_STRATEGY,
                "browser_minimum_major": SYSTEM_EDGE_MINIMUM_MAJOR,
                "system_browser_required": True,
                "runtime_downloads_allowed": False,
                "startup_canary_required": True,
            }
            if any(motion_runtime.get(key) != value for key, value in fixed.items()):
                errors.append("离线动画运行时路径、系统 Edge 或启动探针合同不正确")
            for key in ("node_version", "hyperframes_version"):
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
            patch_manifest = _parse_json(
                read, "third_party/hyperframes/windows-mf-patch.json", errors
            )
            patcher_contract = (
                patch_manifest.get("patcher") if isinstance(patch_manifest, dict) else None
            )
            packaging_contract = (
                patch_manifest.get("formal_packaging_contract")
                if isinstance(patch_manifest, dict)
                else None
            )
            expected_modified = [
                {
                    "path": "dist/cli.js",
                    "upstream_sha256": HYPERFRAMES_UPSTREAM_CLI_SHA256,
                    "patched_sha256": HYPERFRAMES_PATCHED_CLI_SHA256,
                    "purposes": patch_manifest.get("modified_files", [{}])[0].get("purposes", [])
                    if isinstance(patch_manifest, dict)
                    and isinstance(patch_manifest.get("modified_files"), list)
                    and patch_manifest.get("modified_files")
                    and isinstance(patch_manifest["modified_files"][0], dict)
                    else [],
                }
            ]
            if (
                not isinstance(patch_manifest, dict)
                or patch_manifest.get("schema_version") != 1
                or patch_manifest.get("patch_id") != HYPERFRAMES_PATCH_ID
                or patch_manifest.get("patch_version") != HYPERFRAMES_PATCH_VERSION
                or patch_manifest.get("component") != f"hyperframes@{HYPERFRAMES_VERSION}"
                or patch_manifest.get("license") != "Apache-2.0"
                or patch_manifest.get("modified_files") != expected_modified
                or not isinstance(patcher_contract, dict)
                or patcher_contract.get("path") != "tools/apply_hyperframes_windows_mf_patch.py"
                or str(patcher_contract.get("sha256", "")).upper()
                != "AFBF8F7F85A4B30FA3E521B2428DF9D1966DBED09B45336955CC0FBC9CB093B6"
                or not isinstance(packaging_contract, dict)
                or packaging_contract.get("mpt_video_codec") != H264_CODEC_STRATEGY
                or packaging_contract.get("report_fields")
                != {
                    "codec_strategy": H264_CODEC_STRATEGY,
                    "patch_id": HYPERFRAMES_PATCH_ID,
                    "patch_version": HYPERFRAMES_PATCH_VERSION,
                    "patched_cli_sha256": HYPERFRAMES_PATCHED_CLI_SHA256,
                }
            ):
                errors.append("HyperFrames Windows-MF 补丁身份或封包合同不正确")
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
            ):
                if digest(source_path) != digest(license_path):
                    errors.append(f"离线动画运行时许可证副本不一致：{license_path}")
            if digest(HYPERFRAMES_UPSTREAM_LICENSE) != expected_hyperframes_lock["license_sha256"]:
                errors.append("HyperFrames 官方 tag 许可证 SHA-256 不一致")
    elif "motion_runtime" in manifest or any(name.startswith(("runtime/node/", "runtime/hyperframes/")) for name in names):
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
    if not customer_motion and isinstance(source, dict) and source.get("mpt_payload_sha256") != _canonical_payload_sha256(
        valid_entries, ("engine/MoneyPrinterTurbo/",)
    ):
        errors.append("source.mpt_payload_sha256 与规范化 MPT 文件集不一致")
    if isinstance(runtime, dict) and runtime.get("payload_sha256") != _canonical_payload_sha256(
        valid_entries, ("runtime/python/", "runtime/ffmpeg/")
    ):
        errors.append("runtime.payload_sha256 与规范化运行时文件集不一致")
    if motion_package and isinstance(motion_runtime, dict) and motion_runtime.get(
        "payload_sha256"
    ) != _canonical_payload_sha256(valid_entries, ("runtime/node/", "runtime/hyperframes/")):
        errors.append("motion_runtime.payload_sha256 与规范化动画运行时文件集不一致")

    if motion_package:
        for relative in names:
            if not relative.startswith("product-assets/motion/") or PurePosixPath(relative).suffix.casefold() not in {
                ".css", ".html", ".js", ".json"
            }:
                continue
            if re.search(r"(?i)\bhttps?://", _decode_text(read(relative))):
                errors.append(f"motion_primary 动画资产含网络资源：{relative}")
                break

    if not customer_motion:
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
        "video_codec": H264_CODEC_STRATEGY,
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
        if lock.get("portable_subset") != {
            "id": MPT_OFFLINE_SUBSET_MARKER,
            "mode": "video_only_adapted_runtime_dependency_closure",
            "deterministic_modifications": EXPECTED_MPT_DETERMINISTIC_MODIFICATIONS,
            "required_probe": EXPECTED_MPT_REQUIRED_PROBE,
        }:
            errors.append("MoneyPrinterTurbo portable_subset 未精确声明三处适配与真实编码探针")
        excluded = lock.get("excluded_components")
        if (
            not isinstance(excluded, list)
            or any(not isinstance(value, str) for value in excluded)
            or len(excluded) != len(set(excluded))
            or frozenset(excluded) != EXPECTED_MPT_EXCLUDED_COMPONENTS
        ):
            errors.append("MoneyPrinterTurbo excluded_components 与正式精简边界不一致")
        expected_license_sha = str(lock.get("license_sha256", "")).upper()
        license_paths = (
            prefix + "LICENSE",
            "third_party/moneyprinterturbo/LICENSE",
            "licenses/MoneyPrinterTurbo-MIT.txt",
        )
        if not re.fullmatch(r"[0-9A-F]{64}", expected_license_sha) or any(digest(path) != expected_license_sha for path in license_paths):
            errors.append("MoneyPrinterTurbo MIT 许可证副本与上游锁不一致")
    router = _decode_text(read(prefix + "app/router.py"))
    task_service = _decode_text(read(prefix + "app/services/task.py"))
    video_service = _decode_text(read(prefix + "app/services/video.py"))
    if (
        "from app.controllers.v1 import video" not in router
        or "controllers.v1 import llm" in router
        or "include_router(llm.router)" in router
    ):
        errors.append("MoneyPrinterTurbo 路由未固定为仅视频生成子集")
    if (
        MPT_OFFLINE_SUBSET_MARKER not in task_service
        or "from app.services import upload_post" in task_service
        or re.search(r"(?m)^\s+llm,\s*$", task_service)
        or "auto_upload = False" not in task_service
        or not re.search(r"def is_configured\(\):\r?\n\s+return False", task_service)
        or "_VIDEO_MUSIC_PROVIDERS = {}" not in task_service
        or "elevenlabs_music" in task_service
        or re.search(r"(?m)^\s+sonilo,\s*$", task_service)
    ):
        errors.append("MoneyPrinterTurbo 任务服务未固定禁用 LLM、外部音乐 Provider 与跨平台发布")
    forbidden_video_tokens = (
        "libx264",
        "openh264",
        "h264_nvenc",
        "h264_amf",
        "h264_qsv",
        "h264_videotoolbox",
    )
    if (
        MPT_H264_MF_CODEC_MARKER not in video_service
        or f'_DEFAULT_VIDEO_CODEC = "{H264_CODEC_STRATEGY}"' not in video_service
        or f'_SUPPORTED_VIDEO_CODECS = ("{H264_CODEC_STRATEGY}",)' not in video_service
        or video_service.count(".write_videofile(") != 1
        or "_fallback_write_videofile" in video_service
        or "_disable_runtime_video_codec" in video_service
        or any(token in video_service for token in forbidden_video_tokens)
        or '"-rate_control", "quality"' not in video_service
        or '"-quality", "72"' not in video_service
        or '"-scenario", "archive"' not in video_service
        or '"-hw_encoding", "0"' not in video_service
        or '"-bf", "0"' not in video_service
    ):
        errors.append("MoneyPrinterTurbo 视频服务未固定为唯一 h264_mf fail-closed 写出合同")
    if any(relative in names for relative in MPT_DISABLED_MUSIC_PROVIDER_FILES):
        errors.append("MoneyPrinterTurbo 正式子集仍包含已禁用的外部音乐 Provider")
    for relative in names:
        if not relative.startswith(prefix + "app/") or not relative.endswith(".py"):
            continue
        text = _decode_text(read(relative))
        try:
            parsed = ast.parse(text, filename=relative)
        except SyntaxError:
            errors.append(f"MoneyPrinterTurbo 正式子集 Python 无法解析：{relative}")
            break
        codec_tokens = {
            match.casefold()
            for node in ast.walk(parsed)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            for match in H264_CODEC_TOKEN_RE.findall(node.value)
        }
        if codec_tokens - {H264_CODEC_STRATEGY}:
            errors.append(
                f"MoneyPrinterTurbo 正式子集含非审核 H.264 编码器：{relative}: "
                + ",".join(sorted(codec_tokens - {H264_CODEC_STRATEGY}))
            )
            break
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
        '"%SHIYI_LAUNCHER_PYTHON%" -I -S -B -X utf8 "%~dp0scripts\\launch_combined.pyc"',
        '--ffmpeg "%~dp0runtime\\ffmpeg\\ffmpeg.exe"',
        '--ffprobe "%~dp0runtime\\ffmpeg\\ffprobe.exe"',
        '--mechanical-review',
    )
    if any(fragment not in launcher for fragment in required_launcher_fragments):
        errors.append("根启动 BAT 未固定便携运行时、素材目录或反向机械审核模式")
    stop_launcher = _decode_text(read(STOP_LAUNCHER_NAME))
    required_stop_fragments = (
        f'set "SHIYI_LAUNCHER_PYTHON={shared_python}"',
        '"%SHIYI_LAUNCHER_PYTHON%" -I -S -B -X utf8 "%~dp0scripts\\launch_combined.pyc" --project-root "%~dp0." --stop',
    )
    if any(fragment not in stop_launcher for fragment in required_stop_fragments):
        errors.append("根关闭 BAT 未使用便携 Python 调用受约束的关闭入口。")
    if "verify_combined_portable.py" in launcher or "verify_combined_portable.py" in stop_launcher:
        errors.append("根 BAT 不得在 Python 启动器之外重复执行整包完整性校验")
    migration_launcher = _decode_text(read(MIGRATION_LAUNCHER_NAME))
    required_migration_fragments = (
        f'set "SHIYI_LAUNCHER_PYTHON={shared_python}"',
        '"%SHIYI_LAUNCHER_PYTHON%" -I -S -B -X utf8 "%~dp0scripts\\launch_combined.pyc" --project-root "%~dp0." --import-runtime "%OLD_RUNTIME%"',
    )
    if any(fragment not in migration_launcher for fragment in required_migration_fragments):
        errors.append("旧版数据迁移 BAT 未使用便携 Python 调用受约束的复制入口。")
    if "verify_combined_portable.py" in migration_launcher:
        errors.append("迁移 BAT 不得在 Python 启动器之外重复执行整包完整性校验")
    installer_launcher = _decode_text(read(INSTALLER_LAUNCHER_NAME))
    required_installer_fragments = (
        f'set "SHIYI_INSTALLER_PYTHON={shared_python}"',
        '"%SHIYI_INSTALLER_PYTHON%" -I -S -B -X utf8 "%~dp0scripts\\install_combined.pyc" --source-root "%~dp0."',
    )
    if any(fragment not in installer_launcher for fragment in required_installer_fragments):
        errors.append("安装 BAT 未使用包内 Python 调用受约束的一键安装入口。")
    for relative in (
        ROOT_LAUNCHER_NAME,
        STOP_LAUNCHER_NAME,
        MIGRATION_LAUNCHER_NAME,
        INSTALLER_LAUNCHER_NAME,
    ):
        if DOWNLOAD_COMMAND_RE.search(_decode_text(read(relative))):
            errors.append(f"{relative} 含运行时下载或安装命令")
    return errors


def _verify_customer_launchers(
    read: Callable[[str], bytes],
) -> list[str]:
    """Verify the pure-motion customer entrypoints without an MPT payload."""

    errors: list[str] = []
    if read("docs/fonts/OFL.txt") != read("licenses/NotoSansSC-OFL.txt"):
        errors.append("Noto Sans SC 许可证副本不一致")
    if read("LICENSE") != read("licenses/PRODUCT-MIT.txt"):
        errors.append("产品许可证副本不一致")
    shared_python = "%~dp0runtime\\python\\python.exe"
    launcher = _decode_text(read(ROOT_LAUNCHER_NAME))
    required_launcher_fragments = (
        f'set "SHIYI_LAUNCHER_PYTHON={shared_python}"',
        '"%SHIYI_LAUNCHER_PYTHON%" -I -S -B -X utf8 "%~dp0scripts\\launch_combined.pyc"',
        '--ffmpeg "%~dp0runtime\\ffmpeg\\ffmpeg.exe"',
        '--ffprobe "%~dp0runtime\\ffmpeg\\ffprobe.exe"',
        "--mechanical-review",
    )
    if any(fragment not in launcher for fragment in required_launcher_fragments):
        errors.append("客户根启动 BAT 未固定纯动画运行时和反向机械审核模式")
    forbidden_launcher_tokens = (
        "MoneyPrinterTurbo",
        "--mpt-root",
        "--mpt-python",
        "--material-root",
        "launch_combined.ps1",
    )
    if any(token.casefold() in launcher.casefold() for token in forbidden_launcher_tokens):
        errors.append("客户根启动 BAT 仍携带实拍引擎或 PowerShell 中转参数")

    stop_launcher = _decode_text(read(STOP_LAUNCHER_NAME))
    expected_stop = (
        '"%SHIYI_LAUNCHER_PYTHON%" -I -S -B -X utf8 '
        '"%~dp0scripts\\launch_combined.pyc" --project-root "%~dp0." --stop'
    )
    if expected_stop not in stop_launcher:
        errors.append("客户关闭 BAT 未使用受约束的字节码入口")
    migration_launcher = _decode_text(read(MIGRATION_LAUNCHER_NAME))
    expected_migration = (
        '"%SHIYI_LAUNCHER_PYTHON%" -I -S -B -X utf8 '
        '"%~dp0scripts\\launch_combined.pyc" --project-root "%~dp0." '
        '--import-runtime "%OLD_RUNTIME%"'
    )
    if expected_migration not in migration_launcher:
        errors.append("客户数据迁移 BAT 未使用受约束的字节码入口")
    installer_launcher = _decode_text(read(INSTALLER_LAUNCHER_NAME))
    expected_installer = (
        '"%SHIYI_INSTALLER_PYTHON%" -I -S -B -X utf8 '
        '"%~dp0scripts\\install_combined.pyc" --source-root "%~dp0."'
    )
    if expected_installer not in installer_launcher:
        errors.append("客户安装 BAT 未使用受约束的字节码入口")
    for relative in (
        ROOT_LAUNCHER_NAME,
        STOP_LAUNCHER_NAME,
        MIGRATION_LAUNCHER_NAME,
        INSTALLER_LAUNCHER_NAME,
    ):
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
            ("runtime/python/", "runtime/ffmpeg/", "runtime/node/", "runtime/hyperframes/")
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
        errors.extend(
            _verify_ffmpeg_distribution(
                names,
                read,
                size,
                digest,
                formal=isinstance(manifest, dict) and manifest.get("schema_version") in {2, 3},
            )
        )
        errors.extend(
            _verify_python_runtime_sbom(
                names,
                read,
                size,
                digest,
                formal=isinstance(manifest, dict) and manifest.get("schema_version") in {2, 3},
            )
        )
        if isinstance(manifest, dict) and manifest.get("schema_version") == 3:
            errors.extend(_verify_customer_launchers(read))
        else:
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
            if relative.casefold() == "runtime/browser" or relative.casefold().startswith(
                "runtime/browser/"
            ):
                errors.append("便携包不得包含 runtime/browser 目录")
                continue
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
