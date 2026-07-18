import json
import os
import tempfile
import unittest
from pathlib import Path

from core.config import ConfigStore
from core.catalog import CatalogError, PackageCatalog
from core.discovery import ProjectDiscovery
from core.motion_director import MotionPlanError, build_motion_plan, build_motion_project, derive_motion_segments, validate_motion_plan
from core.orchestrator import JobStore, local_fallback_plan
from core.provider import OpenAICompatibleProvider, ProviderError
from core.production import DEFAULT_SCRIPT, review_script


class ConfigTests(unittest.TestCase):
    def test_defaults_and_safe_key_handling(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ConfigStore(Path(folder))
            self.assertEqual(store.load()["provider"]["model"], "deepseek-v4-flash")
            public = store.save({"provider": {"model": "custom-model", "api_key": "secret", "persist_api_key": False}})
            self.assertEqual(public["provider"]["model"], "custom-model")
            self.assertTrue(public["provider"]["has_api_key"])
            self.assertFalse((Path(folder) / "secrets.json").exists())
            self.assertNotIn("secret", (Path(folder) / "config.json").read_text(encoding="utf-8"))

    def test_storage_layout_is_created_and_configurable(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder) / "runtime"
            storage = Path(folder) / "managed-storage"
            store = ConfigStore(runtime)
            public = store.save({"storage": {"root": str(storage)}})
            self.assertEqual(Path(public["storage"]["root"]), storage.resolve())
            for name in ("tools", "models", "downloads", "cache", "temp", "logs", "projects"):
                self.assertTrue((storage / name).is_dir())

    def test_storage_rejects_drive_root(self):
        if os.name != "nt":
            self.skipTest("Windows drive-root rule")
        with tempfile.TemporaryDirectory() as folder:
            store = ConfigStore(Path(folder) / "runtime")
            with self.assertRaises(ValueError):
                store.save({"storage": {"root": Path(folder).anchor}})


class CatalogTests(unittest.TestCase):
    def test_catalog_is_safe_and_profiles_cover_boundaries(self):
        catalog_path = Path(__file__).resolve().parents[1] / "catalog" / "package-catalog.json"
        catalog = PackageCatalog(catalog_path).load()
        self.assertFalse(catalog["policy"]["auto_install_enabled"])
        self.assertEqual(PackageCatalog.select_profile(catalog, 0)["id"], "cpu_or_gpu_below_6gb")
        self.assertEqual(PackageCatalog.select_profile(catalog, 12)["id"], "gpu_10_to_16gb")
        self.assertEqual(PackageCatalog.select_profile(catalog, 23.9)["id"], "gpu_17_to_23gb")
        self.assertEqual(PackageCatalog.select_profile(catalog, 24)["id"], "gpu_24_to_79gb")
        self.assertEqual(PackageCatalog.select_profile(catalog, 80)["id"], "gpu_80gb_plus")

    def test_catalog_rejects_agent_urls(self):
        with self.assertRaises(CatalogError):
            PackageCatalog.validate({
                "policy": {
                    "agent_may_browse_for_install_sources": False,
                    "agent_may_submit_urls": True,
                    "auto_install_enabled": False,
                },
                "packages": [{"id": "unsafe", "sources": []}],
            })


class DiscoveryTests(unittest.TestCase):
    def test_discovers_video_and_voice_projects(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "video-autopilot"
            video.mkdir()
            (video / "pyproject.toml").write_text("[project]\nname='demo'", encoding="utf-8")
            (video / "main.py").write_text("", encoding="utf-8")
            voice = root / "Local-Voice-TTS"
            voice.mkdir()
            (voice / "requirements.txt").write_text("", encoding="utf-8")
            result = ProjectDiscovery(max_depth=2).scan([str(root)])
            caps = {cap for tool in result["tools"] for cap in tool["capabilities"]}
            self.assertIn("video_editing", caps)
            self.assertIn("voice_generation", caps)


class ProviderTests(unittest.TestCase):
    def test_parse_fenced_json(self):
        value = OpenAICompatibleProvider.parse_json_content('```json\n{"goal":"x","steps":[]}\n```')
        self.assertEqual(value["goal"], "x")

    def test_invalid_json_raises(self):
        with self.assertRaises(ProviderError):
            OpenAICompatibleProvider.parse_json_content("not json")


class OrchestratorTests(unittest.TestCase):
    def test_job_lifecycle_stops_at_adapter_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            plan = local_fallback_plan("做一条视频", [])
            jobs = JobStore(Path(folder))
            job = jobs.create(plan)
            self.assertEqual(job["status"], "planned")
            job = jobs.approve(job["id"])
            self.assertEqual(job["status"], "approved")
            job = jobs.run_safe(job["id"])
            self.assertEqual(job["status"], "needs_attention")
            event_file = Path(folder) / "jobs" / job["id"] / "events.jsonl"
            self.assertEqual(len(event_file.read_text(encoding="utf-8").splitlines()), 3)

    def test_production_job_can_store_human_script(self):
        with tempfile.TemporaryDirectory() as folder:
            plan = local_fallback_plan("做一条视频", [])
            jobs = JobStore(Path(folder))
            job = jobs.create(plan, production_input={"topic": "demo"})
            jobs.approve(job["id"])
            updated = jobs.update_script(job["id"], DEFAULT_SCRIPT)
            self.assertEqual(updated["status"], "approved")
            script_path = Path(folder) / "jobs" / job["id"] / "approved_script.json"
            self.assertEqual(json.loads(script_path.read_text(encoding="utf-8"))["script"], DEFAULT_SCRIPT)


class ProductionTests(unittest.TestCase):
    def test_default_script_requires_human_review_but_is_not_blocked(self):
        result = review_script(DEFAULT_SCRIPT)
        self.assertFalse(result["blocked"])
        self.assertEqual(result["status"], "human_review_required")
        self.assertEqual(len(result["conditions_present"]), 6)

    def test_dangerous_claim_is_blocked(self):
        result = review_script(DEFAULT_SCRIPT + "本产品绝对安全并且彻底去除甲醛。")
        self.assertTrue(result["blocked"])
        self.assertTrue(any(item["type"] == "banned_phrase" for item in result["warnings"]))

    def test_motion_director_builds_non_static_plan_and_project(self):
        segments = [
            {"kicker": f"要点{i}", "title": f"动态标题{i}", "caption": f"这是第{i}个可视化解释。"}
            for i in range(1, 7)
        ]
        plan = build_motion_plan("怎样判断一份检测报告？", "新房家庭", segments, 48.0)
        report = validate_motion_plan(plan)
        self.assertTrue(report["no_static_only_scenes"])
        self.assertGreaterEqual(report["visual_family_count"], 3)
        self.assertEqual(plan["scenes"][-1]["visual_type"], "orbit-summary")
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "animation"
            built = build_motion_project(output, plan)
            self.assertFalse(built["has_audio"])
            html = (output / "index.html").read_text(encoding="utf-8")
            self.assertIn("怎样判断一份检测报告？", html)
            self.assertNotIn("__MOTION_PLAN_JSON__", html)

    def test_motion_director_rejects_static_or_unknown_visuals(self):
        segments = [
            {"kicker": f"要点{i}", "title": f"标题{i}", "caption": "简短字幕"}
            for i in range(1, 5)
        ]
        plan = build_motion_plan("测试", "家庭", segments, 45.0)
        plan["scenes"][1]["visual_type"] = "static-card"
        with self.assertRaises(MotionPlanError):
            validate_motion_plan(plan)

    def test_motion_segments_follow_new_script_instead_of_demo_copy(self):
        script = (
            "新家具没有明显气味，不等于可以只靠鼻子判断。"
            "气味可能来自多种挥发物，嗅觉只能提供线索。"
            "温度、湿度和通风状态会影响体感。"
            "短暂通风后的变化不能代表长期水平。"
            "单件家具和整屋环境不能直接等同。"
            "判断时要看检测方法和报告来源。"
            "涉及入住决策时，应结合真实房屋情况。"
        )
        segments = derive_motion_segments("气味小就代表甲醛少吗？", script)
        self.assertGreaterEqual(len(segments), 4)
        joined = "".join(item["caption"] for item in segments)
        self.assertIn("新家具", joined)
        self.assertNotIn("除醛率 99", joined)
        self.assertTrue(all(len(item["caption"]) <= 62 for item in segments))


if __name__ == "__main__":
    unittest.main()
