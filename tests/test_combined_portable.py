from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tools.build_combined_portable import (
    CHECKSUMS_FILE,
    EXPECTED_MPT_COMMIT,
    FIXED_ZIP_TIME,
    PACKAGE_MANIFEST,
    PACKAGE_ROOT_NAME,
    ROOT_LAUNCHER_NAME,
    BuildInputs,
    build_combined_portable,
    sha256_file,
)
from tools.verify_combined_portable import verify_folder, verify_zip
from scripts.launch_combined import LauncherError, verify_packaged_integrity


class CombinedPortableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.mpt = self.root / "mpt"
        self.python_runtime = self.root / "python-runtime"
        self.ffmpeg_runtime = self.root / "ffmpeg-runtime"
        self.materials = (self.root / "input-01.mp4", self.root / "input-02.mp4")
        self.ffmpeg_license = self.root / "ffmpeg-license.txt"
        self._make_repo_fixture()
        self._make_mpt_fixture()
        self._make_runtime_fixture()
        for index, material in enumerate(self.materials, start=1):
            material.write_bytes(b"fixture-mp4-" + bytes([index]))
        self.ffmpeg_license.write_text("FFmpeg fixture license\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, root: Path, relative: str, content: str | bytes = "fixture\n") -> Path:
        path = root / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def _make_repo_fixture(self) -> None:
        product_license = "Product MIT fixture\n"
        mpt_license = "MoneyPrinterTurbo MIT fixture\n"
        self._write(self.repo, "app.py", "print('fixture app')\n")
        self._write(self.repo, "LICENSE", product_license)
        self._write(self.repo, "core/__init__.py", "")
        self._write(self.repo, "core/runtime.py", "VALUE = 1\n")
        self._write(self.repo, "core/__pycache__/ignored.pyc", b"ignored")
        self._write(self.repo, "static/index.html", "<main>fixture</main>\n")
        self._write(self.repo, "static/app.js", "console.log('fixture');\n")
        self._write(self.repo, "catalog/tools.json", "{}\n")
        self._write(self.repo, "agent-skills/fixture/SKILL.md", "# fixture\n")
        self._write(self.repo, "docs/fonts/NotoSansSC-Regular.ttf", b"regular-font")
        self._write(self.repo, "docs/fonts/NotoSansSC-Bold.ttf", b"bold-font")
        self._write(self.repo, "docs/fonts/OFL.txt", "Noto OFL fixture\n")
        self._write(self.repo, "docs/fonts/SOURCE.md", "Noto source fixture\n")
        source_root = Path(__file__).resolve().parents[1]
        self._write(
            self.repo,
            "scripts/launch_combined.py",
            (source_root / "scripts/launch_combined.py").read_bytes(),
        )
        self._write(
            self.repo,
            "scripts/launch_combined.ps1",
            (source_root / "scripts/launch_combined.ps1").read_bytes(),
        )
        verifier_source = source_root / "tools" / "verify_combined_portable.py"
        self._write(self.repo, "tools/verify_combined_portable.py", verifier_source.read_bytes())
        self._write(self.repo, "third_party/moneyprinterturbo/LICENSE", mpt_license)
        self._write(self.repo, "third_party/moneyprinterturbo/README.md", "# boundary\n")
        license_sha = hashlib.sha256(
            (self.repo / "third_party/moneyprinterturbo/LICENSE").read_bytes()
        ).hexdigest().upper()
        self._write(
            self.repo,
            "third_party/moneyprinterturbo/upstream-lock.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "MoneyPrinterTurbo",
                    "upstream_version": "1.3.3",
                    "upstream_commit": EXPECTED_MPT_COMMIT,
                    "license": "MIT",
                    "license_sha256": license_sha,
                },
                ensure_ascii=False,
            )
            + "\n",
        )

    def _make_mpt_fixture(self) -> None:
        boundary_license = (self.repo / "third_party/moneyprinterturbo/LICENSE").read_text(encoding="utf-8")
        self._write(self.mpt, "app/__init__.py", "")
        self._write(self.mpt, "app/asgi.py", "APP = 'fixture'\n")
        self._write(self.mpt, "app/prompts.json", "{}\n")
        self._write(self.mpt, "app/__pycache__/ignored.pyc", b"ignored")
        self._write(
            self.mpt,
            "pyproject.toml",
            '[project]\nname = "moneyprinterturbo"\nversion = "1.3.3"\n',
        )
        self._write(self.mpt, "uv.lock", "version = 1\n")
        self._write(self.mpt, "LICENSE", boundary_license)
        self._write(self.mpt, "resource/public/index.html", "<main>MPT</main>\n")
        # These upstream assets are intentionally present in the input and must never be copied.
        self._write(self.mpt, "resource/fonts/proprietary.ttf", b"forbidden")
        self._write(self.mpt, "resource/songs/sample.mp3", b"forbidden")
        self._write(self.mpt, "webui/app.py", "raise RuntimeError\n")
        self._write(self.mpt, "docs/readme.md", "excluded\n")
        self._write(self.mpt, "tests/test_upstream.py", "raise RuntimeError\n")

    def _make_runtime_fixture(self) -> None:
        self._write(self.python_runtime, "python.exe", b"portable-python")
        self._write(self.python_runtime, "LICENSE.txt", "Python license fixture\n")
        self._write(self.python_runtime, "Lib/site-packages/fixture.py", "VALUE = 1\n")
        self._write(self.python_runtime, "Lib/site-packages/__pycache__/ignored.pyc", b"ignored")
        self._write(self.ffmpeg_runtime, "ffmpeg.exe", b"ffmpeg")
        self._write(self.ffmpeg_runtime, "ffprobe.exe", b"ffprobe")
        for name in (
            "avcodec-61.dll",
            "avfilter-10.dll",
            "avformat-61.dll",
            "avutil-59.dll",
            "swresample-5.dll",
            "swscale-8.dll",
        ):
            self._write(self.ffmpeg_runtime, name, name.encode("ascii"))

    def _inputs(self, suffix: str = "one") -> BuildInputs:
        return BuildInputs(
            repo=self.repo,
            mpt_source=self.mpt,
            python_runtime=self.python_runtime,
            ffmpeg_runtime=self.ffmpeg_runtime,
            ffmpeg_license=self.ffmpeg_license,
            materials=self.materials,
            output=self.root / f"portable-{suffix}",
            zip_path=self.root / f"portable-{suffix}.zip",
            verify_source_control=False,
            verify_runtime_executables=False,
            repo_commit="a" * 40,
        )

    def test_builds_and_verifies_exact_combined_layout(self) -> None:
        inputs = self._inputs()
        manifest = build_combined_portable(inputs)

        self.assertEqual([], verify_folder(inputs.output))
        self.assertEqual([], verify_zip(inputs.zip_path))
        self.assertTrue((inputs.output / "runtime/python/python.exe").is_file())
        self.assertTrue((inputs.output / "runtime/ffmpeg/avcodec-61.dll").is_file())
        self.assertTrue((inputs.output / "engine/MoneyPrinterTurbo/app/asgi.py").is_file())
        self.assertTrue((inputs.output / "engine/MoneyPrinterTurbo/storage/local_videos/material-01.mp4").is_file())
        self.assertFalse((inputs.output / "engine/MoneyPrinterTurbo/resource/songs").exists())
        self.assertFalse((inputs.output / "engine/MoneyPrinterTurbo/webui").exists())
        self.assertFalse((inputs.output / "engine/MoneyPrinterTurbo/docs").exists())
        self.assertFalse((inputs.output / "engine/MoneyPrinterTurbo/tests").exists())
        self.assertFalse((inputs.output / "core/__pycache__").exists())
        self.assertNotIn("ShiyiContentFactory.exe", {path.name for path in inputs.output.rglob("*")})
        self.assertFalse(manifest["runtime"]["runtime_downloads_allowed"])
        self.assertRegex(manifest["runtime"]["payload_sha256"], r"^[0-9A-F]{64}$")
        self.assertRegex(manifest["source"]["mpt_payload_sha256"], r"^[0-9A-F]{64}$")
        self.assertEqual("engine/MoneyPrinterTurbo/storage/local_videos", manifest["materials"]["root"])

        launcher_bytes = (inputs.output / ROOT_LAUNCHER_NAME).read_bytes()
        self.assertFalse(launcher_bytes.startswith(b"\xef\xbb\xbf"))
        launcher = launcher_bytes.decode("ascii")
        self.assertIn("scripts\\launch_combined.ps1", launcher)
        shared = "%~dp0runtime\\python\\python.exe"
        self.assertIn(f'-MptPython "{shared}"', launcher)
        self.assertIn(f'-AppPython "{shared}"', launcher)
        self.assertIn(
            '"%SHIYI_LAUNCHER_PYTHON%" -I -S -B -X utf8 '
            '"%~dp0tools\\verify_combined_portable.py" "%~dp0." --startup',
            launcher,
        )
        powershell_launcher = (inputs.output / "scripts" / "launch_combined.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('@("-I", "-S", "-B", "-X", "utf8", $launcher', powershell_launcher)
        self.assertIn("[switch]$AgentTestReview", powershell_launcher)
        self.assertIn('if ($AgentTestReview) { $arguments += "--agent-test-review" }', powershell_launcher)
        self.assertNotRegex(launcher.casefold(), r"pip\s+install|uv\s+sync|npx\s+--yes")
        packaged_verifier = subprocess.run(
            [sys.executable, str(inputs.output / "tools/verify_combined_portable.py"), str(inputs.output), "--startup"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(0, packaged_verifier.returncode, packaged_verifier.stdout + packaged_verifier.stderr)

    def test_manifest_and_zip_are_deterministic(self) -> None:
        first = self._inputs("first")
        second = self._inputs("second")
        build_combined_portable(first)
        build_combined_portable(second)

        self.assertEqual(sha256_file(first.zip_path), sha256_file(second.zip_path))
        first_manifest = json.loads((first.output / PACKAGE_MANIFEST).read_text(encoding="utf-8"))
        expected_paths = sorted(
            (
                path.relative_to(first.output).as_posix()
                for path in first.output.rglob("*")
                if path.is_file() and path.name not in {PACKAGE_MANIFEST, CHECKSUMS_FILE}
            ),
            key=str.casefold,
        )
        self.assertEqual(expected_paths, [entry["path"] for entry in first_manifest["files"]])
        for entry in first_manifest["files"]:
            target = first.output / Path(entry["path"])
            self.assertEqual(target.stat().st_size, entry["size"])
            self.assertEqual(sha256_file(target), entry["sha256"])
        with zipfile.ZipFile(first.zip_path) as archive:
            self.assertEqual(
                sorted(archive.namelist(), key=str.casefold),
                archive.namelist(),
            )
            self.assertTrue(all(name.startswith(PACKAGE_ROOT_NAME + "/") for name in archive.namelist()))
        self.assertTrue(all(info.date_time == FIXED_ZIP_TIME for info in archive.infolist()))
        longest = max(
            len(f"C:\\Users\\Default\\Downloads\\{name.replace('/', '\\')}")
            for name in archive.namelist()
        )
        self.assertLessEqual(longest, 248)

    def test_zip_rejects_windows_casefold_collisions(self) -> None:
        inputs = self._inputs("zip-casefold")
        build_combined_portable(inputs)
        prefix = PACKAGE_ROOT_NAME + "/runtime/python/Lib/site-packages/"
        with zipfile.ZipFile(inputs.zip_path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(prefix + "CaseProbe.dat", b"first")
            archive.writestr(prefix + "caseprobe.dat", b"second")
        errors = verify_zip(inputs.zip_path)
        self.assertTrue(any("Windows" in error and "冲突路径" in error for error in errors), errors)

    def test_zip_rejects_windows_device_and_invalid_component_names(self) -> None:
        for name in ("CON.dat", "name. ", "bad:name.dat"):
            with self.subTest(name=name):
                inputs = self._inputs("zip-invalid-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8])
                build_combined_portable(inputs)
                prefix = PACKAGE_ROOT_NAME + "/runtime/python/Lib/site-packages/"
                with zipfile.ZipFile(inputs.zip_path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr(prefix + name, b"invalid-on-windows")
                errors = verify_zip(inputs.zip_path)
                self.assertTrue(any("Windows 非法" in error for error in errors), errors)

    def test_zip_rejects_non_regular_file_entries(self) -> None:
        inputs = self._inputs("zip-non-regular")
        build_combined_portable(inputs)
        relative = PACKAGE_ROOT_NAME + "/runtime/python/Lib/site-packages/link.dat"
        info = zipfile.ZipInfo(relative, date_time=FIXED_ZIP_TIME)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(inputs.zip_path, "a") as archive:
            archive.writestr(info, b"outside-target")
        errors = verify_zip(inputs.zip_path)
        self.assertTrue(any("普通文件" in error for error in errors), errors)

    def test_existing_success_is_never_overwritten(self) -> None:
        inputs = self._inputs("existing")
        inputs.output.mkdir()
        sentinel = inputs.output / "keep.txt"
        sentinel.write_text("previous-success", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            build_combined_portable(inputs)
        self.assertEqual("previous-success", sentinel.read_text(encoding="utf-8"))
        self.assertFalse(inputs.zip_path.exists())

    def test_staging_failure_does_not_publish_partial_output(self) -> None:
        inputs = self._inputs("failed")
        with patch("tools.build_combined_portable._write_fixed_zip", side_effect=RuntimeError("fixture failure")):
            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                build_combined_portable(inputs)
        self.assertFalse(inputs.output.exists())
        self.assertFalse(inputs.zip_path.exists())

    def test_validator_recomputes_hashes_and_scans_paths_and_secrets(self) -> None:
        inputs = self._inputs("tamper")
        build_combined_portable(inputs)
        app = inputs.output / "app.py"
        app.write_text("print('tampered')\n", encoding="utf-8")
        errors = verify_folder(inputs.output)
        self.assertTrue(any("SHA-256" in error for error in errors), errors)

        usage = inputs.output / "使用说明.txt"
        usage.write_text('cache = "C:\\Users\\ROG\\private"\n', encoding="utf-8")
        errors = verify_folder(inputs.output)
        self.assertTrue(any("Windows 绝对路径" in error for error in errors), errors)

        usage.write_text('api_key = "abcdefghijklmnop"\n', encoding="utf-8")
        errors = verify_folder(inputs.output)
        self.assertTrue(any("Key、Cookie" in error for error in errors), errors)

    def test_python_launcher_rechecks_package_when_root_bat_is_bypassed(self) -> None:
        inputs = self._inputs("direct-launch")
        build_combined_portable(inputs)
        verify_packaged_integrity(inputs.output)

        target = inputs.output / "engine" / "MoneyPrinterTurbo" / "app" / "asgi.py"
        target.write_text("APP = 'tampered'\n", encoding="utf-8")
        with self.assertRaises(LauncherError) as context:
            verify_packaged_integrity(inputs.output)
        self.assertEqual("PACKAGE_INTEGRITY_MISMATCH", context.exception.code)
        self.assertNotIn(str(target), json.dumps(context.exception.as_dict(), ensure_ascii=False))

    def test_rejects_invalid_material_count_and_non_relocatable_runtime(self) -> None:
        no_materials = replace(self._inputs("none"), materials=())
        with self.assertRaisesRegex(ValueError, "1 到 24"):
            build_combined_portable(no_materials)

        self._write(self.python_runtime, "python._pth", "C:\\Users\\ROG\\host-python\n")
        non_relocatable = self._inputs("absolute")
        with self.assertRaisesRegex(ValueError, "不可迁移|Windows 绝对路径"):
            build_combined_portable(non_relocatable)
        self.assertFalse(non_relocatable.output.exists())
        self.assertFalse(non_relocatable.zip_path.exists())

    def test_runtime_vendor_examples_do_not_mask_real_relocation_metadata(self) -> None:
        self._write(
            self.python_runtime,
            "Lib/site-packages/vendor/example.py",
            'EXAMPLE = "C:\\\\Users\\\\someone\\\\fixture"\napi_key = "example-key"\n',
        )
        valid = self._inputs("vendor-example")
        build_combined_portable(valid)
        self.assertEqual([], verify_folder(valid.output))

        self._write(
            self.python_runtime,
            "Lib/site-packages/vendor-1.0.dist-info/direct_url.json",
            '{"url":"file:///C:/private/build"}\n',
        )
        invalid = self._inputs("direct-url")
        with self.assertRaisesRegex(ValueError, "direct_url"):
            build_combined_portable(invalid)
        self.assertFalse(invalid.output.exists())

    def test_rejects_python_scripts_entrypoints_with_embedded_build_paths(self) -> None:
        self._write(self.python_runtime, "Scripts/uvicorn.exe", b"embedded-build-path")
        inputs = self._inputs("runtime-scripts")
        with self.assertRaisesRegex(ValueError, "Scripts"):
            build_combined_portable(inputs)
        self.assertFalse(inputs.output.exists())

    def test_rejects_python_automatic_startup_hooks(self) -> None:
        for name in (
            "Lib/site-packages/sitecustomize.py",
            "Lib/site-packages/usercustomize.py",
            "Lib/site-packages/sitecustomize/__init__.py",
            "Lib/site-packages/SITECUSTOMIZE.cpython-312.pyc",
        ):
            with self.subTest(name=name):
                runtime = self.root / ("hook-runtime-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8])
                shutil.copytree(self.python_runtime, runtime)
                self._write(runtime, name, "raise RuntimeError('startup hook')\n")
                inputs = replace(self._inputs("startup-hook-" + runtime.name), python_runtime=runtime)
                with self.assertRaisesRegex(ValueError, "启动钩子"):
                    build_combined_portable(inputs)
                self.assertFalse(inputs.output.exists())

    def test_startup_rechecks_immutable_payload_but_allows_bounded_state(self) -> None:
        inputs = self._inputs("startup")
        build_combined_portable(inputs)
        self._write(inputs.output, "runtime/config.json", "{}\n")
        self._write(inputs.output, "runtime/jobs/job-1/job.json", "{}\n")
        self._write(inputs.output, "engine/MoneyPrinterTurbo/storage/tasks/task-1/final.mp4", b"rendered")

        self.assertNotEqual([], verify_folder(inputs.output))
        self.assertEqual([], verify_folder(inputs.output, allow_runtime_state=True))

        self._write(inputs.output, "engine/MoneyPrinterTurbo/app/__pycache__/asgi.cpython-314.pyc", b"generated")
        bytecode_errors = verify_folder(inputs.output, allow_runtime_state=True)
        self.assertTrue(any("非白名单" in error or "清单" in error for error in bytecode_errors), bytecode_errors)

        self._write(inputs.output, "runtime/jobs/job-1/evil.py", "raise RuntimeError\n")
        errors = verify_folder(inputs.output, allow_runtime_state=True)
        self.assertTrue(any("未声明类型或可执行文件" in error for error in errors), errors)

        (inputs.output / "runtime/jobs/job-1/evil.py").unlink()
        self._write(inputs.output, "core/unlisted.py", "VALUE = 2\n")
        errors = verify_folder(inputs.output, allow_runtime_state=True)
        self.assertTrue(any("实际文件集合不一致" in error for error in errors), errors)

    def test_mutable_state_uses_an_allowlist_not_a_partial_executable_denylist(self) -> None:
        inputs = self._inputs("mutable-types")
        build_combined_portable(inputs)
        for suffix in ("vbs", "js", "jse", "wsf", "hta", "pyw", "lnk"):
            self._write(inputs.output, f"runtime/jobs/job-1/evil.{suffix}", b"fixture")
        errors = verify_folder(inputs.output, allow_runtime_state=True)
        for suffix in ("vbs", "js", "jse", "wsf", "hta", "pyw", "lnk"):
            self.assertTrue(any(f"evil.{suffix}" in error for error in errors), errors)

    def test_formal_preflight_executes_python_ffmpeg_and_ffprobe(self) -> None:
        inputs = replace(self._inputs("preflight"), verify_runtime_executables=True)
        results = [
            subprocess.CompletedProcess([], 0, stdout="Python 3.12.13\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="ffmpeg version 7.1\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="ffprobe version 7.1\n", stderr=""),
        ]
        with patch("tools.build_combined_portable.subprocess.run", side_effect=results) as runner:
            build_combined_portable(inputs)
        self.assertEqual(3, runner.call_count)
        self.assertEqual("--version", runner.call_args_list[0].args[0][1])
        self.assertEqual("-version", runner.call_args_list[1].args[0][1])
        self.assertEqual("-version", runner.call_args_list[2].args[0][1])

    @unittest.skipUnless(os.name == "nt" and hasattr(Path, "is_junction"), "NTFS Junction test")
    def test_rejects_junctions_in_sources_and_post_launch_state(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        self._write(outside, "leak.py", "SECRET = 'outside'\n")

        source_junction = self.repo / "core" / "pivot"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(source_junction), str(outside)],
            check=False,
            capture_output=True,
        )
        if created.returncode != 0:
            self.skipTest("cannot create NTFS Junction")
        try:
            self.assertTrue(source_junction.is_junction())
            with self.assertRaisesRegex(ValueError, "Junction|重解析"):
                build_combined_portable(self._inputs("source-junction"))
        finally:
            os.rmdir(source_junction)

        inputs = self._inputs("state-junction")
        build_combined_portable(inputs)
        state_parent = inputs.output / "runtime" / "jobs"
        state_parent.mkdir(parents=True)
        state_junction = state_parent / "pivot"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(state_junction), str(outside)],
            check=False,
            capture_output=True,
        )
        if created.returncode != 0:
            self.skipTest("cannot create NTFS Junction")
        try:
            errors = verify_folder(inputs.output, allow_runtime_state=True)
            self.assertTrue(any("Junction" in error or "重解析" in error for error in errors), errors)
        finally:
            os.rmdir(state_junction)


if __name__ == "__main__":
    unittest.main()
