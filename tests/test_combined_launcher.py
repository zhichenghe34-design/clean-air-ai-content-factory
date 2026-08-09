from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.launch_combined import (
    EXPECTED_MPT_COMMIT,
    EXPECTED_MPT_VERSION,
    LauncherConfig,
    LauncherError,
    build_app_command,
    build_app_environment,
    build_parser,
    build_engine_command,
    build_engine_environment,
    config_from_args,
    probe_preinstalled_runtimes,
    run_combined,
    validate_mpt_network_config,
    validate_preinstalled_layout,
    wait_for_mpt_health,
)


class FakeProcess:
    _next_pid = 41000

    def __init__(self, poll_values=None):
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1
        self._poll_values = list(poll_values or [None])
        self.terminated = False

    def poll(self):
        if len(self._poll_values) > 1:
            return self._poll_values.pop(0)
        return self._poll_values[0]

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


class CombinedLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.project = self.root / "project"
        self.mpt = self.root / "mpt"
        (self.project / "docs" / "fonts").mkdir(parents=True)
        (self.project / "third_party" / "moneyprinterturbo").mkdir(parents=True)
        (self.project / ".venv" / "Scripts").mkdir(parents=True)
        (self.mpt / "app").mkdir(parents=True)
        (self.mpt / ".venv" / "Scripts").mkdir(parents=True)
        (self.mpt / "resource" / "fonts").mkdir(parents=True)
        (self.mpt / "storage" / "local_videos").mkdir(parents=True)
        for path in (
            self.project / "app.py",
            self.project / ".venv" / "Scripts" / "python.exe",
            self.project / "ffmpeg.exe",
            self.project / "ffprobe.exe",
            self.project / "docs" / "fonts" / "NotoSansSC-Regular.ttf",
            self.project / "docs" / "fonts" / "NotoSansSC-Bold.ttf",
            self.mpt / "app" / "asgi.py",
            self.mpt / ".venv" / "Scripts" / "python.exe",
        ):
            path.write_bytes(b"fixture")
        (self.mpt / "resource" / "fonts" / "NotoSansSC-Regular.ttf").write_bytes(b"fixture")
        (self.mpt / "storage" / "local_videos" / "verified.mp4").write_bytes(b"fixture-video")
        (self.mpt / "UPSTREAM_COMMIT").write_text(EXPECTED_MPT_COMMIT + "\n", encoding="utf-8")
        (self.mpt / "pyproject.toml").write_text(
            f'[project]\nname = "moneyprinterturbo"\nversion = "{EXPECTED_MPT_VERSION}"\n',
            encoding="utf-8",
        )
        (self.project / "third_party" / "moneyprinterturbo" / "upstream-lock.json").write_text(
            json.dumps(
                {
                    "upstream_version": EXPECTED_MPT_VERSION,
                    "upstream_commit": EXPECTED_MPT_COMMIT,
                    "license": "MIT",
                }
            ),
            encoding="utf-8",
        )
        self._write_safe_mpt_config()
        self.config = LauncherConfig(
            project_root=self.project,
            mpt_root=self.mpt,
            mpt_python=self.mpt / ".venv" / "Scripts" / "python.exe",
            ffmpeg=self.project / "ffmpeg.exe",
            ffprobe=self.project / "ffprobe.exe",
            font_regular=self.project / "docs" / "fonts" / "NotoSansSC-Regular.ttf",
            font_bold=self.project / "docs" / "fonts" / "NotoSansSC-Bold.ttf",
            material_root=self.mpt / "storage" / "local_videos",
            app_python=self.project / ".venv" / "Scripts" / "python.exe",
            app_script=self.project / "app.py",
            open_browser=False,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_safe_mpt_config(self, host="127.0.0.1", endpoint=""):
        (self.mpt / "config.toml").write_text(
            f'listen_host = "{host}"\nlisten_port = 8080\n[app]\nendpoint = "{endpoint}"\n',
            encoding="utf-8",
        )

    def test_preflight_accepts_only_complete_pinned_layout(self):
        validate_preinstalled_layout(self.config)
        (self.mpt / "pyproject.toml").write_text(
            '[project]\nname = "moneyprinterturbo"\nversion = "9.9.9"\n', encoding="utf-8"
        )
        with self.assertRaises(LauncherError) as context:
            validate_preinstalled_layout(self.config)
        self.assertEqual(context.exception.code, "MPT_VERSION_MISMATCH")

    def test_preflight_rejects_material_directory_outside_mpt_allowlist(self):
        outside = self.project / "materials"
        outside.mkdir()
        (outside / "verified.mp4").write_bytes(b"fixture-video")
        unsafe = LauncherConfig(**{**self.config.__dict__, "material_root": outside})
        with self.assertRaises(LauncherError) as context:
            validate_preinstalled_layout(unsafe)
        self.assertEqual(context.exception.code, "MATERIAL_ROOT_NOT_MPT_ALLOWLIST")
        self.assertNotIn(str(outside), json.dumps(context.exception.as_dict()))

    def test_mpt_config_rejects_public_host_and_external_endpoint_without_echoing_value(self):
        self._write_safe_mpt_config(host="0.0.0.0")
        with self.assertRaises(LauncherError) as public_host:
            validate_mpt_network_config(self.mpt, 19080)
        self.assertEqual(public_host.exception.code, "MPT_CONFIG_HOST_NOT_LOOPBACK")
        self.assertNotIn("0.0.0.0", json.dumps(public_host.exception.as_dict()))

        self._write_safe_mpt_config(endpoint="https://example.invalid/api")
        with self.assertRaises(LauncherError) as public_endpoint:
            validate_mpt_network_config(self.mpt, 19080)
        self.assertEqual(public_endpoint.exception.code, "MPT_CONFIG_ENDPOINT_NOT_LOOPBACK")
        self.assertNotIn("example.invalid", json.dumps(public_endpoint.exception.as_dict()))

    def test_commands_are_fixed_loopback_without_shell_or_provider_arguments(self):
        engine = build_engine_command(self.config, 19080)
        app = build_app_command(self.config, 18765)
        self.assertEqual(
            engine,
            [
                str(self.config.mpt_python),
                "-E",
                "-s",
                "-B",
                "-X",
                "utf8",
                "-m",
                "uvicorn",
                "app.asgi:app",
                "--host",
                "127.0.0.1",
                "--port",
                "19080",
                "--log-level",
                "warning",
            ],
        )
        self.assertEqual(app[1:7], ["-E", "-s", "-B", "-X", "utf8", str(self.config.app_script)])
        self.assertEqual(app[-4:], ["--host", "127.0.0.1", "--port", "18765"])
        joined = " ".join(engine + app).lower()
        for forbidden in ("deepseek", "api_key", "authorization", "cookie", "secret"):
            self.assertNotIn(forbidden, joined)

    def test_engine_environment_drops_keys_and_cookies_while_app_receives_adapter_address(self):
        parent = {
            "PATH": "safe-bin",
            "SYSTEMROOT": "C:\\Windows",
            "DEEPSEEK_API_KEY": "should-not-leak",
            "OPENAI_API_KEY": "should-not-leak",
            "COOKIE": "should-not-leak",
            "AUTHORIZATION": "should-not-leak",
            "SHIYI_ALLOW_TEST_PROVIDER": "1",
            "SHIYI_EXPERIMENTAL_DYNAMIC_TOPICS": "1",
            "SHIYI_AGENT_TEST_REVIEW": "1",
            "PYTHONPATH": "C:\\outside-injection",
            "PYTHONHOME": "C:\\outside-runtime",
        }
        engine_env = build_engine_environment(parent, self.config)
        self.assertEqual(engine_env["PYTHONUTF8"], "1")
        self.assertEqual(engine_env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(engine_env["IMAGEIO_FFMPEG_EXE"], str(self.config.ffmpeg))
        self.assertNotIn("PYTHONPATH", engine_env)
        self.assertNotIn("PYTHONHOME", engine_env)
        self.assertNotIn("DEEPSEEK_API_KEY", engine_env)
        self.assertNotIn("OPENAI_API_KEY", engine_env)
        self.assertNotIn("COOKIE", engine_env)
        self.assertNotIn("AUTHORIZATION", engine_env)

        app_env = build_app_environment(parent, self.config, app_port=18765, mpt_port=19080)
        self.assertEqual(app_env["SHIYI_MPT_ENABLED"], "1")
        self.assertEqual(app_env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(app_env["SHIYI_MPT_BASE_URL"], "http://127.0.0.1:19080/api/v1")
        self.assertEqual(app_env["SHIYI_MPT_HEALTH_VERIFIED"], "1")
        self.assertEqual(app_env["SHIYI_MPT_MATERIAL_STRATEGY"], "local")
        self.assertEqual(app_env["SHIYI_MPT_LOCAL_MATERIAL_DIR"], str(self.config.material_root))
        self.assertEqual(app_env["FFMPEG_PATH"], str(self.config.ffmpeg))
        self.assertEqual(app_env["FFPROBE_PATH"], str(self.config.ffprobe))
        self.assertNotIn("SHIYI_ALLOW_TEST_PROVIDER", app_env)
        self.assertNotIn("SHIYI_EXPERIMENTAL_DYNAMIC_TOPICS", app_env)
        self.assertNotIn("SHIYI_AGENT_TEST_REVIEW", app_env)
        self.assertNotIn("PYTHONPATH", app_env)
        self.assertNotIn("PYTHONHOME", app_env)

        agent_test_config = LauncherConfig(
            **{**self.config.__dict__, "agent_test_review": True}
        )
        agent_test_env = build_app_environment(
            parent,
            agent_test_config,
            app_port=18765,
            mpt_port=19080,
        )
        self.assertEqual(agent_test_env["SHIYI_AGENT_TEST_REVIEW"], "1")

    def test_packaged_config_ignores_host_path_overrides_and_rejects_explicit_escape(self):
        (self.project / "PACKAGE-MANIFEST.json").write_text("{}\n", encoding="utf-8")
        expected_python = self.project / "runtime" / "python" / "python.exe"
        official = build_parser().parse_args(
            [
                "--project-root",
                str(self.project),
                "--mpt-root",
                str(self.project / "engine" / "MoneyPrinterTurbo"),
                "--mpt-python",
                str(expected_python),
                "--app-python",
                str(expected_python),
                "--ffmpeg",
                str(self.project / "runtime" / "ffmpeg" / "ffmpeg.exe"),
                "--ffprobe",
                str(self.project / "runtime" / "ffmpeg" / "ffprobe.exe"),
                "--material-root",
                str(self.project / "engine" / "MoneyPrinterTurbo" / "storage" / "local_videos"),
            ]
        )
        poisoned = {
            "SHIYI_APP_EXECUTABLE": str(self.root / "outside.exe"),
            "SHIYI_NOTO_BOLD": str(self.root / "unlicensed.ttf"),
            "SHIYI_MPT_ROOT": str(self.root / "outside-mpt"),
        }
        with patch.dict(os.environ, poisoned, clear=False):
            config = config_from_args(official)
        self.assertIsNone(config.app_executable)
        self.assertEqual(expected_python.resolve(), config.app_python)
        self.assertEqual((self.project / "docs/fonts/NotoSansSC-Bold.ttf").resolve(), config.font_bold)
        self.assertEqual((self.project / "engine/MoneyPrinterTurbo").resolve(), config.mpt_root)

        escaped = build_parser().parse_args(
            ["--project-root", str(self.project), "--app-executable", str(self.root / "outside.exe")]
        )
        with self.assertRaises(LauncherError) as context:
            config_from_args(escaped)
        self.assertEqual("PACKAGE_PATH_OVERRIDE_REJECTED", context.exception.code)

    def test_runtime_probe_uses_preinstalled_interpreters_and_never_installs(self):
        commands = []

        def fake_runner(command, **kwargs):
            commands.append(list(command))
            return subprocess.CompletedProcess(command, 0, b"", b"")

        probe_preinstalled_runtimes(self.config, runner=fake_runner)
        self.assertEqual(
            commands,
            [
                [str(self.config.mpt_python), "-I", "-B", "-c", "import fastapi, uvicorn"],
                [str(self.config.app_python), "-I", "-B", "-c", "import PIL, ddgs, langgraph"],
            ],
        )
        flattened = " ".join(value for command in commands for value in command).lower()
        for forbidden in ("pip", "uv sync", "npm", "download", "install"):
            self.assertNotIn(forbidden, flattened)

    def test_health_requires_real_video_api_path(self):
        good = json.dumps({"paths": {"/api/v1/videos": {"post": {}}}}).encode()
        wait_for_mpt_health(
            "http://127.0.0.1:19080/openapi.json",
            timeout_seconds=1,
            requester=lambda _url, _timeout: good,
        )

        ticks = iter((0.0, 0.0, 0.2, 0.6, 1.1))
        with self.assertRaises(LauncherError) as context:
            wait_for_mpt_health(
                "http://127.0.0.1:19080/openapi.json",
                timeout_seconds=1,
                requester=lambda _url, _timeout: b'{"paths": {}}',
                clock=lambda: next(ticks),
                sleeper=lambda _seconds: None,
            )
        self.assertEqual(context.exception.code, "MPT_HEALTH_TIMEOUT")

    def test_controller_starts_engine_before_app_sets_exact_cors_and_cleans_both(self):
        created = []
        processes = [FakeProcess([None, None]), FakeProcess([None, 0])]

        def fake_popen(command, **kwargs):
            created.append((list(command), kwargs))
            return processes[len(created) - 1]

        terminated = []
        with (
            patch("scripts.launch_combined.select_loopback_port", side_effect=[18765, 19080]),
            patch("scripts.launch_combined._emit"),
        ):
            result = run_combined(
                self.config,
                popen_factory=fake_popen,
                health_waiter=lambda *_args, **_kwargs: None,
                workbench_health_waiter=lambda *_args, **_kwargs: None,
                process_terminator=lambda process: terminated.append(process),
                sleeper=lambda _seconds: None,
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(created), 2)
        engine_command, engine_options = created[0]
        app_command, app_options = created[1]
        self.assertIn("uvicorn", engine_command)
        self.assertTrue(any(Path(value).name == "app.py" for value in app_command))
        self.assertEqual(engine_options["env"]["CORS_ALLOWED_ORIGINS"], "http://127.0.0.1:18765")
        self.assertNotIn("SHIYI_MPT_BASE_URL", engine_options["env"])
        self.assertEqual(app_options["env"]["SHIYI_MPT_BASE_URL"], "http://127.0.0.1:19080/api/v1")
        self.assertEqual(terminated, [processes[1], processes[0]])


if __name__ == "__main__":
    unittest.main()
