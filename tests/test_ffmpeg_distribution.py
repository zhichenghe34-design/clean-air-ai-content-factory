from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.verify_ffmpeg_distribution import (
    DEFAULT_LOCK,
    READY_STATUS,
    REPO_ROOT,
    load_json,
    pe_imports,
    validate_lock,
    verify_ffmpeg_distribution,
    verify_release_manifest,
    verify_repository_files,
    verify_runtime_dir,
    verify_source_dir,
)


RUNTIME_NAMES = (
    "avcodec-63.dll",
    "avfilter-12.dll",
    "avformat-63.dll",
    "avutil-61.dll",
    "ffmpeg.exe",
    "ffprobe.exe",
    "swresample-7.dll",
    "swscale-10.dll",
    "zlib1.dll",
)
FILTERS = ["amix", "aresample", "atempo", "format", "fps", "scale", "setpts"]
COMMANDS = [f"probe-{index:02d}" for index in range(21)]
SOURCE_NAME = "test-source-companion.zip"
ROOT_PREFIX = "test-source-companion"


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def base_lock() -> dict[str, object]:
    runtime_files = [
        {
            "name": name,
            "bytes": len(name.encode()),
            "sha256": digest(name.encode()),
            "imports": [],
        }
        for name in RUNTIME_NAMES
    ]
    tool_ids = (
        "diffutils",
        "gnu-make",
        "msvc-cl",
        "msvc-link",
        "msvc-nmake",
        "msys2-base",
        "nasm",
        "windows-sdk-rc",
    )
    return {
        "schema_version": 2,
        "component": "test LGPL FFmpeg runtime",
        "distribution_status": READY_STATUS,
        "license": "LGPL-2.1-or-later",
        "build": {
            "ffmpeg_version": "8.0.git",
            "ffmpeg_commit": "a" * 40,
            "target": "windows-x86_64-msvc-shared",
            "external_libraries": ["mediafoundation", "zlib"],
            "hardware_acceleration_system_interfaces": ["d3d11va"],
            "configure_flags": [
                "--prefix=/ffmpeg-lgpl",
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
            ],
            "sources": [
                {
                    "id": "ffmpeg",
                    "companion_path": "sources/ffmpeg.tar.gz",
                    "bytes": len(b"ffmpeg-source"),
                    "sha256": digest(b"ffmpeg-source"),
                    "upstream_url": "https://example.invalid/ffmpeg.tar.gz",
                    "license": "LGPL-2.1-or-later",
                },
                {
                    "id": "zlib",
                    "companion_path": "sources/zlib.tar.gz",
                    "bytes": len(b"zlib-source"),
                    "sha256": digest(b"zlib-source"),
                    "upstream_url": "https://example.invalid/zlib.tar.gz",
                    "license": "Zlib",
                },
            ],
            "repository_files": [
                {"path": "locked.txt", "bytes": 6, "sha256": digest(b"locked")}
            ],
            "toolchain": [
                {
                    "id": tool_id,
                    "version": "1",
                    "bytes": 1,
                    "sha256": digest(tool_id.encode()),
                    "upstream_url": f"https://example.invalid/{tool_id}",
                }
                for tool_id in tool_ids
            ],
        },
        "runtime": {
            "directory": "third_party/ffmpeg/runtime/win-x64",
            "files": runtime_files,
            "allowed_windows_system_imports": ["kernel32.dll"],
            "dynamically_loaded_windows_system_interfaces": ["d3d11.dll", "mfplat.dll"],
            "forbidden_file_name_patterns": [
                r"(?i)\.lib$",
                r"(?i)^avdevice-",
                r"(?i)^ffplay\.exe$",
                r"(?i)(?:x264|rav1e)",
            ],
        },
        "capabilities": {
            "status": "passed",
            "report_member": "evidence/probe-report.json",
            "report_bytes": 1,
            "report_sha256": "b" * 64,
            "required_command_names": COMMANDS,
            "required_filters": FILTERS,
        },
        "source_companion": {
            "status": "frozen",
            "name": SOURCE_NAME,
            "bytes": 1,
            "sha256": "c" * 64,
            "root_prefix": ROOT_PREFIX,
            "manifest_name": "MANIFEST.json",
            "manifest_bytes": 1,
            "manifest_sha256": "d" * 64,
            "required_members": [
                "sources/ffmpeg.tar.gz",
                "sources/zlib.tar.gz",
                "evidence/probe-report.json",
            ],
        },
        "release_contract": {
            "repository": "owner/project",
            "tag": "v0.3.0",
            "object_code_asset_name_regex": (
                r"^ShiyiContentFactory-v0\.3\.0-(?:motion-primary|customer-clean-motion)-"
                r"[0-9a-f]{7,40}-Windows-x64\.zip$"
            ),
        },
        "official_compliance_references": ["https://ffmpeg.org/legal.html"],
    }


