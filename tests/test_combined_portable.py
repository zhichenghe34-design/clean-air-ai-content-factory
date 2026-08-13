from __future__ import annotations

import base64
import gzip
import hashlib
import importlib.metadata as importlib_metadata
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from core.motion_runtime_contract import (
    HYPERFRAMES_VERSION as CONTRACT_HYPERFRAMES_VERSION,
    NODE_MINIMUM_MAJOR as CONTRACT_NODE_MINIMUM_MAJOR,
)
from scripts import launch_combined
from tools.build_combined_portable import (
    CHECKSUMS_FILE,
    EXPECTED_MPT_COMMIT,
    EXPECTED_MPT_DETERMINISTIC_MODIFICATIONS,
    EXPECTED_MPT_REQUIRED_PROBE,
    FIXED_ZIP_TIME,
    INSTALLER_LAUNCHER_NAME,
    PACKAGE_MANIFEST,
    PACKAGE_ROOT_NAME,
    MIGRATION_LAUNCHER_NAME,
    ROOT_LAUNCHER_NAME,
    STOP_LAUNCHER_NAME,
    BuildInputs,
    MotionRuntimeInputs,
    MOTION_PACKAGE_PROFILE,
    PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS,
    SYSTEM_EDGE_BROWSER_STRATEGY,
    SYSTEM_EDGE_MINIMUM_MAJOR,
    build_combined_portable,
    _read_hyperframes_license_contract,
    _validate_hyperframes_dependency_closure,
    sha256_file,
)
from tools import verify_combined_portable
from tools.verify_combined_portable import verify_folder, verify_zip
from scripts.launch_combined import LauncherError, verify_packaged_integrity


