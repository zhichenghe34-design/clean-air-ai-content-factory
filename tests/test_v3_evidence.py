from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.capability_pack import normalize_capability_pack


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import build_v3_evidence
import capture_v3_bootstrap
import run_isolated_instance
import verify_v3_evidence


class IsolatedInstanceTests(unittest.TestCase):
    def test_bootstrap_capture_only_accepts_loopback_origin(self):
        for unsafe in ("https://127.0.0.1:8785", "http://localhost:8785", "http://user:pass@127.0.0.1:8785", "http://127.0.0.1:8785/api"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(capture_v3_bootstrap.BootstrapError):
                    capture_v3_bootstrap.LocalApi(unsafe)

    def test_bootstrap_gate_failure_persists_sanitized_diagnostic_without_retry(self):
        with tempfile.TemporaryDirectory(prefix="shiyi-bootstrap-gate-") as temporary:
            root = Path(temporary)
            formal, isolated = root / "formal.json", root / "isolated.json"
            formal.write_text('{"protected":"ciphertext"}\n', encoding="utf-8")
            isolated.write_bytes(formal.read_bytes())
            fake_api = mock.Mock()
            fake_api.request.side_effect = [
                {"ok": True, "connection_verified": True, "model": "test-model"},
                {
                    "source": "local_safe_agent",
                    "notice": "动态能力包未通过严格反证审核，已本地降级",
                    "candidates": [{}, {}, {}],
                    "capability_pack": {"audit": {"status": "local_safe_fallback"}},
                    "capability_review": None,
                    "pretask_provider_budget": {"attempted": 2, "limit": 3},
                },
            ]
            with mock.patch.object(capture_v3_bootstrap, "LocalApi", return_value=fake_api):
                with self.assertRaises(capture_v3_bootstrap.BootstrapError):
                    capture_v3_bootstrap.capture("http://127.0.0.1:8785", root / "output", "咖啡店内容验证", formal, isolated)
            diagnostic = json.loads((root / "output" / "bootstrap-diagnostic.json").read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["source"], "local_safe_agent")
            self.assertFalse(diagnostic["automatic_paid_retry_started"])
            self.assertEqual(diagnostic["tasks_created"], 0)

    def test_rejects_formal_runtime_repo_and_disk_root(self):
        safe = Path(tempfile.gettempdir()) / "shiyi-v03-safe-storage-test"
        for unsafe in (
            run_isolated_instance.FORMAL_RUNTIME,
            run_isolated_instance.REPO_ROOT,
            run_isolated_instance.REPO_ROOT.parent,
            Path(run_isolated_instance.REPO_ROOT.anchor),
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(run_isolated_instance.IsolationError):
                    run_isolated_instance.validate_isolated_paths(unsafe, safe)

    def test_prepares_only_isolated_config_and_clears_key_environment(self):
        with tempfile.TemporaryDirectory(prefix="shiyi-v03-isolation-") as temporary:
            root = Path(temporary)
            runtime, storage = run_isolated_instance.validate_isolated_paths(root / "runtime", root / "storage")
            formal_before = (
                run_isolated_instance.FORMAL_RUNTIME.stat().st_mtime_ns
                if run_isolated_instance.FORMAL_RUNTIME.exists()
                else None
            )
            config_path = run_isolated_instance.prepare_isolated_config(runtime, storage, "deepseek-v4-flash")
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["storage"]["root"], str(storage))
            self.assertEqual(config["research"]["max_provider_calls_per_job"], 7)
            with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "must-not-leak"}):
                child = run_isolated_instance.build_child_environment(runtime)
            self.assertNotIn("DEEPSEEK_API_KEY", child)
            self.assertEqual(child["SHIYI_RUNTIME_DIR"], str(runtime))
            formal_after = (
                run_isolated_instance.FORMAL_RUNTIME.stat().st_mtime_ns
                if run_isolated_instance.FORMAL_RUNTIME.exists()
                else None
            )
            self.assertEqual(formal_before, formal_after)