def probe_report(lock: dict[str, object]) -> bytes:
    runtime = lock["runtime"]
    assert isinstance(runtime, dict)
    report = {
        "status": "passed",
        "runtime_files": {
            item["name"]: {"bytes": item["bytes"], "sha256": item["sha256"]}
            for item in runtime["files"]
        },
        "commands": [{"name": name, "returncode": 0} for name in COMMANDS],
        "required_filters": FILTERS,
        "canary_contract": {
            "video_codec": "h264",
            "audio_codec": "aac",
            "width": 1080,
            "height": 1920,
            "pixel_format": "yuv420p",
            "frame_rate": "30/1",
        },
    }
    return (json.dumps(report, sort_keys=True) + "\n").encode()


def write_companion(
    directory: Path,
    lock: dict[str, object],
    *,
    extra_payloads: dict[str, bytes] | None = None,
) -> Path:
    payloads = {
        "sources/ffmpeg.tar.gz": b"ffmpeg-source",
        "sources/zlib.tar.gz": b"zlib-source",
        "evidence/probe-report.json": probe_report(lock),
    }
    payloads.update(extra_payloads or {})
    capabilities = lock["capabilities"]
    assert isinstance(capabilities, dict)
    report = payloads["evidence/probe-report.json"]
    capabilities["report_bytes"] = len(report)
    capabilities["report_sha256"] = digest(report)
    manifest = {
        "schema_version": 1,
        "ffmpeg_commit": lock["build"]["ffmpeg_commit"],
        "files": [
            {"path": name, "bytes": len(payload), "sha256": digest(payload)}
            for name, payload in sorted(payloads.items())
        ],
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    companion = lock["source_companion"]
    assert isinstance(companion, dict)
    companion["manifest_bytes"] = len(manifest_bytes)
    companion["manifest_sha256"] = digest(manifest_bytes)
    path = directory / SOURCE_NAME
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{ROOT_PREFIX}/MANIFEST.json", manifest_bytes)
        for name, payload in sorted(payloads.items()):
            archive.writestr(f"{ROOT_PREFIX}/{name}", payload)
    companion["bytes"] = path.stat().st_size
    companion["sha256"] = digest(path.read_bytes())
    return path


def release_manifest(lock: dict[str, object], *, include_source: bool = True) -> dict[str, object]:
    companion = lock["source_companion"]
    assert isinstance(companion, dict)
    assets: list[dict[str, object]] = [
        {
            "name": "ShiyiContentFactory-v0.3.0-motion-primary-1234567-Windows-x64.zip",
            "size": 100,
            "digest": "sha256:" + "e" * 64,
        }
    ]
    if include_source:
        assets.append(
            {
                "name": companion["name"],
                "size": companion["bytes"],
                "digest": "sha256:" + companion["sha256"],
            }
        )
    return {
        "html_url": "https://github.com/owner/project/releases/tag/v0.3.0",
        "tag_name": "v0.3.0",
        "assets": assets,
    }


class FFmpegDistributionTests(unittest.TestCase):
    def test_production_lock_and_curated_runtime_are_valid_and_ready(self):
        lock = load_json(DEFAULT_LOCK)
        self.assertEqual(validate_lock(lock), [])
        self.assertEqual(lock["distribution_status"], READY_STATUS)
        runtime_dir = REPO_ROOT / lock["runtime"]["directory"]
        self.assertEqual(verify_runtime_dir(lock, runtime_dir), [])

    def test_production_configuration_excludes_gpl_and_network(self):
        lock = load_json(DEFAULT_LOCK)
        flags = " ".join(lock["build"]["configure_flags"])
        self.assertIn("--disable-network", flags)
        self.assertNotIn("--enable-gpl", flags)
        self.assertNotIn("--enable-version3", flags)
        self.assertNotIn("libx264", flags)
        self.assertNotIn("rav1e", flags)

    def test_runtime_tamper_and_extra_file_fail(self):
        lock = load_json(DEFAULT_LOCK)
        source = REPO_ROOT / lock["runtime"]["directory"]
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            for item in source.iterdir():
                (target / item.name).write_bytes(item.read_bytes())
            (target / "ffmpeg.exe").write_bytes(b"tampered")
            errors = verify_runtime_dir(lock, target)
            self.assertTrue(any("size mismatch for ffmpeg.exe" in error for error in errors))
            (target / "ffmpeg.exe").write_bytes((source / "ffmpeg.exe").read_bytes())
            (target / "avdevice-63.dll").write_bytes(b"x")
            errors = verify_runtime_dir(lock, target)
            self.assertIn("unexpected runtime entry: avdevice-63.dll", errors)

    def test_forbidden_gpl_configuration_is_rejected(self):
        lock = base_lock()
        lock["build"]["configure_flags"].append("--enable-gpl")
        errors = validate_lock(lock)
        self.assertIn("forbidden configure token is present: --enable-gpl", errors)

    def test_forbidden_or_incomplete_runtime_set_is_rejected(self):
        lock = base_lock()
        lock["runtime"]["files"].append(
            {"name": "ffplay.exe", "bytes": 1, "sha256": "a" * 64, "imports": []}
        )
        errors = validate_lock(lock)
        self.assertTrue(any("runtime file set mismatch" in error for error in errors))
        self.assertIn("forbidden runtime file is locked: ffplay.exe", errors)

    def test_complete_source_companion_and_probe_report_pass(self):
        lock = base_lock()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_companion(directory, lock)
            self.assertEqual(validate_lock(lock), [])
            self.assertEqual(verify_source_dir(lock, directory), [])

    def test_source_companion_tamper_fails_outer_hash(self):
        lock = base_lock()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = write_companion(directory, lock)
            path.write_bytes(path.read_bytes() + b"tampered")
            errors = verify_source_dir(lock, directory)
            self.assertTrue(any("size mismatch" in error for error in errors))

    def test_sensitive_drive_path_in_public_evidence_fails(self):
        lock = base_lock()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            leaked_path = ("D:" + "\\private\\build\n").encode()
            write_companion(
                directory,
                lock,
                extra_payloads={"evidence/leak.log": b"developer path " + leaked_path},
            )
            errors = verify_source_dir(lock, directory)
            self.assertTrue(any("sensitive/local evidence leak" in error for error in errors))

    def test_probe_report_status_or_runtime_mismatch_fails(self):
        lock = base_lock()
        bad_report = json.loads(probe_report(lock))
        bad_report["status"] = "failed"
        payload = (json.dumps(bad_report, sort_keys=True) + "\n").encode()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_companion(
                directory,
                lock,
                extra_payloads={"evidence/probe-report.json": payload},
            )
            errors = verify_source_dir(lock, directory)
            self.assertIn("capability report status is not passed", errors)

    def test_same_release_object_and_source_companion_pass(self):
        lock = base_lock()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_companion(root, lock)
            manifest = root / "release.json"
            manifest.write_text(json.dumps(release_manifest(lock)), encoding="utf-8")
            self.assertEqual(verify_release_manifest(lock, manifest), [])

    def test_same_release_missing_or_mismatched_source_fails(self):
        lock = base_lock()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_companion(root, lock)
            missing = root / "missing.json"
            missing.write_text(
                json.dumps(release_manifest(lock, include_source=False)), encoding="utf-8"
            )
            errors = verify_release_manifest(lock, missing)
            self.assertTrue(any("missing source companion" in error for error in errors))
            mismatch_data = release_manifest(lock)
            mismatch_data["assets"][1]["digest"] = "sha256:" + "f" * 64
            mismatch = root / "mismatch.json"
            mismatch.write_text(json.dumps(mismatch_data), encoding="utf-8")
            errors = verify_release_manifest(lock, mismatch)
            self.assertTrue(any("SHA-256 mismatch" in error for error in errors))

    def test_repository_file_identity_is_checked(self):
        lock = base_lock()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "locked.txt").write_bytes(b"locked")
            self.assertEqual(verify_repository_files(lock, root), [])
            (root / "locked.txt").write_bytes(b"broken")
            self.assertTrue(verify_repository_files(lock, root))

    def test_release_ready_requires_all_three_inputs(self):
        lock = base_lock()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path = root / "lock.json"
            lock_path.write_text(json.dumps(lock), encoding="utf-8")
            errors = verify_ffmpeg_distribution(lock_path, require_release_ready=True)
            self.assertIn("--require-release-ready requires --runtime-dir", errors)
            self.assertIn("--require-release-ready requires --source-dir", errors)
            self.assertIn("--require-release-ready requires --release-manifest", errors)

    def test_pe_parser_rejects_non_pe(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "not-pe.exe"
            path.write_bytes(b"not a PE")
            with self.assertRaises(ValueError):
                pe_imports(path)


if __name__ == "__main__":
    unittest.main()
