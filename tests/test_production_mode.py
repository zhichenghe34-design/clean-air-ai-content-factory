from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

import app
from core.motion_runtime_contract import HYPERFRAMES_RENDERER, HYPERFRAMES_VERSION
from core.orchestrator import (
    UnprocessableError,
    is_legacy_footage_input,
    preserve_legacy_footage_contract,
    render_result_is_diagnostic,
    validate_topic_input,
)
from core.production import (
    ProductionRunner,
    _hyperframes_commands,
    _hyperframes_subprocess_environment,
    resolve_production_mode,
)


SAFE_SCRIPT = (
    "先把客户真正关心的问题说清楚，再解释判断条件和适用边界。"
    "公开内容只使用已经确认的资料，不把经验描述成事实，也不承诺未经验证的效果。"
    "如果证据不足，就明确说明还需要核对什么，并给出可以继续询问和查证的下一步。"
) * 3
APPROVALS = {"research": {"status": "approved"}, "compliance": {"status": "approved"}}


def _prepare_stage(folder: Path) -> None:
    payloads = {
        "research.json": {"findings": []},
        "insight.json": {},
        "script_variants.json": {"variants": [{"script": SAFE_SCRIPT}], "provider": {}},
        "approved_script.json": {"script": SAFE_SCRIPT},
        "review.json": {"status": "needs_human", "blocked": False, "warnings": []},
    }
    for name, payload in payloads.items():
        (folder / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _voice_adapter(folder: Path, _script: str, _config: dict) -> dict:
    (folder / "voice.wav").write_bytes(b"RIFF" + b"\0" * 64)
    return {"engine": "contract-test"}


def _render_adapter(folder: Path, _motion_plan: dict, _config: dict) -> dict:
    (folder / "final.mp4").write_bytes(b"contract-test-video")
    return {
        "mode": "contract-test",
        "duration_seconds": 52.0,
        "width": 1080,
        "height": 1920,
        "video_codec": "h264",
        "audio_codec": "aac",
    }


def _visual_qc(_video_path: Path, *, output_dir: Path, **_kwargs) -> dict:
    output = Path(output_dir)
    (output / "contact-sheet.png").write_bytes(b"contact-sheet")
    payload = {
        "schema_version": 1,
        "status": "passed",
        "sample_count": 12,
        "blocking_reasons": [],
        "review_reasons": [],
    }
    (output / "visual-qc.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


class ProductionModeValidationTests(unittest.TestCase):
    def base_input(self) -> dict:
        return {"topic": "本地门店短视频选题", "audience": "本地服务潜在客户"}

    def test_default_is_fail_closed_motion_with_legacy_fields(self):
        normalized = validate_topic_input(self.base_input())
        self.assertEqual(normalized["production_mode"], "motion")
        self.assertEqual(normalized["render_mode"], "animated")
        self.assertIs(normalized["require_animation"], True)

    def test_selected_mode_canonicalizes_legacy_response_fields(self):
        expected = {
            "motion": ("animated", True),
            "footage": ("animated", False),
            "hybrid": ("animated", False),
            "simple": ("simple", False),
        }
        for mode, legacy in expected.items():
            with self.subTest(mode=mode):
                values = {**self.base_input(), "production_mode": mode}
                normalized = validate_topic_input(values)
                self.assertEqual(
                    (normalized["render_mode"], normalized["require_animation"]), legacy
                )

    def test_invalid_mode_is_rejected_as_422_contract_error(self):
        with self.assertRaises(UnprocessableError) as raised:
            validate_topic_input({**self.base_input(), "production_mode": "automatic"})
        self.assertEqual(raised.exception.status, 422)

    def test_legacy_simple_and_explicit_adapter_resolution_remain_narrow(self):
        self.assertEqual(resolve_production_mode({"render_mode": "simple"}), "simple")
        self.assertEqual(resolve_production_mode({}, legacy_engine_adapter=True), "footage")
        self.assertEqual(
            resolve_production_mode({"production_mode": "motion"}, legacy_engine_adapter=True),
            "motion",
        )

    def test_legacy_footage_signature_excludes_simple_and_animation_required(self):
        legacy = {"render_mode": "animated", "require_animation": False}
        self.assertTrue(is_legacy_footage_input(legacy))
        self.assertFalse(is_legacy_footage_input({"render_mode": "simple"}))
        self.assertFalse(is_legacy_footage_input({"require_animation": True}))
        self.assertFalse(is_legacy_footage_input({**legacy, "production_mode": "motion"}))

    def test_safe_migration_does_not_add_mode_fields_to_legacy_footage_job(self):
        legacy = {
            **self.base_input(),
            "render_mode": "animated",
            "require_animation": False,
        }
        normalized = validate_topic_input(legacy)
        preserved = preserve_legacy_footage_contract(legacy, normalized)
        self.assertNotIn("production_mode", preserved)
        self.assertEqual(preserved["render_mode"], "animated")
        self.assertIs(preserved["require_animation"], False)


class ProductionModeDispatchTests(unittest.TestCase):
    def runner(self, **kwargs) -> ProductionRunner:
        return ProductionRunner(
            voice_adapter=_voice_adapter,
            render_adapter=_render_adapter,
            visual_qc_adapter=_visual_qc,
            **kwargs,
        )

    def test_real_motion_call_ignores_mpt_adapter_and_reports_hyperframes(self):
        class Engine:
            def __init__(self):
                self.calls = 0

            def run(self, **_kwargs):
                self.calls += 1
                raise AssertionError("MPT must not run for motion")

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _prepare_stage(root)
            engine = Engine()
            runner = ProductionRunner(
                voice_adapter=_voice_adapter,
                visual_qc_adapter=_visual_qc,
                production_engine_adapter=engine,
            )

            def real_hyperframes_call(
                _runner: ProductionRunner,
                output: Path,
                _motion_plan: dict,
                _config: dict,
            ) -> dict:
                (output / "final.mp4").write_bytes(b"real-hyperframes-contract-video")
                return {
                    "mode": "animated_hyperframes",
                    "duration_seconds": 52.0,
                    "width": 1080,
                    "height": 1920,
                    "video_codec": "h264",
                    "audio_codec": "aac",
                }

            with (
                mock.patch.object(ProductionRunner, "_audio_duration", return_value=52.0),
                mock.patch.object(
                    ProductionRunner,
                    "_render_animated_video",
                    autospec=True,
                    side_effect=real_hyperframes_call,
                ) as hyperframes_render,
            ):
                report = runner.run_render_stage(
                    root,
                    {"topic": "本地门店短视频选题", "audience": "潜在客户", "production_mode": "motion"},
                    APPROVALS,
                )
        self.assertEqual(engine.calls, 0)
        hyperframes_render.assert_called_once()
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["production_mode"], "motion")
        self.assertEqual(report["production_engine"]["name"], "HyperFrames")
        self.assertTrue(report["render"]["production_mode"] == "motion")
        self.assertFalse(report["render"].get("diagnostic_only", False))
        self.assertEqual(report["render"]["caption_validation"]["status"], "passed")

    def test_injected_motion_adapter_is_named_and_manifest_ineligible(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _prepare_stage(root)
            runner = self.runner()
            with mock.patch.object(ProductionRunner, "_audio_duration", return_value=52.0):
                report = runner.run_render_stage(
                    root,
                    {
                        "topic": "本地门店短视频选题",
                        "audience": "潜在客户",
                        "production_mode": "motion",
                    },
                    APPROVALS,
                )
        identity = report["render"]["renderer_identity"]
        self.assertTrue(report["render"]["diagnostic_only"])
        self.assertTrue(render_result_is_diagnostic(report))
        self.assertEqual(identity["kind"], "injected_test_adapter")
        self.assertIs(identity["formal_engine"], False)
        self.assertIn("_render_adapter", identity["callable"])
        self.assertEqual(report["production_engine"]["name"], "Injected render adapter")
        self.assertEqual(report["production_engine"]["health"], "diagnostic_only")
        self.assertNotEqual(report["production_engine"]["name"], "HyperFrames")

    def test_footage_requires_mpt_and_never_falls_back_to_motion(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _prepare_stage(root)
            runner = self.runner()
            with self.assertRaisesRegex(RuntimeError, "MoneyPrinterTurbo"):
                runner.run_render_stage(
                    root,
                    {"topic": "本地门店短视频选题", "audience": "潜在客户", "production_mode": "footage"},
                    APPROVALS,
                )
            self.assertFalse((root / "final.mp4").exists())

    def test_hybrid_is_explicitly_unimplemented_before_any_engine_call(self):
        runner = self.runner()
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "尚未实现"):
                runner.run_render_stage(
                    Path(folder),
                    {"topic": "本地门店短视频选题", "audience": "潜在客户", "production_mode": "hybrid"},
                    APPROVALS,
                )

    def test_simple_output_is_diagnostic_and_not_a_formal_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _prepare_stage(root)
            runner = self.runner()
            with mock.patch.object(ProductionRunner, "_audio_duration", return_value=52.0):
                report = runner.run_render_stage(
                    root,
                    {"topic": "本地门店短视频选题", "audience": "潜在客户", "production_mode": "simple"},
                    APPROVALS,
                )
        self.assertEqual(report["status"], "diagnostic_only")
        self.assertTrue(report["render"]["diagnostic_only"])
        self.assertEqual(report["production_engine"]["health"], "diagnostic_only")


class HyperFramesRuntimeContractTests(unittest.TestCase):
    def test_motion_caption_contract_binds_the_entire_approved_script(self):
        with tempfile.TemporaryDirectory() as folder_name:
            captions = Path(folder_name) / "captions.srt"
            midpoint = len(SAFE_SCRIPT) // 2
            captions.write_text(
                "1\n00:00:00,000 --> 00:00:26,000\n"
                f"{SAFE_SCRIPT[:midpoint]}\n\n"
                "2\n00:00:26,000 --> 00:00:52,000\n"
                f"{SAFE_SCRIPT[midpoint:]}\n",
                encoding="utf-8",
            )
            report = ProductionRunner._validate_motion_captions(captions, 52.0, SAFE_SCRIPT)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["cue_count"], 2)
            with self.assertRaisesRegex(RuntimeError, "批准脚本不一致"):
                ProductionRunner._validate_motion_captions(captions, 52.0, SAFE_SCRIPT + "遗漏")

    def test_motion_media_contract_rejects_non_h264_aac_before_publication(self):
        probe = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "width": 1080,
                    "height": 1920,
                    "r_frame_rate": "30/1",
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": "52.0"},
        }
        with tempfile.TemporaryDirectory() as folder_name:
            video = Path(folder_name) / "final.mp4"
            video.write_bytes(b"video")
            with (
                mock.patch("core.production._tool_available", return_value=True),
                mock.patch("core.production.subprocess.check_output", return_value=json.dumps(probe)),
            ):
                with self.assertRaisesRegex(RuntimeError, "H.264/AAC"):
                    ProductionRunner._probe_motion_video(video)

    def test_packaged_commands_use_pinned_node_cli_and_browser_without_npm(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "contains-npm-runtime"
            node = root / "node.exe"
            cli = root / "node_modules" / "hyperframes" / "bin" / "hyperframes.mjs"
            browser = root / "chrome.exe"
            cli.parent.mkdir(parents=True)
            for path in (node, cli, browser):
                path.write_bytes(b"runtime")
            (cli.parent.parent / "package.json").write_text(
                json.dumps({"name": "hyperframes", "version": HYPERFRAMES_VERSION}),
                encoding="utf-8",
            )
            environment = {
                "SHIYI_NODE_EXECUTABLE": str(node),
                "SHIYI_HYPERFRAMES_CLI": str(cli),
                "HYPERFRAMES_BROWSER_PATH": str(browser),
                "SHIYI_PACKAGED_RUNTIME": "1",
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                check, render, runtime = _hyperframes_commands("high")
        self.assertEqual(check, [str(node), str(cli), "check", "--strict"])
        self.assertEqual(render[:3], [str(node), str(cli), "render"])
        self.assertIn("high", render)
        self.assertIn("--no-best-effort", render)
        self.assertIn("--strict", render)
        forbidden_launchers = {"npm", "npm.cmd", "npx", "npx.cmd"}
        for command in (check, render):
            self.assertTrue(
                forbidden_launchers.isdisjoint(
                    Path(str(argument)).name.casefold() for argument in command
                )
            )
        self.assertEqual(runtime["browser"], browser)
        self.assertEqual(runtime["runtime_source"], "packaged")
        self.assertEqual(runtime["version"], HYPERFRAMES_VERSION)
        self.assertEqual(runtime["renderer"], HYPERFRAMES_RENDERER)

    def test_hyperframes_environment_keeps_runtime_but_drops_provider_secrets(self):
        source = {
            "PATH": "fixed-runtime-path",
            "HYPERFRAMES_BROWSER_PATH": "bundled-browser.exe",
            "HYPERFRAMES_NO_AUTO_INSTALL": "1",
            "DEEPSEEK_API_KEY": "provider-secret",
            "DEEPSEEK_BASE_URL": "https://untrusted.invalid",
            "OPENAI_API_KEY": "provider-secret",
            "ANTHROPIC_AUTH_TOKEN": "provider-secret",
            "GEMINI_API_KEY": "provider-secret",
            "OPENROUTER_API_KEY": "provider-secret",
            "AIHUBMIX_API_KEY": "provider-secret",
            "HF_TOKEN": "provider-secret",
            "AUTHORIZATION": "provider-secret",
            "COOKIE": "provider-secret",
            "MODEL_PROVIDER": "provider-secret",
            "PRODUCER_ENDPOINT": "https://untrusted.invalid",
        }
        with mock.patch.dict(os.environ, source, clear=True):
            environment = _hyperframes_subprocess_environment()
        self.assertEqual("fixed-runtime-path", environment["PATH"])
        self.assertEqual("bundled-browser.exe", environment["HYPERFRAMES_BROWSER_PATH"])
        self.assertEqual("1", environment["HYPERFRAMES_NO_AUTO_INSTALL"])
        for name in set(source) - {
            "PATH",
            "HYPERFRAMES_BROWSER_PATH",
            "HYPERFRAMES_NO_AUTO_INSTALL",
        }:
            self.assertNotIn(name, environment)

    def test_explicit_hyperframes_runtime_version_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            node = root / "node.exe"
            cli = root / "node_modules" / "hyperframes" / "bin" / "hyperframes.mjs"
            browser = root / "chrome.exe"
            cli.parent.mkdir(parents=True)
            for path in (node, cli, browser):
                path.write_bytes(b"runtime")
            (cli.parent.parent / "package.json").write_text(
                json.dumps({"name": "hyperframes", "version": "0.7.85"}),
                encoding="utf-8",
            )
            environment = {
                "SHIYI_NODE_EXECUTABLE": str(node),
                "SHIYI_HYPERFRAMES_CLI": str(cli),
                "HYPERFRAMES_BROWSER_PATH": str(browser),
                "SHIYI_PACKAGED_RUNTIME": "1",
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                with self.assertRaisesRegex(ValueError, "运行时版本不匹配"):
                    _hyperframes_commands("standard")

    def test_partial_packaged_runtime_fails_closed(self):
        environment = {
            "SHIYI_NODE_EXECUTABLE": "missing-node.exe",
            "SHIYI_HYPERFRAMES_CLI": "",
            "HYPERFRAMES_BROWSER_PATH": "",
            "SHIYI_PACKAGED_RUNTIME": "1",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            with self.assertRaisesRegex(FileNotFoundError, "运行时不完整"):
                _hyperframes_commands("standard")


class VoiceFallbackContractTests(unittest.TestCase):
    def test_sapi_fallback_uses_systemroot_powershell_absolute_path(self):
        with tempfile.TemporaryDirectory() as folder_name:
            root = Path(folder_name)
            job = root / "job"
            job.mkdir()
            powershell = (
                root / "Windows" / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            )
            powershell.parent.mkdir(parents=True)
            powershell.write_bytes(b"powershell")
            commands: list[list[str]] = []

            def fallback_run(command: list[str], **_kwargs):
                commands.append(command)
                (job / "voice.wav").write_bytes(b"RIFF" + b"\0" * 64)
                return mock.Mock(stdout="", stderr="")

            with (
                mock.patch.dict(os.environ, {"SystemRoot": str(root / "Windows")}, clear=False),
                mock.patch("core.production.VOICE_WORKBENCH", root / "missing-workbench.py"),
                mock.patch("core.production.subprocess.run", side_effect=fallback_run),
            ):
                report = ProductionRunner._synthesize_voice(job, "固定路径语音测试", {})
            self.assertEqual(report["engine"], "windows_sapi")
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0][0], str(powershell.resolve()))
            self.assertTrue(Path(commands[0][0]).is_absolute())
            self.assertNotEqual(commands[0][0].casefold(), "powershell.exe")

    def test_sapi_fallback_fails_closed_when_fixed_powershell_is_missing(self):
        with tempfile.TemporaryDirectory() as folder_name:
            root = Path(folder_name)
            job = root / "job"
            job.mkdir()
            with (
                mock.patch.dict(os.environ, {"SystemRoot": str(root / "Windows")}, clear=False),
                mock.patch("core.production.VOICE_WORKBENCH", root / "missing-workbench.py"),
                mock.patch(
                    "core.production.subprocess.run",
                    side_effect=AssertionError("missing fixed PowerShell must never execute"),
                ),
            ):
                with self.assertRaisesRegex(FileNotFoundError, "固定路径不可用"):
                    ProductionRunner._synthesize_voice(job, "固定路径语音测试", {})


class ProductionEngineSummaryTests(unittest.TestCase):
    def test_legacy_failed_and_successful_jobs_resolve_footage_without_mutation(self):
        legacy_input = {
            "topic": "除甲醛内容生产",
            "render_mode": "animated",
            "require_animation": False,
        }
        failed = {
            "id": "legacy-failed",
            "status": "failed",
            "last_failed_stage": "render",
            "production_input": deepcopy(legacy_input),
            "artifacts": [],
        }
        successful = {
            "id": "legacy-success",
            "status": "complete",
            "production_input": deepcopy(legacy_input),
            "artifacts": ["final.mp4", "engine_report.json"],
            "current_run_id": "mpt-run-1",
        }
        failed_before = deepcopy(failed)
        successful_before = deepcopy(successful)
        with mock.patch.object(
            app,
            "production_engine_binding",
            return_value=(object(), {}, {"name": "MoneyPrinterTurbo", "health": "ready"}),
        ):
            failed_summary = app.job_with_engine_summary(failed)
            successful_summary = app.job_with_engine_summary(successful)
        self.assertEqual(app.production_mode_for_persisted_job(failed), "footage")
        self.assertEqual(app.production_mode_for_persisted_job(successful), "footage")
        self.assertEqual(failed_summary["production_engine"]["selected_mode"], "footage")
        self.assertEqual(successful_summary["production_engine"]["selected_mode"], "footage")
        self.assertEqual(successful_summary["production_engine"]["last_successful_run"], "mpt-run-1")
        self.assertEqual(failed, failed_before)
        self.assertEqual(successful, successful_before)

    def test_app_binds_mpt_only_for_footage_render_stage(self):
        sentinel_adapter = object()
        with mock.patch.object(
            app,
            "production_engine_binding",
            return_value=(sentinel_adapter, {"material_strategy": "local"}, {}),
        ) as binding:
            self.assertEqual(
                app.production_engine_adapter_for_mode("motion", render_stage_requested=True),
                (None, {}),
            )
            self.assertEqual(
                app.production_engine_adapter_for_mode("footage", render_stage_requested=False),
                (None, {}),
            )
            adapter, options = app.production_engine_adapter_for_mode(
                "footage", render_stage_requested=True
            )
        self.assertIs(adapter, sentinel_adapter)
        self.assertEqual(options, {"material_strategy": "local"})
        binding.assert_called_once_with(strict=True)

    def test_job_summary_follows_selected_motion_mode_not_mpt_environment(self):
        with mock.patch.object(
            app,
            "_resolve_hyperframes_runtime",
            return_value={"runtime_source": "packaged"},
        ), mock.patch.object(
            app,
            "production_engine_binding",
            side_effect=AssertionError("MPT summary must not be consulted for motion"),
        ):
            result = app.job_with_engine_summary(
                {"id": "job-motion", "production_input": {"production_mode": "motion"}, "artifacts": []}
            )
        self.assertEqual(result["production_engine"]["selected_mode"], "motion")
        self.assertEqual(result["production_engine"]["name"], "HyperFrames")
        self.assertEqual(result["production_engine"]["version"], HYPERFRAMES_VERSION)
        self.assertEqual(result["production_engine"]["renderer"], HYPERFRAMES_RENDERER)
        self.assertEqual(result["production_engine"]["runtime_source"], "packaged")

    def test_hybrid_summary_is_honestly_unimplemented(self):
        summary = app.production_mode_summary("hybrid")
        self.assertFalse(summary["enabled"])
        self.assertEqual(summary["selected_mode"], "hybrid")
        self.assertEqual(summary["health"], "not_implemented")


if __name__ == "__main__":
    unittest.main()
