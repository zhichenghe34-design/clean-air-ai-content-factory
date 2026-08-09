from __future__ import annotations

import argparse
import hashlib
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
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PROJECT_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_SOURCE_ROOT))
from core.motion_runtime_contract import HYPERFRAMES_VERSION, NODE_MINIMUM_MAJOR


PACKAGE_ROOT_NAME = "Shiyi"
PACKAGE_VERSION = "0.3.0"
PACKAGE_MANIFEST = "PACKAGE-MANIFEST.json"
CHECKSUMS_FILE = "SHA256SUMS.txt"
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
EXPECTED_MPT_COMMIT = "254cd028906ee657eab844dc94087cdbea2a7aa8"
EXPECTED_MPT_VERSION = "1.3.3"
MOTION_PACKAGE_PROFILE = "motion_primary"
LEGACY_PACKAGE_PROFILE = "legacy_combined"
HYPERFRAMES_RUNTIME_MANIFEST = "RUNTIME-MANIFEST.json"
HYPERFRAMES_CLI_RELATIVE = Path("node_modules/hyperframes/bin/hyperframes.mjs")
HYPERFRAMES_PACKAGE_RELATIVE = Path("node_modules/hyperframes/package.json")
HYPERFRAMES_UPSTREAM_COMMIT = "1a52351f05237433006e6ca92db18feafed16fed"
HYPERFRAMES_UPSTREAM_TAG = "v0.7.86"
HYPERFRAMES_REPOSITORY = "https://github.com/heygen-com/hyperframes"

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
    "third_party/hyperframes/LICENSE",
    "third_party/hyperframes/README.md",
    "third_party/hyperframes/upstream-lock.json",
    "third_party/hyperframes/dependency-license-overrides.json",
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
class MotionRuntimeInputs:
    node_runtime: Path
    hyperframes_runtime: Path
    browser_runtime: Path
    node_version: str
    hyperframes_version: str
    browser_version: str


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
        "schema_version": 1,
        "runtime_kind": "hyperframes_node_modules_closure",
        "platform": "win32-x64",
        "entry": HYPERFRAMES_CLI_RELATIVE.as_posix(),
        "hyperframes_version": expected_version,
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
        "HYPERFRAMES_BROWSER_PATH": str(package / "runtime" / "browser" / "chrome-headless-shell.exe"),
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
    browser = package / "runtime" / "browser" / "chrome-headless-shell.exe"
    probes = (
        ([str(node), "--version"], motion.node_version, "Node"),
        ([str(node), str(cli), "--version"], motion.hyperframes_version, "HyperFrames"),
        ([str(browser), "--version"], motion.browser_version, "headless browser"),
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

    if inputs.package_profile not in {LEGACY_PACKAGE_PROFILE, MOTION_PACKAGE_PROFILE}:
        raise ValueError("未知便携包 profile")
    motion = inputs.motion_runtime
    if inputs.package_profile == MOTION_PACKAGE_PROFILE and motion is None:
        raise ValueError("motion_primary v0.3 便携包必须显式提供离线动画运行时")
    if inputs.package_profile == LEGACY_PACKAGE_PROFILE and motion is not None:
        raise ValueError("legacy_combined 不得混入未声明的动画运行时")
    if motion is not None:
        for root, label in (
            (motion.node_runtime, "固定 Node runtime"),
            (motion.hyperframes_runtime, "固定 HyperFrames runtime"),
            (motion.browser_runtime, "固定 headless browser runtime"),
        ):
            _require_directory(root.resolve(), label)
        _require_file(motion.node_runtime.resolve() / "node.exe", "固定 Node 入口")
        _require_file(motion.node_runtime.resolve() / "LICENSE", "Node 许可证")
        _require_file(motion.hyperframes_runtime.resolve() / HYPERFRAMES_PACKAGE_RELATIVE, "HyperFrames package.json")
        _require_file(motion.hyperframes_runtime.resolve() / HYPERFRAMES_CLI_RELATIVE, "HyperFrames CLI")
        _require_file(motion.browser_runtime.resolve() / "chrome-headless-shell.exe", "headless browser")
        _require_file(motion.browser_runtime.resolve() / "LICENSE.headless_shell", "headless browser 许可证")
        if not all(re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", value) for value in (
            motion.node_version, motion.hyperframes_version, motion.browser_version
        )):
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
        _validate_hyperframes_dependency_closure(motion.hyperframes_runtime.resolve(), motion.hyperframes_version)
        _read_hyperframes_license_contract(repo)

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
    if inputs.motion_runtime is not None:
        motion = inputs.motion_runtime
        hyperframes_license_contract = _read_hyperframes_license_contract(inputs.repo.resolve())
        _copy_file(motion.node_runtime.resolve() / "node.exe", package / "runtime" / "node" / "node.exe")
        _copy_file(motion.node_runtime.resolve() / "LICENSE", package / "runtime" / "node" / "LICENSE")
        _copy_hyperframes_dependency_closure(
            motion.hyperframes_runtime.resolve(),
            package / "runtime" / "hyperframes",
        )
        hyperframes_sbom = _write_hyperframes_runtime_manifest(
            package / "runtime" / "hyperframes",
            motion.hyperframes_version,
            hyperframes_license_contract,
        )
        _copy_motion_tree(motion.browser_runtime.resolve(), package / "runtime" / "browser")
    else:
        hyperframes_sbom = None

    license_dir = package / "licenses"
    _copy_file(inputs.repo.resolve() / "LICENSE", license_dir / "PRODUCT-MIT.txt")
    _copy_file(inputs.mpt_source.resolve() / "LICENSE", license_dir / "MoneyPrinterTurbo-MIT.txt")
    _copy_file(inputs.repo.resolve() / "docs" / "fonts" / "OFL.txt", license_dir / "NotoSansSC-OFL.txt")
    _copy_file(inputs.ffmpeg_license.resolve(), license_dir / "FFmpeg-license.txt")
    _copy_file(python_license, license_dir / "Python-license.txt")
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
        _copy_file(
            inputs.motion_runtime.browser_runtime.resolve() / "LICENSE.headless_shell",
            license_dir / "Chrome-Headless-Shell-license.txt",
        )
        assert hyperframes_sbom is not None
        (license_dir / "HyperFrames-third-party-SBOM.json").write_text(
            json.dumps(hyperframes_sbom, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if inputs.ffmpeg_build_info is not None:
        _copy_file(inputs.ffmpeg_build_info.resolve(), license_dir / "FFmpeg-build-info.txt")
    motion_label = "、Node、HyperFrames 与 Chrome Headless Shell" if inputs.motion_runtime is not None else ""
    (license_dir / "README.txt").write_text(
        f"本目录集中保存产品、MoneyPrinterTurbo、Noto Sans SC、FFmpeg、Python{motion_label} 的许可证副本。\n"
        "Python 运行时所带第三方依赖的许可证元数据仍保留在 runtime/python 内。\n"
        + (
            "HyperFrames 的传递依赖版本、SPDX 声明及实际存在的 LICENSE/NOTICE 文件见 "
            "HyperFrames-third-party-SBOM.json；并不宣称每个二进制包都附有独立许可证正文。\n"
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
    if inputs.motion_runtime is not None:
        motion = inputs.motion_runtime
        manifest["package_profile"] = MOTION_PACKAGE_PROFILE
        manifest["motion_runtime"] = {
            "mode": "offline_bundled_required",
            "node": "runtime/node/node.exe",
            "node_version": motion.node_version,
            "hyperframes_cli": f"runtime/hyperframes/{HYPERFRAMES_CLI_RELATIVE.as_posix()}",
            "hyperframes_version": motion.hyperframes_version,
            "closure_manifest": f"runtime/hyperframes/{HYPERFRAMES_RUNTIME_MANIFEST}",
            "browser": "runtime/browser/chrome-headless-shell.exe",
            "browser_version": motion.browser_version,
            "runtime_downloads_allowed": False,
            "system_fallback_allowed": False,
            "payload_sha256": _canonical_payload_sha256(
                files, ("runtime/node/", "runtime/hyperframes/", "runtime/browser/")
            ),
        }
        manifest["mutable_state"]["workbench_immutable_children"].extend(
            ["node", "hyperframes", "browser"]
        )
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
        if inputs.motion_runtime is not None and inputs.verify_runtime_executables:
            _verify_motion_executables(package, inputs.motion_runtime)
        _write_root_launcher(package)
        manifest = _write_manifests(package, repo_commit, inputs)

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
    parser.add_argument("--node-runtime", type=Path, required=True)
    parser.add_argument("--hyperframes-runtime", type=Path, required=True)
    parser.add_argument("--browser-runtime", type=Path, required=True)
    parser.add_argument("--node-version", required=True)
    parser.add_argument("--hyperframes-version", required=True)
    parser.add_argument("--browser-version", required=True)
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
                package_profile=MOTION_PACKAGE_PROFILE,
                motion_runtime=MotionRuntimeInputs(
                    node_runtime=args.node_runtime,
                    hyperframes_runtime=args.hyperframes_runtime,
                    browser_runtime=args.browser_runtime,
                    node_version=args.node_version,
                    hyperframes_version=args.hyperframes_version,
                    browser_version=args.browser_version,
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
