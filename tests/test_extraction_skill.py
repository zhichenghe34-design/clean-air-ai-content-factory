from __future__ import annotations

import importlib.util
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "agent-skills" / "extract-web-platform-content" / "scripts" / "extract_url.py"
SPEC = importlib.util.spec_from_file_location("extract_url_skill", SCRIPT)
assert SPEC and SPEC.loader
EXTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXTRACT)


class ExtractionSkillTests(unittest.TestCase):
    def test_parser_config_is_allowlisted_and_secret_free(self) -> None:
        config = EXTRACT.build_isolated_parser_config(
            {
                "enable_asr": True,
                "whisper_model": "tiny",
                "api_key": "should-never-leave-source-config",
                "cookie": "private-cookie",
                "authorization": "Bearer private",
                "unknown_field": "ignored",
            },
            Path("input"),
            Path("output"),
        )
        rendered = str(config).lower()
        self.assertTrue(config["enable_asr"])
        self.assertEqual(config["whisper_model"], "tiny")
        for marker in ("api_key", "cookie", "authorization", "private"):
            self.assertNotIn(marker, rendered)

    def test_platform_routes_upgrade_instead_of_stopping(self) -> None:
        routes = EXTRACT.planned_routes("https://www.douyin.com/video/123")
        self.assertEqual(routes[0], "one_stop_media_parser")
        self.assertIn("playwright", routes)
        self.assertEqual(routes[-1], "manual_auth")

    def test_private_literal_url_is_blocked(self) -> None:
        with self.assertRaises(EXTRACT.ExtractionError):
            EXTRACT.validate_public_url("http://127.0.0.1/private", resolve_dns=False)

    def test_proxy_fake_ip_is_allowed_only_for_domain_with_proxy(self) -> None:
        answer = [(2, 1, 6, "", ("198.18.4.13", 443))]
        with patch.object(EXTRACT.socket, "getaddrinfo", return_value=answer), patch.object(
            EXTRACT.urllib.request, "getproxies", return_value={"https": "http://127.0.0.1:7890"}
        ):
            EXTRACT.validate_public_url("https://example.com/")
        with patch.object(EXTRACT.socket, "getaddrinfo", return_value=answer), patch.object(
            EXTRACT.urllib.request, "getproxies", return_value={}
        ):
            with self.assertRaises(EXTRACT.ExtractionError):
                EXTRACT.validate_public_url("https://example.com/")

    def test_html_extraction_drops_scripts(self) -> None:
        title, text = EXTRACT.extract_html(
            "<html><head><title>可信标题</title><script>ignore me</script></head>"
            "<body><main>第一段证据</main><style>hidden</style><p>第二段证据</p></body></html>"
        )
        self.assertEqual(title, "可信标题")
        self.assertIn("第一段证据", text)
        self.assertNotIn("ignore me", text)

    def test_dry_run_writes_no_network_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = EXTRACT.extract(
                "https://www.bilibili.com/video/BV1example",
                Path(temp),
                dry_run=True,
            )
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["source"]["platform"], "bilibili")
        self.assertTrue(all(item["status"] == "planned" for item in result["attempts"]))

    def test_sparse_http_automatically_upgrades_to_browser(self) -> None:
        direct = {"final_url": "https://example.com", "title": "Direct", "text": "too short", "sha256": "a", "content_type": "text/html"}
        rendered = {"final_url": "https://example.com", "title": "Rendered", "text": "完整正文" * 100, "sha256": "b", "content_type": "text/html"}
        with tempfile.TemporaryDirectory() as temp, patch.object(
            EXTRACT, "validate_public_url", return_value=urllib.parse.urlsplit("https://example.com")
        ), patch.object(EXTRACT, "direct_http", return_value=direct), patch.object(
            EXTRACT, "playwright_extract", return_value=rendered
        ):
            result = EXTRACT.extract("https://example.com", Path(temp))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["source"]["title"], "Rendered")
        self.assertEqual([item["route"] for item in result["attempts"]], ["direct_http", "playwright"])

    def test_missing_optional_browser_is_reported_as_adapter_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.object(
            EXTRACT, "validate_public_url", return_value=urllib.parse.urlsplit("https://example.com")
        ), patch.object(
            EXTRACT, "direct_http", side_effect=EXTRACT.ExtractionError("no readable body")
        ), patch.object(
            EXTRACT, "playwright_extract", side_effect=EXTRACT.ExtractionError("Playwright is not installed")
        ):
            result = EXTRACT.extract("https://example.com", Path(temp))
        self.assertEqual(result["status"], "adapter_missing")
        self.assertIn("install/configure", result["next_action"])


if __name__ == "__main__":
    unittest.main()
