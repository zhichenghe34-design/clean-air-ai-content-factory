import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.orchestrator import (
    CANONICAL_ARTIFACTS,
    ConflictError,
    JobStore,
    UnprocessableError,
    file_sha256,
    local_fallback_plan,
)
from core.provider import BudgetLedger, ProviderError, validate_provider_base_url
from core.production import build_local_variants, estimate_narration_duration, review_script
from core.secrets import protect_secret, unprotect_secret


VALID_TOPIC = "气味小就代表甲醛少吗？"
LONG_SAFE_SCRIPT = (
    "气味小不等于甲醛一定少。鼻子感受到的只是线索，不能替代规范检测。"
    "判断室内空气信息时，要核对对象、使用场景、剂量、空间体积、作用时间、初始浓度、检测方法和报告来源。"
    "实验条件与真实房间不同，结论就不能直接照搬。缺少完整来源和适用边界时，也不能把宣传话术理解成入住保证。"
    "更稳妥的做法是保存完整检测报告，持续通风，并在重要入住决定前结合真实房屋情况请专业人员判断。"
)


class FakeStageRunner:
    def __init__(self, fail_stage=None):
        self.fail_stage = fail_stage
        self.budget = BudgetLedger(limit=7)

    def run_research_stage(self, output, production_input):
        if self.fail_stage == "research":
            raise RuntimeError("research failed")
        finding = {
            "claim": "气味不能单独证明甲醛浓度",
            "source_urls": ["https://example.test/source"],
            "script_eligible": True,
            "review_status": "human_verified",
            "reviewer": "fake-agent",
            "reviewed_at": "2026-01-01T00:00:00+08:00",
            "evidence": [{"quote": "测试证据", "evidence_type": "verbatim"}],
        }
        (output / "research.json").write_text(json.dumps({"findings": [finding], "provenance": {"reviewer": "fake-agent"}, "evidence_review": {"reviewer": "fake-agent", "status": "human_verified"}}, ensure_ascii=False), encoding="utf-8")
        (output / "insight.json").write_text(json.dumps({"topic": production_input["topic"]}, ensure_ascii=False), encoding="utf-8")

    def run_content_stage(self, output, production_input, research_approval):
        if self.fail_stage == "content":
            raise RuntimeError("content failed")
        variants = build_local_variants(production_input["topic"], production_input["audience"])
        selected = dict(variants[0])
        selected["script"] = LONG_SAFE_SCRIPT
        (output / "script_variants.json").write_text(json.dumps({"variants": variants}, ensure_ascii=False), encoding="utf-8")
        (output / "approved_script.json").write_text(json.dumps(selected, ensure_ascii=False), encoding="utf-8")
        (output / "review.json").write_text(json.dumps(review_script(LONG_SAFE_SCRIPT), ensure_ascii=False), encoding="utf-8")

    def run_render_stage(self, output, production_input, approvals):
        if self.fail_stage == "render":
            (output / "failed-only.log").write_text("not public", encoding="utf-8")
            raise RuntimeError("render failed")
        for name in CANONICAL_ARTIFACTS:
            path = output / name
            if path.exists():
                continue
            if path.suffix == ".json":
                path.write_text(json.dumps({"name": name}, ensure_ascii=False), encoding="utf-8")
            else:
                path.write_bytes(("fake:" + name).encode("utf-8"))


class SlowResearchRunner(FakeStageRunner):
    def __init__(self, started, release):
        super().__init__()
        self.started = started
        self.release = release

    def run_research_stage(self, output, production_input):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test release timeout")
        return super().run_research_stage(output, production_input)


def advance_to_content_gate(jobs, job, runner):
    job = jobs.approve(job["id"])
    job = jobs.advance(job["id"], runner, "research-0001")
    research_path = jobs.resolve_review_artifact(job["id"], "research.json")
    research = json.loads(research_path.read_text(encoding="utf-8"))
    finding = research["findings"][0]
    job = jobs.approve_research(job["id"], {
        "decision": "approved",
        "reviewer": "测试审核员",
        "note": "测试审批",
        "artifact_sha256": file_sha256(research_path),
        "findings": [{"finding_id": finding["finding_id"], "decision": "approved", "evidence_type": "paraphrase"}],
    })
    return jobs.advance(job["id"], runner, "content-0001")


def approve_compliance(jobs, job):
    review_path = jobs.resolve_review_artifact(job["id"], "review.json")
    script_path = jobs.resolve_review_artifact(job["id"], "approved_script.json")
    return jobs.approve_compliance(job["id"], {
        "decision": "approved",
        "reviewer": "测试审核员",
        "note": "测试审批",
        "artifact_sha256": file_sha256(review_path),
        "script_sha256": file_sha256(script_path),
    })


