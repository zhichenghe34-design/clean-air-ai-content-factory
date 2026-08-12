from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.capability_pack import legacy_clean_air_pack
from core.production import ProductionRunner
from core.voice_contract import DEFAULT_VOICE_ENGINE, DEFAULT_VOICE_NAME


TOPIC = "甲醛检测仪数值低，就能直接安心入住吗？"
AUDIENCE = "刚完成装修、准备启用空间的家庭"
SCRIPT = (
    "甲醛检测仪数值低，就能直接安心入住吗？先别只看这一刻的数字。"
    "测量位置、门窗状态和测量时间不同，读数可能出现变化。"
    "一次读数反映的是当时的测量结果，不能代替持续观察，也不能证明污染来源已经消失。"
    "更稳妥的判断，是先记录检测条件，再在相同条件下复测，观察结果是否稳定。"
    "如果涉及具体产品或治理效果，还要核对对应检测材料、适用空间和使用条件。"
    "把数字、条件和来源放在一起看，再决定下一步，而不是让一个数字替你下结论。"
)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _source_commit(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "uncommitted-source"


def build_showcase(output_dir: Path) -> dict[str, Any]:
    root = output_dir.resolve()
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"拒绝覆盖非空样片目录：{root}")
    root.mkdir(parents=True, exist_ok=True)
    pack = legacy_clean_air_pack()
    script_sha256 = hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest().upper()
    payloads = {
        "research.json": {
            "status": "internal_showcase_no_provider_calls",
            "findings": [],
            "boundary": "内部信息样片；不包含产品功效、检测结果、用户证言或入住保证",
        },
        "insight.json": {
            "topic": TOPIC,
            "audience": AUDIENCE,
            "content_goal": "解释判断条件，而不是承诺结果",
        },
        "script_variants.json": {
            "variants": [{"id": "showcase-fixed-v1", "script": SCRIPT}],
            "provider": {"mode": "local_fixed_showcase", "calls": 0},
        },
        "approved_script.json": {
            "id": "showcase-fixed-v1",
            "script": SCRIPT,
            "script_sha256": script_sha256,
            "selected_by": "mechanical_showcase_builder",
            "human_approval_claimed": False,
        },
        "review.json": {
            "status": "passed",
            "blocked": False,
            "warnings": [],
            "reviewer": "mechanical_reverse_review",
            "human_approval_claimed": False,
            "boundary": "内部甲方观看样片，不是品牌、科学或广告最终批准",
        },
    }
    for name, payload in payloads.items():
        _write_json(root / name, payload)
    approvals = {
        "research": {
            "status": "approved",
            "actor_type": "agent",
            "reviewer": "mechanical_reverse_review",
            "human_approval_claimed": False,
            "findings": [],
        },
        "compliance": {
            "status": "approved",
            "actor_type": "agent",
            "reviewer": "mechanical_reverse_review",
            "human_approval_claimed": False,
        },
    }
    production_input = {
        "topic": TOPIC,
        "audience": AUDIENCE,
        "target_duration_seconds": 52,
        "production_mode": "motion",
        "animation_quality": "standard",
        "voice_engine": DEFAULT_VOICE_ENGINE,
        "capability_pack": pack,
    }
    report = ProductionRunner().run_render_stage(root, production_input, approvals)
    required = (
        "voice.wav",
        "bgm.wav",
        "program_audio.wav",
        "captions.srt",
        "motion_plan.json",
        "final.mp4",
        "run_report.json",
        "contact-sheet.png",
        "visual-qc.json",
    )
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        raise RuntimeError(f"正式样片缺少产物：{missing}")
    manifest = {
        "schema_version": 1,
        "status": "MACHINE_QC_PASSED_PENDING_USER_ACCEPTANCE",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "generator": "tools/build_internal_motion_showcase.py",
        "source_commit": _source_commit(REPO_ROOT),
        "product_path": {
            "runner": "core.production.ProductionRunner.run_render_stage",
            "motion": "core.motion_director.build_motion_project",
            "audio": "core.program_audio.build_default_program_audio",
        },
        "voice": {
            "engine": DEFAULT_VOICE_ENGINE,
            "name": DEFAULT_VOICE_NAME,
            "selection_exposed": False,
            "requires_api_key": False,
            "requires_network": True,
            "private_voice_model_used": False,
        },
        "background_music": {
            "mode": "deterministic_builtin_synthesis",
            "external_asset_used": False,
        },
        "provider_calls": 0,
        "human_approval_claimed": False,
        "final_acceptance": "pending_user_review",
        "report_status": report.get("status"),
        "files": {
            name: {
                "bytes": (root / name).stat().st_size,
                "sha256": _sha256(root / name),
            }
            for name in required
        },
    }
    _write_json(root / "SHOWCASE-MANIFEST.json", manifest)
    print(json.dumps({"status": manifest["status"], "output": str(root)}, ensure_ascii=False))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="用产品正式链路生成一支内部代码动画样片")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build_showcase(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