class CombinedPortableTests(unittest.TestCase):
    def test_builder_loads_verifier_under_isolated_cli_python(self) -> None:
        builder = Path(__file__).resolve().parents[1] / "tools/build_combined_portable.py"
        probe = (
            "import runpy\n"
            f"namespace = runpy.run_path({str(builder)!r}, run_name='isolated_builder_probe')\n"
            "verify_folder, verify_zip = namespace['_load_portable_verifiers']()\n"
            "assert callable(verify_folder) and callable(verify_zip)\n"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-X", "utf8", "-c", probe],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.mpt = self.root / "mpt"
        self.python_runtime = self.root / "python-runtime"
        self.ffmpeg_runtime = self.repo / "third_party" / "ffmpeg" / "runtime" / "win-x64"
        self.materials = (self.root / "input-01.mp4", self.root / "input-02.mp4")
        self._make_repo_fixture()
        self._make_mpt_fixture()
        self._make_runtime_fixture()
        for index, material in enumerate(self.materials, start=1):
            material.write_bytes(b"fixture-mp4-" + bytes([index]))

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
        self._write(
            self.repo,
            "examples/pattern_cards.jsonl",
            '{"item_id":"fixture","status":"structure_only_not_evidence"}\n',
        )
        self._write(self.repo, "core/__init__.py", "")
        self._write(self.repo, "core/runtime.py", "VALUE = 1\n")
        self._write(self.repo, "core/__pycache__/ignored.pyc", b"ignored")
        self._write(self.repo, "static/index.html", "<main>fixture</main>\n")
        self._write(self.repo, "static/app.js", "console.log('fixture');\n")
        self._write(self.repo, "catalog/tools.json", "{}\n")
        self._write(self.repo, "agent-skills/fixture/SKILL.md", "# fixture\n")
        self._write(self.repo, "agent-skills/fixture/assets/hero.png", b"fixture-png")
        self._write(self.repo, "docs/fonts/NotoSansSC-Regular.ttf", b"regular-font")
        self._write(self.repo, "docs/fonts/NotoSansSC-Bold.ttf", b"bold-font")
        self._write(self.repo, "docs/fonts/OFL.txt", "Noto OFL fixture\n")
        self._write(self.repo, "docs/fonts/SOURCE.md", "Noto source fixture\n")
        source_root = Path(__file__).resolve().parents[1]
        self._write(
            self.repo,
            "scripts/install_combined.py",
            (source_root / "scripts/install_combined.py").read_bytes(),
        )
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
        for name in ("build_public_evidence.py", "verify_public_evidence.py"):
            self._write(self.repo, f"tools/{name}", (source_root / "tools" / name).read_bytes())
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
                    "portable_subset": {
                        "id": "SHIYI_MPT_OFFLINE_SUBSET_V1",
                        "mode": "video_only_adapted_runtime_dependency_closure",
                        "deterministic_modifications": EXPECTED_MPT_DETERMINISTIC_MODIFICATIONS,
                        "required_probe": EXPECTED_MPT_REQUIRED_PROBE,
                    },
                    "excluded_components": [
                        "resource/fonts",
                        "resource/songs",
                        "webui",
                        "app/services/llm.py",
                        "app/controllers/v1/llm.py",
                        "app/services/upload_post.py",
                        "app/services/elevenlabs_music.py",
                        "app/services/sonilo.py",
                    ],
                },
                ensure_ascii=False,
            )
            + "\n",
        )
        source_root = Path(__file__).resolve().parents[1]
        for relative in (
            "third_party/hyperframes/LICENSE",
            "third_party/hyperframes/README.md",
            "third_party/hyperframes/upstream-lock.json",
            "third_party/hyperframes/windows-mf-patch.json",
            "tools/apply_hyperframes_windows_mf_patch.py",
            "tools/verify_ffmpeg_distribution.py",
            "third_party/python_runtime/moviepy-windows-mf-patch.json",
            "tools/apply_moviepy_windows_mf_patch.py",
        ):
            self._write(self.repo, relative, (source_root / relative).read_bytes())
        dependency_license = self._write(
            self.repo,
            "third_party/hyperframes/dependency-licenses/esbuild-win32-x64-MIT.txt",
            "Verified fixture MIT license text\n",
        )
        self._write(
            self.repo,
            "third_party/hyperframes/dependency-license-overrides.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "complete",
                    "verified_overrides": [
                        {
                            "name": "@esbuild/win32-x64",
                            "version": "0.25.12",
                            "spdx": "MIT",
                            "copyright": "Copyright fixture",
                            "source_url": (
                                "https://raw.githubusercontent.com/evanw/esbuild/"
                                "208f539945b145e7c9d6d844290f81c3fe5af320/npm/esbuild/LICENSE.md"
                            ),
                            "source_commit": "208f539945b145e7c9d6d844290f81c3fe5af320",
                            "source_sha256": sha256_file(dependency_license),
                            "license_file": "dependency-licenses/esbuild-win32-x64-MIT.txt",
                            "license_sha256": sha256_file(dependency_license),
                            "notice_files": [],
                        }
                    ],
                    "unresolved": [],
                },
                ensure_ascii=False,
            )
            + "\n",
        )
        self._write(
            self.repo,
            "third_party/python_runtime/README.md",
            "# fixture Python runtime license boundary\n",
        )
        self._write(
            self.repo,
            "third_party/python_runtime/pruned-import-boundary.json",
            (source_root / "third_party/python_runtime/pruned-import-boundary.json").read_bytes(),
        )
        self._write(
            self.repo,
            "third_party/python_runtime/dependency-license-overrides.json",
            json.dumps(
                {
                    "schema_version": 2,
                    "status": "complete",
                    "verified_overrides": [],
                    "unresolved": [],
                },
                ensure_ascii=False,
            )
            + "\n",
        )

    def _make_mpt_fixture(self) -> None:
        boundary_license = (self.repo / "third_party/moneyprinterturbo/LICENSE").read_text(encoding="utf-8")
        self._write(self.mpt, "app/__init__.py", "")
        self._write(self.mpt, "app/asgi.py", "APP = 'fixture'\n")
        self._write(
            self.mpt,
            "app/router.py",
            "from app.controllers.v1 import llm, video\n\n"
            "root_api_router = object()\n"
            "root_api_router.include_router(video.router)\n"
            "root_api_router.include_router(llm.router)\n",
        )
        self._write(
            self.mpt,
            "app/services/task.py",
            "from app.services import (\n"
            "    elevenlabs_music,\n"
            "    llm,\n"
            "    material,\n"
            "    sonilo,\n"
            ")\n"
            "from app.services import upload_post\n"
            "from app.services import state as sm\n"
            "from app.utils import file_security, utils\n\n\n"
            "_VIDEO_MUSIC_PROVIDERS = {\n"
            "    'sonilo': {'service': sonilo},\n"
            "    'elevenlabs': {'service': elevenlabs_music},\n"
            "}\n\n\n"
            "def _get_video_music_prompt(params: VideoParams) -> str:\n"
            "    return ''\n\n\n"
            "def render_with_approved_input(params):\n"
            "    return params.video_script, params.video_terms\n",
        )
        self._write(self.mpt, "app/prompts.json", "{}\n")
        self._write(self.mpt, "app/services/llm.py", "raise RuntimeError('excluded llm')\n")
        self._write(self.mpt, "app/controllers/v1/llm.py", "raise RuntimeError('excluded route')\n")
        self._write(self.mpt, "app/services/upload_post.py", "raise RuntimeError('excluded upload')\n")
        self._write(self.mpt, "app/services/elevenlabs_music.py", "VIDEO_CODEC = 'libx264'\n")
        self._write(self.mpt, "app/services/sonilo.py", "VIDEO_CODEC = 'libx264'\n")
        self._write(
            self.mpt,
            "app/services/video.py",
            "from functools import lru_cache\n\n"
            "_DEFAULT_VIDEO_CODEC = \"libx264\"\n"
            "_SUPPORTED_VIDEO_CODECS = (\n"
            "    \"libx264\",\n"
            "    \"h264_nvenc\",\n"
            "    \"h264_amf\",\n"
            "    \"h264_qsv\",\n"
            "    \"h264_mf\",\n"
            "    \"h264_videotoolbox\",\n"
            ")\n"
            "_runtime_disabled_video_codecs = set()\n\n\n"
            "def _get_configured_video_codec() -> str:\n"
            "    return _DEFAULT_VIDEO_CODEC\n\n\n"
            "@lru_cache(maxsize=16)\n"
            "def _ffmpeg_encoder_exists(ffmpeg_binary: str, codec: str) -> bool:\n"
            "    \"\"\"\n"
            "    检查当前 FFmpeg 是否声明支持指定编码器。\n\n"
            "    这只能证明 FFmpeg 编译时包含该 encoder，不能证明当前机器硬件和驱动\n"
            "    一定可用。因此实际编码失败时仍会再回退到 libx264。\n"
            "    \"\"\"\n"
            "    return True\n\n\n"
            "def _get_effective_video_codec(preferred_codec: str | None = None) -> str:\n"
            "    return preferred_codec or _DEFAULT_VIDEO_CODEC\n\n\n"
            "def _disable_runtime_video_codec(codec: str, reason: str):\n"
            "    _runtime_disabled_video_codecs.add(codec)\n\n\n"
            "def _get_temp_audio_dir(output_dir: str) -> str:\n"
            "    return output_dir\n\n\n"
            "def _fallback_write_videofile(clip, output_file: str, failed_codec: str, reason: str, **kwargs):\n"
            "    clip.write_videofile(output_file, codec=_DEFAULT_VIDEO_CODEC, **kwargs)\n"
            "    return _DEFAULT_VIDEO_CODEC\n\n\n"
            "def _write_videofile_with_codec_fallback(clip, output_file: str, codec: str, **kwargs):\n"
            "    clip.write_videofile(output_file, codec=codec, **kwargs)\n"
            "    return codec\n\n\n"
            "def _escape_ffmpeg_concat_path(file_path: str) -> str:\n"
            "    return file_path\n\n\n"
            "def concat_video_clips_with_ffmpeg(codec, threads=2):\n"
            "    command = [\n"
            "            \"-c:v\",\n"
            "            codec,\n"
            "            \"-threads\",\n"
            "            str(threads or 2),\n"
            "            \"-pix_fmt\",\n"
            "            \"yuv420p\",\n"
            "    ]\n"
            "    def run_concat(value):\n"
            "        return value\n"
            "    try:\n"
            "        effective_codec = _get_effective_video_codec()\n"
            "        try:\n"
            "            return run_concat(effective_codec)\n"
            "        except Exception as exc:\n"
            "            if effective_codec == _DEFAULT_VIDEO_CODEC:\n"
            "                raise\n"
            "            result_codec = run_concat(_DEFAULT_VIDEO_CODEC)\n"
            "            _disable_runtime_video_codec(effective_codec, str(exc))\n"
            "            return result_codec\n"
            "    finally:\n"
            "        pass\n\n\n"
            "def process_image(final_clip, video_file):\n"
            "                final_clip.write_videofile(video_file, fps=30, logger=None)\n",
        )
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
        self._write(
            self.python_runtime,
            "Lib/site-packages/imageio_ffmpeg/binaries/README.md",
            "Bundled binary metadata fixture\n",
        )
        self._write(
            self.python_runtime,
            "Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe",
            b"redundant-ffmpeg",
        )
        metadata = (
            "Metadata-Version: 2.4\n"
            "Name: fixture\n"
            "Version: 1.0\n"
            "License-Expression: MIT\n"
            "License-File: LICENSE\n"
            "\n"
        )
        self._write(
            self.python_runtime,
            "Lib/site-packages/fixture-1.0.dist-info/METADATA",
            metadata,
        )
        self._write(
            self.python_runtime,
            "Lib/site-packages/fixture-1.0.dist-info/licenses/LICENSE",
            "Fixture MIT license text\n",
        )
        self._write(
            self.python_runtime,
            "Lib/site-packages/fixture-1.0.dist-info/RECORD",
            "fixture.py,,\n"
            "imageio_ffmpeg/binaries/README.md,,\n"
            "imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe,,\n"
            "fixture-1.0.dist-info/METADATA,,\n"
            "fixture-1.0.dist-info/licenses/LICENSE,,\n"
            "fixture-1.0.dist-info/RECORD,,\n",
        )
        prune_roots = {
            "ctranslate2",
            "faster-whisper",
            "litellm",
            "onnxruntime",
            "streamlit",
            "streamlit-tour",
            "tokenizers",
        }
        orphan_names = sorted(set(PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS) - prune_roots)
        for name, version in sorted(PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS.items()):
            dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
            requirements: list[str] = []
            if name == "faster-whisper":
                requirements = [
                    "ctranslate2<5,>=4.0",
                    "onnxruntime<2,>=1.14",
                    "tokenizers<1,>=0.13",
                ]
            elif name == "streamlit":
                requirements = orphan_names
            elif name == "streamlit-tour":
                requirements = ["streamlit>=1.51"]
            metadata_lines = [
                "Metadata-Version: 2.4",
                f"Name: {name}",
                f"Version: {version}",
            ]
            metadata_lines.extend(f"Requires-Dist: {requirement}" for requirement in requirements)
            metadata_lines.append("")
            self._write(
                self.python_runtime,
                f"Lib/site-packages/{dist_info}/METADATA",
                "\n".join(metadata_lines) + "\n",
            )
            self._write(
                self.python_runtime,
                f"Lib/site-packages/{dist_info}/RECORD",
                f"{dist_info}/METADATA,,\n{dist_info}/RECORD,,\n",
            )
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
        ffmpeg_license_root = self.repo / "third_party" / "ffmpeg" / "licenses"
        for name in (
            "FFmpeg-COPYING.LGPLv2.1",
            "FFmpeg-COPYING.LGPLv3",
            "FFmpeg-LICENSE.md",
            "zlib-LICENSE",
        ):
            self._write(ffmpeg_license_root, name, f"{name} fixture\n")
        entries = [
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in sorted(self.ffmpeg_runtime.iterdir(), key=lambda item: item.name.casefold())
        ]
        self._write(
            self.repo,
            "third_party/ffmpeg/upstream-lock.json",
            json.dumps({"schema_version": 1, "runtime": {"files": entries}}, indent=2) + "\n",
        )

    def _inputs(self, suffix: str = "one") -> BuildInputs:
        return BuildInputs(
            repo=self.repo,
            mpt_source=self.mpt,
            python_runtime=self.python_runtime,
            materials=self.materials,
            output=self.root / f"portable-{suffix}",
            zip_path=self.root / f"portable-{suffix}.zip",
            verify_source_control=False,
            verify_runtime_executables=False,
            repo_commit="a" * 40,
        )

    def _motion_inputs(self, suffix: str = "motion") -> BuildInputs:
        source_root = Path(__file__).resolve().parents[1]
        source_ffmpeg = source_root / "third_party" / "ffmpeg"
        fixture_ffmpeg = self.repo / "third_party" / "ffmpeg"
        if fixture_ffmpeg.exists():
            shutil.rmtree(fixture_ffmpeg)
        shutil.copytree(source_ffmpeg, fixture_ffmpeg)
        raw_reviewed_python = os.environ.get("SHIYI_REVIEWED_PYTHON_SITE_PACKAGES", "").strip()
        if raw_reviewed_python:
            reviewed_python = Path(raw_reviewed_python)
        else:
            try:
                reviewed_python = Path(importlib_metadata.distribution("moviepy").locate_file(""))
            except importlib_metadata.PackageNotFoundError:
                self.skipTest(
                    "motion-primary package integration requires the frozen external MoviePy 2.2.1 runtime input; "
                    "set SHIYI_REVIEWED_PYTHON_SITE_PACKAGES for release-gate runs"
                )
        reviewed_python = reviewed_python.resolve()
        reviewed_writer = reviewed_python / "moviepy/video/io/ffmpeg_writer.py"
        reviewed_record = reviewed_python / "moviepy-2.2.1.dist-info/RECORD"
        if (
            not reviewed_writer.is_file()
            or not reviewed_record.is_file()
            or sha256_file(reviewed_writer)
            != "347E9EE5403A0CBFFDDF6205D7DE9A8B38708BDC9853F22383CFE4987AFA62D3"
            or sha256_file(reviewed_record)
            not in {
                # Exact frozen uv-installed runtime input.
                "F8D61AAAE58D557D0F67AF5016B5AD15D791A9D79E3B7121BC3CD9C296D78ED8",
                # The same official wheel archive before uv adds INSTALLER and REQUESTED.
                "290DB0D8A3047B66AC881C733121587FDA59B5DF95811D17DF86457A3014186B",
            }
        ):
            self.fail(
                "motion-primary tests require the exact reviewed MoviePy 2.2.1 site-packages; "
                "set SHIYI_REVIEWED_PYTHON_SITE_PACKAGES to the protected release input"
            )
        for name in ("moviepy", "moviepy-2.2.1.dist-info"):
            shutil.copytree(
                reviewed_python / name,
                self.python_runtime / "Lib" / "site-packages" / name,
                dirs_exist_ok=True,
            )
        staged_dist = self.python_runtime / "Lib/site-packages/moviepy-2.2.1.dist-info"
        staged_record = staged_dist / "RECORD"
        if sha256_file(staged_record) == "290DB0D8A3047B66AC881C733121587FDA59B5DF95811D17DF86457A3014186B":
            (staged_dist / "INSTALLER").write_bytes(b"uv")
            (staged_dist / "REQUESTED").write_bytes(b"")
            record_lines = staged_record.read_text(encoding="utf-8").splitlines()
            record_lines.extend((
                "moviepy-2.2.1.dist-info/INSTALLER,sha256=5hhM4Q4mYTT9z6QB6PGpUAW81PGNFrYrdXMj4oM_6ak,2",
                "moviepy-2.2.1.dist-info/REQUESTED,sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0",
            ))
            record_lines.sort(key=lambda line: line.split(",", 1)[0])
            staged_record.write_text("\n".join(record_lines) + "\n", encoding="utf-8", newline="\n")
        self.assertEqual(
            "F8D61AAAE58D557D0F67AF5016B5AD15D791A9D79E3B7121BC3CD9C296D78ED8",
            sha256_file(staged_record),
        )
        node = self.root / "node-runtime"
        hyperframes = self.root / "hyperframes-runtime"
        self._write(node, "node.exe", b"fixed-node")
        self._write(node, "LICENSE", "Node license fixture\n")
        self._write(
            hyperframes,
            "node_modules/hyperframes/package.json",
            '{"name":"hyperframes","version":"0.7.86","license":"Apache-2.0",'
            '"type":"module","dependencies":{"esbuild":"0.25.12"}}\n',
        )
        self._write(
            hyperframes,
            "node_modules/hyperframes/bin/hyperframes.mjs",
            "import { marker } from 'esbuild';\nif (marker !== 'bundled') process.exit(9);\nconsole.log('0.7.86');\n",
        )
        self._write(
            hyperframes,
            "node_modules/hyperframes/dist/cli.js",
            (source_root / "node_modules/hyperframes/dist/cli.js").read_bytes(),
        )
        self._write(
            hyperframes,
            "node_modules/esbuild/package.json",
            '{"name":"esbuild","version":"0.25.12","license":"MIT","type":"module",'
            '"exports":"./index.js","optionalDependencies":{"@esbuild/win32-x64":"0.25.12"}}\n',
        )
        self._write(hyperframes, "node_modules/esbuild/index.js", "export const marker = 'bundled';\n")
        self._write(hyperframes, "node_modules/esbuild/LICENSE.md", "MIT fixture\n")
        self._write(
            hyperframes,
            "node_modules/@esbuild/win32-x64/package.json",
            '{"name":"@esbuild/win32-x64","version":"0.25.12","license":"MIT"}\n',
        )
        return replace(
            self._inputs(suffix),
            package_profile=MOTION_PACKAGE_PROFILE,
            motion_runtime=MotionRuntimeInputs(
                node_runtime=node,
                hyperframes_runtime=hyperframes,
                node_version="22.13.1",
                hyperframes_version="0.7.86",
            ),
        )

    def test_motion_primary_requires_and_hashes_complete_offline_runtime(self) -> None:
        with self.assertRaisesRegex(ValueError, "必须显式提供离线动画运行时"):
            build_combined_portable(
                replace(self._inputs("missing-motion"), package_profile=MOTION_PACKAGE_PROFILE)
            )

        inputs = self._motion_inputs()
        manifest = build_combined_portable(inputs)
        self.assertEqual(2, manifest["schema_version"])
        self.assertEqual("motion_primary", manifest["package_profile"])
        motion_manifest = manifest["motion_runtime"]
        self.assertEqual("offline_bundled_with_system_browser", motion_manifest["mode"])
        self.assertEqual(SYSTEM_EDGE_BROWSER_STRATEGY, motion_manifest["browser_strategy"])
        self.assertEqual(SYSTEM_EDGE_MINIMUM_MAJOR, motion_manifest["browser_minimum_major"])
        self.assertTrue(motion_manifest["system_browser_required"])
        self.assertNotIn("browser_payload", motion_manifest)
        self.assertTrue(motion_manifest["startup_canary_required"])
        self.assertFalse(manifest["motion_runtime"]["runtime_downloads_allowed"])
        self.assertNotIn("browser_version", motion_manifest)
        self.assertNotIn("system_fallback_allowed", motion_manifest)
        self.assertEqual("h264_mf", motion_manifest["codec_strategy"])
        self.assertEqual("shiyi-hyperframes-windows-mf", motion_manifest["hyperframes_patch_id"])
        self.assertEqual("1.2.0", motion_manifest["hyperframes_patch_version"])
        self.assertEqual(
            "86DA751BA397FF551355BA0C90370D732A297C3DC4652C981E9A8146D8EAC108",
            motion_manifest["hyperframes_patched_cli_sha256"],
        )
        runtime_contract = manifest["runtime"]
        self.assertEqual("h264_mf", runtime_contract["video_codec"])
        self.assertEqual("LGPL-2.1-or-later", runtime_contract["ffmpeg_distribution"]["license"])
        self.assertEqual(9, runtime_contract["ffmpeg_distribution"]["runtime_file_count"])
        self.assertEqual("shiyi-moviepy-windows-mf", runtime_contract["moviepy_patch"]["patch_id"])
        self.assertTrue(runtime_contract["moviepy_patch"]["record_consistent"])
        for relative in (
            "runtime/node/node.exe",
            "runtime/hyperframes/node_modules/hyperframes/bin/hyperframes.mjs",
            "runtime/hyperframes/node_modules/esbuild/index.js",
            "runtime/hyperframes/RUNTIME-MANIFEST.json",
            "licenses/Node-license.txt",
            "licenses/HyperFrames-Apache-2.0.txt",
            "licenses/HyperFrames-third-party-SBOM.json",
            "licenses/hyperframes-dependencies/esbuild-win32-x64-MIT.txt",
            "licenses/FFmpeg-runtime-lock.json",
            "licenses/FFmpeg-COPYING.LGPLv2.1",
            "licenses/FFmpeg-COPYING.LGPLv3",
            "licenses/FFmpeg-LICENSE.md",
            "licenses/zlib-LICENSE",
        ):
            self.assertTrue((inputs.output / relative).is_file(), relative)
        ffmpeg_lock = json.loads(
            (inputs.output / "licenses/FFmpeg-runtime-lock.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            (inputs.output / "third_party/ffmpeg/upstream-lock.json").read_bytes(),
            (inputs.output / "licenses/FFmpeg-runtime-lock.json").read_bytes(),
        )
        expected_ffmpeg = {entry["name"] for entry in ffmpeg_lock["runtime"]["files"]}
        actual_ffmpeg = {path.name for path in (inputs.output / "runtime/ffmpeg").iterdir()}
        self.assertEqual(expected_ffmpeg, actual_ffmpeg)
        self.assertEqual(9, len(actual_ffmpeg))
        for entry in ffmpeg_lock["runtime"]["files"]:
            runtime_file = inputs.output / "runtime/ffmpeg" / entry["name"]
            self.assertEqual(entry["bytes"], runtime_file.stat().st_size)
            self.assertEqual(entry["sha256"].upper(), sha256_file(runtime_file))
            self.assertEqual(b"MZ", runtime_file.read_bytes()[:2])
        hyperframes_manifest = json.loads(
            (inputs.output / "runtime/hyperframes/RUNTIME-MANIFEST.json").read_text(encoding="utf-8")
        )
        self.assertEqual(2, hyperframes_manifest["schema_version"])
        self.assertEqual("h264_mf", hyperframes_manifest["codec_strategy"])
        self.assertEqual("shiyi-hyperframes-windows-mf", hyperframes_manifest["patch_id"])
        self.assertEqual(
            hyperframes_manifest["patched_cli_sha256"],
            sha256_file(inputs.output / "runtime/hyperframes/node_modules/hyperframes/dist/cli.js"),
        )
        config = tomllib.loads(
            (inputs.output / "engine/MoneyPrinterTurbo/config.toml").read_text(encoding="utf-8")
        )
        self.assertEqual("h264_mf", config["app"]["video_codec"])
        video_service = (
            inputs.output / "engine/MoneyPrinterTurbo/app/services/video.py"
        ).read_text(encoding="utf-8")
        self.assertIn("SHIYI_MPT_H264_MF_CODEC_V1", video_service)
        self.assertEqual(1, video_service.count(".write_videofile("))
        self.assertNotIn("libx264", video_service)
        self.assertFalse(
            (inputs.output / "engine/MoneyPrinterTurbo/app/services/sonilo.py").exists()
        )
        self.assertFalse(
            (inputs.output / "engine/MoneyPrinterTurbo/app/services/elevenlabs_music.py").exists()
        )
        python_sbom = json.loads(
            (inputs.output / "licenses/Python-runtime-SBOM.json").read_text(encoding="utf-8")
        )
        moviepy = next(
            item for item in python_sbom["distributions"] if item["normalized_name"] == "moviepy"
        )
        self.assertEqual("2.2.1", moviepy["version"])
        self.assertEqual("2.1.2", moviepy["modification"]["module_reported_version"])
        self.assertEqual(
            "DFE76CD8AED151B99881DD01FA2BC1E040D0788EC364A8C6EF14020F2009D8B9",
            moviepy["modification"]["writer_sha256"],
        )
        self.assertFalse((inputs.output / "runtime/browser").exists())
        self.assertFalse((inputs.output / "licenses/Chrome-Headless-Shell-license.txt").exists())
        self.assertEqual([], verify_folder(inputs.output))

        ffmpeg_binary = inputs.output / "runtime/ffmpeg/ffmpeg.exe"
        original_ffmpeg = ffmpeg_binary.read_bytes()
        ffmpeg_binary.write_bytes(original_ffmpeg + b"tamper")
        errors = verify_folder(inputs.output)
        self.assertTrue(any("FFmpeg runtime" in error or "SHA-256" in error for error in errors), errors)
        ffmpeg_binary.write_bytes(original_ffmpeg)
        self.assertEqual([], verify_folder(inputs.output))

        original_video_service = video_service
        video_service_path = inputs.output / "engine/MoneyPrinterTurbo/app/services/video.py"
        video_service_path.write_text(
            original_video_service + '\nSHIYI_UNREVIEWED_CODEC = "h264_vaapi"\n',
            encoding="utf-8",
        )
        errors = verify_folder(inputs.output)
        self.assertTrue(
            any("非审核 H.264 编码器" in error and "h264_vaapi" in error for error in errors),
            errors,
        )
        video_service_path.write_text(original_video_service, encoding="utf-8")
        self.assertEqual([], verify_folder(inputs.output))

        bundled_browser = inputs.output / "runtime/browser/chrome-headless-shell.exe"
        bundled_browser.parent.mkdir(parents=True)
        bundled_browser.write_bytes(b"forbidden-browser")
        errors = verify_folder(inputs.output)
        self.assertTrue(any("runtime/browser" in error for error in errors), errors)

    def test_verifier_rejects_even_an_empty_bundled_browser_directory(self) -> None:
        inputs = self._motion_inputs("empty-browser-directory")
        build_combined_portable(inputs)
        (inputs.output / "runtime/browser").mkdir()
        errors = verify_folder(inputs.output)
        self.assertTrue(any("runtime/browser" in error for error in errors), errors)

    def test_formal_sources_reject_prepatched_or_drifted_runtime_inputs(self) -> None:
        inputs = self._motion_inputs("formal-source-drift")
        cli = inputs.motion_runtime.hyperframes_runtime / "node_modules/hyperframes/dist/cli.js"
        upstream_cli = cli.read_bytes()
        patch_result = subprocess.run(
            [
                sys.executable,
                str(self.repo / "tools/apply_hyperframes_windows_mf_patch.py"),
                "--package-root",
                str(cli.parents[1]),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, patch_result.returncode, patch_result.stdout + patch_result.stderr)
        with self.assertRaisesRegex(ValueError, "未修改的 npm CLI|unmodified npm CLI"):
            build_combined_portable(inputs)
        cli.write_bytes(upstream_cli)

        moviepy_writer = (
            self.python_runtime
            / "Lib/site-packages/moviepy/video/io/ffmpeg_writer.py"
        )
        moviepy_writer.write_bytes(moviepy_writer.read_bytes() + b"\n# drift\n")
        with self.assertRaisesRegex(ValueError, "exact upstream 2.2.1 writer/RECORD pair"):
            build_combined_portable(inputs)

    def test_formal_ffmpeg_source_is_exact_and_old_external_cli_args_are_rejected(self) -> None:
        inputs = self._motion_inputs("formal-ffmpeg-extra")
        self._write(self.ffmpeg_runtime, "extra-codec.dll", b"forbidden")
        with self.assertRaisesRegex(ValueError, "exact frozen file set"):
            build_combined_portable(inputs)

        command = [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "tools/build_combined_portable.py"),
            "--mpt-source", "x",
            "--python-runtime", "x",
            "--node-runtime", "x",
            "--hyperframes-runtime", "x",
            "--node-version", "22.13.1",
            "--hyperframes-version", "0.7.86",
            "--material", "x.mp4",
            "--output", "out",
            "--zip", "out.zip",
            "--ffmpeg-runtime", "legacy-btbn",
            "--ffmpeg-license", "legacy-gpl.txt",
            "--ffmpeg-build-info", "legacy-build.txt",
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("unrecognized arguments", completed.stderr)
        self.assertIn("--ffmpeg-runtime", completed.stderr)

    def test_zip_verifier_rejects_chrome_cft_and_edge_payloads(self) -> None:
        forbidden = (
            "runtime/browser/chrome-headless-shell.exe",
            "runtime/chrome-for-testing/chrome.exe",
            "runtime/edge/msedge.exe",
            "licenses/Chrome-Headless-Shell-license.txt",
        )
        for index, relative in enumerate(forbidden):
            with self.subTest(relative=relative):
                inputs = self._motion_inputs(f"forbidden-browser-{index}")
                build_combined_portable(inputs)
                with zipfile.ZipFile(inputs.zip_path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr(f"{PACKAGE_ROOT_NAME}/{relative}", b"forbidden")
                errors = verify_zip(inputs.zip_path)
                self.assertTrue(any("Chrome" in error and "payload" in error for error in errors), errors)

    def test_real_installed_closure_has_complete_exact_version_license_evidence(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        inventory = _validate_hyperframes_dependency_closure(repository_root, "0.7.86")
        identities = {(item["name"], item["version"]) for item in inventory}
        self.assertIn(("hyperframes", "0.7.86"), identities)
        self.assertIn(("esbuild", "0.25.12"), identities)
        contract = _read_hyperframes_license_contract(repository_root)
        overrides = contract["overrides"]
        self.assertIsInstance(overrides, dict)
        self.assertEqual(
            {
                ("@esbuild/win32-x64", "0.25.12"),
                ("@puppeteer/browsers", "3.0.6"),
                ("brotli", "1.3.3"),
                ("data-uri-to-buffer", "4.0.1"),
                ("dfa", "1.2.0"),
                ("fontkit", "2.0.4"),
                ("onnxruntime-common", "1.21.1"),
                ("onnxruntime-node", "1.21.1"),
                ("puppeteer-core", "25.4.0"),
            },
            set(overrides),
        )
        for name in ("onnxruntime-common", "onnxruntime-node"):
            notice_sources = overrides[(name, "1.21.1")]["notice_sources"]
            self.assertEqual(1, len(notice_sources))
            self.assertEqual(
                "8C06E8CFF286A4A117B3B246A4C7DA68428A144AF757823DB50E3D6520941EC6",
                notice_sources[0]["source_sha256"],
            )

    def test_license_contract_rejects_unpinned_source_or_tampered_notice(self) -> None:
        source = Path(__file__).resolve().parents[1] / "third_party" / "hyperframes"
        for mutation, expected_error in (
            ("source_commit", "官方来源锁无效"),
            ("notice_sha256", "NOTICE.*哈希不一致"),
        ):
            with self.subTest(mutation=mutation):
                root = self.root / f"license-{mutation}"
                boundary = root / "third_party" / "hyperframes"
                shutil.copytree(source, boundary)
                override_path = boundary / "dependency-license-overrides.json"
                payload = json.loads(override_path.read_text(encoding="utf-8"))
                if mutation == "source_commit":
                    payload["verified_overrides"][0]["source_commit"] = "0" * 40
                else:
                    onnx = next(
                        item
                        for item in payload["verified_overrides"]
                        if item["name"] == "onnxruntime-node"
                    )
                    onnx["notice_files"][0]["notice_sha256"] = "0" * 64
                override_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, expected_error):
                    _read_hyperframes_license_contract(root)

    def test_motion_primary_rejects_any_other_hyperframes_version(self) -> None:
        inputs = self._motion_inputs("wrong-hf-version")
        assert inputs.motion_runtime is not None
        with self.assertRaisesRegex(ValueError, "仅允许固定版本 0.7.86"):
            build_combined_portable(
                replace(
                    inputs,
                    motion_runtime=replace(inputs.motion_runtime, hyperframes_version="0.7.103"),
                )
            )

    @unittest.skipUnless(shutil.which("node"), "requires a local Node executable for the isolated copied-CLI proof")
    def test_copied_hyperframes_closure_runs_with_copied_node(self) -> None:
        inputs = self._motion_inputs("real-copied-cli")
        assert inputs.motion_runtime is not None
        system_node = Path(shutil.which("node") or "")
        shutil.copy2(system_node, inputs.motion_runtime.node_runtime / "node.exe")
        version = subprocess.run(
            [str(system_node), "--version"], check=True, capture_output=True, text=True
        ).stdout.strip().lstrip("v")
        inputs = replace(
            inputs,
            motion_runtime=replace(inputs.motion_runtime, node_version=version),
        )
        build_combined_portable(inputs)
        packaged_node = inputs.output / "runtime/node/node.exe"
        packaged_cli = inputs.output / "runtime/hyperframes/node_modules/hyperframes/bin/hyperframes.mjs"
        isolated_environment = {
            key: os.environ[key]
            for key in launch_combined.ENGINE_ENV_ALLOWLIST
            if os.environ.get(key)
        }
        system_root = isolated_environment.get("SYSTEMROOT") or isolated_environment.get("WINDIR")
        fixed_path = [str(packaged_node.parent)]
        if system_root:
            fixed_path.append(str(Path(system_root) / "System32"))
        isolated_environment.update(
            {
                "PATH": os.pathsep.join(fixed_path),
                "NODE_PATH": "",
                "NPM_CONFIG_OFFLINE": "true",
            }
        )
        completed = subprocess.run(
            [str(packaged_node), str(packaged_cli), "--version"],
            cwd=inputs.output / "runtime/hyperframes",
            env=isolated_environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("0.7.86", completed.stdout)

    def test_motion_primary_rejects_network_animation_assets(self) -> None:
        self._write(
            self.repo,
            "agent-skills/fixture/network.html",
            '<script src="https://cdn.example.invalid/runtime.js"></script>\n',
        )
        inputs = self._motion_inputs("network-motion")
        with self.assertRaisesRegex(ValueError, "网络资源"):
            build_combined_portable(inputs)
        self.assertFalse(inputs.output.exists())

    def test_builds_and_verifies_exact_combined_layout(self) -> None:
        inputs = self._inputs()
        manifest = build_combined_portable(inputs)

        self.assertEqual([], verify_folder(inputs.output))
        self.assertEqual([], verify_zip(inputs.zip_path))
        self.assertTrue((inputs.output / "tools/build_public_evidence.py").is_file())
        self.assertTrue((inputs.output / "tools/verify_public_evidence.py").is_file())
        self.assertTrue((inputs.output / "examples/pattern_cards.jsonl").is_file())
        self.assertTrue((inputs.output / "runtime/python/python.exe").is_file())
        self.assertTrue((inputs.output / "runtime/ffmpeg/avcodec-61.dll").is_file())
        self.assertTrue(
            (inputs.output / "runtime/python/Lib/site-packages/imageio_ffmpeg/binaries/README.md").is_file()
        )
        self.assertFalse(
            (
                inputs.output
                / "runtime/python/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe"
            ).exists()
        )
        self.assertTrue((inputs.output / "engine/MoneyPrinterTurbo/app/asgi.py").is_file())
        for relative in (
            "app/services/llm.py",
            "app/controllers/v1/llm.py",
            "app/services/upload_post.py",
        ):
            self.assertFalse((inputs.output / "engine/MoneyPrinterTurbo" / relative).exists())
        router = (inputs.output / "engine/MoneyPrinterTurbo/app/router.py").read_text(encoding="utf-8")
        task_service = (inputs.output / "engine/MoneyPrinterTurbo/app/services/task.py").read_text(encoding="utf-8")
        self.assertNotIn("include_router(llm.router)", router)
        self.assertIn("SHIYI_MPT_OFFLINE_SUBSET_V1", task_service)
        self.assertNotIn("from app.services import upload_post", task_service)
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
        python_sbom = json.loads(
            (inputs.output / "licenses/Python-runtime-SBOM.json").read_text(encoding="utf-8")
        )
        self.assertEqual(2, python_sbom["schema_version"])
        self.assertEqual(1, python_sbom["distribution_count"])
        self.assertEqual(
            [(name, version) for name, version in sorted(PYTHON_RUNTIME_PRUNED_DISTRIBUTIONS.items())],
            [(item["name"], item["version"]) for item in python_sbom["pruned_distributions"]],
        )
        self.assertEqual(0, python_sbom["project_verified_license_overrides"])
        distribution = python_sbom["distributions"][0]
        self.assertEqual(("fixture", "1.0", "MIT"), (
            distribution["normalized_name"],
            distribution["version"],
            distribution["license"],
        ))
        self.assertEqual("installed_distribution", distribution["license_evidence_source"])
        self.assertEqual(
            ["runtime/python/Lib/site-packages/fixture-1.0.dist-info/licenses/LICENSE"],
            [entry["path"] for entry in distribution["license_evidence"]],
        )
        self.assertRegex(distribution["payload_sha256"], r"^[0-9A-F]{64}$")

        launcher_bytes = (inputs.output / ROOT_LAUNCHER_NAME).read_bytes()
        self.assertFalse(launcher_bytes.startswith(b"\xef\xbb\xbf"))
        launcher = launcher_bytes.decode("ascii")
        self.assertIn("scripts\\launch_combined.ps1", launcher)
        shared = "%~dp0runtime\\python\\python.exe"
        self.assertIn(f'-MptPython "{shared}"', launcher)
        self.assertIn(f'-AppPython "{shared}"', launcher)
        self.assertIn("-MechanicalReview", launcher)
        self.assertNotIn("verify_combined_portable.py", launcher)
        powershell_launcher = (inputs.output / "scripts" / "launch_combined.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('@("-I", "-S", "-B", "-X", "utf8", $launcher', powershell_launcher)
        self.assertIn("[switch]$AgentTestReview", powershell_launcher)
        self.assertIn('if ($AgentTestReview) { $arguments += "--agent-test-review" }', powershell_launcher)
        self.assertIn("[switch]$MechanicalReview", powershell_launcher)
        self.assertIn('if ($MechanicalReview) { $arguments += "--mechanical-review" }', powershell_launcher)
        self.assertNotRegex(launcher.casefold(), r"pip\s+install|uv\s+sync|npx\s+--yes")
        stop_launcher = (inputs.output / STOP_LAUNCHER_NAME).read_text(encoding="ascii")
        self.assertNotIn("verify_combined_portable.py", stop_launcher)
        self.assertIn("--stop", stop_launcher)
        migration_launcher = (inputs.output / MIGRATION_LAUNCHER_NAME).read_text(encoding="ascii")
        self.assertNotIn("verify_combined_portable.py", migration_launcher)
        self.assertIn("--import-runtime", migration_launcher)
        installer_launcher = (inputs.output / INSTALLER_LAUNCHER_NAME).read_text(encoding="ascii")
        self.assertIn("runtime\\python\\python.exe", installer_launcher)
        self.assertIn("scripts\\install_combined.py", installer_launcher)
        self.assertIn("--source-root \"%~dp0.\"", installer_launcher)
        self.assertNotRegex(installer_launcher.casefold(), r"pip\s+install|npm\s+install|npx\s+")
        usage = (inputs.output / "使用说明.txt").read_text(encoding="utf-8")
        self.assertIn("LocalAppData\\ShiyiContentFactory\\UserData", usage)
        self.assertIn(INSTALLER_LAUNCHER_NAME, usage)
        self.assertIn("安装位置和数据位置是两回事", usage)
        self.assertIn("纯动画视频组件", usage)
        self.assertIn("实拍素材功能本版未开放", usage)
        self.assertIn("技术 JSON 仅作附件", usage)
        self.assertNotIn("HyperFrames", usage)
        self.assertNotIn("MoneyPrinterTurbo", usage)
        self.assertIn(STOP_LAUNCHER_NAME, usage)
        self.assertIn(MIGRATION_LAUNCHER_NAME, usage)
        self.assertIn("反向机械证据审核", usage)
        self.assertIn("不要求中途人工审查或改稿", usage)
        self.assertIn("不会降级成诊断语音或发布无声成片", usage)
        self.assertNotIn("两处暂停", usage)
        self.assertNotIn("Microsoft Huihui", usage)
        self.assertEqual(
            {
                "user_data_root": "%LOCALAPPDATA%/ShiyiContentFactory/UserData",
                "launcher_state_root": "%LOCALAPPDATA%/ShiyiContentFactory/Launcher",
                "package_runtime_mutable": False,
                "moneyprinterturbo_root": "engine/MoneyPrinterTurbo/storage",
                "moneyprinterturbo_immutable_children": ["local_videos"],
                "executable_files_allowed": False,
            },
            manifest["mutable_state"],
        )
        packaged_verifier = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-X",
                "utf8",
                str(inputs.output / "tools/verify_combined_portable.py"),
                str(inputs.output),
                "--startup",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(0, packaged_verifier.returncode, packaged_verifier.stdout + packaged_verifier.stderr)
        packaged_launcher = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-X",
                "utf8",
                str(inputs.output / "scripts/launch_combined.py"),
                "--help",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(0, packaged_launcher.returncode, packaged_launcher.stdout + packaged_launcher.stderr)

    def test_verifier_rejects_redundant_runtime_binary_and_locked_mpt_components(self) -> None:
        inputs = self._inputs("forbidden-package-content")
        build_combined_portable(inputs)
        redundant_binary = (
            inputs.output
            / "runtime/python/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-extra.exe"
        )
        self._write(redundant_binary.parent, redundant_binary.name, b"redundant")
        self._write(
            inputs.output,
            "engine/MoneyPrinterTurbo/app/services/upload_post.py",
            "raise RuntimeError('forbidden')\n",
        )

        errors = verify_folder(inputs.output)
        self.assertTrue(any("imageio_ffmpeg" in error and "FFmpeg" in error for error in errors), errors)
        self.assertTrue(any("上游锁明确排除" in error and "upload_post.py" in error for error in errors), errors)

        with zipfile.ZipFile(inputs.zip_path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                PACKAGE_ROOT_NAME
                + "/runtime/python/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-extra.exe",
                b"redundant",
            )
            archive.writestr(
                PACKAGE_ROOT_NAME + "/engine/MoneyPrinterTurbo/app/services/LLM.py",
                b"raise RuntimeError\n",
            )
        zip_errors = verify_zip(inputs.zip_path)
        self.assertTrue(any("imageio_ffmpeg" in error and "FFmpeg" in error for error in zip_errors), zip_errors)
        self.assertTrue(
            any("上游锁明确排除" in error and "llm.py" in error.casefold() for error in zip_errors),
            zip_errors,
        )

    def test_python_runtime_license_closure_is_exact_version_and_fail_closed(self) -> None:
        embedded_license = (
            self.python_runtime
            / "Lib/site-packages/fixture-1.0.dist-info/licenses/LICENSE"
        )
        embedded_license.unlink()
        record_path = self.python_runtime / "Lib/site-packages/fixture-1.0.dist-info/RECORD"
        record_path.write_text(
            record_path.read_text(encoding="utf-8").replace(
                "fixture-1.0.dist-info/licenses/LICENSE,,\n",
                "",
            ),
            encoding="utf-8",
        )

        component = {
            "type": "library",
            "bom-ref": "pkg:cargo/fixture-native@1.0",
            "name": "fixture-native",
            "version": "1.0",
            "scope": "required",
            "licenses": [{"expression": "MIT"}],
            "hashes": [],
        }
        native_sbom_bytes = (
            json.dumps(
                {"components": [component], "dependencies": [{"ref": component["bom-ref"]}]},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        native_sbom = self._write(
            self.python_runtime,
            "Lib/site-packages/fixture-1.0.dist-info/sboms/fixture.cdx.json",
            native_sbom_bytes,
        )
        record_path.write_text(
            record_path.read_text(encoding="utf-8")
            + "fixture-1.0.dist-info/sboms/fixture.cdx.json,,\n",
            encoding="utf-8",
        )
        identity = {
            "bom_ref": component["bom-ref"],
            "name": component["name"],
            "version": component["version"],
            "scope": component["scope"],
            "license_expressions": ["MIT"],
            "sha256": [],
        }
        identity_line = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        corpus_bytes = (
            "FIXTURE THIRD-PARTY LICENSE CORPUS\n"
            f"Component: {identity_line}\n"
            "Fixture native MIT license text\n"
        ).encode("utf-8")
        encoded_bytes = base64.b64encode(gzip.compress(corpus_bytes, compresslevel=9, mtime=0)) + b"\n"
        encoded = self._write(
            self.repo,
            "third_party/python_runtime/dependency-licenses/fixture-1.0-third-party.txt.gz.b64",
            encoded_bytes,
        )

        missing = self._inputs("python-license-missing")
        with self.assertRaisesRegex(ValueError, "缺少许可证正文且无精确版本覆盖"):
            build_combined_portable(missing)
        self.assertFalse(missing.output.exists())

        evidence = self._write(
            self.repo,
            "third_party/python_runtime/dependency-licenses/fixture-1.0-MIT.txt",
            "Exact fixture MIT license text\n",
        )
        evidence_sha = sha256_file(evidence)
        commit = "b" * 40
        override = {
            "name": "fixture",
            "version": "1.0",
            "spdx": "MIT",
            "source_repository": "https://github.com/example/fixture",
            "source_tag": "v1.0",
            "source_commit": commit,
            "source_url": f"https://raw.githubusercontent.com/example/fixture/{commit}/LICENSE",
            "source_sha256": evidence_sha,
            "license_file": "dependency-licenses/fixture-1.0-MIT.txt",
            "license_sha256": evidence_sha,
            "additional_evidence": [
                {
                    "kind": "notice",
                    "source_kind": "embedded-runtime-sbom-derived-license-corpus",
                    "bound_runtime_path": "Lib/site-packages/fixture-1.0.dist-info/sboms/fixture.cdx.json",
                    "bound_runtime_sha256": sha256_file(native_sbom),
                    "component_count": 1,
                    "dependency_count": 1,
                    "component_identity_sha256": hashlib.sha256((identity_line + "\n").encode("utf-8")).hexdigest().upper(),
                    "encoded_file": "dependency-licenses/fixture-1.0-third-party.txt.gz.b64",
                    "encoded_sha256": sha256_file(encoded),
                    "encoding": "base64+gzip",
                    "evidence_file": "dependency-licenses/fixture-1.0-third-party.txt",
                    "evidence_sha256": hashlib.sha256(corpus_bytes).hexdigest().upper(),
                    "evidence_size": len(corpus_bytes),
                }
            ],
        }
        override_path = self.repo / "third_party/python_runtime/dependency-license-overrides.json"
        override_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "status": "complete",
                    "verified_overrides": [override],
                    "unresolved": [],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        inputs = self._inputs("python-license-override")
        build_combined_portable(inputs)
        self.assertEqual([], verify_folder(inputs.output))
        sbom = json.loads(
            (inputs.output / "licenses/Python-runtime-SBOM.json").read_text(encoding="utf-8")
        )
        distribution = sbom["distributions"][0]
        self.assertEqual(1, sbom["project_verified_license_overrides"])
        self.assertEqual("project_verified_override", distribution["license_evidence_source"])
        self.assertEqual(override, distribution["override"])
        evidence_path = inputs.output / "licenses/python-runtime-dependencies/fixture-1.0-third-party.txt"
        evidence_path.write_text("tampered\n", encoding="utf-8")
        errors = verify_folder(inputs.output)
        self.assertTrue(any("Python runtime" in error and "许可证" in error for error in errors), errors)

    def test_python_runtime_sbom_rejects_unowned_and_identity_drift(self) -> None:
        inputs = self._inputs("python-sbom-drift")
        build_combined_portable(inputs)
        orphan = inputs.output / "runtime/python/Lib/site-packages/orphan.py"
        orphan.write_text("VALUE = 2\n", encoding="utf-8")
        errors = verify_folder(inputs.output)
        self.assertTrue(any("未归属" in error for error in errors), errors)
        orphan.unlink()

        sbom_path = inputs.output / "licenses/Python-runtime-SBOM.json"
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        sbom["distributions"][0]["version"] = "9.9"
        sbom_path.write_text(json.dumps(sbom, ensure_ascii=False) + "\n", encoding="utf-8")
        errors = verify_folder(inputs.output)
        self.assertTrue(
            any("Python runtime SBOM" in error or "METADATA" in error for error in errors),
            errors,
        )

    def test_python_runtime_prune_is_version_locked_and_residual_closed(self) -> None:
        inputs = self._inputs("python-prune-residual")
        build_combined_portable(inputs)
        residual = inputs.output / "runtime/python/Lib/site-packages/streamlit/residual.py"
        residual.parent.mkdir(parents=True, exist_ok=True)
        residual.write_text("VALUE = 1\n", encoding="utf-8")
        errors = verify_folder(inputs.output)
        self.assertTrue(any("未归属" in error for error in errors), errors)

        streamlit_metadata = next(
            (self.python_runtime / "Lib/site-packages").glob("streamlit-*.dist-info/METADATA")
        )
        streamlit_metadata.write_text(
            streamlit_metadata.read_text(encoding="utf-8").replace(
                "Version: 1.59.1",
                "Version: 9.9.9",
            ),
            encoding="utf-8",
        )
        drifted = self._inputs("python-prune-version-drift")
        with self.assertRaisesRegex(ValueError, "受控裁剪版本漂移"):
            build_combined_portable(drifted)
        self.assertFalse(drifted.output.exists())

    def test_python_runtime_prune_dependency_graph_drift_fails_closed(self) -> None:
        fixture_metadata = self.python_runtime / "Lib/site-packages/fixture-1.0.dist-info/METADATA"
        fixture_metadata.write_text(
            fixture_metadata.read_text(encoding="utf-8").replace(
                "\n\n",
                "\nRequires-Dist: pyarrow\n\n",
            ),
            encoding="utf-8",
        )
        drifted = self._inputs("python-prune-graph-drift")
        with self.assertRaisesRegex(ValueError, "受控裁剪依赖图漂移"):
            build_combined_portable(drifted)
        self.assertFalse(drifted.output.exists())

    def test_python_runtime_pruned_import_boundary_rejects_new_retained_reference(self) -> None:
        fixture_module = self.python_runtime / "Lib/site-packages/fixture.py"
        fixture_module.write_text("import jinja2\n", encoding="utf-8")
        inputs = self._inputs("python-pruned-import-retained")
        with self.assertRaisesRegex(ValueError, "未审批的已裁依赖 import"):
            build_combined_portable(inputs)
        self.assertFalse(inputs.output.exists())

    def test_python_runtime_pruned_import_boundary_rejects_formal_reference(self) -> None:
        (self.repo / "app.py").write_text("import av\n", encoding="utf-8")
        inputs = self._inputs("python-pruned-import-formal")
        with self.assertRaisesRegex(ValueError, "正式 Python payload.*未审批"):
            build_combined_portable(inputs)
        self.assertFalse(inputs.output.exists())

    def test_python_runtime_pruned_import_contract_is_exact_version_locked(self) -> None:
        contract_path = self.repo / "third_party/python_runtime/pruned-import-boundary.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["pruned_distributions"][0]["version"] = "9.9.9"
        contract_path.write_text(json.dumps(contract, ensure_ascii=False) + "\n", encoding="utf-8")
        inputs = self._inputs("python-pruned-import-contract-drift")
        with self.assertRaisesRegex(ValueError, "import 模块锁|distribution 锁"):
            build_combined_portable(inputs)
        self.assertFalse(inputs.output.exists())

    def test_pruned_runtime_ignores_generated_bytecode_but_rejects_unowned_source(self) -> None:
        generated = self._write(
            self.python_runtime,
            "Lib/site-packages/streamlit/__pycache__/unowned.cpython-312.pyc",
            b"generated-bytecode",
        )
        direct_pyc = self._write(
            self.python_runtime,
            "Lib/site-packages/streamlit/unowned.pyc",
            b"generated-bytecode",
        )
        inputs = self._inputs("python-pruned-generated-bytecode")
        build_combined_portable(inputs)
        for source in (generated, direct_pyc):
            relative = source.relative_to(self.python_runtime)
            self.assertFalse((inputs.output / "runtime/python" / relative).exists())
            manifest = json.loads((inputs.output / PACKAGE_MANIFEST).read_text(encoding="utf-8"))
            self.assertNotIn(
                (Path("runtime/python") / relative).as_posix(),
                {item["path"] for item in manifest["files"]},
            )

        unowned_source = self._write(
            self.python_runtime,
            "Lib/site-packages/streamlit/unowned.py",
            "VALUE = 1\n",
        )
        rejected = self._inputs("python-pruned-unowned-source")
        with self.assertRaisesRegex(ValueError, "RECORD|未归属"):
            build_combined_portable(rejected)
        self.assertFalse(rejected.output.exists())
        unowned_source.unlink()

    def test_rejects_mpt_lock_that_drops_an_excluded_component(self) -> None:
        lock_path = self.repo / "third_party/moneyprinterturbo/upstream-lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["excluded_components"].remove("app/services/upload_post.py")
        lock_path.write_text(json.dumps(lock, ensure_ascii=False) + "\n", encoding="utf-8")

        inputs = self._inputs("mpt-lock-exclusions")
        with self.assertRaisesRegex(ValueError, "excluded_components"):
            build_combined_portable(inputs)
        self.assertFalse(inputs.output.exists())
        self.assertFalse(inputs.zip_path.exists())

    def test_pre_integrity_runtime_constants_match_shared_contract(self) -> None:
        self.assertEqual(CONTRACT_HYPERFRAMES_VERSION, verify_combined_portable.HYPERFRAMES_VERSION)
        self.assertEqual(CONTRACT_NODE_MINIMUM_MAJOR, verify_combined_portable.NODE_MINIMUM_MAJOR)
        self.assertEqual(CONTRACT_HYPERFRAMES_VERSION, launch_combined.HYPERFRAMES_VERSION)
        self.assertEqual(CONTRACT_NODE_MINIMUM_MAJOR, launch_combined.NODE_MINIMUM_MAJOR)

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
        fixture_record = self.python_runtime / "Lib/site-packages/fixture-1.0.dist-info/RECORD"
        fixture_record.write_text(
            fixture_record.read_text(encoding="utf-8") + "vendor/example.py,,\n",
            encoding="utf-8",
        )
        valid = self._inputs("vendor-example")
        build_combined_portable(valid)
        self.assertEqual([], verify_folder(valid.output))

        self._write(
            self.python_runtime,
            "Lib/site-packages/fixture-1.0.dist-info/direct_url.json",
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
        external_state_errors = verify_folder(inputs.output, allow_runtime_state=True)
        self.assertTrue(any("runtime/config.json" in error for error in external_state_errors), external_state_errors)
        (inputs.output / "runtime/config.json").unlink()
        shutil.rmtree(inputs.output / "runtime/jobs")
        self.assertEqual([], verify_folder(inputs.output, allow_runtime_state=True))

        self._write(inputs.output, "engine/MoneyPrinterTurbo/app/__pycache__/asgi.cpython-314.pyc", b"generated")
        bytecode_errors = verify_folder(inputs.output, allow_runtime_state=True)
        self.assertTrue(any("非白名单" in error or "清单" in error for error in bytecode_errors), bytecode_errors)

        self._write(inputs.output, "runtime/jobs/job-1/evil.py", "raise RuntimeError\n")
        errors = verify_folder(inputs.output, allow_runtime_state=True)
        self.assertTrue(any("非白名单" in error or "清单" in error for error in errors), errors)

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
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ]
        with patch("tools.build_combined_portable.subprocess.run", side_effect=results) as runner:
            build_combined_portable(inputs)
        self.assertEqual(4, runner.call_count)
        self.assertEqual("--version", runner.call_args_list[0].args[0][1])
        self.assertEqual("-version", runner.call_args_list[1].args[0][1])
        self.assertEqual("-version", runner.call_args_list[2].args[0][1])
        self.assertIn("from app.asgi import app", runner.call_args_list[3].args[0][-1])
        self.assertIn("uvicorn.protocols.http.auto", runner.call_args_list[3].args[0][-1])
        self.assertIn("pkg_resources", runner.call_args_list[3].args[0][-1])

    def test_formal_preflight_fails_closed_when_mpt_subset_does_not_start(self) -> None:
        inputs = replace(self._inputs("mpt-preflight-failure"), verify_runtime_executables=True)
        results = [
            subprocess.CompletedProcess([], 0, stdout="Python 3.12.13\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="ffmpeg version 7.1\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="ffprobe version 7.1\n", stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="ImportError"),
        ]
        with patch("tools.build_combined_portable.subprocess.run", side_effect=results):
            with self.assertRaisesRegex(ValueError, "启动探针失败"):
                build_combined_portable(inputs)
        self.assertFalse(inputs.output.exists())
        self.assertFalse(inputs.zip_path.exists())

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
