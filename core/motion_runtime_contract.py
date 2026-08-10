from __future__ import annotations

import math


MOTION_ENGINE_NAME = "HyperFrames"
HYPERFRAMES_VERSION = "0.7.86"
HYPERFRAMES_RENDERER = "hyperframes-waapi-v1"
NODE_MINIMUM_MAJOR = 22
H264_CODEC_STRATEGY = "h264_mf"
H264_ENCODER = "h264_mf"
H264_MF_QUALITY_BY_TIER = {"draft": 60, "standard": 72, "high": 80}
H264_MF_CRF_QUALITY_ANCHORS = ((0.0, 100), (15.0, 80), (18.0, 72), (28.0, 60), (51.0, 1))
HYPERFRAMES_PATCH_ID = "shiyi-hyperframes-windows-mf"
HYPERFRAMES_PATCH_VERSION = "1.2.0"
HYPERFRAMES_UPSTREAM_CLI_SHA256 = (
    "B89672986C4487A133B241261AC610EA9F9CCDE467F206E18A60BEFFACAB6CB8"
)
HYPERFRAMES_PATCHED_CLI_SHA256 = (
    "86DA751BA397FF551355BA0C90370D732A297C3DC4652C981E9A8146D8EAC108"
)
SYSTEM_BROWSER_STRATEGY = "trusted_system_edge"
SYSTEM_BROWSER_MINIMUM_MAJOR = 151


def h264_mf_quality_from_crf(crf_equivalent: float | int) -> int:
    """Map the public CRF-shaped control to Media Foundation quality deterministically."""

    value = float(crf_equivalent)
    if not math.isfinite(value) or not 0 <= value <= 51:
        raise ValueError("H.264 quality must be a CRF-equivalent number from 0 to 51")
    for (left_crf, left_quality), (right_crf, right_quality) in zip(
        H264_MF_CRF_QUALITY_ANCHORS,
        H264_MF_CRF_QUALITY_ANCHORS[1:],
    ):
        if value <= right_crf:
            ratio = (value - left_crf) / (right_crf - left_crf)
            mapped = left_quality + ratio * (right_quality - left_quality)
            return max(1, min(100, math.floor(mapped + 0.5)))
    return 1


def h264_mf_quality_for_tier(tier: str) -> int:
    try:
        return H264_MF_QUALITY_BY_TIER[tier]
    except KeyError as exc:
        raise ValueError(f"unknown H.264 quality tier: {tier}") from exc


def h264_mf_video_args(
    *,
    crf_equivalent: float | int = 20,
    video_bitrate: str | None = None,
    gop_size: int | None = None,
) -> list[str]:
    """Return the single Windows-portable H.264 encoder policy used by project FFmpeg calls."""

    args = ["-c:v", H264_ENCODER]
    if video_bitrate is not None:
        bitrate = str(video_bitrate).strip()
        if not bitrate:
            raise ValueError("video_bitrate must not be empty")
        args.extend(["-rate_control", "cbr", "-b:v", bitrate])
    else:
        args.extend(
            [
                "-rate_control",
                "quality",
                "-quality",
                str(h264_mf_quality_from_crf(crf_equivalent)),
            ]
        )
    args.extend(["-scenario", "archive", "-hw_encoding", "0"])
    if gop_size is not None:
        if isinstance(gop_size, bool) or not isinstance(gop_size, int) or gop_size <= 0:
            raise ValueError("gop_size must be a positive integer")
        args.extend(
            [
                "-g",
                str(gop_size),
                "-keyint_min",
                str(gop_size),
                "-force_key_frames",
                f"expr:eq(mod(n,{gop_size}),0)",
                "-flags",
                "+cgop",
            ]
        )
    args.extend(["-bf", "0", "-pix_fmt", "yuv420p"])
    return args
