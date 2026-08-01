import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import app
from core.config import ConfigStore
from core.orchestrator import JobStore, local_fallback_plan


class ApiV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.old_runtime = app.RUNTIME_DIR
        self.old_config = app.config_store
        self.old_jobs = app.job_store
        app.RUNTIME_DIR = root
        app.config_store = ConfigStore(root)
        app.job_store = JobStore(root)
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
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = body if isinstance(body, (bytes, type(None))) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        connection.request(method, path, body=payload, headers=headers or {})
        response = connection.getresponse()
        data = response.read().decode("utf-8")
        result = response.status, {key.lower(): value for key, value in response.getheaders()}, data
        connection.close()
        return result

    def secure_headers(self, **extra):
        headers = {
            "Content-Type": "application/json",
            "Cookie": self.cookie,
            "X-Shiyi-CSRF": self.csrf,
            "Origin": f"http://127.0.0.1:{self.port}",
        }
        headers.update(extra)
        return headers

    def test_csrf_origin_and_content_type_are_enforced(self):
        status, _, body = self.request("POST", "/api/demo-job", {}, {"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_session")

        headers = self.secure_headers(Origin="http://evil.test")
        status, _, body = self.request("POST", "/api/demo-job", {}, headers)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "origin_rejected")

        headers = self.secure_headers(**{"Content-Type": "text/plain"})
        status, _, body = self.request("POST", "/api/demo-job", b"{}", headers)
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(body)["error"]["code"], "unprocessable")

    def test_invalid_input_returns_json_without_disconnect(self):
        plan = local_fallback_plan("生成测试视频", [])
        status, headers, body = self.request("POST", "/api/jobs", {"plan": plan, "production_input": {"topic": "今天吃什么", "audience": "家庭"}}, self.secure_headers())
        self.assertEqual(status, 422)
        self.assertTrue(headers["content-type"].startswith("application/json"))
        self.assertIn("error", json.loads(body))

        status, headers, body = self.request("GET", "/api/jobs/../../secret")
        self.assertIn(status, {400, 404})
        self.assertTrue(headers["content-type"].startswith("application/json"))
        self.assertIn("error", json.loads(body))

    def test_valid_create_and_execution_authorization(self):
        plan = local_fallback_plan("生成测试视频", [])
        status, _, body = self.request("POST", "/api/jobs", {"plan": plan, "production_input": {"topic": "气味小就代表甲醛少吗？", "audience": "家庭"}}, self.secure_headers())
        self.assertEqual(status, 201)
        job = json.loads(body)
        status, _, body = self.request("POST", f"/api/jobs/{job['id']}/approve", {}, self.secure_headers())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "authorized")


if __name__ == "__main__":
    unittest.main()
