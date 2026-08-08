from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from core.production_engine import (
    ENGINE_COMMIT,
    ENGINE_NAME,
    ENGINE_VERSION,
    DownloadTransportResponse,
    JsonTransportResponse,
    ProductionEngineAdapter,
    ProductionEngineError,
    validate_engine_base_url,
)


TASK_ID = "11111111-1111-4111-8111-111111111111"
SCRIPT = "第一行必须原样保留。\n第二行也不能由引擎改写。"
KEYWORDS = ["室内空气检测", "通风与检测报告"]


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeTransport:
    def __init__(self, statuses=None, *, post_final_url: str | None = None) -> None:
        self.statuses = list(statuses or [self.complete_status()])
        self.post_final_url = post_final_url
        self.requests: list[dict] = []
        self.downloads: list[dict] = []

    @staticmethod
    def complete_status(**updates):
        data = {
            "task_id": TASK_ID,
            "state": 1,
            "progress": 100,
            "videos": [f"/tasks/{TASK_ID}/final-1.mp4"],
            "audio_file": f"C:/mpt/storage/tasks/{TASK_ID}/audio.mp3",
            "audio_duration": 52,
            "subtitle_path": f"C:/mpt/storage/tasks/{TASK_ID}/subtitle.srt",
            "materials": ["C:/private/path/that-must-not-be-reported.mp4"],
            "api_key": "must-not-leak",
        }
        data.update(updates)
        return {"status": 200, "message": "success", "data": data}

    def request_json(self, method, url, *, payload, timeout_seconds):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        if method == "POST":
            return JsonTransportResponse(
                {"status": 200, "message": "success", "data": {"task_id": TASK_ID}},
                self.post_final_url or url,
            )
        if method == "DELETE":
            return JsonTransportResponse(
                {"status": 409, "message": "busy", "data": {}}, url
            )
        response = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return JsonTransportResponse(response, url)

    def download(self, url, destination, *, timeout_seconds, max_bytes):
        if url.endswith("/final-1.mp4"):
            payload = b"fake-mp4"
        elif url.endswith("/audio.mp3"):
            payload = b"fake-mp3"
        elif url.endswith("/subtitle.srt"):
            payload = b"1\n00:00:00,000 --> 00:00:02,000\ncaption\n"
        elif url.endswith("/script.json"):
            payload = json.dumps(
                {
                    "script": SCRIPT,
                    "params": {"api_key": "must-not-leak"},
                    "material_sources": [
                        {
                            "provider": "pexels",
                            "local_file": "asset-1.mp4",
                            "duration": 8,
                            "search_term": "室内空气",
                            "asset_id": "4401",
                            "source_page": "https://www.pexels.com/video/4401?token=secret",
                            "creator": {
                                "id": "creator-1",
                                "name": "Public Creator",
                                "profile_page": "https://www.pexels.com/@creator?auth=secret",
                                "authorization": "must-not-leak",
                            },
                            "rendition": {"id": "hd", "width": 1080, "height": 1920},
                            "cookie": "must-not-leak",
                        }
                    ],
                },
                ensure_ascii=False,
            ).encode("utf-8")
        else:
            raise AssertionError(f"unexpected download: {url}")
        if len(payload) > max_bytes:
            raise AssertionError("fake payload exceeds test limit")
        destination.write_bytes(payload)
        self.downloads.append({"url": url, "destination": destination})
        return DownloadTransportResponse(url, len(payload))


def make_adapter(
    transport: FakeTransport,
    clock: FakeClock | None = None,
    timeout=10,
    local_material_root: Path | None = None,
):
    clock = clock or FakeClock()
    return ProductionEngineAdapter(
        "http://127.0.0.1:8080/api/v1",
        transport=transport,
        timeout_seconds=timeout,
        poll_interval_seconds=1,
        local_material_root=local_material_root,
        clock=clock.now,
        sleeper=clock.sleep,
    )


