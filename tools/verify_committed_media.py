from __future__ import annotations

import json
from pathlib import Path

from verify_release_bundle import (
    EXPECTED_EVIDENCE_SHA256,
    EXPECTED_FINAL_SHA256,
    EXPECTED_SAMPLE_SHA256,
    probe,
    sha256,
)


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    errors: list[str] = []
    old_demo = repo / "media" / "console-demo.mp4"
    if old_demo.exists():
        errors.append("media/console-demo.mp4 仍在当前发布树；旧版只应保留在 Git 历史和 legacy 包")

    evidence_zip = repo / "evidence" / "v2-real-deepseek-20260801-022153.zip"
    sample = repo / "media" / "sample.mp4"
    demo = repo / "media" / "agent-workbench-demo.mp4"
    final = repo / "evidence" / "v2-real-deepseek-20260801-022153" / "final.mp4"
    for path in (evidence_zip, sample, demo, final):
        if not path.is_file():
            errors.append(f"缺少媒体或证据源：{path.relative_to(repo)}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    if sha256(evidence_zip) != EXPECTED_EVIDENCE_SHA256:
        errors.append("真实证据 ZIP 哈希发生变化")
    if sha256(sample) != EXPECTED_SAMPLE_SHA256:
        errors.append("设计基准样片哈希发生变化")
    if sha256(final) != EXPECTED_FINAL_SHA256:
        errors.append("真实联调成片哈希发生变化")

    checks = {
        "Agent Demo": (probe(demo), (1440, 900), (60, 90), None),
        "设计样片": (probe(sample), (1080, 1920), (45, 60), "aac"),
        "真实联调成片": (probe(final), (1080, 1920), (45, 60), "aac"),
    }
    summary = {}
    for label, (media, size, duration, audio) in checks.items():
        summary[label] = media
        if media["codec"] != "h264" or media["pixel_format"] != "yuv420p":
            errors.append(f"{label} 不是 H.264/yuv420p")
        if (media["width"], media["height"]) != size:
            errors.append(f"{label} 分辨率异常：{media['width']}×{media['height']}")
        if not duration[0] <= media["duration"] <= duration[1]:
            errors.append(f"{label} 时长异常：{media['duration']:.3f} 秒")
        if media["audio_codec"] != audio:
            errors.append(f"{label} 音轨异常：{media['audio_codec']}")
    if abs(checks["Agent Demo"][0]["fps"] - 30) > 0.05:
        errors.append(f"Agent Demo 帧率异常：{checks['Agent Demo'][0]['fps']:.3f}")
    if not checks["Agent Demo"][0]["faststart"]:
        errors.append("Agent Demo 未启用 faststart")

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(json.dumps({"status": "COMMITTED_MEDIA_OK", "media": summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
