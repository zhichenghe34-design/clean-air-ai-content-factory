from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.launch_combined import (
    EXPECTED_MPT_COMMIT,
    EXPECTED_MPT_VERSION,
    HYPERFRAMES_PATCHED_CLI_SHA256,
    MOTION_OPTIONAL_MPT_HEALTH_TIMEOUT_SECONDS,
    SYSTEM_EDGE_BROWSER_STRATEGY,
    SYSTEM_EDGE_MINIMUM_MAJOR,
    SYSTEM_EDGE_RELATIVE_PATH,
    LauncherConfig,
    LauncherError,
    _validate_trusted_system_edge_path,
    _port_is_available,
    build_app_command,
    build_app_environment,
    _claim_launcher_state,
    _query_windows_process,
    _same_windows_path,
    _write_launcher_state,
    build_parser,
    build_engine_command,
    build_engine_environment,
    config_from_args,
    import_legacy_runtime,
    probe_preinstalled_runtimes,
    resolve_trusted_system_edge,
    run_combined,
    select_loopback_port,
    stop_recorded_processes,
    validate_mpt_network_config,
    validate_preinstalled_layout,
    wait_for_mpt_health,
    wait_for_workbench_health,
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
        self.program_files = self.root / "Program Files"
        self.edge = self.program_files / SYSTEM_EDGE_RELATIVE_PATH
        self.edge.parent.mkdir(parents=True)
        self.edge.write_bytes(b"signed-edge-fixture")
        self.windows_root = self.root / "Windows"
        self.powershell = self.windows_root / "System32/WindowsPowerShell/v1.0/powershell.exe"
        self.powershell.parent.mkdir(parents=True)
        self.powershell.write_bytes(b"powershell-fixture")
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

    def _write_safe_mpt_config(self, host="127.0.0.1", endpoint="", video_codec="h264_mf"):
        (self.mpt / "config.toml").write_text(
            f'listen_host = "{host}"\nlisten_port = 8080\n[app]\n'
            f'endpoint = "{endpoint}"\nvideo_codec = "{video_codec}"\n',
            encoding="utf-8",
        )

    @staticmethod
    def _edge_identity_payload(
        *,
        version="151.0.4129.72",
        signature_status="Valid",
        product_name="Microsoft Edge",
        company_name="Microsoft Corporation",
    ):
        return {
            "signature_status": signature_status,
            "signer_subject": "CN=Microsoft Windows, O=Microsoft Corporation, L=Redmond",
            "product_name": product_name,
            "company_name": company_name,
            "product_version": version,
            "file_version": version,
        }

    def _edge_identity_runner(self, **payload_overrides):
        payload = self._edge_identity_payload(**payload_overrides)

        def runner(command, **kwargs):
            self.assertEqual(str(self.edge.resolve()), kwargs["env"]["SHIYI_EDGE_SIGNATURE_TARGET"])
            return subprocess.CompletedProcess(command, 0, json.dumps(payload).encode("utf-8"), b"")

        return runner

    def test_preflight_accepts_only_complete_pinned_layout(self):
        validate_preinstalled_layout(self.config)
        (self.mpt / "pyproject.toml").write_text(
            '[project]\nname = "moneyprinterturbo"\nversion = "9.9.9"\n', encoding="utf-8"
        )
        with self.assertRaises(LauncherError) as context:
            validate_preinstalled_layout(self.config)
        self.assertEqual(context.exception.code, "MPT_VERSION_MISMATCH")

    def test_trusted_system_edge_accepts_only_signed_microsoft_product_at_fixed_path(self):
        with patch("scripts.launch_combined._trusted_windows_root", return_value=self.windows_root):
            detected_path, version = resolve_trusted_system_edge(
                runner=self._edge_identity_runner(),
                program_files_roots=(self.program_files,),
                powershell=self.powershell,
            )
        self.assertEqual(self.edge.resolve(), detected_path)
        self.assertEqual("151.0.4129.72", version)

    def test_trusted_system_edge_fails_closed_when_missing_or_too_old(self):
        empty_root = self.root / "Empty Program Files"
        empty_root.mkdir()
        with self.assertRaises(LauncherError) as missing:
            resolve_trusted_system_edge(program_files_roots=(empty_root,), powershell=self.powershell)
        self.assertEqual("SYSTEM_EDGE_MISSING", missing.exception.code)

        with (
            patch("scripts.launch_combined._trusted_windows_root", return_value=self.windows_root),
            self.assertRaises(LauncherError) as old,
        ):
            resolve_trusted_system_edge(
                runner=self._edge_identity_runner(version="150.0.0.0"),
                program_files_roots=(self.program_files,),
                powershell=self.powershell,
            )
        self.assertEqual("SYSTEM_EDGE_TOO_OLD", old.exception.code)

    def test_trusted_system_edge_rejects_spoofed_signature_and_product_identity(self):
        for overrides, expected_code in (
            ({"signature_status": "NotSigned"}, "SYSTEM_EDGE_SIGNATURE_INVALID"),
            ({"product_name": "Chromium"}, "SYSTEM_EDGE_IDENTITY_INVALID"),
            ({"company_name": "Fixture Corp"}, "SYSTEM_EDGE_IDENTITY_INVALID"),
        ):
            with self.subTest(overrides=overrides):
                with (
                    patch("scripts.launch_combined._trusted_windows_root", return_value=self.windows_root),
                    self.assertRaises(LauncherError) as rejected,
                ):
                    resolve_trusted_system_edge(
                        runner=self._edge_identity_runner(**overrides),
                        program_files_roots=(self.program_files,),
                        powershell=self.powershell,
                    )
                self.assertEqual(expected_code, rejected.exception.code)

    def test_local_app_data_edge_path_is_not_a_trusted_system_browser(self):
        local_edge = self.root / "LocalAppData/Microsoft/Edge/Application/msedge.exe"
        local_edge.parent.mkdir(parents=True)
        local_edge.write_bytes(b"user-writable-edge")
        with self.assertRaises(LauncherError) as rejected:
            _validate_trusted_system_edge_path(
                local_edge,
                program_files_roots=(self.program_files,),
            )
        self.assertEqual("SYSTEM_EDGE_PATH_UNTRUSTED", rejected.exception.code)

    def test_motion_config_ignores_browser_cli_and_host_environment_overrides(self):
        args = build_parser().parse_args(
            [
                "--project-root",
                str(self.project),
                "--require-motion-runtime",
                "--node-executable",
                str(self.project / "runtime/node/node.exe"),
                "--hyperframes-cli",
                str(self.project / "runtime/hyperframes/node_modules/hyperframes/bin/hyperframes.mjs"),
                "--hyperframes-browser",
                str(self.root / "LocalAppData/Microsoft/Edge/Application/msedge.exe"),
                "--browser-version",
                "999.0.0.0",
                "--node-version",
                "22.13.1",
                "--hyperframes-version",
                "0.7.86",
            ]
        )
        host_overrides = {
            "HYPERFRAMES_BROWSER_PATH": str(self.root / "host-msedge.exe"),
            "SHIYI_HYPERFRAMES_BROWSER_STRATEGY": "host_override",
            "SHIYI_HYPERFRAMES_BROWSER_VERSION": "999.0.0.0",
            "SHIYI_HYPERFRAMES_BROWSER_MINIMUM_MAJOR": "999",
        }
        with (
            patch.dict(os.environ, host_overrides, clear=False),
            patch(
                "scripts.launch_combined.resolve_trusted_system_edge",
                return_value=(self.edge.resolve(), "151.0.4129.72"),
            ),
        ):
            config = config_from_args(args)
        self.assertEqual(self.edge.resolve(), config.hyperframes_browser)
        self.assertEqual("151.0.4129.72", config.browser_version)
        self.assertEqual(SYSTEM_EDGE_BROWSER_STRATEGY, config.browser_strategy)
        self.assertEqual(SYSTEM_EDGE_MINIMUM_MAJOR, config.browser_minimum_major)

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

        self._write_safe_mpt_config(video_codec="libx264")
        with self.assertRaises(LauncherError) as wrong_codec:
            validate_mpt_network_config(self.mpt, 19080)
        self.assertEqual("MPT_CONFIG_CODEC_STRATEGY_MISMATCH", wrong_codec.exception.code)

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
        self.assertEqual(
            app[-5:],
            ["--host", "127.0.0.1", "--port", "18765", "--strict-port"],
        )
        joined = " ".join(engine + app).lower()
        for forbidden in ("deepseek", "api_key", "authorization", "cookie", "secret"):
            self.assertNotIn(forbidden, joined)

    def test_occupied_loopback_port_is_never_reported_available(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            port = occupied.getsockname()[1]
            self.assertFalse(_port_is_available(port))
            if port <= 65516:
                self.assertNotEqual(port, select_loopback_port(port))

    def test_engine_environment_drops_keys_and_cookies_while_app_receives_adapter_address(self):
        parent = {
            "PATH": "safe-bin",
            "SYSTEMROOT": "C:\\Windows",
            "DEEPSEEK_API_KEY": "should-not-leak",
            "DEEPSEEK_BASE_URL": "https://untrusted.invalid",
            "OPENAI_API_KEY": "should-not-leak",
            "COOKIE": "should-not-leak",
            "AUTHORIZATION": "should-not-leak",
            "SHIYI_ALLOW_TEST_PROVIDER": "1",
            "SHIYI_EXPERIMENTAL_DYNAMIC_TOPICS": "1",
            "SHIYI_AGENT_TEST_REVIEW": "1",
            "SHIYI_STAGE_REVIEW_MODE": "mechanical",
            "SHIYI_INTERNAL_DIAGNOSTICS": "1",
            "SHIYI_LAUNCH_INSTANCE_TOKEN": "host-controlled-token",
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
        self.assertNotIn("SHIYI_STAGE_REVIEW_MODE", app_env)
        self.assertNotIn("SHIYI_INTERNAL_DIAGNOSTICS", app_env)
        self.assertNotIn("SHIYI_LAUNCH_INSTANCE_TOKEN", app_env)
        self.assertNotIn("PYTHONPATH", app_env)
        self.assertNotIn("PYTHONHOME", app_env)
        self.assertEqual("should-not-leak", app_env["DEEPSEEK_API_KEY"])
        self.assertNotIn("DEEPSEEK_BASE_URL", app_env)
        self.assertNotIn("OPENAI_API_KEY", app_env)
        self.assertNotIn("COOKIE", app_env)
        self.assertNotIn("AUTHORIZATION", app_env)

        bound_app_env = build_app_environment(
            parent,
            self.config,
            app_port=18765,
            mpt_port=19080,
            launch_instance_token="launcher-generated-token",
        )
        self.assertEqual(
            "launcher-generated-token",
            bound_app_env["SHIYI_LAUNCH_INSTANCE_TOKEN"],
        )

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

        mechanical_config = LauncherConfig(
            **{**self.config.__dict__, "mechanical_review": True}
        )
        mechanical_env = build_app_environment(
            parent,
            mechanical_config,
            app_port=18765,
            mpt_port=19080,
        )
        self.assertNotIn("SHIYI_AGENT_TEST_REVIEW", mechanical_env)
        self.assertEqual(
            mechanical_env["SHIYI_STAGE_REVIEW_MODE"], "mechanical"
        )

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

    def test_packaged_app_uses_stable_per_user_runtime_directory(self):
        (self.project / "PACKAGE-MANIFEST.json").write_text("{}\n", encoding="utf-8")
        local_app_data = self.root / "LocalAppData"
        environment = build_app_environment(
            {
                "LOCALAPPDATA": str(local_app_data),
                "SYSTEMROOT": "C:\\Windows",
                "CHROME_LOG_FILE": str(self.project / "runtime" / "browser" / "debug.log"),
            },
            self.config,
            app_port=18765,
            mpt_port=19080,
        )
        self.assertEqual(
            str((local_app_data / "ShiyiContentFactory" / "UserData").resolve()),
            environment["SHIYI_RUNTIME_DIR"],
        )
        self.assertEqual(
            str((local_app_data / "ShiyiContentFactory" / "Launcher" / "chrome-debug.log").resolve()),
            environment["CHROME_LOG_FILE"],
        )
        self.assertNotIn(str(self.project / "runtime" / "browser"), environment["CHROME_LOG_FILE"])

    def test_packaged_app_rejects_local_app_data_inside_package(self):
        (self.project / "PACKAGE-MANIFEST.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(LauncherError) as context:
            build_app_environment(
                {"LOCALAPPDATA": str(self.project / "host-data"), "SYSTEMROOT": "C:\\Windows"},
                self.config,
                app_port=18765,
                mpt_port=19080,
            )
        self.assertEqual("LOCAL_APP_DATA_UNSAFE", context.exception.code)

    def test_packaged_single_instance_rejects_local_app_data_inside_package_before_writing(self):
        (self.project / "PACKAGE-MANIFEST.json").write_text("{}\n", encoding="utf-8")
        unsafe_root = self.project / "host-data"
        with (
            patch.dict(os.environ, {"LOCALAPPDATA": str(unsafe_root)}, clear=False),
            self.assertRaises(LauncherError) as context,
        ):
            _claim_launcher_state(
                self.project,
                started_at="2026-08-10T00:00:00+00:00",
                query_process=lambda _pid: None,
            )
        self.assertEqual("LOCAL_APP_DATA_UNSAFE", context.exception.code)
        self.assertFalse(unsafe_root.exists())

    def test_stop_entry_validates_identity_then_kills_only_recorded_trees(self):
        state_dir = self.project / "runtime" / "combined-launcher"
        state_dir.mkdir(parents=True)
        started = "2026-08-10T00:00:00+00:00"
        app_executable = str((self.project / ".venv" / "Scripts" / "python.exe").resolve())
        launcher_executable = str(Path(os.sys.executable).resolve())
        state_path = state_dir / "launcher-state.json"
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "project_root": str(self.project.resolve()),
                    "started_at": started,
                    "app_port": 8765,
                    "mpt_port": 19080,
                    "launcher": {"pid": 42001, "executable": launcher_executable, "started_at": started},
                    "app": {"pid": 42002, "executable": app_executable, "started_at": started},
                    "mpt": None,
                }
            ) + "\n",
            encoding="utf-8",
        )
        identities = {
            42001: {"pid": 42001, "path": launcher_executable, "started_at": started},
            42002: {"pid": 42002, "path": app_executable, "started_at": started},
        }
        commands = []

        def runner(command, **_kwargs):
            commands.append(list(command))
            return subprocess.CompletedProcess(command, 0, b"", b"")

        result = stop_recorded_processes(
            self.project,
            query_process=lambda pid: identities.get(pid),
            runner=runner,
        )
        self.assertEqual([42002, 42001], result["stopped_pids"])
        self.assertEqual(
            [["taskkill", "/PID", "42002", "/T", "/F"], ["taskkill", "/PID", "42001", "/T", "/F"]],
            commands,
        )
        self.assertFalse(state_path.exists())

    def test_stop_entry_refuses_pid_reuse(self):
        state_dir = self.project / "runtime" / "combined-launcher"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "launcher-state.json"
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "project_root": str(self.project.resolve()),
                    "started_at": "2026-08-10T00:00:00+00:00",
                    "app_port": 8765,
                    "mpt_port": 19080,
                    "launcher": {"pid": 42001, "executable": str(Path(os.sys.executable).resolve()), "started_at": "2026-08-10T00:00:00+00:00"},
                    "app": None,
                    "mpt": None,
                }
            ) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(LauncherError) as context:
            stop_recorded_processes(
                self.project,
                query_process=lambda _pid: {
                    "pid": 42001,
                    "path": str((self.root / "other.exe").resolve()),
                    "started_at": "2026-08-10T00:00:01+00:00",
                },
                runner=lambda *_args, **_kwargs: self.fail("must not kill mismatched pid"),
            )
        self.assertEqual("PROCESS_IDENTITY_MISMATCH", context.exception.code)
        self.assertTrue(state_path.exists())

    def test_stop_entry_rejects_state_owned_by_another_packaged_root_before_query(self):
        local_app_data = self.root / "LocalAppData"
        (self.project / "PACKAGE-MANIFEST.json").write_text("{}\n", encoding="utf-8")
        other = self.root / "other-package"
        other.mkdir()
        (other / "PACKAGE-MANIFEST.json").write_text("{}\n", encoding="utf-8")
        state_path = local_app_data / "ShiyiContentFactory" / "Launcher" / "launcher-state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "phase": "ready",
                    "project_root": str(other.resolve()),
                    "started_at": "2026-08-10T00:00:00+00:00",
                    "app_port": 8765,
                    "mpt_port": 19080,
                    "launcher": {
                        "pid": 42001,
                        "executable": str(Path(os.sys.executable).resolve()),
                        "started_at": "2026-08-10T00:00:00+00:00",
                    },
                    "app": None,
                    "mpt": None,
                }
            ) + "\n",
            encoding="utf-8",
        )
        with (
            patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}, clear=False),
            self.assertRaises(LauncherError) as context,
        ):
            stop_recorded_processes(
                self.project,
                query_process=lambda _pid: self.fail("foreign package state must not query a process"),
                runner=lambda *_args, **_kwargs: self.fail("foreign package state must not kill a process"),
            )
        self.assertEqual("LAUNCHER_STATE_ROOT_MISMATCH", context.exception.code)
        self.assertTrue(state_path.exists())

    def test_stop_entry_refuses_same_executable_with_reused_later_pid(self):
        state_dir = self.project / "runtime" / "combined-launcher"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "launcher-state.json"
        executable = str(Path(os.sys.executable).resolve())
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "phase": "ready",
                    "project_root": str(self.project.resolve()),
                    "started_at": "2026-08-10T00:00:00+00:00",
                    "app_port": 8765,
                    "mpt_port": 19080,
                    "launcher": {
                        "pid": 42001,
                        "executable": executable,
                        "started_at": "2026-08-10T00:00:00+00:00",
                    },
                    "app": None,
                    "mpt": None,
                }
            ) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(LauncherError) as context:
            stop_recorded_processes(
                self.project,
                query_process=lambda _pid: {
                    "pid": 42001,
                    "path": executable,
                    "started_at": "2026-08-10T06:00:00+00:00",
                },
                runner=lambda *_args, **_kwargs: self.fail("must not kill a reused pid"),
            )
        self.assertEqual("PROCESS_IDENTITY_MISMATCH", context.exception.code)
        self.assertTrue(state_path.exists())

    def test_single_instance_claim_rejects_live_recorded_launcher(self):
        state_dir = self.project / "runtime" / "combined-launcher"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "launcher-state.json"
        executable = str(Path(os.sys.executable).resolve())
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "phase": "ready",
                    "project_root": str(self.project.resolve()),
                    "started_at": "2026-08-10T00:00:00+00:00",
                    "app_port": 8765,
                    "mpt_port": 19080,
                    "launcher": {"pid": 42001, "executable": executable, "started_at": "2026-08-10T00:00:00+00:00"},
                    "app": None,
                    "mpt": None,
                }
            ) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(LauncherError) as context:
            _claim_launcher_state(
                self.project,
                started_at="2026-08-10T00:01:00+00:00",
                query_process=lambda pid: {"pid": pid, "path": executable, "started_at": "2026-08-10T00:00:00+00:00"},
            )
        self.assertEqual("WORKBENCH_ALREADY_RUNNING", context.exception.code)

    @unittest.skipUnless(os.name == "nt", "Windows process identity is Windows-only")
    def test_process_identity_query_writes_utf8_bytes_without_trusting_console_code_page(self):
        captured: dict[str, object] = {}
        payload = {
            "pid": 42001,
            "path": str(self.root / "中文目录" / "runtime" / "python" / "python.exe"),
            "started_at": "2026-08-10T00:00:00.0000000Z",
        }

        def runner(command, **kwargs):
            captured["command"] = list(command)
            captured["kwargs"] = dict(kwargs)
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                b"",
            )

        result = _query_windows_process(42001, runner=runner)

        self.assertEqual(payload, result)
        powershell_command = captured["command"][-1]
        self.assertIn("[System.Text.UTF8Encoding]::new($false).GetBytes($json)", powershell_command)
        self.assertIn("[Console]::OpenStandardOutput()", powershell_command)
        self.assertEqual(subprocess.PIPE, captured["kwargs"]["stdout"])
        self.assertEqual(subprocess.PIPE, captured["kwargs"]["stderr"])

    @unittest.skipUnless(os.name == "nt", "Windows process identity is Windows-only")
    def test_process_identity_query_survives_cp936_and_chinese_executable_path(self):
        helper_dir = self.root / "中文 空格 & path"
        helper_dir.mkdir()
        helper = helper_dir / "process-helper.exe"
        ping = Path(os.environ["SYSTEMROOT"]) / "System32" / "ping.exe"
        shutil.copy2(ping, helper)
        process = subprocess.Popen(
            [str(helper), "-n", "20", "127.0.0.1"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        def cp936_runner(command, **kwargs):
            forced = list(command)
            forced[-1] = (
                "[Console]::OutputEncoding=[System.Text.Encoding]::GetEncoding(936);"
                + str(forced[-1])
            )
            return subprocess.run(forced, **kwargs)

        try:
            self.assertIsNone(process.poll())
            identity = _query_windows_process(process.pid, runner=cp936_runner)
            self.assertIsNotNone(identity)
            self.assertEqual(process.pid, identity["pid"])
            self.assertTrue(_same_windows_path(str(helper), str(identity["path"])))
            self.assertIsInstance(identity["started_at"], str)
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def test_single_instance_claim_replaces_stale_record(self):
        state_dir = self.project / "runtime" / "combined-launcher"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "launcher-state.json"
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "phase": "starting",
                    "project_root": str(self.project.resolve()),
                    "started_at": "2026-08-10T00:00:00+00:00",
                    "app_port": None,
                    "mpt_port": None,
                    "launcher": {"pid": 42001, "executable": str(Path(os.sys.executable).resolve()), "started_at": "2026-08-10T00:00:00+00:00"},
                    "app": None,
                    "mpt": None,
                }
            ) + "\n",
            encoding="utf-8",
        )
        claimed = _claim_launcher_state(
            self.project,
            started_at="2026-08-10T00:01:00+00:00",
            query_process=lambda _pid: None,
        )
        payload = json.loads(claimed.read_text(encoding="utf-8"))
        self.assertEqual(os.getpid(), payload["launcher"]["pid"])
        self.assertEqual("starting", payload["phase"])

    def test_packaged_releases_share_one_per_user_single_instance_lock(self):
        local_app_data = self.root / "LocalAppData"
        first = self.root / "package-a"
        second = self.root / "package-b"
        for package in (first, second):
            package.mkdir()
            (package / "PACKAGE-MANIFEST.json").write_text("{}\n", encoding="utf-8")
        actual = {
            "pid": os.getpid(),
            "path": str(Path(os.sys.executable).resolve()),
            "started_at": "2026-08-10T00:00:00+00:00",
        }
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}, clear=False):
            _claim_launcher_state(
                first,
                started_at="2026-08-10T00:00:01+00:00",
                query_process=lambda _pid: actual,
            )
            with self.assertRaises(LauncherError) as context:
                _claim_launcher_state(
                    second,
                    started_at="2026-08-10T00:00:02+00:00",
                    query_process=lambda _pid: actual,
                )
        self.assertEqual("WORKBENCH_ALREADY_RUNNING", context.exception.code)

    def test_ready_state_preserves_claimed_launcher_process_start_time(self):
        actual_started = "2026-08-10T00:00:00+00:00"
        claimed = _claim_launcher_state(
            self.project,
            started_at="2026-08-10T00:10:00+00:00",
            query_process=lambda pid: {
                "pid": pid,
                "path": str(Path(os.sys.executable).resolve()),
                "started_at": actual_started,
            },
        )
        app_process = FakeProcess()
        _write_launcher_state(
            self.config,
            started_at="2026-08-10T00:10:00+00:00",
            app_port=8765,
            mpt_port=63863,
            app_process=app_process,
            engine_process=None,
            app_started_at="2026-08-10T00:10:01+00:00",
            engine_started_at=None,
        )
        payload = json.loads(claimed.read_text(encoding="utf-8"))
        self.assertEqual(actual_started, payload["launcher"]["started_at"])

    def test_legacy_runtime_migration_is_copy_only_hash_checked_and_conflict_safe(self):
        source = self.root / "old-package" / "runtime"
        (source / "jobs" / "job-1").mkdir(parents=True)
        (source / "capability-packs").mkdir()
        (source / "config.json").write_text('{"provider":"deepseek"}\n', encoding="utf-8")
        secret_bytes = b"dpapi-ciphertext-fixture"
        (source / "secrets.json").write_bytes(secret_bytes)
        (source / "rules.json").write_text("{}\n", encoding="utf-8")
        corrections = '{"event_id":"correction-1","kind":"script"}\n'
        (source / "corrections.jsonl").write_text(corrections, encoding="utf-8")
        (source / "jobs" / "job-1" / "job.json").write_text('{"status":"complete"}\n', encoding="utf-8")
        (source / "jobs" / "job-1" / "final.mp4").write_bytes(b"video")
        local_app_data = self.root / "LocalAppData"
        result = import_legacy_runtime(source, environment={"LOCALAPPDATA": str(local_app_data)})
        destination = local_app_data / "ShiyiContentFactory" / "UserData"
        self.assertEqual("migrated", result["status"])
        self.assertTrue(result["secrets_migrated"])
        self.assertEqual(secret_bytes, (destination / "secrets.json").read_bytes())
        self.assertEqual(corrections, (destination / "corrections.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(secret_bytes, (source / "secrets.json").read_bytes())
        self.assertTrue((destination / "jobs" / "job-1" / "final.mp4").is_file())

        repeated = import_legacy_runtime(source, environment={"LOCALAPPDATA": str(local_app_data)})
        self.assertEqual("already_migrated", repeated["status"])
        (destination / "config.json").write_text('{"provider":"changed"}\n', encoding="utf-8")
        with self.assertRaises(LauncherError) as conflict:
            import_legacy_runtime(source, environment={"LOCALAPPDATA": str(local_app_data)})
        self.assertEqual("MIGRATION_DESTINATION_CONFLICT", conflict.exception.code)
        self.assertEqual('{"provider":"changed"}\n', (destination / "config.json").read_text(encoding="utf-8"))

    def test_legacy_runtime_migration_rejects_executable_or_script_payloads(self):
        source = self.root / "unsafe-old-runtime"
        (source / "jobs" / "job-1").mkdir(parents=True)
        (source / "jobs" / "job-1" / "payload.py").write_text("raise SystemExit\n", encoding="utf-8")
        with self.assertRaises(LauncherError) as context:
            import_legacy_runtime(
                source,
                environment={"LOCALAPPDATA": str(self.root / "LocalAppData")},
            )
        self.assertEqual("MIGRATION_FILE_TYPE_REJECTED", context.exception.code)
        self.assertFalse((self.root / "LocalAppData" / "ShiyiContentFactory" / "UserData").exists())

    def test_packaged_legacy_migration_rejects_local_app_data_inside_package_before_writing(self):
        (self.project / "PACKAGE-MANIFEST.json").write_text("{}\n", encoding="utf-8")
        source = self.root / "legacy-runtime"
        source.mkdir()
        original = b'{"provider":"deepseek"}\n'
        source_file = source / "config.json"
        source_file.write_bytes(original)
        unsafe_root = self.project / "host-data"

        with self.assertRaises(LauncherError) as context:
            import_legacy_runtime(
                source,
                environment={"LOCALAPPDATA": str(unsafe_root)},
                project_root=self.project,
            )

        self.assertEqual("LOCAL_APP_DATA_UNSAFE", context.exception.code)
        self.assertFalse(unsafe_root.exists())
        self.assertEqual(original, source_file.read_bytes())

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
        command_arguments = {
            value.casefold()
            for command in commands
            for value in command[1:]
        }
        for forbidden in ("pip", "uv", "npm", "npx", "download", "install"):
            self.assertNotIn(forbidden, command_arguments)

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

    def test_workbench_health_requires_product_and_fresh_instance_identity(self):
        expected = hashlib.sha256(b"fresh-launch-token").hexdigest()
        valid = json.dumps(
            {
                "name": "时宜 Agent 内容工厂",
                "version": "0.3.0",
                "schema_version": 2,
                "launch_instance_sha256": expected,
            }
        ).encode("utf-8")
        wait_for_workbench_health(
            "http://127.0.0.1:18765/api/status",
            timeout_seconds=1,
            requester=lambda _url, _timeout: valid,
            process=FakeProcess([None, None]),
            expected_instance_sha256=expected,
        )

        foreign = json.dumps(
            {
                "name": "时宜 Agent 内容工厂",
                "version": "0.3.0",
                "schema_version": 2,
                "launch_instance_sha256": hashlib.sha256(b"old-token").hexdigest(),
            }
        ).encode("utf-8")
        ticks = iter((0.0, 0.0, 2.0))
        with self.assertRaises(LauncherError) as mismatch:
            wait_for_workbench_health(
                "http://127.0.0.1:18765/api/status",
                timeout_seconds=1,
                requester=lambda _url, _timeout: foreign,
                clock=lambda: next(ticks),
                sleeper=lambda _seconds: None,
                process=FakeProcess([None]),
                expected_instance_sha256=expected,
            )
        self.assertEqual("WORKBENCH_HEALTH_TIMEOUT", mismatch.exception.code)

        with self.assertRaises(LauncherError) as exited_after_response:
            wait_for_workbench_health(
                "http://127.0.0.1:18765/api/status",
                timeout_seconds=1,
                requester=lambda _url, _timeout: valid,
                process=FakeProcess([None, 9]),
                expected_instance_sha256=expected,
            )
        self.assertEqual("WORKBENCH_EARLY_EXIT", exited_after_response.exception.code)

        arbitrary_ticks = iter((0.0, 0.0, 2.0))
        with self.assertRaises(LauncherError) as arbitrary_json:
            wait_for_workbench_health(
                "http://127.0.0.1:18765/api/status",
                timeout_seconds=1,
                requester=lambda _url, _timeout: b"{}",
                clock=lambda: next(arbitrary_ticks),
                sleeper=lambda _seconds: None,
            )
        self.assertEqual("WORKBENCH_HEALTH_TIMEOUT", arbitrary_json.exception.code)

    def test_controller_never_publishes_ready_for_foreign_workbench_identity(self):
        created = []
        processes = [FakeProcess([None]), FakeProcess([None])]

        def fake_popen(command, **kwargs):
            created.append((list(command), kwargs))
            return processes[len(created) - 1]

        def foreign_health(url, **kwargs):
            foreign = json.dumps(
                {
                    "name": "时宜 Agent 内容工厂",
                    "version": "0.3.0",
                    "schema_version": 2,
                    "launch_instance_sha256": hashlib.sha256(b"foreign-token").hexdigest(),
                }
            ).encode("utf-8")
            ticks = iter((0.0, 0.0, 2.0))
            wait_for_workbench_health(
                url,
                timeout_seconds=1,
                requester=lambda _url, _timeout: foreign,
                clock=lambda: next(ticks),
                sleeper=lambda _seconds: None,
                process=kwargs["process"],
                expected_instance_sha256=kwargs["expected_instance_sha256"],
            )

        terminated = []
        with (
            patch("scripts.launch_combined.select_loopback_port", side_effect=[18765, 19080]),
            patch("scripts.launch_combined._write_launcher_state") as write_ready,
            patch("scripts.launch_combined._emit") as emit,
        ):
            with self.assertRaises(LauncherError) as context:
                run_combined(
                    self.config,
                    popen_factory=fake_popen,
                    health_waiter=lambda *_args, **_kwargs: None,
                    workbench_health_waiter=foreign_health,
                    process_terminator=lambda process: terminated.append(process),
                    sleeper=lambda _seconds: None,
                )
        self.assertEqual("WORKBENCH_HEALTH_TIMEOUT", context.exception.code)
        write_ready.assert_not_called()
        emit.assert_not_called()
        self.assertEqual([processes[1], processes[0]], terminated)

    def test_motion_launcher_mode_uses_only_bundled_tools_and_starts_without_mpt(self):
        node = self.project / "runtime" / "node" / "node.exe"
        cli = self.project / "runtime" / "hyperframes" / "node_modules" / "hyperframes" / "bin" / "hyperframes.mjs"
        bundle = cli.parent.parent / "dist" / "cli.js"
        for path in (node, cli, bundle):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        motion = LauncherConfig(
            **{
                **self.config.__dict__,
                "mpt_root": self.root / "missing-mpt",
                "mpt_python": self.root / "missing-mpt" / "python.exe",
                "material_root": self.root / "missing-mpt" / "materials",
                "motion_runtime_required": True,
                "node_executable": node,
                "hyperframes_cli": cli,
                "hyperframes_browser": self.edge,
                "node_version": "22.13.1",
                "hyperframes_version": "0.7.86",
                "browser_version": "151.0.4129.72",
                "browser_strategy": SYSTEM_EDGE_BROWSER_STRATEGY,
                "browser_minimum_major": SYSTEM_EDGE_MINIMUM_MAJOR,
            }
        )
        with (
            patch("scripts.launch_combined._file_sha256", return_value=HYPERFRAMES_PATCHED_CLI_SHA256),
            patch("scripts.launch_combined._trusted_program_files_roots", return_value=(self.program_files,)),
        ):
            validate_preinstalled_layout(motion)
            with self.assertRaises(LauncherError) as wrong_hyperframes:
                validate_preinstalled_layout(
                    LauncherConfig(**{**motion.__dict__, "hyperframes_version": "0.7.103"})
                )
        self.assertEqual("HYPERFRAMES_VERSION_MISMATCH", wrong_hyperframes.exception.code)
        with (
            patch("scripts.launch_combined._file_sha256", return_value="0" * 64),
            patch("scripts.launch_combined._trusted_program_files_roots", return_value=(self.program_files,)),
            self.assertRaises(LauncherError) as wrong_patch,
        ):
            validate_preinstalled_layout(motion)
        self.assertEqual("HYPERFRAMES_PATCH_MISMATCH", wrong_patch.exception.code)
        probe_commands = []

        def fake_motion_probe(command, **_kwargs):
            probe_commands.append(list(command))
            environment = _kwargs.get("env", {})
            if environment.get("SHIYI_EDGE_SIGNATURE_TARGET"):
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(self._edge_identity_payload()).encode("utf-8"),
                    b"",
                )
            if "check" in command:
                return subprocess.CompletedProcess(command, 0, b'{"ok":true}', b"")
            if command[0] == str(self.config.ffmpeg):
                Path(command[-1]).write_bytes(b"fixture-mp4")
                return subprocess.CompletedProcess(command, 0, b"", b"")
            if command[0] == str(self.config.ffprobe):
                streams = {
                    "streams": [
                        {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"},
                        {"codec_type": "audio", "codec_name": "aac"},
                    ]
                }
                return subprocess.CompletedProcess(command, 0, json.dumps(streams).encode(), b"")
            stdout = (
                b"v22.13.1"
                if command == [str(node), "--version"]
                else b"0.7.86"
                if command == [str(node), str(cli), "--version"]
                else b""
            )
            return subprocess.CompletedProcess(command, 0, stdout, b"")

        with patch(
            "scripts.launch_combined._trusted_program_files_roots",
            return_value=(self.program_files,),
        ):
            probe_preinstalled_runtimes(motion, runner=fake_motion_probe)
        self.assertIn([str(node), "--version"], probe_commands)
        self.assertIn([str(node), str(cli), "--version"], probe_commands)
        self.assertTrue(any("Get-AuthenticodeSignature" in command[-1] for command in probe_commands))
        self.assertTrue(any("check" in command and "--strict" in command for command in probe_commands))
        self.assertFalse(any(command[:2] == [str(self.edge), "--version"] for command in probe_commands))
        self.assertFalse(any("-ProbeOnly" in command or "sapi_tts.ps1" in " ".join(command) for command in probe_commands))
        encode_command = next(command for command in probe_commands if command[0] == str(self.config.ffmpeg))
        self.assertIn("h264_mf", encode_command)
        self.assertIn("quality", encode_command)
        self.assertIn("archive", encode_command)
        self.assertIn("-framerate", encode_command)
        self.assertNotIn("lavfi", encode_command)
        for forbidden in ("libx264", "-crf", "-preset", "-x264-params"):
            self.assertNotIn(forbidden, encode_command)
        environment = build_app_environment(
            {
                "PATH": "host-npm-and-chrome",
                "SYSTEMROOT": "WindowsRoot",
                "NODE_OPTIONS": "--import=host-loader.mjs",
                "NODE_PATH": "host-node-modules",
                "NPM_CONFIG_REGISTRY": "https://registry.invalid",
                "COREPACK_HOME": "host-corepack",
                "HTTPS_PROXY": "http://proxy.invalid",
                "SHIYI_MOTION_HEALTH_VERIFIED": "1",
                "HYPERFRAME_RUNTIME_URL": "https://runtime.invalid",
                "HYPERFRAMES_PREVIEW_HOST": "preview.invalid",
                "PRODUCER_ENDPOINT": "https://producer.invalid",
                "KEEP_TEMP": "1",
                "GEMINI_API_KEY": "model-secret",
                "OPENROUTER_API_KEY": "model-secret",
                "HF_TOKEN": "model-secret",
                "MODEL_PROVIDER": "host-model",
                "PUPPETEER_EXECUTABLE_PATH": "host-browser.exe",
                "HYPERFRAMES_BROWSER_PATH": "host-browser.exe",
                "SHIYI_HYPERFRAMES_BROWSER_STRATEGY": "host",
                "SHIYI_HYPERFRAMES_BROWSER_VERSION": "999.0.0.0",
                "SHIYI_HYPERFRAMES_BROWSER_MINIMUM_MAJOR": "999",
            },
            motion,
            app_port=18765,
            mpt_port=19080,
        )
        self.assertEqual("0", environment["SHIYI_MPT_ENABLED"])
        self.assertEqual(str(node), environment["SHIYI_NODE_EXECUTABLE"])
        self.assertEqual(str(cli), environment["SHIYI_HYPERFRAMES_CLI"])
        self.assertEqual("h264_mf", environment["SHIYI_HYPERFRAMES_CODEC_STRATEGY"])
        self.assertEqual(str(self.edge), environment["HYPERFRAMES_BROWSER_PATH"])
        self.assertEqual(SYSTEM_EDGE_BROWSER_STRATEGY, environment["SHIYI_HYPERFRAMES_BROWSER_STRATEGY"])
        self.assertEqual("151.0.4129.72", environment["SHIYI_HYPERFRAMES_BROWSER_VERSION"])
        self.assertEqual(str(SYSTEM_EDGE_MINIMUM_MAJOR), environment["SHIYI_HYPERFRAMES_BROWSER_MINIMUM_MAJOR"])
        self.assertNotIn("host-npm-and-chrome", environment["PATH"])
        self.assertIn(
            str(Path("WindowsRoot") / "System32" / "WindowsPowerShell" / "v1.0"),
            environment["PATH"].split(os.pathsep),
        )
        self.assertIn(
            str(Path("WindowsRoot") / "System32"),
            environment["PATH"].split(os.pathsep),
        )
        for stripped in (
            "NODE_OPTIONS",
            "NODE_PATH",
            "NPM_CONFIG_REGISTRY",
            "COREPACK_HOME",
            "HTTPS_PROXY",
            "SHIYI_MOTION_HEALTH_VERIFIED",
            "HYPERFRAME_RUNTIME_URL",
            "HYPERFRAMES_PREVIEW_HOST",
            "PRODUCER_ENDPOINT",
            "KEEP_TEMP",
            "GEMINI_API_KEY",
            "OPENROUTER_API_KEY",
            "HF_TOKEN",
            "MODEL_PROVIDER",
            "PUPPETEER_EXECUTABLE_PATH",
        ):
            self.assertNotIn(stripped, environment)
        for name in (
            "HYPERFRAMES_NO_UPDATE_CHECK",
            "HYPERFRAMES_NO_AUTO_INSTALL",
            "HYPERFRAMES_NO_TELEMETRY",
            "HYPERFRAMES_SKIP_SKILLS",
            "DO_NOT_TRACK",
            "NO_UPDATE_NOTIFIER",
            "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD",
        ):
            self.assertEqual("1", environment[name])
        self.assertEqual("true", environment["NPM_CONFIG_OFFLINE"])
        self.assertEqual("true", environment["PUPPETEER_SKIP_DOWNLOAD"])

        verified_environment = build_app_environment(
            {"SYSTEMROOT": "WindowsRoot"},
            motion,
            app_port=18765,
            mpt_port=19080,
            motion_health_verified=True,
        )
        self.assertEqual("1", verified_environment["SHIYI_MOTION_HEALTH_VERIFIED"])

        created = []
        app_process = FakeProcess([None, 0])
        workbench_checks = []

        def fake_popen(command, **kwargs):
            created.append((list(command), kwargs))
            return app_process

        with (
            patch("scripts.launch_combined.select_loopback_port", side_effect=[18765, 19080]),
            patch("scripts.launch_combined._emit") as emit,
        ):
            result = run_combined(
                motion,
                popen_factory=fake_popen,
                health_waiter=lambda *_args, **_kwargs: self.fail("MPT must not be probed"),
                workbench_health_waiter=lambda *_args, **kwargs: workbench_checks.append(kwargs),
                process_terminator=lambda _process: None,
                sleeper=lambda _seconds: None,
                motion_health_verified=True,
            )
        self.assertEqual(0, result)
        self.assertEqual(1, len(created))
        self.assertEqual("0", created[0][1]["env"]["SHIYI_MPT_ENABLED"])
        self.assertEqual("1", created[0][1]["env"]["SHIYI_MOTION_HEALTH_VERIFIED"])
        launch_token = created[0][1]["env"]["SHIYI_LAUNCH_INSTANCE_TOKEN"]
        self.assertEqual(
            hashlib.sha256(launch_token.encode("utf-8")).hexdigest(),
            workbench_checks[0]["expected_instance_sha256"],
        )
        mpt_health_file = Path(created[0][1]["env"]["SHIYI_MPT_HEALTH_FILE"])
        self.assertEqual(
            {"healthy": False, "schema_version": 1},
            json.loads(mpt_health_file.read_text(encoding="utf-8")),
        )
        payload = emit.call_args.args[0]
        self.assertEqual("ready", payload["motion_engine"]["health"])
        self.assertEqual(SYSTEM_EDGE_BROWSER_STRATEGY, payload["motion_engine"]["browser_strategy"])
        self.assertEqual("151.0.4129.72", payload["motion_engine"]["detected_edge_version"])
        self.assertEqual(
            {"hyperframes_strict_check": "passed", "h264_mf_encode_probe": "passed"},
            payload["motion_engine"]["startup_canaries"],
        )
        self.assertEqual("disabled", payload["production_engine"]["health"])

        def app_start_failure(*_args, **_kwargs):
            raise OSError("fixture start failure")

        with patch("scripts.launch_combined.select_loopback_port", side_effect=[18765, 19080]):
            with self.assertRaises(LauncherError) as failed_start:
                run_combined(
                    motion,
                    popen_factory=app_start_failure,
                    health_waiter=lambda *_args, **_kwargs: self.fail("MPT must not be probed"),
                    workbench_health_waiter=lambda *_args, **_kwargs: None,
                    process_terminator=lambda _process: None,
                    sleeper=lambda _seconds: None,
                    motion_health_verified=True,
                )
        self.assertEqual("WORKBENCH_START_FAILED", failed_start.exception.code)

        interrupted_process = FakeProcess([None, None])
        interrupted_launch = []
        terminated = []

        def interrupted_popen(command, **kwargs):
            interrupted_launch.append((list(command), kwargs))
            return interrupted_process

        with (
            patch("scripts.launch_combined.select_loopback_port", side_effect=[18765, 19080]),
            patch("scripts.launch_combined._emit"),
        ):
            result = run_combined(
                motion,
                popen_factory=interrupted_popen,
                health_waiter=lambda *_args, **_kwargs: self.fail("MPT must not be probed"),
                workbench_health_waiter=lambda *_args, **_kwargs: None,
                process_terminator=lambda process: terminated.append(process),
                sleeper=lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
                motion_health_verified=True,
            )
        self.assertEqual(130, result)
        self.assertEqual([interrupted_process, None], terminated)
        interrupted_health = Path(
            interrupted_launch[0][1]["env"]["SHIYI_MPT_HEALTH_FILE"]
        )
        self.assertEqual(
            {"healthy": False, "schema_version": 1},
            json.loads(interrupted_health.read_text(encoding="utf-8")),
        )

    def test_motion_primary_revokes_mpt_health_if_optional_engine_crashes(self):
        node = self.project / "runtime/node/node.exe"
        cli = self.project / "runtime/hyperframes/node_modules/hyperframes/bin/hyperframes.mjs"
        for path in (node, cli):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        motion = LauncherConfig(
            **{
                **self.config.__dict__,
                "motion_runtime_required": True,
                "node_executable": node,
                "hyperframes_cli": cli,
                "hyperframes_browser": self.edge,
                "node_version": "22.13.1",
                "hyperframes_version": "0.7.86",
                "browser_version": "151.0.4129.72",
                "browser_strategy": SYSTEM_EDGE_BROWSER_STRATEGY,
                "browser_minimum_major": SYSTEM_EDGE_MINIMUM_MAJOR,
            }
        )
        processes = [FakeProcess([None, 1]), FakeProcess([None, None, 0])]
        created = []
        health_timeouts = []

        def fake_popen(command, **kwargs):
            created.append((list(command), kwargs))
            return processes[len(created) - 1]

        with (
            patch("scripts.launch_combined.select_loopback_port", side_effect=[18765, 19080]),
            patch("scripts.launch_combined._emit"),
        ):
            result = run_combined(
                motion,
                popen_factory=fake_popen,
                health_waiter=lambda *_args, **kwargs: health_timeouts.append(
                    kwargs["timeout_seconds"]
                ),
                workbench_health_waiter=lambda *_args, **_kwargs: None,
                process_terminator=lambda _process: None,
                sleeper=lambda _seconds: None,
                motion_health_verified=True,
            )
        self.assertEqual(0, result)
        self.assertEqual(
            [MOTION_OPTIONAL_MPT_HEALTH_TIMEOUT_SECONDS],
            health_timeouts,
        )
        app_environment = created[1][1]["env"]
        self.assertEqual("1", app_environment["SHIYI_MPT_HEALTH_VERIFIED"])
        health_path = Path(app_environment["SHIYI_MPT_HEALTH_FILE"])
        self.assertEqual(
            {"healthy": False, "schema_version": 1},
            json.loads(health_path.read_text(encoding="utf-8")),
        )

    def test_controller_starts_engine_before_app_sets_exact_cors_and_cleans_both(self):
        created = []
        processes = [FakeProcess([None, None]), FakeProcess([None, 0])]

        def fake_popen(command, **kwargs):
            created.append((list(command), kwargs))
            return processes[len(created) - 1]

        terminated = []
        workbench_checks = []
        with (
            patch("scripts.launch_combined.select_loopback_port", side_effect=[18765, 19080]),
            patch("scripts.launch_combined._emit"),
        ):
            result = run_combined(
                self.config,
                popen_factory=fake_popen,
                health_waiter=lambda *_args, **_kwargs: None,
                workbench_health_waiter=lambda *_args, **kwargs: workbench_checks.append(kwargs),
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
        launch_token = app_options["env"]["SHIYI_LAUNCH_INSTANCE_TOKEN"]
        self.assertEqual(
            hashlib.sha256(launch_token.encode("utf-8")).hexdigest(),
            workbench_checks[0]["expected_instance_sha256"],
        )
        self.assertEqual(terminated, [processes[1], processes[0]])


if __name__ == "__main__":
    unittest.main()
