from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import struct
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = REPO_ROOT / "third_party" / "ffmpeg" / "upstream-lock.json"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
READY_STATUS = "release_ready_source_companion_frozen"
REQUIRED_CONFIGURE_FLAGS = {
    "--toolchain=msvc",
    "--arch=x86_64",
    "--target-os=win64",
    "--enable-shared",
    "--disable-static",
    "--disable-debug",
    "--disable-doc",
    "--disable-ffplay",
    "--disable-autodetect",
    "--disable-network",
    "--disable-avdevice",
    "--enable-w32threads",
    "--enable-zlib",
    "--enable-ffmpeg",
    "--enable-ffprobe",
    "--enable-mediafoundation",
    "--enable-d3d11va",
    "--enable-encoder=h264_mf",
    "--enable-encoder=aac",
}
FORBIDDEN_CONFIGURE_TOKENS = {
    "--enable-gpl",
    "--enable-version3",
    "--enable-nonfree",
    "libx264",
    "libx265",
    "libvpx",
    "libaom",
    "libfdk",
    "rav1e",
}
REQUIRED_TOOL_IDS = {
    "diffutils",
    "gnu-make",
    "msvc-cl",
    "msvc-link",
    "msvc-nmake",
    "msys2-base",
    "nasm",
    "windows-sdk-rc",
}
PUBLIC_TEXT_SUFFIXES = {
    ".h",
    ".json",
    ".log",
    ".md",
    ".patch",
    ".ps1",
    ".py",
    ".txt",
}
_USER_HOME_SEGMENT = "users"
SENSITIVE_TEXT_PATTERNS = {
    "drive-qualified absolute path": re.compile(r"(?i)(?<![a-z0-9_])[a-z]:[\\/]"),
    "Windows user-home path": re.compile(
        r"(?i)(?:[\\/]" + _USER_HOME_SEGMENT + r"[\\/])"
    ),
    "local host name": re.compile(r"(?i)\b(?:LAPTOP|DESKTOP)-[a-z0-9]{4,}"),
    "MSYS local build path": re.compile(r"(?i)/[a-z]/(?:syi_ffmpeg|devtools)"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _safe_file_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and Path(value).name == value
        and "/" not in value
        and "\\" not in value
    )


def _safe_relative_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ":" not in path.parts[0] and ".." not in path.parts


def _valid_https_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _validate_frozen_file(item: Any, prefix: str, errors: list[str]) -> None:
    if not isinstance(item, dict):
        errors.append(f"{prefix} must be an object")
        return
    if not _safe_file_name(item.get("name")):
        errors.append(f"{prefix}.name must be a safe file name")
    if not _positive_int(item.get("bytes")):
        errors.append(f"{prefix}.bytes must be a positive integer")
    if not _valid_sha256(item.get("sha256")):
        errors.append(f"{prefix}.sha256 must be a lowercase SHA-256")


def validate_lock(lock: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if lock.get("schema_version") != 2:
        errors.append("schema_version must equal 2")
    if lock.get("distribution_status") != READY_STATUS:
        errors.append(f"distribution_status must equal {READY_STATUS}")
    if lock.get("license") != "LGPL-2.1-or-later":
        errors.append("license must be LGPL-2.1-or-later")

    build = lock.get("build")
    if not isinstance(build, dict):
        errors.append("build must be an object")
        build = {}
    if not isinstance(build.get("ffmpeg_version"), str) or not build.get("ffmpeg_version"):
        errors.append("build.ffmpeg_version must be non-empty")
    if not isinstance(build.get("ffmpeg_commit"), str) or COMMIT_RE.fullmatch(
        build.get("ffmpeg_commit", "")
    ) is None:
        errors.append("build.ffmpeg_commit must be a full lowercase commit")
    if build.get("target") != "windows-x86_64-msvc-shared":
        errors.append("build.target must equal windows-x86_64-msvc-shared")
    if build.get("external_libraries") != ["mediafoundation", "zlib"]:
        errors.append("build.external_libraries must be exactly mediafoundation,zlib")
    if build.get("hardware_acceleration_system_interfaces") != ["d3d11va"]:
        errors.append("build.hardware_acceleration_system_interfaces must be exactly d3d11va")

    flags = build.get("configure_flags")
    if not isinstance(flags, list) or not all(isinstance(item, str) for item in flags):
        errors.append("build.configure_flags must be an array of strings")
        flags = []
    if len(flags) != len(set(flags)):
        errors.append("build.configure_flags must not contain duplicates")
    for required in sorted(REQUIRED_CONFIGURE_FLAGS - set(flags)):
        errors.append(f"required configure flag is missing: {required}")
    joined_flags = " ".join(flags).lower()
    for forbidden in sorted(FORBIDDEN_CONFIGURE_TOKENS):
        if forbidden in joined_flags:
            errors.append(f"forbidden configure token is present: {forbidden}")

    sources = build.get("sources")
    if not isinstance(sources, list) or len(sources) != 2:
        errors.append("build.sources must contain exactly FFmpeg and zlib")
        sources = []
    source_ids: set[str] = set()
    for index, item in enumerate(sources):
        prefix = f"build.sources[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        source_id = item.get("id")
        if source_id not in {"ffmpeg", "zlib"}:
            errors.append(f"{prefix}.id must be ffmpeg or zlib")
        elif source_id in source_ids:
            errors.append(f"duplicate source id: {source_id}")
        else:
            source_ids.add(source_id)
        if not _safe_relative_path(item.get("companion_path")):
            errors.append(f"{prefix}.companion_path is unsafe")
        _validate_frozen_file(
            {"name": PurePosixPath(str(item.get("companion_path", ""))).name, **item},
            prefix,
            errors,
        )
        if not _valid_https_url(item.get("upstream_url")):
            errors.append(f"{prefix}.upstream_url must be HTTPS")
        if not isinstance(item.get("license"), str) or not item.get("license"):
            errors.append(f"{prefix}.license must be non-empty")
    if source_ids != {"ffmpeg", "zlib"}:
        errors.append("build.sources must identify exactly ffmpeg and zlib")

    repository_files = build.get("repository_files")
    if not isinstance(repository_files, list) or not repository_files:
        errors.append("build.repository_files must be a non-empty array")
        repository_files = []
    repo_paths: set[str] = set()
    for index, item in enumerate(repository_files):
        prefix = f"build.repository_files[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        path = item.get("path")
        if not _safe_relative_path(path):
            errors.append(f"{prefix}.path is unsafe")
        elif path in repo_paths:
            errors.append(f"duplicate repository path: {path}")
        else:
            repo_paths.add(path)
        if not _positive_int(item.get("bytes")) or not _valid_sha256(item.get("sha256")):
            errors.append(f"{prefix} must have frozen bytes and SHA-256")

    tools = build.get("toolchain")
    if not isinstance(tools, list) or not tools:
        errors.append("build.toolchain must be a non-empty array")
        tools = []
    tool_ids: set[str] = set()
    for index, item in enumerate(tools):
        prefix = f"build.toolchain[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        tool_id = item.get("id")
        if not isinstance(tool_id, str) or not tool_id:
            errors.append(f"{prefix}.id must be non-empty")
        elif tool_id in tool_ids:
            errors.append(f"duplicate toolchain id: {tool_id}")
        else:
            tool_ids.add(tool_id)
        if not isinstance(item.get("version"), str) or not item.get("version"):
            errors.append(f"{prefix}.version must be non-empty")
        if not _positive_int(item.get("bytes")) or not _valid_sha256(item.get("sha256")):
            errors.append(f"{prefix} must have frozen executable/package bytes and SHA-256")
        if not _valid_https_url(item.get("upstream_url")):
            errors.append(f"{prefix}.upstream_url must be HTTPS")
    for missing in sorted(REQUIRED_TOOL_IDS - tool_ids):
        errors.append(f"required toolchain record is missing: {missing}")

    runtime = lock.get("runtime")
    if not isinstance(runtime, dict):
        errors.append("runtime must be an object")
        runtime = {}
    files = runtime.get("files")
    if not isinstance(files, list) or not files:
        errors.append("runtime.files must be a non-empty array")
        files = []
    runtime_names: set[str] = set()
    runtime_imports: dict[str, list[str]] = {}
    for index, item in enumerate(files):
        prefix = f"runtime.files[{index}]"
        _validate_frozen_file(item, prefix, errors)
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str):
            if name in runtime_names:
                errors.append(f"duplicate runtime file: {name}")
            runtime_names.add(name)
        imports = item.get("imports")
        if not isinstance(imports, list) or not all(
            isinstance(value, str) and value == value.lower() and value.endswith(".dll")
            for value in imports
        ):
            errors.append(f"{prefix}.imports must be lowercase DLL names")
            imports = []
        if len(imports) != len(set(imports)):
            errors.append(f"{prefix}.imports must be unique")
        if isinstance(name, str):
            runtime_imports[name] = imports
    expected_runtime = {
        "avcodec-63.dll",
        "avfilter-12.dll",
        "avformat-63.dll",
        "avutil-61.dll",
        "ffmpeg.exe",
        "ffprobe.exe",
        "swresample-7.dll",
        "swscale-10.dll",
        "zlib1.dll",
    }
    if runtime_names != expected_runtime:
        errors.append(
            f"runtime file set mismatch: {sorted(runtime_names)} != {sorted(expected_runtime)}"
        )
    system_imports = runtime.get("allowed_windows_system_imports")
    if not isinstance(system_imports, list) or not all(
        isinstance(value, str) and value == value.lower() and value.endswith(".dll")
        for value in system_imports
    ):
        errors.append("runtime.allowed_windows_system_imports must be lowercase DLL names")
        system_imports = []
    allowed_imports = {name.lower() for name in runtime_names} | set(system_imports)
    for file_name, imports in runtime_imports.items():
        for imported in imports:
            if imported not in allowed_imports:
                errors.append(f"unaccounted PE import in {file_name}: {imported}")
    forbidden_names = runtime.get("forbidden_file_name_patterns")
    if not isinstance(forbidden_names, list) or not forbidden_names:
        errors.append("runtime.forbidden_file_name_patterns must be non-empty")
    else:
        for pattern in forbidden_names:
            try:
                compiled = re.compile(pattern)
            except (TypeError, re.error) as exc:
                errors.append(f"invalid forbidden runtime pattern {pattern!r}: {exc}")
                continue
            for name in runtime_names:
                if compiled.search(name):
                    errors.append(f"forbidden runtime file is locked: {name}")

    capabilities = lock.get("capabilities")
    if not isinstance(capabilities, dict):
        errors.append("capabilities must be an object")
        capabilities = {}
    if capabilities.get("status") != "passed":
        errors.append("capabilities.status must equal passed")
    if not _safe_relative_path(capabilities.get("report_member")):
        errors.append("capabilities.report_member is unsafe")
    if not _positive_int(capabilities.get("report_bytes")) or not _valid_sha256(
        capabilities.get("report_sha256")
    ):
        errors.append("capabilities report must have frozen bytes and SHA-256")
    commands = capabilities.get("required_command_names")
    if not isinstance(commands, list) or len(commands) < 20 or len(commands) != len(set(commands)):
        errors.append("capabilities.required_command_names must contain at least 20 unique probes")
    filters = capabilities.get("required_filters")
    if not isinstance(filters, list) or set(filters) != {
        "amix",
        "aresample",
        "atempo",
        "format",
        "fps",
        "scale",
        "setpts",
    }:
        errors.append("capabilities.required_filters does not match the media contract")

    companion = lock.get("source_companion")
    if not isinstance(companion, dict):
        errors.append("source_companion must be an object")
        companion = {}
    if companion.get("status") != "frozen":
        errors.append("source_companion.status must equal frozen")
    _validate_frozen_file(companion, "source_companion", errors)
    if not _safe_file_name(companion.get("manifest_name")):
        errors.append("source_companion.manifest_name must be a safe file name")
    if not _positive_int(companion.get("manifest_bytes")) or not _valid_sha256(
        companion.get("manifest_sha256")
    ):
        errors.append("source_companion manifest must have frozen bytes and SHA-256")
    if not isinstance(companion.get("root_prefix"), str) or not companion.get("root_prefix"):
        errors.append("source_companion.root_prefix must be non-empty")
    required_members = companion.get("required_members")
    if not isinstance(required_members, list) or not required_members or not all(
        _safe_relative_path(value) for value in required_members
    ):
        errors.append("source_companion.required_members must be safe relative paths")

    release = lock.get("release_contract")
    if not isinstance(release, dict):
        errors.append("release_contract must be an object")
        release = {}
    repository = release.get("repository")
    if not isinstance(repository, str) or re.fullmatch(r"[^/\s]+/[^/\s]+", repository) is None:
        errors.append("release_contract.repository must be owner/repository")
    if not isinstance(release.get("tag"), str) or not release.get("tag"):
        errors.append("release_contract.tag must be non-empty")
    try:
        re.compile(release.get("object_code_asset_name_regex", ""))
    except (TypeError, re.error) as exc:
        errors.append(f"invalid object-code asset regex: {exc}")
    return errors


def _read_u16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise ValueError("truncated PE u16")
    return struct.unpack_from("<H", data, offset)[0]


def _read_u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise ValueError("truncated PE u32")
    return struct.unpack_from("<I", data, offset)[0]


def pe_imports(path: Path) -> list[str]:
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError("missing MZ header")
    pe_offset = _read_u32(data, 0x3C)
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ValueError("missing PE signature")
    coff = pe_offset + 4
    section_count = _read_u16(data, coff + 2)
    optional_size = _read_u16(data, coff + 16)
    optional = coff + 20
    if optional + optional_size > len(data):
        raise ValueError("truncated optional header")
    magic = _read_u16(data, optional)
    if magic == 0x20B:
        directory_count_offset, directories_offset = 108, 112
        image_base = struct.unpack_from("<Q", data, optional + 24)[0]
    elif magic == 0x10B:
        directory_count_offset, directories_offset = 92, 96
        image_base = _read_u32(data, optional + 28)
    else:
        raise ValueError(f"unsupported PE optional-header magic: {magic:#x}")
    directory_count = _read_u32(data, optional + directory_count_offset)
    section_table = optional + optional_size
    sections: list[tuple[int, int, int, int]] = []
    for index in range(section_count):
        offset = section_table + index * 40
        if offset + 40 > len(data):
            raise ValueError("truncated PE section table")
        virtual_size = _read_u32(data, offset + 8)
        virtual_address = _read_u32(data, offset + 12)
        raw_size = _read_u32(data, offset + 16)
        raw_offset = _read_u32(data, offset + 20)
        sections.append((virtual_address, max(virtual_size, raw_size), raw_offset, raw_size))

    def rva_to_offset(rva: int) -> int:
        for virtual_address, mapped_size, raw_offset, raw_size in sections:
            if virtual_address <= rva < virtual_address + mapped_size:
                relative = rva - virtual_address
                if relative >= raw_size or raw_offset + relative >= len(data):
                    raise ValueError(f"PE RVA maps beyond raw data: {rva:#x}")
                return raw_offset + relative
        raise ValueError(f"PE RVA is outside all sections: {rva:#x}")

    def read_c_string(rva: int) -> str:
        offset = rva_to_offset(rva)
        end = data.find(b"\0", offset, min(len(data), offset + 1024))
        if end < 0:
            raise ValueError("unterminated PE import name")
        return data[offset:end].decode("ascii").lower()

    imports: set[str] = set()
    if directory_count > 1:
        import_rva = _read_u32(data, optional + directories_offset + 8)
        if import_rva:
            offset = rva_to_offset(import_rva)
            for _ in range(4096):
                descriptor = data[offset : offset + 20]
                if len(descriptor) != 20:
                    raise ValueError("truncated PE import descriptor")
                fields = struct.unpack("<IIIII", descriptor)
                if fields == (0, 0, 0, 0, 0):
                    break
                imports.add(read_c_string(fields[3]))
                offset += 20
            else:
                raise ValueError("PE import descriptor limit exceeded")
    if directory_count > 13:
        delay_rva = _read_u32(data, optional + directories_offset + 13 * 8)
        if delay_rva:
            offset = rva_to_offset(delay_rva)
            for _ in range(4096):
                descriptor = data[offset : offset + 32]
                if len(descriptor) != 32:
                    raise ValueError("truncated PE delay-import descriptor")
                fields = struct.unpack("<IIIIIIII", descriptor)
                if fields == (0, 0, 0, 0, 0, 0, 0, 0):
                    break
                name_rva = fields[1] if fields[0] & 1 else fields[1] - image_base
                imports.add(read_c_string(name_rva))
                offset += 32
            else:
                raise ValueError("PE delay-import descriptor limit exceeded")
    return sorted(imports)


def verify_runtime_dir(lock: dict[str, Any], runtime_dir: Path) -> list[str]:
    errors: list[str] = []
    if not runtime_dir.is_dir():
        return [f"runtime directory is missing: {runtime_dir}"]
    expected = {item["name"]: item for item in lock["runtime"]["files"]}
    actual = {item.name: item for item in runtime_dir.iterdir()}
    for name in sorted(set(expected) - set(actual)):
        errors.append(f"runtime file is missing: {name}")
    for name in sorted(set(actual) - set(expected)):
        errors.append(f"unexpected runtime entry: {name}")
    for name in sorted(set(expected) & set(actual)):
        path = actual[name]
        item = expected[name]
        if not path.is_file():
            errors.append(f"runtime entry is not a file: {name}")
            continue
        if path.stat().st_size != item["bytes"]:
            errors.append(f"runtime size mismatch for {name}: {path.stat().st_size} != {item['bytes']}")
            continue
        actual_sha = sha256(path)
        if actual_sha != item["sha256"]:
            errors.append(f"runtime SHA-256 mismatch for {name}: {actual_sha}")
            continue
        try:
            imports = pe_imports(path)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            errors.append(f"cannot parse PE imports for {name}: {exc}")
            continue
        if imports != sorted(item["imports"]):
            errors.append(f"PE imports mismatch for {name}: {imports} != {sorted(item['imports'])}")
    return errors


def verify_repository_files(lock: dict[str, Any], repo_root: Path) -> list[str]:
    errors: list[str] = []
    for item in lock["build"]["repository_files"]:
        path = repo_root / PurePosixPath(item["path"])
        if not path.is_file():
            errors.append(f"locked repository file is missing: {item['path']}")
            continue
        if path.stat().st_size != item["bytes"]:
            errors.append(f"repository file size mismatch for {item['path']}")
            continue
        if sha256(path) != item["sha256"]:
            errors.append(f"repository file SHA-256 mismatch for {item['path']}")
    return errors


def _verify_probe_report(
    lock: dict[str, Any], manifest_files: dict[str, dict[str, Any]], payloads: dict[str, bytes]
) -> list[str]:
    errors: list[str] = []
    capabilities = lock["capabilities"]
    member = capabilities["report_member"]
    item = manifest_files.get(member)
    payload = payloads.get(member)
    if item is None or payload is None:
        return [f"source companion is missing capability report: {member}"]
    if item["bytes"] != capabilities["report_bytes"] or item["sha256"] != capabilities[
        "report_sha256"
    ]:
        errors.append("capability report manifest identity does not match the lock")
    try:
        report = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return errors + [f"cannot parse capability report: {exc}"]
    if not isinstance(report, dict) or report.get("status") != "passed":
        errors.append("capability report status is not passed")
        return errors
    report_runtime = report.get("runtime_files")
    expected_runtime = {
        item["name"]: {"bytes": item["bytes"], "sha256": item["sha256"]}
        for item in lock["runtime"]["files"]
    }
    if report_runtime != expected_runtime:
        errors.append("capability report runtime identity does not match the lock")
    command_names = {
        item.get("name") for item in report.get("commands", []) if isinstance(item, dict)
    }
    for required in capabilities["required_command_names"]:
        if required not in command_names:
            errors.append(f"capability report is missing command: {required}")
    if report.get("required_filters") != capabilities["required_filters"]:
        errors.append("capability report filter contract does not match the lock")
    contract = report.get("canary_contract")
    expected_contract = {
        "video_codec": "h264",
        "audio_codec": "aac",
        "width": 1080,
        "height": 1920,
        "pixel_format": "yuv420p",
        "frame_rate": "30/1",
    }
    if contract != expected_contract:
        errors.append("capability report canary contract is invalid")
    return errors


def _public_text_leaks(relative: str, payload: bytes) -> list[str]:
    if PurePosixPath(relative).suffix.lower() not in PUBLIC_TEXT_SUFFIXES:
        return []
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return ["public text member is not valid UTF-8"]
    return [name for name, pattern in SENSITIVE_TEXT_PATTERNS.items() if pattern.search(text)]


def _inspect_source_companion(lock: dict[str, Any], path: Path) -> list[str]:
    errors: list[str] = []
    companion = lock["source_companion"]
    try:
        with zipfile.ZipFile(path) as archive:
            infos = [item for item in archive.infolist() if not item.is_dir()]
            names = [item.filename for item in infos]
            if len(names) != len(set(names)):
                errors.append("source companion contains duplicate member names")
            root = companion["root_prefix"].rstrip("/")
            prefix = root + "/"
            unsafe = [
                name
                for name in names
                if not name.startswith(prefix)
                or name.startswith("/")
                or ".." in PurePosixPath(name).parts
                or re.match(r"^[A-Za-z]:", name)
            ]
            if unsafe:
                errors.append(f"unsafe source companion members: {unsafe[:3]}")
            for info in infos:
                mode = (info.external_attr >> 16) & 0o170000
                if mode not in {0, 0o100000}:
                    errors.append(f"non-regular ZIP member is forbidden: {info.filename}")
                if info.flag_bits & 0x1:
                    errors.append(f"encrypted ZIP member is forbidden: {info.filename}")
            manifest_member = prefix + companion["manifest_name"]
            if manifest_member not in names:
                return errors + [f"source companion manifest is missing: {manifest_member}"]
            manifest_bytes = archive.read(manifest_member)
            if len(manifest_bytes) != companion["manifest_bytes"]:
                errors.append("source companion manifest byte size mismatch")
            if sha256_bytes(manifest_bytes) != companion["manifest_sha256"]:
                errors.append("source companion manifest SHA-256 mismatch")
            try:
                manifest = json.loads(manifest_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return errors + [f"cannot parse source companion manifest: {exc}"]
            if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
                return errors + ["source companion manifest schema_version must equal 1"]
            if manifest.get("ffmpeg_commit") != lock["build"]["ffmpeg_commit"]:
                errors.append("source companion manifest FFmpeg commit mismatch")
            raw_files = manifest.get("files")
            if not isinstance(raw_files, list):
                return errors + ["source companion manifest files must be an array"]
            manifest_files: dict[str, dict[str, Any]] = {}
            payloads: dict[str, bytes] = {}
            for index, item in enumerate(raw_files):
                if not isinstance(item, dict) or not _safe_relative_path(item.get("path")):
                    errors.append(f"invalid source companion manifest entry: {index}")
                    continue
                relative = item["path"]
                if relative in manifest_files:
                    errors.append(f"duplicate source companion manifest path: {relative}")
                    continue
                if not _positive_int(item.get("bytes")) or not _valid_sha256(item.get("sha256")):
                    errors.append(f"unfrozen source companion manifest entry: {relative}")
                    continue
                member_name = prefix + relative
                if member_name not in names:
                    errors.append(f"source companion payload is missing: {relative}")
                    continue
                payload = archive.read(member_name)
                if len(payload) != item["bytes"] or sha256_bytes(payload) != item["sha256"]:
                    errors.append(f"source companion payload identity mismatch: {relative}")
                    continue
                leaks = _public_text_leaks(relative, payload)
                for leak in leaks:
                    errors.append(f"sensitive/local evidence leak in {relative}: {leak}")
                manifest_files[relative] = item
                payloads[relative] = payload
            expected_names = {prefix + relative for relative in manifest_files} | {manifest_member}
            if set(names) != expected_names:
                errors.append("source companion ZIP members do not exactly match its manifest")
            for required in companion["required_members"]:
                if required not in manifest_files:
                    errors.append(f"required source companion member is missing: {required}")
            for source in lock["build"]["sources"]:
                relative = source["companion_path"]
                item = manifest_files.get(relative)
                if item is None:
                    errors.append(f"locked source is absent from companion: {relative}")
                elif item["bytes"] != source["bytes"] or item["sha256"] != source["sha256"]:
                    errors.append(f"locked source identity mismatch in companion: {relative}")
            errors.extend(_verify_probe_report(lock, manifest_files, payloads))
    except (OSError, zipfile.BadZipFile, EOFError) as exc:
        errors.append(f"cannot inspect source companion {path.name}: {exc}")
    return errors


def verify_source_dir(
    lock: dict[str, Any], source_dir: Path, *, inspect_archives: bool = True
) -> list[str]:
    del inspect_archives  # The LGPL companion is always inspected fail-closed.
    if not source_dir.is_dir():
        return [f"source asset directory is missing: {source_dir}"]
    companion = lock["source_companion"]
    path = source_dir / companion["name"]
    if not path.is_file():
        return [f"source companion is missing: {companion['name']}"]
    errors: list[str] = []
    if path.stat().st_size != companion["bytes"]:
        errors.append(f"source companion size mismatch: {path.stat().st_size} != {companion['bytes']}")
        return errors
    if sha256(path) != companion["sha256"]:
        errors.append(f"source companion SHA-256 mismatch: {path.name}")
        return errors
    errors.extend(_inspect_source_companion(lock, path))
    return errors


def _release_repository(data: dict[str, Any]) -> str | None:
    repository = data.get("repository")
    if isinstance(repository, str):
        return repository
    for field in ("html_url", "url"):
        value = data.get(field)
        if not isinstance(value, str):
            continue
        match = re.search(r"github\.com/(?:repos/)?([^/]+/[^/]+?)(?:/releases|$)", value)
        if match:
            return match.group(1)
    return None


def _asset_digest(asset: dict[str, Any]) -> str | None:
    value = asset.get("sha256", asset.get("digest"))
    if isinstance(value, str) and value.startswith("sha256:"):
        value = value.removeprefix("sha256:")
    return value if _valid_sha256(value) else None


def _asset_size(asset: dict[str, Any]) -> int | None:
    value = asset.get("bytes", asset.get("size"))
    return value if _positive_int(value) else None


def verify_release_manifest(lock: dict[str, Any], manifest_path: Path) -> list[str]:
    try:
        data = load_json(manifest_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [f"cannot read release manifest {manifest_path}: {exc}"]
    errors: list[str] = []
    release = lock["release_contract"]
    repository = _release_repository(data)
    if repository != release["repository"]:
        errors.append(f"release repository mismatch: {repository!r} != {release['repository']!r}")
    tag = data.get("tag", data.get("tag_name"))
    if tag != release["tag"]:
        errors.append(f"release tag mismatch: {tag!r} != {release['tag']!r}")
    raw_assets = data.get("assets")
    if not isinstance(raw_assets, list):
        return errors + ["release manifest assets must be an array"]
    assets: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw_assets):
        if not isinstance(item, dict) or not _safe_file_name(item.get("name")):
            errors.append(f"release asset {index} has an invalid name")
            continue
        if item["name"] in assets:
            errors.append(f"duplicate release asset name: {item['name']}")
        else:
            assets[item["name"]] = item
    pattern = re.compile(release["object_code_asset_name_regex"])
    object_assets = [item for name, item in assets.items() if pattern.fullmatch(name)]
    if not object_assets:
        errors.append("same Release does not contain the required v0.3.0 Windows object-code ZIP")
    for item in object_assets:
        if _asset_size(item) is None or _asset_digest(item) is None:
            errors.append(f"object-code asset lacks frozen size/SHA-256: {item['name']}")
    companion = lock["source_companion"]
    source_asset = assets.get(companion["name"])
    if source_asset is None:
        errors.append(f"same Release is missing source companion: {companion['name']}")
    else:
        if _asset_size(source_asset) != companion["bytes"]:
            errors.append(f"release source companion size mismatch: {companion['name']}")
        if _asset_digest(source_asset) != companion["sha256"]:
            errors.append(f"release source companion SHA-256 mismatch: {companion['name']}")
    return errors


def verify_ffmpeg_distribution(
    lock_path: Path,
    *,
    runtime_dir: Path | None = None,
    source_dir: Path | None = None,
    release_manifest: Path | None = None,
    repo_root: Path | None = None,
    require_release_ready: bool = False,
    inspect_archives: bool = True,
) -> list[str]:
    try:
        lock = load_json(lock_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [f"cannot read FFmpeg lock {lock_path}: {exc}"]
    errors = validate_lock(lock)
    if errors:
        return errors
    if repo_root is not None:
        errors.extend(verify_repository_files(lock, repo_root))
    if runtime_dir is not None:
        errors.extend(verify_runtime_dir(lock, runtime_dir))
    if source_dir is not None:
        errors.extend(verify_source_dir(lock, source_dir, inspect_archives=inspect_archives))
    if release_manifest is not None:
        errors.extend(verify_release_manifest(lock, release_manifest))
    if require_release_ready:
        if runtime_dir is None:
            errors.append("--require-release-ready requires --runtime-dir")
        if source_dir is None:
            errors.append("--require-release-ready requires --source-dir")
        if release_manifest is None:
            errors.append("--require-release-ready requires --release-manifest")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed LGPL FFmpeg runtime/source-companion/same-Release verifier."
    )
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--release-manifest", type=Path)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--require-release-ready", action="store_true")
    parser.add_argument("--inspect-archives", action="store_true")
    parser.add_argument("--lock-only", action="store_true")
    args = parser.parse_args(argv)
    if args.lock_only and any(
        (
            args.runtime_dir is not None,
            args.source_dir is not None,
            args.release_manifest is not None,
            args.require_release_ready,
            args.inspect_archives,
        )
    ):
        parser.error("--lock-only cannot be combined with verification inputs or release readiness")
    errors = verify_ffmpeg_distribution(
        args.lock,
        runtime_dir=None if args.lock_only else args.runtime_dir,
        source_dir=None if args.lock_only else args.source_dir,
        release_manifest=None if args.lock_only else args.release_manifest,
        repo_root=args.repo_root,
        require_release_ready=False if args.lock_only else args.require_release_ready,
        inspect_archives=True,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        print("FFMPEG_DISTRIBUTION_BLOCKED")
        return 1
    print("FFMPEG_DISTRIBUTION_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
