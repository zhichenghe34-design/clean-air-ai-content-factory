from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from core.motion_runtime_contract import SYSTEM_BROWSER_MINIMUM_MAJOR, SYSTEM_BROWSER_STRATEGY


class MotionHealthHandshakeTests(unittest.TestCase):
    def test_motion_never_reports_ready_without_browser_and_launcher_handshake(self) -> None:
        with patch.object(
            app,
            "_resolve_hyperframes_runtime",
            return_value={"runtime_source": "development_repo", "browser": None},
        ), patch.dict(os.environ, {}, clear=True):
            summary = app.production_mode_summary("motion")
        self.assertFalse(summary["enabled"])
        self.assertEqual("unavailable", summary["health"])

        with tempfile.TemporaryDirectory() as directory:
            browser = Path(directory) / "msedge.exe"
            browser.write_bytes(b"fixture")
            runtime = {
                "runtime_source": "packaged",
                "browser": browser,
                "browser_strategy": SYSTEM_BROWSER_STRATEGY,
                "browser_version": "151.0.1000.1",
                "browser_minimum_major": SYSTEM_BROWSER_MINIMUM_MAJOR,
            }
            with patch.object(app, "_resolve_hyperframes_runtime", return_value=runtime), patch.dict(
                os.environ, {}, clear=True
            ):
                unverified = app.production_mode_summary("motion")
            self.assertFalse(unverified["enabled"])
            self.assertEqual("configured_unverified", unverified["health"])
            self.assertEqual(SYSTEM_BROWSER_STRATEGY, unverified["browser_strategy"])
            self.assertEqual("151.0.1000.1", unverified["browser_version"])

            with patch.object(app, "_resolve_hyperframes_runtime", return_value=runtime), patch.dict(
                os.environ, {"SHIYI_MOTION_HEALTH_VERIFIED": "1"}, clear=True
            ):
                verified = app.production_mode_summary("motion")
            self.assertTrue(verified["enabled"])
            self.assertEqual("ready", verified["health"])
            self.assertEqual(SYSTEM_BROWSER_MINIMUM_MAJOR, verified["browser_minimum_major"])

    def test_mpt_health_file_revokes_stale_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "mpt-health.json"
            state.write_text(json.dumps({"schema_version": 1, "healthy": True}) + "\n", encoding="utf-8")
            environment = {
                "SHIYI_MPT_HEALTH_VERIFIED": "1",
                "SHIYI_MPT_HEALTH_FILE": str(state),
            }
            with patch.dict(os.environ, environment, clear=True):
                self.assertTrue(app._mpt_health_verified())
                state.write_text(
                    json.dumps({"schema_version": 1, "healthy": False}) + "\n", encoding="utf-8"
                )
                self.assertFalse(app._mpt_health_verified())


if __name__ == "__main__":
    unittest.main()