class V3EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v3-evidence-test-")
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.rule_id = "rule-0123456789abcdefabcd"
        self.correction_id = "correction-0123456789abcdef0123456789abcdef"
        self.jobs = ["job-cafe-real-001", "job-cafe-local-002", "job-cafe-local-003"]
        self.pack = normalize_capability_pack(
            {
                "industry": "本地咖啡门店",
                "audience": "附近上班族",
                "platforms": ["抖音"],
                "content_purpose": "新品认知与到店决策",
                "risk_level": "medium",
            },
            "为本地咖啡店策划三支竖屏短视频",
            "deepseek",
            audit={"status": "passed", "generated_by": "adversarial_agent", "reviewer": "strict_counterevidence_review"},
        )
        self._write_fixture()
        self.media_patch = mock.patch.object(
            verify_v3_evidence,
            "probe_video",
            return_value={"duration_seconds": 50.0, "width": 1080, "height": 1920, "video_codec": "h264", "audio_codec": "aac"},
        )
        self.media_patch.start()

    def tearDown(self):
        self.media_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8", newline="\n")

    def _write_fixture(self) -> None:
        validation = (
            "# v0.3 通用餐饮证据\n\n"
            "咖啡店是受控演示场景。只有第一个任务使用真实 DeepSeek；"
            "另外两个只证明本地降级与 Skill 晋升机制。没有真实企业采用或业务效果数据。v0.3 尚未发布。\n"
        )
        self._write_text(self.source / "VALIDATION.md", validation)
        self._write_text(self.source / "PILOT.md", "# 试点说明\n\n未命名本地咖啡店；人工审批人：何sir。\n")
        self._write_json(self.source / "capability-pack.json", self.pack)
        self._write_json(self.source / "capability-review.json", {"status": "passed", "issues": [], "safe_scope": []})
        self._write_json(self.source / "topics-response.json", {
            "source": "deepseek_bootstrap",
            "candidates": [{"id": f"topic-{index}"} for index in range(1, 4)],
            "capability_pack": self.pack,
            "pretask_provider_budget": {"attempted": 2, "limit": 3},
        })
        self._write_json(self.source / "provider-validation.json", {
            "connection_succeeded": True,
            "source": "deepseek_bootstrap",
            "formal_secret_sha256_before": "a" * 64,
            "formal_secret_sha256_after": "a" * 64,
            "isolated_secret_removed": True,
            "restart_provider_state": "unconfigured",
        })
        self._write_json(self.source / "correction-event.json", {
            "correction_kind": "style", "effective_scope": "project", "interrupt_supported": False,
            "correction": {"id": self.correction_id, "rule_id": self.rule_id, "kind": "style", "scope": "project"},
        })
        self._write_json(self.source / "learning-snapshots.json", {
            "snapshots": [
                {
                    "phase": phase,
                    "success_count": count,
                    "rule_id": self.rule_id,
                    "job_ids": self.jobs[:count],
                    "skill_ids": ["learned-0123456789abcdefabcd"] if count == 3 else [],
                }
                for count, phase in enumerate(("after_correction", "after_real", "after_local_1", "after_local_2"))
            ]
        })
        self._write_text(self.source / "skill" / "SKILL.md", "# 已验证纠错规则\n\n禁止强促销措辞。\n")
        self._write_json(self.source / "skill" / "skill.json", {
            "instruction_only": True,
            "source_rule_ids": [self.rule_id],
            "source_correction_ids": [self.correction_id],
            "success_job_ids": self.jobs,
        })
        self._write_json(self.source / "skill" / "examples.json", {"examples": []})
        self._write_json(self.source / "skill" / "tests.json", {"checks": []})
        for index, directory in enumerate(verify_v3_evidence.TASK_DIRS, start=1):
            task = self.source / directory
            research = {"status": "complete" if index == 1 else "offline"}
            review = {"blocked": False, "learning_rule_ids": [self.rule_id]}
            approved = {"script": "说明适用条件，不使用强促销措辞。"}
            variants = {"provider": {"source": "DeepSeek" if index == 1 else "local_deterministic"}, "variants": []}
            report = {
                "learning_rule_ids": [self.rule_id],
                "provider": {"source": "DeepSeek" if index == 1 else "local_deterministic"},
            }
            for name, value in {
                "research.json": research,
                "insight.json": {"learning_rules": [{"rule_id": self.rule_id}]},
                "script_variants.json": variants,
                "approved_script.json": approved,
                "review.json": review,
                "motion_plan.json": {"scenes": []},
                "run_report.json": report,
            }.items():
                self._write_json(task / name, value)
            self._write_text(task / "captions.srt", "1\n00:00:00,000 --> 00:00:50,000\n测试字幕\n")
            (task / "voice.wav").write_bytes(b"RIFF-test")
            (task / "final.mp4").write_bytes(b"mp4-test")
            approvals = {
                "research": {"status": "approved", "reviewer": "何sir", "reviewed_at": "2026-08-08T12:00:00+08:00", "artifact_sha256": verify_v3_evidence.sha256(task / "research.json")},
                "compliance": {"status": "approved", "reviewer": "何sir", "reviewed_at": "2026-08-08T12:10:00+08:00", "artifact_sha256": verify_v3_evidence.sha256(task / "review.json"), "script_sha256": verify_v3_evidence.sha256(task / "approved_script.json")},
            }
            self._write_json(task / "approvals.json", approvals)
            self._write_json(task / "manifest.json", {
                "schema_version": 2, "status": "complete", "stage": "render",
                "job_id": self.jobs[index - 1], "run_id": f"run-{index}",
                "capability_pack": {"id": self.pack["id"], "sha256": self.pack["sha256"]},
                "learning_rule_ids": [self.rule_id],
                "budget": {"limit": 7, "attempted": 2 if index == 1 else 0, "succeeded": 1 if index == 1 else 0},
            })

    def _build(self, suffix: str = "one") -> Path:
        output = self.root / f"evidence-{suffix}"
        build_v3_evidence.build(self.source, output, self.root / f"evidence-{suffix}.zip")
        return output

    def test_builds_exact_50_file_deterministic_package(self):
        first = self._build("first")
        second = self._build("second")
        self.assertEqual(len([path for path in first.rglob("*") if path.is_file()]), 50)
        self.assertEqual(
            verify_v3_evidence.sha256(self.root / "evidence-first.zip"),
            verify_v3_evidence.sha256(self.root / "evidence-second.zip"),
        )
        self.assertEqual(verify_v3_evidence.verify(first)[0], [])

    def test_detects_payload_tampering(self):
        output = self._build("tamper")
        (output / "task-2-local" / "research.json").write_text("{}\n", encoding="utf-8")
        errors, _ = verify_v3_evidence.verify(output)
        self.assertTrue(any("SHA-256" in error or "SHA256SUMS" in error for error in errors))

    def test_rejects_duplicate_jobs_and_different_capability_pack(self):
        output = self._build("identity")
        manifest = json.loads((output / "task-3-local" / "manifest.json").read_text(encoding="utf-8"))
        manifest["job_id"] = self.jobs[1]
        manifest["capability_pack"]["sha256"] = "f" * 64
        self._write_json(output / "task-3-local" / "manifest.json", manifest)
        errors, _ = verify_v3_evidence.verify(output)
        self.assertTrue(any("Job ID" in error for error in errors))
        self.assertTrue(any("能力包哈希" in error for error in errors))

    def test_rejects_rule_not_adopted_and_fake_human_approval(self):
        output = self._build("adoption")
        report = json.loads((output / "task-2-local" / "run_report.json").read_text(encoding="utf-8"))
        report["learning_rule_ids"] = []
        self._write_json(output / "task-2-local" / "run_report.json", report)
        approvals = json.loads((output / "task-2-local" / "approvals.json").read_text(encoding="utf-8"))
        approvals["research"]["reviewer"] = "agent"
        self._write_json(output / "task-2-local" / "approvals.json", approvals)
        errors, _ = verify_v3_evidence.verify(output)
        self.assertTrue(any("没有实际采用" in error for error in errors))
        self.assertTrue(any("不是有效人工审批" in error for error in errors))

    def test_rejects_provider_mode_impersonation(self):
        output = self._build("provider")
        provider = json.loads((output / "provider-validation.json").read_text(encoding="utf-8"))
        provider["connection_succeeded"] = False
        provider["source"] = "local_safe_agent"
        self._write_json(output / "provider-validation.json", provider)
        errors, _ = verify_v3_evidence.verify(output)
        self.assertTrue(any("真实DeepSeek" in error for error in errors))

    def test_rejects_secret_and_local_machine_path(self):
        output = self._build("scan")
        self._write_text(output / "PILOT.md", "api_key=ds-abcdefghijklmnop C:\\Users\\Example\\secret.json\n")
        errors, _ = verify_v3_evidence.verify(output)
        self.assertTrue(any("API key" in error for error in errors))
        self.assertTrue(any("user home path" in error or "Windows absolute path" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
