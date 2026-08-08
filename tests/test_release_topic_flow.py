import http.client
import json
import os
import tempfile
import threading
import unittest
from collections import OrderedDict
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import app
from core.config import ConfigStore
from core.learning import LearningStore
from core.orchestrator import JobStore


class ReleaseTopicFlowTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "",
                "SHIYI_EXPERIMENTAL_DYNAMIC_TOPICS": "",
            },
        )
        self.environment.start()
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.old_runtime = app.RUNTIME_DIR
        self.old_config = app.config_store
        self.old_jobs = app.job_store
        self.old_learning = app.learning_store
        self.old_registry = app.capability_registry
        self.old_budgets = app.pretask_provider_budgets
        self.old_bundles = app.topic_selection_bundles
        app.RUNTIME_DIR = root
        app.config_store = ConfigStore(root)
        app.job_store = JobStore(root)
        app.learning_store = LearningStore(root)
        app.capability_registry = app.CapabilityPackRegistry(root)
        app.pretask_provider_budgets = OrderedDict()
        app.topic_selection_bundles = OrderedDict()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.AppHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, headers, body = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        self.csrf = json.loads(body)["csrf_token"]
        self.cookie = headers["set-cookie"].split(";", 1)[0]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        app.RUNTIME_DIR = self.old_runtime
        app.config_store = self.old_config
        app.job_store = self.old_jobs
        app.learning_store = self.old_learning
        app.capability_registry = self.old_registry
        app.pretask_provider_budgets = self.old_budgets
        app.topic_selection_bundles = self.old_bundles
        self.temp.cleanup()
        self.environment.stop()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = body if isinstance(body, (bytes, type(None))) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        connection.request(method, path, body=payload, headers=headers or {})
        response = connection.getresponse()
        data = response.read().decode("utf-8")
        result = response.status, {key.lower(): value for key, value in response.getheaders()}, data
        connection.close()
        return result

    def secure_headers(self):
        return {
            "Content-Type": "application/json",
            "Cookie": self.cookie,
            "X-Shiyi-CSRF": self.csrf,
            "Origin": f"http://127.0.0.1:{self.port}",
        }

    def post_topics(self, goal, excluded=None, **extra):
        payload = {"goal": goal, "excluded_topics": excluded or []}
        payload.update(extra)
        status, _, body = self.request("POST", "/api/agent/topics", payload, self.secure_headers())
        return status, json.loads(body)

    def test_release_path_skips_dynamic_bootstrap_and_calls_suggest_once(self):
        goal = "为社区咖啡店制作一条新品介绍短视频"
        calls = {"suggest": 0, "bootstrap": 0, "review": 0}
        captured = {}

        class ReleaseProvider:
            def __init__(self, budget):
                self.budget = budget

            def bootstrap_project(self, *_args):
                calls["bootstrap"] += 1
                raise AssertionError("release path must not bootstrap")

            def adversarial_review_capability_pack(self, *_args):
                calls["review"] += 1
                raise AssertionError("release path must not run capability review")

            def suggest_topics(self, supplied_goal, excluded, pack, memory_rules):
                calls["suggest"] += 1
                captured.update(goal=supplied_goal, excluded=excluded, pack=pack, memory_rules=memory_rules)
                token = self.budget.begin("topic_suggestion")
                result = app.AppHandler._safe_topic_candidates(excluded, supplied_goal, pack)
                self.budget.finish(token, ok=True)
                return result

        with (
            mock.patch.object(app.config_store, "get_api_key", return_value="test-key"),
            mock.patch.object(
                app.AppHandler,
                "_provider",
                new=lambda _handler, budget=None: ReleaseProvider(budget),
            ),
        ):
            status, result = self.post_topics(goal)

        self.assertEqual(status, 200)
        self.assertEqual(calls, {"suggest": 1, "bootstrap": 0, "review": 0})
        self.assertEqual(captured["goal"], goal)
        self.assertEqual(captured["pack"]["sha256"], result["capability_pack"]["sha256"])
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(result["source"], "deepseek")
        self.assertIsNone(result["capability_review"])
        self.assertEqual(result["capability_review_failure_kind"], "not_run")
        self.assertEqual(result["bootstrap_failure_kind"], "not_run")
        self.assertIsNone(result["bootstrap_schema_diagnostic"])
        self.assertIn("确定性安全能力包", result["notice"])
        self.assertIn("仍须在后续研究阶段逐条核验", result["screening"])
        self.assertEqual(result["pretask_provider_budget"]["attempted"], 1)
        self.assertEqual(result["pretask_provider_budget"]["succeeded"], 1)
        self.assertEqual(app.job_store.list(), [])

    def test_provider_failure_falls_back_without_dynamic_calls(self):
        goal = "为本地面包店制作一条新品介绍短视频"
        calls = {"suggest": 0}

        class FailingProvider:
            def __init__(self, budget):
                self.budget = budget

            def bootstrap_project(self, *_args):
                raise AssertionError("release path must not bootstrap")

            def adversarial_review_capability_pack(self, *_args):
                raise AssertionError("release path must not run capability review")

            def suggest_topics(self, *_args):
                calls["suggest"] += 1
                token = self.budget.begin("topic_suggestion")
                self.budget.finish(token, ok=False, error_type="ProviderError")
                raise app.ProviderError("opaque provider failure")

        with (
            mock.patch.object(app.config_store, "get_api_key", return_value="test-key"),
            mock.patch.object(
                app.AppHandler,
                "_provider",
                new=lambda _handler, budget=None: FailingProvider(budget),
            ),
        ):
            status, result = self.post_topics(goal)

        self.assertEqual(status, 200)
        self.assertEqual(calls["suggest"], 1)
        self.assertEqual(result["source"], "local_safe_agent")
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(result["pretask_provider_budget"]["attempted"], 1)
        self.assertEqual(result["pretask_provider_budget"]["failed"], 1)
        self.assertNotIn("opaque provider failure", json.dumps(result, ensure_ascii=False))
        self.assertEqual(app.job_store.list(), [])

    def test_no_key_uses_zero_provider_calls_and_keeps_validation(self):
        goal = "帮我制作室内空气检测科普短视频"
        provider_factory = mock.Mock(side_effect=AssertionError("provider must not be constructed"))
        with (
            mock.patch.object(app.config_store, "get_api_key", return_value=""),
            mock.patch.object(app.AppHandler, "_provider", new=provider_factory),
        ):
            status, first = self.post_topics(goal)
            self.assertEqual(status, 200)
            excluded = [item["title"] for item in first["candidates"]]
            status, refreshed = self.post_topics(
                goal,
                excluded,
                capability_pack=first["capability_pack"],
            )

        provider_factory.assert_not_called()
        self.assertEqual(first["pretask_provider_budget"]["attempted"], 0)
        self.assertEqual(refreshed["pretask_provider_budget"]["attempted"], 0)
        self.assertEqual(len(refreshed["candidates"]), 3)
        self.assertTrue({item["title"] for item in refreshed["candidates"]}.isdisjoint(excluded))
        self.assertIn("未配置 DeepSeek Key", first["notice"])

        invalid_payloads = [
            {"goal": "甲醛知", "excluded_topics": []},
            {"goal": goal, "excluded_topics": "不是数组"},
            {"goal": goal, "excluded_topics": ["选题"] * 25},
            {"goal": goal, "excluded_topics": [123]},
            {"goal": goal, "excluded_topics": ["甲" * 81]},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                status, _, body = self.request(
                    "POST", "/api/agent/topics", payload, self.secure_headers(),
                )
                self.assertEqual(status, 422)
                self.assertEqual(json.loads(body)["error"]["code"], "unprocessable")
        self.assertEqual(app.job_store.list(), [])


if __name__ == "__main__":
    unittest.main()
