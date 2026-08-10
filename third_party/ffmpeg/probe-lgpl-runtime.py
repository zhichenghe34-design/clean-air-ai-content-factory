from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import struct
import subprocess
import sys
import time
import wave
import zlib
from pathlib import Path
from typing import Any


REQUIRED_FILTERS = {
    "amix",
    "aresample",
    "atempo",
    "format",
    "fps",
    "scale",
    "setpts",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def make_png(width: int, height: int, phase: int) -> bytes:
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend(
                (
                    (x * 3 + phase * 29) & 0xFF,
                    (y * 2 + phase * 47) & 0xFF,
                    ((x + y) * 2 + phase * 61) & 0xFF,
                )
            )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + png_chunk(b"IEND", b"")
    )


def make_wav(path: Path, *, frequency: float, seconds: float = 1.2) -> None:
    sample_rate = 48_000
    sample_count = int(sample_rate * seconds)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        frames = bytearray()
        for index in range(sample_count):
            fade = min(1.0, index / 1200, (sample_count - index) / 1200)
            value = int(9000 * fade * math.sin(2 * math.pi * frequency * index / sample_rate))
            frames.extend(struct.pack("<h", value))
        output.writeframes(bytes(frames))


class Probe:
    def __init__(self, runtime_dir: Path, work_dir: Path) -> None:
        self.runtime_dir = runtime_dir.resolve()
        self.work_dir = work_dir.resolve()
        self.ffmpeg = self.runtime_dir / "ffmpeg.exe"
        self.ffprobe = self.runtime_dir / "ffprobe.exe"
        self.logs_dir = self.work_dir / "logs"
        self.logs_dir.mkdir(parents=True)
        self.commands: list[dict[str, Any]] = []

    def run(
        self,
        name: str,
        executable: Path,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
    ) -> str:
        argv = [str(executable), *args]
        started = time.monotonic()
        process = subprocess.run(
            argv,
            cwd=self.work_dir,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        elapsed = round(time.monotonic() - started, 3)
        log_path = self.logs_dir / f"{len(self.commands) + 1:02d}-{name}.log"
        log_path.write_bytes(process.stdout)
        record = {
            "name": name,
            "argv": argv,
            "returncode": process.returncode,
            "elapsed_seconds": elapsed,
            "log": log_path.relative_to(self.work_dir).as_posix(),
            "log_sha256": sha256(log_path),
        }
        self.commands.append(record)
        if process.returncode != 0:
            tail = process.stdout.decode("utf-8", errors="replace")[-4000:]
            raise RuntimeError(f"{name} failed with exit code {process.returncode}\n{tail}")
        return process.stdout.decode("utf-8", errors="replace")

    def ffmpeg_run(
        self, name: str, args: list[str], *, input_bytes: bytes | None = None
    ) -> str:
        return self.run(
            name,
            self.ffmpeg,
            ["-hide_banner", "-nostdin", *args],
            input_bytes=input_bytes,
        )

    def probe_json(self, name: str, path: Path) -> dict[str, Any]:
        output = self.run(
            name,
            self.ffprobe,
            [
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
        )
        return json.loads(output)


def require_canary_contract(metadata: dict[str, Any]) -> None:
    streams = metadata.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if not isinstance(video, dict) or not isinstance(audio, dict):
        raise RuntimeError("canary must contain both video and audio streams")
    expected_video = {
        "codec_name": "h264",
        "width": 1080,
        "height": 1920,
        "pix_fmt": "yuv420p",
        "r_frame_rate": "30/1",
    }
    for key, expected in expected_video.items():
        if video.get(key) != expected:
            raise RuntimeError(f"canary video {key}={video.get(key)!r}, expected {expected!r}")
    if audio.get("codec_name") != "aac" or audio.get("sample_rate") != "48000":
        raise RuntimeError(f"unexpected canary audio contract: {audio}")
    duration = float(metadata.get("format", {}).get("duration", 0))
    if not 0.9 <= duration <= 1.1:
        raise RuntimeError(f"canary duration is outside tolerance: {duration}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe the frozen LGPL Windows FFmpeg runtime")
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--mp3-fixture", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runtime_dir = args.runtime_dir.resolve()
    work_dir = args.work_dir.resolve()
    mp3_fixture = args.mp3_fixture.resolve()
    required_runtime = {
        "avcodec-63.dll",
        "avfilter-12.dll",
        "avformat-63.dll",
        "avutil-61.dll",
        "ffmpeg.exe",
        "ffprobe.exe",
        "swresample-7.dll",
        "swscale-10.dll",
        "zlib1.dll",
    }
    actual_runtime = {item.name for item in runtime_dir.iterdir() if item.is_file()}
    if actual_runtime != required_runtime:
        missing = sorted(required_runtime - actual_runtime)
        extra = sorted(actual_runtime - required_runtime)
        raise RuntimeError(f"runtime file set mismatch; missing={missing}, extra={extra}")
    if not mp3_fixture.is_file():
        raise RuntimeError(f"MP3 fixture is missing: {mp3_fixture}")
    if work_dir.exists() and any(work_dir.iterdir()):
        raise RuntimeError(f"probe work directory must be empty: {work_dir}")
    work_dir.mkdir(parents=True, exist_ok=True)
    probe = Probe(runtime_dir, work_dir)

    png_payloads: list[bytes] = []
    for index in range(10):
        payload = make_png(360, 640, index)
        (work_dir / f"frame-{index:03d}.png").write_bytes(payload)
        png_payloads.append(payload)
    make_wav(work_dir / "tone-440.wav", frequency=440)
    make_wav(work_dir / "tone-660.wav", frequency=660)
    local_mp3 = work_dir / "probe-tone.mp3"
    shutil.copy2(mp3_fixture, local_mp3)
    (work_dir / "captions.srt").write_text(
        "1\n00:00:00,000 --> 00:00:00,900\nLGPL runtime canary\n",
        encoding="utf-8",
    )

    filters = probe.ffmpeg_run("filters", ["-filters"])
    missing_filters = sorted(name for name in REQUIRED_FILTERS if name not in filters)
    if missing_filters:
        raise RuntimeError(f"required filters are missing: {missing_filters}")

    canary = work_dir / "canary-1080x1920-h264-aac.mp4"
    probe.ffmpeg_run(
        "png-sequence-h264-aac-canary",
        [
            "-y",
            "-framerate",
            "10",
            "-i",
            "frame-%03d.png",
            "-i",
            "tone-440.wav",
            "-i",
            "tone-660.wav",
            "-filter_complex",
            (
                "[0:v]setpts=PTS-STARTPTS,fps=30,"
                "scale=1080:1920:flags=bicubic,format=nv12[v];"
                "[1:a]aresample=48000,atempo=1.0[a1];"
                "[2:a]aresample=48000,atempo=1.0,volume=0.25[a2];"
                "[a1][a2]amix=inputs=2:duration=first:normalize=0[a]"
            ),
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-t",
            "1",
            "-c:v",
            "h264_mf",
            "-b:v",
            "4M",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(canary),
        ],
    )
    canary_metadata = probe.probe_json("ffprobe-canary-json", canary)
    require_canary_contract(canary_metadata)

    pipe_video = work_dir / "image2pipe-h264.mp4"
    probe.ffmpeg_run(
        "png-image2pipe-h264",
        [
            "-y",
            "-f",
            "image2pipe",
            "-framerate",
            "10",
            "-vcodec",
            "png",
            "-i",
            "pipe:0",
            "-vf",
            "fps=30,scale=1080:1920:flags=bicubic,format=nv12",
            "-t",
            "1",
            "-an",
            "-c:v",
            "h264_mf",
            "-b:v",
            "2M",
            str(pipe_video),
        ],
        input_bytes=b"".join(png_payloads),
    )
    pipe_metadata = probe.probe_json("ffprobe-image2pipe-json", pipe_video)
    pipe_stream = next(
        item for item in pipe_metadata["streams"] if item.get("codec_type") == "video"
    )
    if (pipe_stream.get("codec_name"), pipe_stream.get("width"), pipe_stream.get("height")) != (
        "h264",
        1080,
        1920,
    ):
        raise RuntimeError(f"image2pipe output contract failed: {pipe_stream}")

    jpeg = work_dir / "frame.jpg"
    probe.ffmpeg_run("png-to-jpeg", ["-y", "-i", "frame-000.png", "-frames:v", "1", str(jpeg)])
    for name, media in (
        ("decode-png", work_dir / "frame-000.png"),
        ("decode-jpeg", jpeg),
        ("decode-wav", work_dir / "tone-440.wav"),
        ("decode-mp3", local_mp3),
    ):
        probe.ffmpeg_run(name, ["-v", "error", "-i", str(media), "-f", "null", "NUL"])

    aac = work_dir / "audio.aac"
    h264 = work_dir / "video.h264"
    probe.ffmpeg_run(
        "extract-aac-adts",
        ["-y", "-i", str(canary), "-map", "0:a:0", "-c:a", "copy", "-f", "adts", str(aac)],
    )
    probe.ffmpeg_run("decode-aac", ["-v", "error", "-i", str(aac), "-f", "null", "NUL"])
    probe.ffmpeg_run(
        "extract-h264-annexb",
        [
            "-y",
            "-i",
            str(canary),
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-bsf:v",
            "h264_mp4toannexb",
            "-f",
            "h264",
            str(h264),
        ],
    )
    probe.ffmpeg_run("decode-h264", ["-v", "error", "-i", str(h264), "-f", "null", "NUL"])

    mov = work_dir / "canary.mov"
    probe.ffmpeg_run("mov-remux", ["-y", "-i", str(canary), "-c", "copy", str(mov)])
    mov_metadata = probe.probe_json("ffprobe-mov-json", mov)
    probe.ffmpeg_run("decode-mov", ["-v", "error", "-i", str(mov), "-f", "null", "NUL"])

    concat_list = work_dir / "concat.txt"
    concat_list.write_text("file 'canary-1080x1920-h264-aac.mp4'\n" * 2, encoding="ascii")
    concat = work_dir / "concat.mp4"
    probe.ffmpeg_run(
        "concat-demux-copy",
        ["-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(concat)],
    )
    concat_metadata = probe.probe_json("ffprobe-concat-json", concat)
    if float(concat_metadata.get("format", {}).get("duration", 0)) < 1.8:
        raise RuntimeError(f"concat duration is too short: {concat_metadata}")

    captioned = work_dir / "captioned.mp4"
    probe.ffmpeg_run(
        "srt-movtext-mux",
        [
            "-y",
            "-i",
            str(canary),
            "-f",
            "srt",
            "-i",
            "captions.srt",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-map",
            "1:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-c:s",
            "mov_text",
            str(captioned),
        ],
    )
    caption_metadata = probe.probe_json("ffprobe-caption-json", captioned)
    if not any(
        item.get("codec_type") == "subtitle" and item.get("codec_name") == "mov_text"
        for item in caption_metadata.get("streams", [])
    ):
        raise RuntimeError(f"mov_text subtitle stream is missing: {caption_metadata}")

    outputs = {}
    for path in sorted(work_dir.iterdir(), key=lambda item: item.name):
        if path.is_file():
            outputs[path.name] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
    runtime_files = {}
    for path in sorted(runtime_dir.iterdir(), key=lambda item: item.name):
        runtime_files[path.name] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
    report = {
        "schema_version": 1,
        "status": "passed",
        "runtime_dir": str(runtime_dir),
        "runtime_files": runtime_files,
        "required_filters": sorted(REQUIRED_FILTERS),
        "canary_contract": {
            "video_codec": "h264",
            "audio_codec": "aac",
            "width": 1080,
            "height": 1920,
            "pixel_format": "yuv420p",
            "frame_rate": "30/1",
        },
        "canary_ffprobe": canary_metadata,
        "image2pipe_ffprobe": pipe_metadata,
        "mov_ffprobe": mov_metadata,
        "concat_ffprobe": concat_metadata,
        "caption_ffprobe": caption_metadata,
        "mp3_fixture": {
            "name": local_mp3.name,
            "bytes": local_mp3.stat().st_size,
            "sha256": sha256(local_mp3),
        },
        "commands": probe.commands,
        "outputs": outputs,
    }
    report_path = work_dir / "probe-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "report": str(report_path), "commands": len(probe.commands)}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FFmpeg capability probe failed: {exc}", file=sys.stderr)
        raise
