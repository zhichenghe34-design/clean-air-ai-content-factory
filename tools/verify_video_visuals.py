from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.production import verify_video_visuals  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抽取12帧并阻断正式成片中的测试彩条或已知测试素材。",
    )
    parser.add_argument("video", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--material-sources", type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--ffprobe", type=Path)
    parser.add_argument(
        "--allow-needs-visual-review",
        action="store_true",
        help="仅用于审查工具链；正式生产仍会在报告中保留 needs_visual_review。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = verify_video_visuals(
        args.video,
        output_dir=args.output_dir,
        material_sources_path=args.material_sources,
        ffmpeg_path=args.ffmpeg,
        ffprobe_path=args.ffprobe,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] == "blocked":
        return 2
    if report["status"] == "needs_visual_review" and not args.allow_needs_visual_review:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