class V2WorkflowTests(unittest.TestCase):
    def make_job(self, root):
        store = JobStore(Path(root))
        return store, store.create(local_fallback_plan("生成赛题视频", []), {"topic": VALID_TOPIC, "audience": "新房家庭"})

    def test_full_state_machine_and_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs, job = self.make_job(folder)
            runner = FakeStageRunner()
            job = advance_to_content_gate(jobs, job, runner)
            self.assertEqual(job["status"], "awaiting_compliance_approval")
            research = json.loads(jobs.resolve_review_artifact(job["id"], "research.json").read_text(encoding="utf-8"))
            self.assertNotIn("human_verified", json.dumps(research, ensure_ascii=False))
            self.assertNotIn("fake-agent", json.dumps(research, ensure_ascii=False))
            self.assertEqual(research["findings"][0]["evidence"][0]["evidence_type"], "unclassified")

            job = approve_compliance(jobs, job)
            job = jobs.advance(job["id"], runner, "render-000001")
            self.assertEqual(job["status"], "complete")
            self.assertIsNotNone(job["current_run_id"])
            manifest = json.loads(jobs.resolve_artifact(job["id"], "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["run_id"], job["current_run_id"])
            self.assertTrue(manifest["approval_hashes"]["research"])
            self.assertTrue(manifest["approval_hashes"]["compliance"])
            self.assertEqual(jobs.resolve_artifact(job["id"], "final.mp4").read_bytes(), b"fake:final.mp4")

    def test_approval_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs, job = self.make_job(folder)
            jobs.approve(job["id"])
            job = jobs.advance(job["id"], FakeStageRunner(), "research-0001")
            research = json.loads(jobs.resolve_review_artifact(job["id"], "research.json").read_text(encoding="utf-8"))
            with self.assertRaises(ConflictError):
                jobs.approve_research(job["id"], {
                    "decision": "approved", "reviewer": "测试审核员", "artifact_sha256": "0" * 64,
                    "findings": [{"finding_id": research["findings"][0]["finding_id"], "decision": "approved", "evidence_type": "verbatim"}],
                })

    def test_script_edit_invalidates_only_compliance_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs, job = self.make_job(folder)
            job = advance_to_content_gate(jobs, job, FakeStageRunner())
            research_hash = job["approvals"]["research"]["artifact_sha256"]
            job = approve_compliance(jobs, job)
            estimate = estimate_narration_duration(LONG_SAFE_SCRIPT)
            job = jobs.update_script(job["id"], LONG_SAFE_SCRIPT + "通风之后仍要看检测结果。", review_script(LONG_SAFE_SCRIPT), estimate)
            self.assertEqual(job["approvals"]["research"]["artifact_sha256"], research_hash)
            self.assertEqual(job["approvals"]["compliance"]["status"], "pending")

    def test_failed_render_does_not_replace_previous_success(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs, job = self.make_job(folder)
            job = advance_to_content_gate(jobs, job, FakeStageRunner())
            job = approve_compliance(jobs, job)
            job = jobs.advance(job["id"], FakeStageRunner(), "render-good-01")
            previous = job["current_run_id"]
            estimate = estimate_narration_duration(LONG_SAFE_SCRIPT)
            job = jobs.update_script(job["id"], LONG_SAFE_SCRIPT, review_script(LONG_SAFE_SCRIPT), estimate)
            job = approve_compliance(jobs, job)
            with self.assertRaises(RuntimeError):
                jobs.advance(job["id"], FakeStageRunner("render"), "render-fail-01")
            current = jobs.get(job["id"])
            self.assertEqual(current["current_run_id"], previous)
            self.assertEqual(jobs.resolve_artifact(job["id"], "final.mp4").read_bytes(), b"fake:final.mp4")
            failed_run = current["runs"][-1]
            self.assertEqual(failed_run["status"], "failed")
            with self.assertRaises(FileNotFoundError):
                jobs.resolve_artifact(job["id"], "final.mp4", failed_run["run_id"])

    def test_idempotent_replay_does_not_create_another_run(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs, job = self.make_job(folder)
            jobs.approve(job["id"])
            first = jobs.advance(job["id"], FakeStageRunner(), "same-key-001")
            second = jobs.advance(job["id"], FakeStageRunner(), "same-key-001")
            self.assertEqual(len(first["runs"]), len(second["runs"]))
            self.assertTrue(second["replayed"])

    def test_concurrent_different_key_conflicts_and_same_key_replays(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs, job = self.make_job(folder)
            jobs.approve(job["id"])
            started, release = threading.Event(), threading.Event()
            errors = []

            def worker():
                try:
                    jobs.advance(job["id"], SlowResearchRunner(started, release), "concurrent-key-01")
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(started.wait(timeout=2))
            replay = jobs.advance(job["id"], FakeStageRunner(), "concurrent-key-01")
            self.assertTrue(replay["replayed"])
            with self.assertRaises(ConflictError):
                jobs.advance(job["id"], FakeStageRunner(), "concurrent-key-02")
            release.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_stale_pid_lock_marks_running_attempt_interrupted(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs, job = self.make_job(folder)
            job_folder = Path(folder) / "jobs" / job["id"]
            raw = json.loads((job_folder / "job.json").read_text(encoding="utf-8"))
            raw["status"] = "research_running"
            raw["active_run_id"] = "stale-run"
            raw["runs"].append({"run_id": "stale-run", "stage": "research", "status": "running"})
            (job_folder / "job.json").write_text(json.dumps(raw), encoding="utf-8")
            (job_folder / "run.lock").write_text(json.dumps({"pid": 2147483647}), encoding="utf-8")
            recovered = jobs.get(job["id"])
            self.assertEqual(recovered["status"], "failed")
            self.assertEqual(recovered["runs"][-1]["status"], "interrupted")
            self.assertFalse((job_folder / "run.lock").exists())

    def test_legacy_job_is_read_only_and_not_rewritten(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs = JobStore(Path(folder))
            legacy = Path(folder) / "jobs" / "legacy-001"
            legacy.mkdir(parents=True)
            raw = '{"id":"legacy-001","status":"complete","artifacts":[]}'
            (legacy / "job.json").write_text(raw, encoding="utf-8")
            job = jobs.get("legacy-001")
            self.assertEqual(job["status"], "legacy_read_only")
            self.assertEqual((legacy / "job.json").read_text(encoding="utf-8"), raw)
            with self.assertRaises(ConflictError):
                jobs.approve("legacy-001")

    def test_invalid_topics_are_422_class_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs = JobStore(Path(folder))
            plan = local_fallback_plan("生成赛题视频", [])
            for topic in ("demo", "今天吃什么比较好", "甲醛"):
                with self.assertRaises(UnprocessableError):
                    jobs.create(plan, {"topic": topic, "audience": "家庭"})
            with self.assertRaises(UnprocessableError):
                jobs.create(plan, {"topic": "装修后如何判断甲醛风险", "audience": "家庭", "api_key": "must-not-be-stored"})
            with self.assertRaises(UnprocessableError):
                jobs.create(plan, {"topic": "装修后如何判断甲醛风险", "audience": "家庭", "target_duration_seconds": 90})
            unsafe_plan = dict(plan)
            unsafe_plan["api_key"] = "must-not-be-stored"
            with self.assertRaises(UnprocessableError):
                jobs.create(unsafe_plan, {"topic": VALID_TOPIC, "audience": "家庭"})


class V2SecurityAndBudgetTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "DPAPI is Windows-only")
    def test_dpapi_roundtrip_and_no_plaintext_storage(self):
        value = "dummy-regression-secret-not-real"
        payload = protect_secret(value)
        self.assertEqual(unprotect_secret(payload), value)
        self.assertNotIn(value, json.dumps(payload))

    @unittest.skipUnless(os.name == "nt", "DPAPI is Windows-only")
    def test_legacy_plaintext_secret_is_migrated_without_a_plaintext_backup(self):
        from core.config import ConfigStore
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "secrets.json"
            value = "dummy-legacy-secret-not-real"
            path.write_text(json.dumps({"api_key": value}), encoding="utf-8")
            store = ConfigStore(Path(folder))
            self.assertEqual(store.get_api_key(), value)
            self.assertNotIn(value, path.read_text(encoding="utf-8"))
            self.assertEqual(list(Path(folder).glob("*.bak")), [])

    def test_provider_error_detail_redacts_secret_fields(self):
        from core.provider import OpenAICompatibleProvider
        provider = OpenAICompatibleProvider({"base_url": "https://api.deepseek.com"}, "private-test-key")
        safe = provider._sanitize_error_detail('{"api_key":"private-test-key","token":"another-private-value"}')
        self.assertNotIn("private-test-key", safe)
        self.assertNotIn("another-private-value", safe)

    def test_provider_whitelist(self):
        self.assertEqual(validate_provider_base_url("https://api.deepseek.com/v1/"), "https://api.deepseek.com/v1")
        for value in ("http://api.deepseek.com", "https://evil.test", "https://user:pass@api.deepseek.com", "https://api.deepseek.com?x=1"):
            with self.assertRaises(ValueError):
                validate_provider_base_url(value)

    def test_loopback_provider_needs_explicit_test_switch(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHIYI_ALLOW_TEST_PROVIDER", None)
            with self.assertRaises(ValueError):
                validate_provider_base_url("http://127.0.0.1:9999/v1")
        with patch.dict(os.environ, {"SHIYI_ALLOW_TEST_PROVIDER": "1"}):
            self.assertEqual(validate_provider_base_url("http://127.0.0.1:9999/v1"), "http://127.0.0.1:9999/v1")

    def test_budget_counts_failed_attempt_and_hard_stops(self):
        self.assertEqual(BudgetLedger(limit=99).limit, 7)
        ledger = BudgetLedger(limit=2)
        one = ledger.begin("research")
        ledger.finish(one, ok=False, error_type="timeout")
        two = ledger.begin("script")
        ledger.finish(two, ok=True)
        with self.assertRaises(ProviderError):
            ledger.begin("review")
        self.assertEqual(ledger.snapshot()["attempted"], 2)
        self.assertEqual(ledger.snapshot()["failed"], 1)

    def test_local_variant_uses_actual_topic_and_medical_causality_blocks(self):
        variants = build_local_variants(VALID_TOPIC, "新房家庭")
        self.assertTrue(all(VALID_TOPIC in item["script"] for item in variants))
        review = review_script("甲醛会导致白血病，买这个除醛产品就可以预防疾病。")
        self.assertEqual(review["status"], "blocked")
        self.assertTrue(any(item["type"] == "unsupported_medical_causality" for item in review["warnings"]))

    def test_numeric_claim_requires_an_approved_finding(self):
        script = "某宣传写着百分之九十九，判断时仍要核对剂量、空间、作用时间、初始浓度、检测方法和报告来源。"
        blocked = review_script(script, [])
        self.assertEqual(blocked["status"], "blocked")
        supported = review_script(script, [{"claim": "广告宣称百分之九十九", "evidence": [{"excerpt": "百分之九十九"}]}])
        self.assertNotEqual(supported["status"], "blocked")

    def test_numeric_custom_topic_is_safely_paraphrased_without_evidence(self):
        variants = build_local_variants("99%除醛率为什么必须看检测条件？", "新房家庭")
        self.assertTrue(all("99%" not in item["script"] for item in variants))
        self.assertTrue(all("高比例除醛率" in item["script"] for item in variants))

    def test_duration_estimate_marks_short_copy(self):
        self.assertLess(estimate_narration_duration("记得通风。") ["estimated_seconds"], 35)
        estimate = estimate_narration_duration(LONG_SAFE_SCRIPT)
        self.assertGreaterEqual(estimate["estimated_seconds"], 35)
        self.assertLessEqual(estimate["estimated_seconds"], 75)

    def test_production_runner_accepts_injected_ci_adapters(self):
        from core.production import ProductionRunner
        with tempfile.TemporaryDirectory() as folder, patch.object(ProductionRunner, "_audio_duration", return_value=52.0):
            root = Path(folder)
            for name, value in {
                "research.json": {"findings": []},
                "insight.json": {},
                "script_variants.json": {"variants": [{"script": LONG_SAFE_SCRIPT}], "provider": {}},
                "approved_script.json": {"script": LONG_SAFE_SCRIPT},
                "review.json": review_script(LONG_SAFE_SCRIPT, []),
            }.items():
                (root / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

            def fake_voice(output, script, config):
                (output / "voice.wav").write_bytes(b"RIFF" + b"\0" * 64)
                return {"engine": "fake_ci"}

            def fake_render(output, motion_plan, config):
                (output / "final.mp4").write_bytes(b"fake-ci-video")
                return {"mode": "fake_ci", "duration_seconds": 52.0, "width": 1080, "height": 1920, "video_codec": "h264", "audio_codec": "aac"}

            runner = ProductionRunner(voice_adapter=fake_voice, render_adapter=fake_render)
            report = runner.run_render_stage(root, {"topic": VALID_TOPIC, "audience": "新房家庭"}, {"research": {"status": "approved"}, "compliance": {"status": "approved"}})
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["render"]["mode"], "fake_ci")


if __name__ == "__main__":
    unittest.main()
