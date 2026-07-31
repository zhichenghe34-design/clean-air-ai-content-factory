from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.motion_director import build_motion_plan
from core.production import DEFAULT_INPUT, ProductionRunner, atomic_json, build_local_variants
from tools.verify_public_evidence import probe_video, validate_srt


def main() -> int:
    parser = argparse.ArgumentParser(description="Run real local SAPI/FFmpeg media smoke without creating a formal approved job.")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("输出目录已存在；请使用新目录，避免覆盖")
    output.mkdir(parents=True)

    config = dict(DEFAULT_INPUT)
    config.update({
        "topic": "装修后闻不到气味是否代表没有甲醛？",
        "audience": "新房家庭",
        "render_mode": "simple",
        "target_duration_seconds": 52,
    })
    script = build_local_variants(config["topic"], config["audience"])[0]["script"]
    runner = ProductionRunner(provider=None)
    voice = runner._synthesize_voice(output, script, config)
    voice.update(runner._normalize_voice_duration(output, config["target_duration_seconds"]))
    segments = runner._segments(config, script)
    duration = runner._audio_duration(output / "voice.wav")
    captions = runner._write_captions(output, segments, duration)
    motion_plan = build_motion_plan(config["topic"], config["audience"], segments, duration)
    atomic_json(output / "motion_plan.json", motion_plan)
    render = runner._render_video(output, segments, captions, duration)
    media = probe_video(output / "final.mp4")
    subtitle_errors = validate_srt(output / "captions.srt", media["duration_seconds"])
    if subtitle_errors:
        raise RuntimeError("字幕校验失败：" + "; ".join(subtitle_errors))
    result = {
        "status": "REAL_MEDIA_SMOKE_OK",
        "evidence_status": "test_only_not_human_approval",
        "voice": voice,
        "render": render,
        "media": media,
    }
    (output / "smoke_report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
