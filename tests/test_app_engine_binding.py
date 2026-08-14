from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app
from core.orchestrator import UnprocessableError


ENGINE_ENV_KEYS = {
    "SHIYI_MPT_ENABLED": "",
    "SHIYI_MPT_BASE_URL": "",
    "SHIYI_MPT_LOCAL_MATERIAL_DIR": "",
    "SHIYI_MPT_MATERIAL_STRATEGY": "",
    "SHIYI_MPT_TIMEOUT_SECONDS": "",
    "SHIYI_MPT_HEALTH_VERIFIED": "",
}


class AppProductionEngineBindingTests(unittest.TestCase):
    def test_disabled_engine_is_reported_honestly(self):
        with mock.patch.dict(os.environ, ENGINE_ENV_KEYS):
            adapter, options, summary = app.production_engine_binding(strict=True)
        self.assertIsNone(adapter)
        self.assertEqual(options, {})
        self.assertFalse(summary["enabled"])
        self.assertEqual(summary["health"], "disabled")

    def test_launcher_verified_local_engine_binds_only_curated_mp4_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "b.mp4").write_bytes(b"b")
            (root / "a.mp4").write_bytes(b"a")
            (root / "ignore.txt").write_text("ignore", encoding="utf-8")
            environment = {
                **ENGINE_ENV_KEYS,
                "SHIYI_MPT_ENABLED": "1",
                "SHIYI_MPT_BASE_URL": "http://127.0.0.1:19080/api/v1",
                "SHIYI_MPT_LOCAL_MATERIAL_DIR": str(root),
                "SHIYI_MPT_MATERIAL_STRATEGY": "local",
                "SHIYI_MPT_TIMEOUT_SECONDS": "900",
                "SHIYI_MPT_HEALTH_VERIFIED": "1",
            }
            with mock.patch.dict(os.environ, environment):
                adapter, options, summary = app.production_engine_binding(strict=True)
        self.assertIsNotNone(adapter)
        self.assertEqual([path.name for path in options["local_material_paths"]], ["a.mp4", "b.mp4"])
        self.assertEqual(summary["health"], "ready")
        self.assertEqual(summary["material_count"], 2)
        self.assertNotIn(str(root), str(summary))

    def test_invalid_engine_config_fails_closed_without_echoing_path(self):
        missing = Path("C:/private/customer-secret/materials")
        environment = {
            **ENGINE_ENV_KEYS,
            "SHIYI_MPT_ENABLED": "1",
            "SHIYI_MPT_BASE_URL": "http://127.0.0.1:19080/api/v1",
            "SHIYI_MPT_LOCAL_MATERIAL_DIR": str(missing),
            "SHIYI_MPT_MATERIAL_STRATEGY": "local",
            "SHIYI_MPT_TIMEOUT_SECONDS": "900",
        }
        with mock.patch.dict(os.environ, environment):
            with self.assertRaises(UnprocessableError) as raised:
                app.production_engine_binding(strict=True)
            _adapter, _options, summary = app.production_engine_binding(strict=False)
        self.assertEqual(raised.exception.details["code"], "local_material_root_required")
        self.assertNotIn("customer-secret", str(raised.exception))
        self.assertNotIn("customer-secret", str(summary))
        self.assertEqual(summary["health"], "misconfigured")

    def test_job_summary_marks_last_engine_success_without_reading_runtime_files(self):
        with mock.patch.dict(os.environ, ENGINE_ENV_KEYS):
            result = app.job_with_engine_summary(
                {
                    "id": "job-1",
                    "current_run_id": "run-1",
                    "artifacts": ["final.mp4", "engine_report.json"],
                }
            )
        self.assertEqual(result["production_engine"]["last_successful_run"], "run-1")


if __name__ == "__main__":
    unittest.main()
