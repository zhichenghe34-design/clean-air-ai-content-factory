from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOUNDARY = ROOT / "third_party" / "moneyprinterturbo"
LOCK_PATH = BOUNDARY / "upstream-lock.json"

EXPECTED_COMMIT = "254cd028906ee657eab844dc94087cdbea2a7aa8"
EXPECTED_LICENSE_SHA256 = "9065C26334AF5CDA00F023564EED62B6EC297FC2366924018958758BC78A0C26"
EXPECTED_SERVICES = {
    "app/services/material.py",
    "app/services/material_cache.py",
    "app/services/task.py",
    "app/services/task_artifacts.py",
    "app/services/voice.py",
    "app/services/subtitle.py",
    "app/services/video.py",
}
EXPECTED_API_CONTROLLERS = {
    "app/controllers/base.py",
    "app/controllers/ping.py",
    "app/controllers/v1/base.py",
    "app/controllers/v1/video.py",
}
EXPECTED_EXCLUSIONS = {
    "resource/fonts",
    "resource/songs",
    "webui",
    "app/services/llm.py",
    "app/controllers/v1/llm.py",
    "app/services/upload_post.py",
}


class MoneyPrinterTurboBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.lock_bytes = LOCK_PATH.read_bytes()
        cls.lock = json.loads(cls.lock_bytes.decode("utf-8"))

    def test_upstream_identity_is_immutable(self) -> None:
        self.assertEqual(self.lock["schema_version"], 1)
        self.assertEqual(self.lock["name"], "MoneyPrinterTurbo")
        self.assertEqual(self.lock["upstream_version"], "1.3.3")
        self.assertEqual(self.lock["upstream_commit"], EXPECTED_COMMIT)
        self.assertEqual(len(self.lock["upstream_commit"]), 40)
        self.assertEqual(self.lock["license"], "MIT")
        self.assertFalse(self.lock["source_imported"])
        self.assertEqual(
            self.lock["integration_status"],
            "cli_and_local_http_smoke_passed_formal_human_e2e_pending",
        )
        self.assertNotIn("latest", self.lock_bytes.decode("utf-8").lower())

    def test_license_is_the_locked_upstream_mit_text(self) -> None:
        license_path = BOUNDARY / self.lock["license_file"]
        digest = hashlib.sha256(license_path.read_bytes()).hexdigest().upper()
        self.assertEqual(self.lock["license_sha256"], EXPECTED_LICENSE_SHA256)
        self.assertEqual(digest, EXPECTED_LICENSE_SHA256)
        license_text = license_path.read_text(encoding="utf-8")
        self.assertIn("MIT License", license_text)
        self.assertIn("Copyright (c) 2024 Harry", license_text)

    def test_adoption_allowlist_and_exclusions_are_explicit(self) -> None:
        self.assertEqual(set(self.lock["allowed_adoption"]["services"]), EXPECTED_SERVICES)
        self.assertEqual(
            set(self.lock["allowed_adoption"]["api_controllers"]),
            EXPECTED_API_CONTROLLERS,
        )
        self.assertEqual(set(self.lock["excluded_components"]), EXPECTED_EXCLUSIONS)
        self.assertTrue(EXPECTED_EXCLUSIONS.isdisjoint(EXPECTED_SERVICES | EXPECTED_API_CONTROLLERS))
        self.assertEqual(self.lock["release_asset_policy"]["font"], "Noto Sans SC")
        self.assertEqual(
            self.lock["release_asset_policy"]["bgm"],
            "disabled_or_separately_verified",
        )
        self.assertFalse(self.lock["claims"]["public_saas"])
        self.assertFalse(self.lock["claims"]["intelligent_content_deduplication"])

    def test_repository_boundary_remains_metadata_only_after_smoke_test(self) -> None:
        files = {
            path.relative_to(BOUNDARY).as_posix()
            for path in BOUNDARY.rglob("*")
            if path.is_file()
        }
        self.assertEqual(files, {"LICENSE", "README.md", "upstream-lock.json"})


if __name__ == "__main__":
    unittest.main()
