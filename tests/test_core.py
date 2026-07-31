import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from core.config import ConfigStore
from core.catalog import CatalogError, PackageCatalog
from core.discovery import ProjectDiscovery
from core.motion_director import MotionPlanError, build_motion_plan, build_motion_project, derive_motion_segments, validate_motion_plan
from core.orchestrator import JobStore, local_fallback_plan
from core.provider import OpenAICompatibleProvider, ProviderError
from core.production import DEFAULT_SCRIPT, ProductionRunner, review_script
from core.web_agent import WebResearchAgent, normalize_research_result
from core.web_tools import TrustedWebToolRegistry


class ConfigTests(unittest.TestCase):
    def test_defaults_and_safe_key_handling(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ConfigStore(Path(folder))
            self.assertEqual(store.load()["provider"]["model"], "deepseek-v4-flash")
            self.assertEqual(store.load()["research"]["max_model_turns"], 4)
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

    def test_tool_call_turn_uses_official_message_shape(self):
        provider = OpenAICompatibleProvider(
            {"base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash"},
            "test-only-key",
        )
        captured = {}

        def fake_send(request):
            captured.update(json.loads(request.data.decode("utf-8")))
            return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": []}}]}

        provider._send = fake_send
        result = provider.chat_with_tools([{"role": "user", "content": "test"}], [])
        self.assertEqual(result["role"], "assistant")
        self.assertEqual(captured["tool_choice"], "auto")
        self.assertNotIn("response_format", captured)


class FakeSearchProvider:
    def search(self, query, max_results):
        return [{"title": "权威资料", "url": "https://example.com/report", "snippet": "检测条件说明"}]


class MockFlashProvider:
    api_key = "mock-only"

    def __init__(self):
        self.calls = 0

    def chat_with_tools(self, messages, tools, tool_choice="auto"):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call-search", "type": "function", "function": {"name": "web_search", "arguments": '{"query":"甲醛 检测条件"}'}}],
            }
        if self.calls == 2:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call-read", "type": "function", "function": {"name": "extract_url", "arguments": '{"url":"https://example.com/report"}'}}],
            }
        return {
            "role": "assistant",
            "content": json.dumps({
                "status": "complete",
                "summary": "检测结果必须结合条件理解",
                "findings": [{
                    "claim": "需看检测条件",
                    "source_urls": ["https://example.com/report"],
                    "evidence": [{
                        "url": "https://example.com/report",
                        "excerpt": "检测结果应结合剂量、空间、时间与方法。",
                        "source_type": "institutional",
                        "retrieved_at": "2026-07-18T16:10:32+08:00",
                    }],
                    "confidence": "high",
                    "limitations": [],
                }],
                "content_patterns": ["主张→条件→建议"],
                "evidence_gaps": [],
                "sources": [{
                    "url": "https://example.com/report",
                    "title": "权威资料",
                    "publisher": "示例机构",
                    "source_type": "institutional",
                    "retrieved_at": "2026-07-18T16:10:32+08:00",
                }],
            }, ensure_ascii=False),
        }

    @staticmethod
    def parse_json_content(content):
        return json.loads(content)