def run_success(adapter: ProductionEngineAdapter, folder: Path):
    return adapter.run(
        approved=True,
        script=SCRIPT,
        keywords=list(KEYWORDS),
        aspect="portrait",
        target_duration_seconds=52,
        staging_dir=folder,
        material_strategy="pexels",
        voice_strategy="edge_tts",
    )


class ProductionEngineAdapterTests(unittest.TestCase):
    def test_pinned_engine_and_unapproved_script_is_rejected_before_transport(self):
        transport = FakeTransport()
        adapter = make_adapter(transport)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ProductionEngineError) as raised:
                adapter.run(
                    approved=False,
                    script=SCRIPT,
                    keywords=list(KEYWORDS),
                    aspect="portrait",
                    target_duration_seconds=52,
                    staging_dir=Path(folder),
                    material_strategy="pexels",
                    voice_strategy="edge_tts",
                )
        self.assertEqual(raised.exception.code, "approval_required")
        self.assertEqual(transport.requests, [])
        self.assertEqual((ENGINE_NAME, ENGINE_VERSION), ("MoneyPrinterTurbo", "1.3.3"))
        self.assertEqual(ENGINE_COMMIT, "254cd028906ee657eab844dc94087cdbea2a7aa8")

    def test_poll_accepts_only_the_runner_safe_adjustment_window(self):
        for duration in (35.0, 75.0):
            with self.subTest(accepted=duration):
                transport = FakeTransport(
                    [FakeTransport.complete_status(audio_duration=duration)]
                )
                status = make_adapter(transport)._poll(TASK_ID, 10.0, None)
                self.assertEqual(status["audio_duration"], duration)

        for duration in (34.999, 75.001):
            with self.subTest(rejected=duration):
                transport = FakeTransport(
                    [FakeTransport.complete_status(audio_duration=duration)]
                )
                with self.assertRaises(ProductionEngineError) as raised:
                    make_adapter(transport)._poll(TASK_ID, 10.0, None)
                self.assertEqual(raised.exception.code, "unsafe_media_duration")

    def test_engine_url_allows_http_loopback_only(self):
        for allowed in (
            "http://127.0.0.1:8080/api/v1",
            "http://localhost:8080/api/v1/",
            "http://[::1]:8080/api/v1",
        ):
            with self.subTest(allowed=allowed):
                self.assertTrue(validate_engine_base_url(allowed).endswith("/api/v1"))
        for blocked in (
            "https://127.0.0.1:8080/api/v1",
            "http://example.com:8080/api/v1",
            "http://user:pass@127.0.0.1:8080/api/v1",
            "http://127.0.0.1:8080/api/v1?token=secret",
            "http://127.0.0.1:8080/other",
        ):
            with self.subTest(blocked=blocked):
                with self.assertRaises(ProductionEngineError):
                    validate_engine_base_url(blocked)

    def test_script_is_sent_verbatim_without_llm_or_provider_fields(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as folder:
            result = run_success(make_adapter(transport), Path(folder))
        post = next(item for item in transport.requests if item["method"] == "POST")
        self.assertEqual(post["payload"]["video_script"], SCRIPT)
        self.assertEqual(post["payload"]["video_terms"], KEYWORDS)
        self.assertEqual(post["payload"]["font_name"], "NotoSansSC-Regular.ttf")
        serialized_keys = json.dumps(post["payload"], ensure_ascii=False).lower()
        for forbidden in (
            "api_key",
            "provider_url",
            "base_url",
            "custom_system_prompt",
            "video_script_prompt",
            "authorization",
            "cookie",
        ):
            self.assertNotIn(forbidden, serialized_keys)
        self.assertEqual(result.task_id, TASK_ID)

    def test_timeout_attempts_cancel_without_publishing_a_result(self):
        clock = FakeClock()
        processing = {
            "status": 200,
            "message": "success",
            "data": {"task_id": TASK_ID, "state": 4, "progress": 20},
        }
        transport = FakeTransport([processing])
        with tempfile.TemporaryDirectory() as folder:
            current_pointer = Path(folder) / "current_run_id"
            current_pointer.write_text("previous-success\n", encoding="utf-8")
            with self.assertRaises(ProductionEngineError) as raised:
                run_success(make_adapter(transport, clock, timeout=2), Path(folder))
            self.assertFalse((Path(folder) / "engine_report.json").exists())
            self.assertEqual(current_pointer.read_text(encoding="utf-8"), "previous-success\n")
        self.assertEqual(raised.exception.code, "engine_timeout")
        self.assertIn("DELETE", [item["method"] for item in transport.requests])

    def test_cancel_event_stops_polling(self):
        transport = FakeTransport()
        event = threading.Event()
        event.set()
        adapter = make_adapter(transport)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ProductionEngineError) as raised:
                adapter.run(
                    approved=True,
                    script=SCRIPT,
                    keywords=list(KEYWORDS),
                    aspect="portrait",
                    target_duration_seconds=52,
                    staging_dir=Path(folder),
                    material_strategy="pexels",
                    voice_strategy="edge_tts",
                    cancel_event=event,
                )
        self.assertEqual(raised.exception.code, "engine_cancelled")

    def test_failed_task_uses_sanitized_error(self):
        failed = {
            "status": 200,
            "message": "success",
            "data": {
                "task_id": TASK_ID,
                "state": -1,
                "progress": 30,
                "failed_stage": "audio",
                "error": "Authorization: Bearer secret-value",
            },
        }
        transport = FakeTransport([failed])
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ProductionEngineError) as raised:
                run_success(make_adapter(transport), Path(folder))
        self.assertEqual(raised.exception.code, "engine_task_failed")
        self.assertEqual(raised.exception.stage, "audio")
        self.assertNotIn("secret-value", str(raised.exception))
        self.assertEqual(transport.downloads, [])

    def test_path_traversal_and_wrong_task_artifacts_are_rejected(self):
        for reference in (
            f"/tasks/{TASK_ID}/../final-1.mp4",
            f"/tasks/{TASK_ID}/%2e%2e/final-1.mp4",
            "/tasks/22222222-2222-4222-8222-222222222222/final-1.mp4",
        ):
            with self.subTest(reference=reference):
                transport = FakeTransport(
                    [FakeTransport.complete_status(videos=[reference])]
                )
                with tempfile.TemporaryDirectory() as folder:
                    with self.assertRaises(ProductionEngineError):
                        run_success(make_adapter(transport), Path(folder))
                self.assertEqual(transport.downloads, [])

    def test_off_origin_effective_url_is_rejected_as_redirect(self):
        transport = FakeTransport(post_final_url="http://example.com/api/v1/videos")
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ProductionEngineError) as raised:
                run_success(make_adapter(transport), Path(folder))
        self.assertEqual(raised.exception.code, "unsafe_engine_response_url")

    def test_online_strategy_rejects_local_material_paths(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            material = folder / "asset.mp4"
            material.write_bytes(b"local-video")
            with self.assertRaises(ProductionEngineError) as raised:
                make_adapter(transport, local_material_root=folder).run(
                    approved=True,
                    script=SCRIPT,
                    keywords=list(KEYWORDS),
                    aspect="portrait",
                    target_duration_seconds=52,
                    staging_dir=folder / "staging",
                    material_strategy="pexels",
                    voice_strategy="edge_tts",
                    local_material_paths=[material],
                )
        self.assertEqual(raised.exception.code, "unexpected_local_materials")
        self.assertEqual(transport.requests, [])

    def test_local_strategy_rejects_missing_directory_and_outside_root(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            allowed = folder / "allowed"
            allowed.mkdir()
            outside = folder / "outside.mp4"
            outside.write_bytes(b"outside")
            adapter = make_adapter(transport, local_material_root=allowed)
            for blocked in (outside, allowed):
                with self.subTest(blocked=blocked):
                    with self.assertRaises(ProductionEngineError):
                        adapter.run(
                            approved=True,
                            script=SCRIPT,
                            keywords=list(KEYWORDS),
                            aspect="portrait",
                            target_duration_seconds=52,
                            staging_dir=folder / "staging",
                            material_strategy="local",
                            voice_strategy="edge_tts",
                            local_material_paths=[blocked],
                        )
        self.assertEqual(transport.requests, [])

    def test_local_strategy_sends_only_loopback_paths_and_publishes_hashes(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            material_root = folder / "materials"
            material_root.mkdir()
            material = material_root / "approved-source.mp4"
            material.write_bytes(b"local-video-bytes")
            staging = folder / "staging"
            result = make_adapter(
                transport, local_material_root=material_root
            ).run(
                approved=True,
                script=SCRIPT,
                keywords=list(KEYWORDS),
                aspect="portrait",
                target_duration_seconds=52,
                staging_dir=staging,
                material_strategy="local",
                voice_strategy="edge_tts",
                local_material_paths=[material],
            )
            post = next(item for item in transport.requests if item["method"] == "POST")
            self.assertEqual(
                post["payload"]["video_materials"],
                [{"provider": "local", "url": str(material.resolve()), "duration": 0}],
            )
            self.assertTrue(post["url"].startswith("http://127.0.0.1:8080/api/v1/"))
            public_sources_text = (staging / "material_sources.json").read_text(
                encoding="utf-8"
            )
            public_sources = json.loads(public_sources_text)
            self.assertNotIn(str(material_root), public_sources_text)
            self.assertNotIn(str(material.resolve()), public_sources_text)
            self.assertEqual(
                public_sources["sources"],
                [
                    {
                        "basename": "approved-source.mp4",
                        "size": len(b"local-video-bytes"),
                        "sha256": "CF53D4F0C6C7BA2DC17A647A335AE3F73F426C78BA6BBB79FEA6C78671BD3E20",
                        "source_type": "local_user_supplied",
                    }
                ],
            )
            self.assertFalse(
                any(item["url"].endswith("/script.json") for item in transport.downloads)
            )
            self.assertIn("material_sources.json", {item.name for item in result.artifacts})

    def test_whitelist_collection_keeps_mp3_private_and_sanitizes_reports(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            result = run_success(make_adapter(transport), folder)
            paths = {item.relative_path for item in result.artifacts}
            self.assertEqual(
                paths,
                {
                    "final.mp4",
                    ".engine-import/audio.mp3",
                    "captions.srt",
                    "material_sources.json",
                    "engine_report.json",
                },
            )
            self.assertFalse((folder / "voice.wav").exists())
            self.assertTrue((folder / ".engine-import" / "audio.mp3").is_file())
            self.assertFalse((folder / ".engine-import" / ".script.json.part").exists())
            report_text = (folder / "engine_report.json").read_text(encoding="utf-8")
            sources_text = (folder / "material_sources.json").read_text(encoding="utf-8")
            report = json.loads(report_text)
            self.assertEqual(report["runtime_requirements"], {"PYTHONUTF8": "1"})
            for forbidden in (
                "must-not-leak",
                "private/path",
                "api_key",
                "authorization",
                "cookie",
                "?token=",
                "?auth=",
            ):
                self.assertNotIn(forbidden, report_text)
                self.assertNotIn(forbidden, sources_text)
            sources = json.loads(sources_text)
            self.assertEqual(sources["sources"][0]["source_page"], "https://www.pexels.com/video/4401")
            self.assertEqual(
                sources["sources"][0]["creator"]["profile_page"],
                "https://www.pexels.com/@creator",
            )


if __name__ == "__main__":
    unittest.main()
