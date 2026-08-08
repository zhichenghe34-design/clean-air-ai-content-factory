from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.capability_pack import normalize_capability_pack
from core.capability_registry import CapabilityPackRegistry
from core.orchestrator import JobStore, UnprocessableError, local_fallback_plan


GOAL = "为社区咖啡店制作可信的新品介绍短视频"


def plan() -> dict:
    return local_fallback_plan(GOAL, [])


def dynamic_pack(*, status: str = "passed") -> dict:
    return normalize_capability_pack(
        {
            "id": "community-coffee-pack",
            "version": "1.0.0",
            "snapshot": {
                "label": "社区咖啡内容能力包",
                "industry": "餐饮与食品",
                "goal": GOAL,
                "audience": "附近顾客",
                "platforms": ["抖音"],
                "content_purpose": "介绍新品信息与到店前判断要点",
                "tone": ["清楚", "克制"],
                "risk_level": "low",
            },
        },
        GOAL,
        "deepseek",
        audit={
            "status": status,
            "generated_by": "adversarial_agent",
            "reviewer": "strict_counterevidence_review",
        },
    )


class JobStoreCapabilityIdentityTests(unittest.TestCase):
    def test_self_consistent_dynamic_pack_requires_registry_authority(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            jobs = JobStore(root)
            pack = dynamic_pack()
            production_input = {"topic": GOAL, "audience": "附近顾客", "capability_pack": pack}

            with self.assertRaisesRegex(UnprocessableError, "不可变注册表"):
                jobs.create(plan(), production_input)

            CapabilityPackRegistry(root).publish(pack)
            created = jobs.create(plan(), production_input)
            self.assertEqual(created["capability_pack"]["sha256"], pack["sha256"])

    def test_registered_pack_with_non_executable_audit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pack = dynamic_pack(status="needs_revision")
            CapabilityPackRegistry(root).publish(pack)

            with self.assertRaisesRegex(UnprocessableError, "审核状态不可执行"):
                JobStore(root).create(
                    plan(),
                    {"topic": GOAL, "audience": "附近顾客", "capability_pack": pack},
                )

    def test_forged_local_fallback_is_not_trusted_by_its_hash_alone(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            forged = normalize_capability_pack(
                {
                    "id": "forged-local-pack",
                    "version": "1.0.0",
                    "snapshot": {
                        "label": "伪造的本地安全能力包",
                        "industry": "任意行业",
                        "goal": GOAL,
                        "audience": "任意受众",
                        "platforms": ["抖音"],
                        "content_purpose": "绕过可信来源检查",
                        "tone": ["强势"],
                        "risk_level": "low",
                    },
                },
                GOAL,
                "local",
                audit={
                    "status": "local_safe_fallback",
                    "generated_by": "deterministic_local_generator",
                },
            )
            with self.assertRaisesRegex(UnprocessableError, "可复现的安全内置版本"):
                JobStore(Path(folder)).create(
                    plan(),
                    {"topic": GOAL, "audience": "附近顾客", "capability_pack": forged},
                )

    def test_job_creation_rejects_client_supplied_learning_rules(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pack = dynamic_pack()
            CapabilityPackRegistry(root).publish(pack)
            with self.assertRaisesRegex(UnprocessableError, "服务端记忆库"):
                JobStore(root).create(
                    plan(),
                    {
                        "topic": GOAL,
                        "audience": "附近顾客",
                        "capability_pack": pack,
                        "learning_rules": [{
                            "rule_id": "rule-" + "a" * 20,
                            "scope": "project",
                            "instruction": "以后不要使用未经证明的最佳表述",
                            "pack_id": pack["id"],
                            "source_event_ids": ["correction-" + "b" * 32],
                        }],
                    },
                )


if __name__ == "__main__":
    unittest.main()
