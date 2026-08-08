from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.capability_pack import CapabilityPackError, normalize_capability_pack
from core.capability_registry import (
    CapabilityPackConflictError,
    CapabilityPackIntegrityError,
    CapabilityPackNotFoundError,
    CapabilityPackRegistry,
    CapabilityPackRegistryError,
)


class CapabilityPackRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = CapabilityPackRegistry(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def make_pack(goal: str, *, version: str = "1.0.0") -> dict:
        return normalize_capability_pack(
            {
                "id": "local-service-pack",
                "version": version,
                "snapshot": {
                    "label": "本地服务内容能力包",
                    "industry": "本地生活服务",
                    "goal": goal,
                    "audience": "正在比较本地服务的潜在客户",
                    "platforms": ["抖音"],
                    "content_purpose": "帮助用户理解服务边界并作出选择",
                    "tone": ["清楚", "可信"],
                    "risk_level": "low",
                },
            },
            goal,
            "test",
            audit={"status": "passed", "checks": ["field_whitelist"]},
        )

    def test_publish_get_and_list_safe_summary(self) -> None:
        pack = self.make_pack("为社区洗衣店制作一条可信的竖屏介绍视频")
        published = self.registry.publish(pack)

        self.assertEqual(published, pack)
        self.assertEqual(self.registry.get(pack["id"]), pack)
        target = self.root / "capability-packs" / pack["id"] / f"{pack['sha256']}.json"
        self.assertTrue(target.is_file())
        raw = target.read_bytes()
        self.assertEqual(
            raw,
            json.dumps(pack, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )

        summaries = self.registry.list()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["id"], pack["id"])
        self.assertEqual(summaries[0]["sha256"], pack["sha256"])
        self.assertEqual(summaries[0]["integrity"], "verified")
        self.assertEqual(summaries[0]["version_count"], 1)
        self.assertNotIn("goal", summaries[0])
        self.assertNotIn("evidence_requirements", summaries[0])
        self.assertNotIn("prohibited_claims", summaries[0])

    def test_historical_versions_remain_addressable(self) -> None:
        first = self.make_pack("为社区洗衣店制作一条可信的竖屏介绍视频", version="1.0.0")
        second = self.make_pack("为社区洗衣店制作一条解释服务流程的竖屏视频", version="2.0.0")

        self.registry.publish(first)
        self.registry.publish(second)

        self.assertEqual(self.registry.get(first["id"], first["sha256"]), first)
        self.assertEqual(self.registry.get(second["id"]), second)
        self.assertEqual(self.registry.list()[0]["version_count"], 2)

    def test_identity_hash_binds_audit_and_same_hash_requires_identical_document(self) -> None:
        pack = self.make_pack("为社区洗衣店制作一条可信的竖屏介绍视频")
        self.registry.publish(pack)
        self.assertEqual(self.registry.publish(copy.deepcopy(pack)), pack)

        audit_tampered = copy.deepcopy(pack)
        audit_tampered["audit"]["note"] = "试图在不更新身份哈希时改写审计"
        with self.assertRaises(CapabilityPackError):
            self.registry.publish(audit_tampered)

        conflicting = copy.deepcopy(pack)
        conflicting["generated_at"] = "2026-08-03T12:00:00+08:00"
        with self.assertRaises(CapabilityPackConflictError):
            self.registry.publish(conflicting)

    def test_tampering_and_bad_hash_are_rejected(self) -> None:
        pack = self.make_pack("为社区洗衣店制作一条可信的竖屏介绍视频")
        self.registry.publish(pack)
        target = self.root / "capability-packs" / pack["id"] / f"{pack['sha256']}.json"
        tampered = copy.deepcopy(pack)
        tampered["snapshot"]["audience"] = "被偷偷改过的受众"
        target.write_text(
            json.dumps(tampered, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

        with self.assertRaises(CapabilityPackIntegrityError):
            self.registry.get(pack["id"])
        with self.assertRaises(CapabilityPackNotFoundError):
            self.registry.get(pack["id"], "0" * 64)

    def test_paths_cannot_escape_registry(self) -> None:
        pack = self.make_pack("为社区洗衣店制作一条可信的竖屏介绍视频")
        self.registry.publish(pack)

        for bad_id in ("../escape", "..\\escape", "/absolute", "A-uppercase"):
            with self.subTest(pack_id=bad_id):
                with self.assertRaises(CapabilityPackRegistryError):
                    self.registry.get(bad_id)
        for bad_sha in ("../latest", "A" * 64, "0" * 63, "0" * 64 + ".json"):
            with self.subTest(sha256=bad_sha):
                with self.assertRaises(CapabilityPackRegistryError):
                    self.registry.get(pack["id"], bad_sha)

        invalid = copy.deepcopy(pack)
        invalid["id"] = "../escape"
        with self.assertRaises(CapabilityPackError):
            self.registry.publish(invalid)
        self.assertFalse((self.root / "escape").exists())

    def test_failed_latest_replace_preserves_previous_latest(self) -> None:
        first = self.make_pack("为社区洗衣店制作一条可信的竖屏介绍视频", version="1.0.0")
        second = self.make_pack("为社区洗衣店制作一条解释服务流程的竖屏视频", version="2.0.0")
        self.registry.publish(first)
        real_replace = os.replace

        def fail_latest(source: os.PathLike[str], target: os.PathLike[str]) -> None:
            if Path(target).name == "latest.json":
                raise OSError("simulated pointer replace failure")
            real_replace(source, target)

        with mock.patch("core.capability_registry.os.replace", side_effect=fail_latest):
            with self.assertRaisesRegex(OSError, "simulated pointer"):
                self.registry.publish(second)

        self.assertEqual(self.registry.get(first["id"]), first)
        staging = list((self.root / "capability-packs" / first["id"]).glob("*.staging"))
        self.assertEqual(staging, [])

    def test_failed_pack_replace_leaves_no_public_entry(self) -> None:
        pack = self.make_pack("为社区洗衣店制作一条可信的竖屏介绍视频")
        with mock.patch("core.capability_registry.os.replace", side_effect=OSError("simulated failure")):
            with self.assertRaisesRegex(OSError, "simulated failure"):
                self.registry.publish(pack)

        pack_dir = self.root / "capability-packs" / pack["id"]
        self.assertFalse((pack_dir / f"{pack['sha256']}.json").exists())
        self.assertFalse((pack_dir / "latest.json").exists())
        self.assertEqual(list(pack_dir.glob("*.staging")), [])


if __name__ == "__main__":
    unittest.main()