class WebAgentTests(unittest.TestCase):
    @staticmethod
    def fake_extract(url, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        return {
            "status": "complete",
            "source": {"final_url": url, "title": "权威资料"},
            "content": {"text": "检测结果应结合剂量、空间、时间与方法。", "text_chars": 22},
            "attempts": [{"route": "fake", "status": "complete"}],
            "warnings": [],
        }

    def test_flash_plans_but_registered_tools_do_the_work(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = TrustedWebToolRegistry(
                Path(folder),
                search_provider=FakeSearchProvider(),
                extractor=self.fake_extract,
            )
            provider = MockFlashProvider()
            result = WebResearchAgent(provider, registry).run("检测条件", "新房家庭")
            self.assertEqual(result["status"], "complete")
            self.assertEqual(provider.calls, 3)
            self.assertEqual([item["tool"] for item in result["tool_trace"]], ["web_search", "extract_url"])
            self.assertTrue(result["tool_trace"][1]["result"]["ok"])
            self.assertEqual(result["evidence_review"]["script_eligible_count"], 1)

    def test_high_confidence_finding_without_excerpt_is_downgraded(self):
        result = normalize_research_result({
            "status": "complete",
            "findings": [{
                "claim": "没有摘录的判断",
                "source_urls": ["https://example.com/report"],
                "confidence": "high",
                "limitations": "仅有模型判断，未取得页面摘录",
            }],
            "sources": [{"url": "https://example.com/report", "title": "来源"}],
            "evidence_gaps": [],
        })
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["findings"][0]["confidence"], "low")
        self.assertFalse(result["findings"][0]["script_eligible"])
        self.assertEqual(
            result["findings"][0]["limitations"],
            ["仅有模型判断，未取得页面摘录", "缺少可回溯的页面证据摘录"],
        )
        self.assertEqual(result["script_eligible_findings"], [])

    def test_repo_tools_can_run_directly(self):
        repo = Path(__file__).resolve().parents[1]
        for name in ("review_research_artifact.py", "run_real_media_smoke.py"):
            completed = subprocess.run(
                [sys.executable, str(repo / "tools" / name), "--help"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_unknown_evidence_url_cannot_enter_script_fact_layer(self):
        result = normalize_research_result({
            "status": "complete",
            "findings": [{
                "claim": "引用了未登记网址",
                "source_urls": ["https://invented.example/claim"],
                "evidence": [{"url": "https://invented.example/claim", "excerpt": "伪证据"}],
                "confidence": "high",
            }],
            "sources": [{"url": "https://example.com/report", "title": "来源"}],
            "evidence_gaps": [],
        })
        self.assertFalse(result["findings"][0]["script_eligible"])
        self.assertEqual(result["findings"][0]["source_urls"], [])

    def test_model_invented_url_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = TrustedWebToolRegistry(Path(folder), search_provider=FakeSearchProvider(), extractor=self.fake_extract)
            result = registry.execute("extract_url", {"url": "https://invented.example/claim"})
            self.assertFalse(result["ok"])
            self.assertIn("拒绝访问", result["error"])

    def test_extractor_failure_reason_is_preserved_in_trace(self):
        def blocked_extract(url, output_dir):
            return {
                "status": "blocked",
                "error": "private or non-global target is blocked",
                "source": {"final_url": url},
                "content": {"text": ""},
                "attempts": [],
                "warnings": [],
            }

        with tempfile.TemporaryDirectory() as folder:
            registry = TrustedWebToolRegistry(
                Path(folder),
                extractor=blocked_extract,
                seed_urls=["https://example.com/report"],
            )
            result = registry.execute("extract_url", {"url": "https://example.com/report"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("private or non-global", result["error"])

    def test_off_topic_search_is_reanchored_to_the_selected_topic(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = TrustedWebToolRegistry(Path(folder), search_provider=FakeSearchProvider(), extractor=self.fake_extract)
            registry.set_topic("99%除醛率为什么必须看检测条件？")
            result = registry.execute("web_search", {"query": "99%的人不知道 爆款选题"})
            self.assertTrue(result["ok"])
            self.assertTrue(result["query_reanchored"])
            self.assertIn("除醛", result["query"])

    def test_research_is_included_in_insight(self):
        research = {
            "status": "complete",
            "findings": [{"claim": "仅留档"}],
            "script_eligible_findings": [{"claim": "可进入脚本"}],
            "tool_trace": [{"tool": "web_search"}],
        }
        insight = ProductionRunner._build_insight({"topic": "测试", "audience": "家庭", "pattern_card_ids": []}, research)
        self.assertEqual(insight["web_research"]["findings"], [{"claim": "可进入脚本"}])
        self.assertNotIn("tool_trace", insight["web_research"])

    def test_global_research_switch_overrides_job_default(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = ProductionRunner(provider=MockFlashProvider(), research_config={"enabled": False})
            result = runner._run_research(Path(folder), {"topic": "测试", "audience": "家庭", "enable_web_research": True})
            self.assertEqual(result["status"], "disabled")


class OrchestratorTests(unittest.TestCase):
    def test_job_lifecycle_stops_at_adapter_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            plan = local_fallback_plan("做一条视频", [])
            jobs = JobStore(Path(folder))
            job = jobs.create(plan)
            self.assertEqual(job["status"], "planned")
            job = jobs.approve(job["id"])
            self.assertEqual(job["status"], "authorized")
            job = jobs.run_safe(job["id"])
            self.assertEqual(job["status"], "needs_attention")
            event_file = Path(folder) / "jobs" / job["id"] / "events.jsonl"
            self.assertEqual(len(event_file.read_text(encoding="utf-8").splitlines()), 3)

    def test_production_job_can_store_human_script(self):
        with tempfile.TemporaryDirectory() as folder:
            plan = local_fallback_plan("做一条视频", [])
            jobs = JobStore(Path(folder))
            job = jobs.create(plan, production_input={"topic": "气味小就代表甲醛少吗？"})
            jobs.approve(job["id"])
            with self.assertRaises(Exception):
                jobs.update_script(job["id"], DEFAULT_SCRIPT, review_script(DEFAULT_SCRIPT), {"estimated_seconds": 50})


class ProductionTests(unittest.TestCase):
    def test_default_script_requires_human_review_but_is_not_blocked(self):
        result = review_script(DEFAULT_SCRIPT)
        self.assertFalse(result["blocked"])
        self.assertEqual(result["status"], "needs_human")
        self.assertEqual(len(result["conditions_present"]), 6)

    def test_dangerous_claim_is_blocked(self):
        result = review_script(DEFAULT_SCRIPT + "本产品绝对安全并且彻底去除甲醛。")
        self.assertTrue(result["blocked"])
        self.assertTrue(any(item["type"] == "banned_phrase" for item in result["warnings"]))

    def test_unsupported_measurement_is_blocked_even_with_six_conditions(self):
        script = DEFAULT_SCRIPT + "实验舱体积是1.5立方米。"
        result = review_script(script)
        self.assertTrue(result["blocked"])
        self.assertTrue(any(item["type"] == "unsupported_measurement" for item in result["warnings"]))

    def test_unsupported_industry_generalization_is_blocked(self):
        result = review_script(DEFAULT_SCRIPT + "商家常拿极限实验数据宣传。")
        self.assertTrue(result["blocked"])
        self.assertTrue(any(item["type"] == "unsupported_generalization" for item in result["warnings"]))

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
