#!/usr/bin/env python3
"""Generate and verify deterministic HyperFrames h264_mf quality evidence.

The input is a byte-exact RGB24 motion-design canary generated with the Python
standard library.  Large source/video/log artifacts stay outside the repository
under ``08_产出与验收``; the repository JSON freezes every command token,
raw probe result, PSNR log, keyframe result, and artifact digest.

This evidence is intentionally Windows-specific.  Exact MP4 hashes are bound to
the frozen FFmpeg runtime, OS build, and Media Foundation software encoder file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 2
EVIDENCE_ID = "hyperframes-h264-mf-quality-20260810-v2"
BASE_WIDTH = 360
BASE_HEIGHT = 640
RASTER_SCALE = 3
WIDTH = BASE_WIDTH * RASTER_SCALE
HEIGHT = BASE_HEIGHT * RASTER_SCALE
FPS = 30
FRAME_COUNT = 60
GOP = 30
TIERS = (("draft", 60), ("standard", 72), ("high", 80))
MF_H264_ENCODER_CLSID = "{6CA50344-051A-4DED-9779-A43305165E35}"
DEFAULT_ARTIFACT_RELATIVE = (
    "08_产出与验收/v0.3发布闭环/20260810-hyperframes-h264-mf-quality-v2"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parents[1]
DEFAULT_MANIFEST = (
    REPO_ROOT / "third_party" / "hyperframes" / "windows-mf-quality-evidence.json"
)
FFMPEG_LOCK = REPO_ROOT / "third_party" / "ffmpeg" / "upstream-lock.json"
FFMPEG_DIR = REPO_ROOT / "third_party" / "ffmpeg" / "runtime" / "win-x64"
FFMPEG = FFMPEG_DIR / "ffmpeg.exe"
FFPROBE = FFMPEG_DIR / "ffprobe.exe"
FONT_PATHS = (
    "docs/fonts/NotoSansSC-Regular.ttf",
    "docs/fonts/NotoSansSC-Bold.ttf",
    "docs/fonts/OFL.txt",
    "docs/fonts/SOURCE.md",
)


GLYPHS = {
    " ": ("00000",) * 7,
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01110"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
}


class EvidenceError(RuntimeError):
    """Raised when evidence cannot be generated or verified exactly."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def file_record(path: Path, *, relative_to: Path | None = None) -> dict[str, Any]:
    name = path.relative_to(relative_to).as_posix() if relative_to else path.name
    return {"path": name, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _pixel(buffer: bytearray, x: int, y: int, color: tuple[int, int, int]) -> None:
    if 0 <= x < BASE_WIDTH and 0 <= y < BASE_HEIGHT:
        offset = (y * BASE_WIDTH + x) * 3
        buffer[offset : offset + 3] = bytes(color)


def _rect(
    buffer: bytearray,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    color: tuple[int, int, int],
) -> None:
    x0, x1 = max(0, min(x0, x1)), min(BASE_WIDTH, max(x0, x1))
    y0, y1 = max(0, min(y0, y1)), min(BASE_HEIGHT, max(y0, y1))
    if x0 >= x1 or y0 >= y1:
        return
    row = bytes(color) * (x1 - x0)
    for y in range(y0, y1):
        start = (y * BASE_WIDTH + x0) * 3
        buffer[start : start + len(row)] = row


def _line(
    buffer: bytearray,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    color: tuple[int, int, int],
    thickness: int = 1,
) -> None:
    dx = abs(x1 - x0)
    sx = 1 if x0 < x1 else -1
    dy = -abs(y1 - y0)
    sy = 1 if y0 < y1 else -1
    error = dx + dy
    radius = max(0, thickness - 1)
    while True:
        for oy in range(-radius, radius + 1):
            for ox in range(-radius, radius + 1):
                _pixel(buffer, x0 + ox, y0 + oy, color)
        if x0 == x1 and y0 == y1:
            break
        twice = 2 * error
        if twice >= dy:
            error += dy
            x0 += sx
        if twice <= dx:
            error += dx
            y0 += sy


def _circle(
    buffer: bytearray,
    cx: int,
    cy: int,
    radius: int,
    color: tuple[int, int, int],
) -> None:
    x, y, error = radius, 0, 1 - radius
    while x >= y:
        for px, py in (
            (cx + x, cy + y),
            (cx + y, cy + x),
            (cx - y, cy + x),
            (cx - x, cy + y),
            (cx - x, cy - y),
            (cx - y, cy - x),
            (cx + y, cy - x),
            (cx + x, cy - y),
        ):
            _pixel(buffer, px, py, color)
        y += 1
        if error < 0:
            error += 2 * y + 1
        else:
            x -= 1
            error += 2 * (y - x) + 1


def _text(
    buffer: bytearray,
    text: str,
    x: int,
    y: int,
    color: tuple[int, int, int],
    scale: int = 1,
) -> None:
    cursor = x
    for character in text:
        rows = GLYPHS.get(character)
        if rows is None:
            raise EvidenceError(f"embedded glyph is missing: {character!r}")
        for row_index, row in enumerate(rows):
            for column_index, bit in enumerate(row):
                if bit == "1":
                    _rect(
                        buffer,
                        cursor + column_index * scale,
                        y + row_index * scale,
                        cursor + (column_index + 1) * scale,
                        y + (row_index + 1) * scale,
                        color,
                    )
        cursor += 6 * scale


def render_frame(index: int) -> bytes:
    """Return one deterministic RGB24 canary frame."""
    if not 0 <= index < FRAME_COUNT:
        raise ValueError(f"frame index out of range: {index}")
    buffer = bytearray(BASE_WIDTH * BASE_HEIGHT * 3)
    phase = index if index < FRAME_COUNT // 2 else FRAME_COUNT - 1 - index
    for y in range(BASE_HEIGHT):
        row_offset = y * BASE_WIDTH * 3
        for x in range(BASE_WIDTH):
            offset = row_offset + x * 3
            buffer[offset] = 10 + (x * 28 // BASE_WIDTH) + (phase * 7 // 29)
            buffer[offset + 1] = 17 + (y * 32 // BASE_HEIGHT) + ((x + index * 3) % 19)
            buffer[offset + 2] = (
                34
                + ((x + y) * 44 // (BASE_WIDTH + BASE_HEIGHT))
                + (index % 13)
            )

    # One-pixel grid, animated fine lines, and high-frequency blocks exercise
    # exactly the detail that differentiates the three Media Foundation tiers.
    grid_color = (46, 62, 82)
    for x in range((index * 2) % 24, BASE_WIDTH, 24):
        _line(buffer, x, 0, x, BASE_HEIGHT - 1, grid_color)
    for y in range((index * 3) % 24, BASE_HEIGHT, 24):
        _line(buffer, 0, y, BASE_WIDTH - 1, y, grid_color)

    _rect(buffer, 20, 24, 340, 112, (14, 25, 39))
    _line(buffer, 20, 112, 340, 112, (73, 222, 197), 2)
    _text(buffer, "H264 MF QUALITY", 36, 42, (238, 247, 249), 2)
    _text(buffer, "FRAME", 36, 82, (137, 230, 211), 1)
    _text(buffer, f"{index:02d}", 78, 82, (255, 207, 96), 1)

    moving_x = 30 + (index * 5) % 300
    _line(buffer, moving_x, 138, 360 - moving_x, 430, (255, 104, 104), 1)
    _line(buffer, 360 - moving_x, 138, moving_x, 430, (93, 205, 255), 2)
    _circle(buffer, 180, 286, 38 + phase, (249, 220, 92))
    _circle(buffer, 180, 286, 14 + phase // 2, (120, 236, 214))

    for y in range(454, 526):
        for x in range(28, 172):
            cell = ((x - 28) // 2 + (y - 454) // 2 + index) & 1
            if cell:
                _pixel(buffer, x, y, (224, 234, 241))
            else:
                _pixel(buffer, x, y, (20, 29, 43))
    for x in range(188, 332):
        value = (x - 188) * 255 // 143
        _rect(buffer, x, 454, x + 1, 526, (value, 255 - value, 128 + value // 2))

    _rect(buffer, 20, 548, 340, 616, (11, 21, 33))
    _text(buffer, "DRAFT 60", 34, 564, (217, 230, 238), 1)
    _text(buffer, "STANDARD 72", 34, 580, (137, 230, 211), 1)
    _text(buffer, "HIGH 80", 34, 596, (255, 207, 96), 1)
    # Scale the designed card to the formal 1080x1920 production raster without
    # using a platform image library, then add native-raster one-pixel details.
    formal = bytearray(WIDTH * HEIGHT * 3)
    for source_y in range(BASE_HEIGHT):
        source_row = memoryview(buffer)[
            source_y * BASE_WIDTH * 3 : (source_y + 1) * BASE_WIDTH * 3
        ]
        expanded = bytearray(WIDTH * 3)
        for source_x in range(BASE_WIDTH):
            source_offset = source_x * 3
            color = source_row[source_offset : source_offset + 3].tobytes()
            destination_offset = source_x * RASTER_SCALE * 3
            expanded[destination_offset : destination_offset + RASTER_SCALE * 3] = (
                color * RASTER_SCALE
            )
        for repeat in range(RASTER_SCALE):
            destination_y = source_y * RASTER_SCALE + repeat
            start = destination_y * WIDTH * 3
            formal[start : start + WIDTH * 3] = expanded

    native_x = 9 + (index * 17) % (WIDTH - 18)
    native_y = 11 + (index * 23) % (HEIGHT - 22)
    for y in range(HEIGHT):
        offset = (y * WIDTH + native_x) * 3
        formal[offset : offset + 3] = b"\xff\xff\xff"
    for x in range(WIDTH):
        offset = (native_y * WIDTH + x) * 3
        formal[offset : offset + 3] = b"\x00\xe5\xff"
    checker_x, checker_y = WIDTH - 196, HEIGHT - 196
    for y in range(checker_y, checker_y + 128):
        for x in range(checker_x, checker_x + 128):
            value = 245 if ((x + y + index) & 1) else 8
            offset = (y * WIDTH + x) * 3
            formal[offset : offset + 3] = bytes((value, value, value))
    return bytes(formal)


def build_source_frames() -> tuple[list[bytes], dict[str, Any]]:
    sequence_digest = hashlib.sha256()
    frame_hashes: list[str] = []
    frames: list[bytes] = []
    for index in range(FRAME_COUNT):
        frame = render_frame(index)
        frames.append(frame)
        sequence_digest.update(frame)
        frame_hashes.append(sha256_bytes(frame))
    return frames, {
        "transport": "generated_in_memory_and_streamed_via_pipe_0",
        "bytes": sum(len(frame) for frame in frames),
        "sha256": sequence_digest.hexdigest().upper(),
        "frame_sha256": frame_hashes,
    }


def normalized_tokens(tokens: Iterable[str], executable: Path) -> list[str]:
    values = [str(token) for token in tokens]
    if values and Path(values[0]).resolve() == executable.resolve():
        values[0] = "{ffmpeg}" if executable == FFMPEG else "{ffprobe}"
    if values and values[-1] == os.devnull:
        values[-1] = "{null_device}"
    return values


def run_command(tokens: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        tokens,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise EvidenceError(
            f"command failed ({completed.returncode}): {tokens!r}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def run_frame_command(
    tokens: list[str], cwd: Path, frames: Iterable[bytes]
) -> subprocess.CompletedProcess[str]:
    """Stream fixed RGB frames to FFmpeg without writing rawvideo to disk."""
    process = subprocess.Popen(
        tokens,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    try:
        for frame in frames:
            process.stdin.write(frame)
        process.stdin.close()
        process.stdin = None
        stdout_bytes, stderr_bytes = process.communicate()
    except BaseException:
        process.kill()
        process.wait()
        raise
    completed = subprocess.CompletedProcess(
        tokens,
        process.returncode,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )
    if completed.returncode != 0:
        raise EvidenceError(
            f"streaming command failed ({completed.returncode}): {tokens!r}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def encode_tokens(quality: int, output_name: str) -> list[str]:
    return [
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-y",
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgb24",
        "-video_size",
        f"{WIDTH}x{HEIGHT}",
        "-framerate",
        str(FPS),
        "-i",
        "pipe:0",
        "-an",
        "-frames:v",
        str(FRAME_COUNT),
        "-c:v",
        "h264_mf",
        "-rate_control",
        "quality",
        "-quality",
        str(quality),
        "-scenario",
        "archive",
        "-hw_encoding",
        "0",
        "-g",
        str(GOP),
        "-keyint_min",
        str(GOP),
        "-force_key_frames",
        f"expr:eq(mod(n,{GOP}),0)",
        "-flags",
        "+cgop",
        "-bf",
        "0",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(FPS),
        "-map_metadata",
        "-1",
        "-movflags",
        "+faststart",
        output_name,
    ]


def probe_tokens(output_name: str) -> list[str]:
    return [
        str(FFPROBE),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        (
            "stream=index,codec_name,codec_long_name,profile,pix_fmt,width,height,"
            "r_frame_rate,avg_frame_rate,nb_frames,duration,bit_rate:"
            "format=duration,size,bit_rate,format_name,format_long_name"
        ),
        "-of",
        "json",
        output_name,
    ]


def keyframe_tokens(output_name: str) -> list[str]:
    return [
        str(FFPROBE),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-skip_frame",
        "nokey",
        "-show_frames",
        "-show_entries",
        (
            "frame=key_frame,pict_type,pts,pts_time,best_effort_timestamp,"
            "best_effort_timestamp_time,pkt_dts,pkt_dts_time"
        ),
        "-of",
        "json",
        output_name,
    ]


def psnr_tokens(output_name: str, stats_name: str) -> list[str]:
    return [
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-y",
        "-i",
        output_name,
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgb24",
        "-video_size",
        f"{WIDTH}x{HEIGHT}",
        "-framerate",
        str(FPS),
        "-i",
        "pipe:0",
        "-lavfi",
        (
            "[0:v]setpts=PTS-STARTPTS,format=yuv420p[dist];"
            "[1:v]setpts=PTS-STARTPTS,format=yuv420p[ref];"
            f"[dist][ref]psnr=stats_file={stats_name}"
        ),
        "-frames:v",
        str(FRAME_COUNT),
        "-f",
        "null",
        os.devnull,
    ]


def comparison_tokens(output_name: str) -> list[str]:
    tile_width = WIDTH // 2
    tile_height = HEIGHT // 2
    layout = f"0_0|{tile_width}_0|0_{tile_height}|{tile_width}_{tile_height}"
    filters = []
    labels = ("reference", "q60", "q72", "q80")
    for index, label in enumerate(labels):
        filters.append(
            f"[{index}:v]scale={tile_width}:{tile_height}:flags=lanczos,"
            f"setsar=1[{label}]"
        )
    filters.append(
        "[reference][q60][q72][q80]"
        f"xstack=inputs=4:layout={layout}:fill=black[comparison]"
    )
    return [
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-y",
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgb24",
        "-video_size",
        f"{WIDTH}x{HEIGHT}",
        "-framerate",
        "1",
        "-i",
        "pipe:0",
        "-ss",
        "1.000000",
        "-i",
        "q60-draft.mp4",
        "-ss",
        "1.000000",
        "-i",
        "q72-standard.mp4",
        "-ss",
        "1.000000",
        "-i",
        "q80-high.mp4",
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[comparison]",
        "-frames:v",
        "1",
        "-c:v",
        "png",
        "-pred",
        "mixed",
        "-compression_level",
        "9",
        "-f",
        "image2",
        output_name,
    ]


def parse_psnr(stderr: str) -> dict[str, float]:
    matches = re.findall(
        r"PSNR y:([0-9.]+) u:([0-9.]+) v:([0-9.]+) "
        r"average:([0-9.]+) min:([0-9.]+) max:([0-9.]+)",
        stderr,
    )
    if not matches:
        raise EvidenceError("FFmpeg PSNR summary was not found")
    y, u, v, average, minimum, maximum = matches[-1]
    return {
        "y": float(y),
        "u": float(u),
        "v": float(v),
        "average": float(average),
        "minimum": float(minimum),
        "maximum": float(maximum),
    }


def capture_text_command(tokens: list[str], cwd: Path) -> dict[str, Any]:
    completed = run_command(tokens, cwd)
    return {
        "command_tokens": normalized_tokens(tokens, Path(tokens[0])),
        "exit_code": completed.returncode,
        "raw_stdout": completed.stdout,
        "stdout_sha256": sha256_bytes(completed.stdout.encode("utf-8")),
        "raw_stderr": completed.stderr,
        "stderr_sha256": sha256_bytes(completed.stderr.encode("utf-8")),
    }


def capture_windows_identity() -> dict[str, Any]:
    if os.name != "nt":
        raise EvidenceError("h264_mf quality evidence can only be generated on Windows")
    try:
        import winreg

        key_path = f"CLSID\\{MF_H264_ENCODER_CLSID}\\InprocServer32"
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key_path) as key:
            encoder_path_value, _ = winreg.QueryValueEx(key, "")
    except (OSError, ImportError) as exc:
        raise EvidenceError(f"Media Foundation H.264 encoder registration missing: {exc}") from exc

    encoder_path = Path(os.path.expandvars(str(encoder_path_value))).resolve()
    if not encoder_path.is_file():
        raise EvidenceError(f"Media Foundation H.264 encoder file missing: {encoder_path}")
    escaped = str(encoder_path).replace("'", "''")
    powershell = (
        "$ErrorActionPreference='Stop';"
        "$os=Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion';"
        f"$file=Get-Item -LiteralPath '{escaped}';"
        f"$sig=Get-AuthenticodeSignature -LiteralPath '{escaped}';"
        "[ordered]@{"
        "product_name=$os.ProductName;display_version=$os.DisplayVersion;"
        "current_build=$os.CurrentBuild;ubr=$os.UBR;"
        "file_version=$file.VersionInfo.FileVersion;"
        "product_version=$file.VersionInfo.ProductVersion;"
        "file_description=$file.VersionInfo.FileDescription;"
        "company_name=$file.VersionInfo.CompanyName;"
        "original_filename=$file.VersionInfo.OriginalFilename;"
        "signature_status=[string]$sig.Status;"
        "signer_subject=if($sig.SignerCertificate){$sig.SignerCertificate.Subject}else{$null}"
        "}|ConvertTo-Json -Compress"
    )
    identity_command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        powershell,
    ]
    completed = run_command(identity_command, REPO_ROOT)
    try:
        properties = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"invalid PowerShell identity JSON: {completed.stdout!r}") from exc
    return {
        "os": {
            "platform_version": platform.version(),
            "architecture": platform.machine(),
            "product_name": properties["product_name"],
            "display_version": properties["display_version"],
            "current_build": str(properties["current_build"]),
            "update_build_revision": int(properties["ubr"]),
        },
        "media_foundation_software_encoder_registration": {
            "clsid": MF_H264_ENCODER_CLSID,
            "inproc_server": str(encoder_path),
            "bytes": encoder_path.stat().st_size,
            "sha256": sha256_file(encoder_path),
            "file_version": properties["file_version"],
            "product_version": properties["product_version"],
            "file_description": properties["file_description"],
            "company_name": properties["company_name"],
            "original_filename": properties["original_filename"],
            "signature_status": properties["signature_status"],
            "signer_subject": properties["signer_subject"],
            "attestation_scope": (
                "The encode command fixes -hw_encoding 0. FFmpeg does not expose the "
                "selected MFT CLSID in its log, so this freezes the registered Microsoft "
                "software H.264 encoder implementation, not a direct per-process CLSID trace."
            ),
        },
    }


def capture_runtime_identity() -> dict[str, Any]:
    lock = json.loads(FFMPEG_LOCK.read_text(encoding="utf-8"))
    actual_files = []
    for frozen in lock["runtime"]["files"]:
        path = FFMPEG_DIR / frozen["name"]
        actual = file_record(path)
        if actual["bytes"] != frozen["bytes"] or actual["sha256"].lower() != frozen[
            "sha256"
        ].lower():
            raise EvidenceError(f"FFmpeg runtime does not match lock: {path}")
        actual_files.append(actual)
    version = capture_text_command([str(FFMPEG), "-version"], REPO_ROOT)
    encoder_help = capture_text_command(
        [str(FFMPEG), "-hide_banner", "-h", "encoder=h264_mf"], REPO_ROOT
    )
    if "Encoder h264_mf" not in version["raw_stdout"] + encoder_help["raw_stdout"] + encoder_help[
        "raw_stderr"
    ]:
        raise EvidenceError("tracked FFmpeg does not expose h264_mf")
    return {
        "ffmpeg_lock": {
            "path": "third_party/ffmpeg/upstream-lock.json",
            "bytes": FFMPEG_LOCK.stat().st_size,
            "sha256": sha256_file(FFMPEG_LOCK),
            "ffmpeg_version": lock["build"]["ffmpeg_version"],
            "ffmpeg_commit": lock["build"]["ffmpeg_commit"],
            "license": lock["license"],
        },
        "runtime_files": actual_files,
        "version_evidence": version,
        "h264_mf_encoder_help_evidence": encoder_help,
    }


def capture_font_audit() -> dict[str, Any]:
    files = [file_record(REPO_ROOT / relative, relative_to=REPO_ROOT) for relative in FONT_PATHS]
    return {
        "files": files,
        "used_by_input_generator": False,
        "reason": (
            "The fixed canary uses an embedded 5x7 bitmap alphabet, avoiding OS/font "
            "rasterizer drift. Tracked Noto Sans SC and its license/source records are "
            "hashed here to document that they were audited but are not hidden inputs."
        ),
    }


def generate_candidate(
    tier: str, quality: int, artifact_root: Path, frames: list[bytes]
) -> dict[str, Any]:
    output_name = f"q{quality}-{tier}.mp4"
    repeat_name = f"repeat-q{quality}-{tier}.mp4"
    encode_log_name = f"encode-q{quality}.stderr.txt"
    repeat_log_name = f"repeat-q{quality}.stderr.txt"
    stats_name = f"psnr-q{quality}.stats.txt"
    psnr_log_name = f"psnr-q{quality}.stderr.txt"
    probe_name = f"probe-q{quality}.json"
    keyframe_name = f"keyframes-q{quality}.json"

    encode_command = encode_tokens(quality, output_name)
    encoded = run_frame_command(encode_command, artifact_root, frames)
    (artifact_root / encode_log_name).write_text(encoded.stderr, encoding="utf-8", newline="\n")
    output = artifact_root / output_name

    repeat_command = encode_tokens(quality, repeat_name)
    repeated = run_frame_command(repeat_command, artifact_root, frames)
    (artifact_root / repeat_log_name).write_text(repeated.stderr, encoding="utf-8", newline="\n")
    repeated_output = artifact_root / repeat_name
    output_sha = sha256_file(output)
    repeat_sha = sha256_file(repeated_output)
    if output_sha != repeat_sha or output.stat().st_size != repeated_output.stat().st_size:
        raise EvidenceError(
            f"h264_mf output is not byte-repeatable for quality {quality}: "
            f"{output_sha} != {repeat_sha}"
        )
    repeated_output.unlink()

    probe_command = probe_tokens(output_name)
    probed = run_command(probe_command, artifact_root)
    (artifact_root / probe_name).write_text(probed.stdout, encoding="utf-8", newline="\n")
    probe_json = json.loads(probed.stdout)
    streams = probe_json.get("streams", [])
    if len(streams) != 1:
        raise EvidenceError(f"unexpected probe stream count for {output_name}: {len(streams)}")
    stream = streams[0]
    required_probe = {
        "codec_name": "h264",
        "pix_fmt": "yuv420p",
        "width": WIDTH,
        "height": HEIGHT,
        "r_frame_rate": f"{FPS}/1",
        "nb_frames": str(FRAME_COUNT),
    }
    for key, expected in required_probe.items():
        if stream.get(key) != expected:
            raise EvidenceError(
                f"probe mismatch for {output_name} {key}: {stream.get(key)!r} != {expected!r}"
            )

    keyframe_command = keyframe_tokens(output_name)
    keyframed = run_command(keyframe_command, artifact_root)
    (artifact_root / keyframe_name).write_text(
        keyframed.stdout, encoding="utf-8", newline="\n"
    )
    keyframe_json = json.loads(keyframed.stdout)
    keyframes = keyframe_json.get("frames", [])
    if len(keyframes) != FRAME_COUNT // GOP:
        raise EvidenceError(
            f"unexpected keyframe count for {output_name}: {len(keyframes)}"
        )

    psnr_command = psnr_tokens(output_name, stats_name)
    measured = run_frame_command(psnr_command, artifact_root, frames)
    (artifact_root / psnr_log_name).write_text(
        measured.stderr, encoding="utf-8", newline="\n"
    )
    stats_text = (artifact_root / stats_name).read_text(encoding="utf-8")
    stats_lines = stats_text.splitlines()
    if len(stats_lines) != FRAME_COUNT:
        raise EvidenceError(
            f"unexpected PSNR frame count for {output_name}: {len(stats_lines)}"
        )

    return {
        "tier": tier,
        "selected_tier": tier,
        "quality": quality,
        "output": file_record(output, relative_to=artifact_root),
        "repeatability": {
            "status": "byte_identical_on_same_runtime_and_os_identity",
            "repeat_sha256": repeat_sha,
            "repeat_bytes": output.stat().st_size,
            "command_tokens": normalized_tokens(repeat_command, FFMPEG),
            "stderr_path": repeat_log_name,
            "stderr_sha256": sha256_file(artifact_root / repeat_log_name),
        },
        "encode": {
            "command_tokens": normalized_tokens(encode_command, FFMPEG),
            "exit_code": encoded.returncode,
            "raw_stderr": encoded.stderr,
            "stderr_sha256": sha256_bytes(encoded.stderr.encode("utf-8")),
            "stderr_path": encode_log_name,
        },
        "probe": {
            "command_tokens": normalized_tokens(probe_command, FFPROBE),
            "exit_code": probed.returncode,
            "raw_stdout": probed.stdout,
            "stdout_sha256": sha256_bytes(probed.stdout.encode("utf-8")),
            "raw_json": probe_json,
            "artifact_path": probe_name,
        },
        "keyframes": {
            "command_tokens": normalized_tokens(keyframe_command, FFPROBE),
            "exit_code": keyframed.returncode,
            "raw_stdout": keyframed.stdout,
            "stdout_sha256": sha256_bytes(keyframed.stdout.encode("utf-8")),
            "raw_json": keyframe_json,
            "count": len(keyframes),
            "artifact_path": keyframe_name,
        },
        "psnr": {
            "command_tokens": normalized_tokens(psnr_command, FFMPEG),
            "exit_code": measured.returncode,
            "raw_stderr": measured.stderr,
            "stderr_sha256": sha256_bytes(measured.stderr.encode("utf-8")),
            "summary": parse_psnr(measured.stderr),
            "raw_stats": stats_text,
            "stats_sha256": sha256_bytes(stats_text.encode("utf-8")),
            "stats_frame_count": len(stats_lines),
            "stats_artifact_path": stats_name,
            "stderr_artifact_path": psnr_log_name,
        },
    }


def generate_comparison(artifact_root: Path, frames: list[bytes]) -> dict[str, Any]:
    output_name = "comparison-2x2-reference-q60-q72-q80.png"
    repeat_name = "repeat-comparison.png"
    log_name = "comparison.stderr.txt"
    repeat_log_name = "repeat-comparison.stderr.txt"
    source_frame_index = FPS
    source_frame = frames[source_frame_index]

    command = comparison_tokens(output_name)
    completed = run_frame_command(command, artifact_root, [source_frame])
    (artifact_root / log_name).write_text(
        completed.stderr, encoding="utf-8", newline="\n"
    )
    output = artifact_root / output_name

    repeat_command = comparison_tokens(repeat_name)
    repeated = run_frame_command(repeat_command, artifact_root, [source_frame])
    (artifact_root / repeat_log_name).write_text(
        repeated.stderr, encoding="utf-8", newline="\n"
    )
    repeated_output = artifact_root / repeat_name
    output_sha = sha256_file(output)
    repeat_sha = sha256_file(repeated_output)
    if output_sha != repeat_sha or output.stat().st_size != repeated_output.stat().st_size:
        raise EvidenceError(
            f"comparison PNG is not byte-repeatable: {output_sha} != {repeat_sha}"
        )
    repeated_output.unlink()
    return {
        "source_frame_index": source_frame_index,
        "source_time_seconds": source_frame_index / FPS,
        "source_frame_sha256": sha256_bytes(source_frame),
        "tile_layout": {
            "canvas": {"width": WIDTH, "height": HEIGHT},
            "tile": {"width": WIDTH // 2, "height": HEIGHT // 2},
            "order": ["reference", "quality_60", "quality_72", "quality_80"],
            "scaler": "lanczos",
        },
        "output": file_record(output, relative_to=artifact_root),
        "command_tokens": normalized_tokens(command, FFMPEG),
        "exit_code": completed.returncode,
        "raw_stderr": completed.stderr,
        "stderr_sha256": sha256_bytes(completed.stderr.encode("utf-8")),
        "stderr_artifact_path": log_name,
        "repeatability": {
            "status": "byte_identical_on_same_runtime_and_os_identity",
            "repeat_sha256": repeat_sha,
            "command_tokens": normalized_tokens(repeat_command, FFMPEG),
            "stderr_path": repeat_log_name,
            "stderr_sha256": sha256_file(artifact_root / repeat_log_name),
        },
    }


def artifact_records(artifact_root: Path) -> list[dict[str, Any]]:
    ignored = {"artifact-manifest.json", "repository-evidence.generated.json"}
    return [
        file_record(path, relative_to=artifact_root)
        for path in sorted(artifact_root.iterdir(), key=lambda item: item.name.lower())
        if path.is_file() and path.name not in ignored
    ]


def generate_evidence(manifest_path: Path, artifact_root: Path) -> dict[str, Any]:
    artifact_root.mkdir(parents=True, exist_ok=True)
    stale_raw_source = artifact_root / "source.rgb"
    if stale_raw_source.exists():
        stale_raw_source.unlink()
    frames, source = build_source_frames()
    candidates = [
        generate_candidate(tier, quality, artifact_root, frames)
        for tier, quality in TIERS
    ]
    comparison = generate_comparison(artifact_root, frames)
    averages = [candidate["psnr"]["summary"]["average"] for candidate in candidates]
    sizes = [candidate["output"]["bytes"] for candidate in candidates]
    if averages != sorted(averages) or sizes != sorted(sizes):
        raise EvidenceError(
            f"quality evidence is not monotonic: PSNR={averages!r}, sizes={sizes!r}"
        )

    files = artifact_records(artifact_root)
    artifact_index = {
        "schema_version": 1,
        "evidence_id": EVIDENCE_ID,
        "files": files,
    }
    index_path = artifact_root / "artifact-manifest.json"
    atomic_write_json(index_path, artifact_index)

    script_path = Path(__file__).resolve()
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "evidence_id": EVIDENCE_ID,
        "evaluated_on": "2026-08-10",
        "generator": {
            "path": "tools/generate_hyperframes_mf_quality_evidence.py",
            "bytes": script_path.stat().st_size,
            "sha256": sha256_file(script_path),
            "python": platform.python_version(),
            "input_dependencies": ["Python standard library", "this exact generator script"],
        },
        "runtime": capture_runtime_identity(),
        "environment": capture_windows_identity(),
        "font_audit": capture_font_audit(),
        "source": {
            "description": (
                "Two-second formal 1080x1920 vertical deterministic motion-design canary "
                "with integer-only gradient animation, native-raster one-pixel lines and "
                "checkerboard, moving geometry, color ramp, and an embedded bitmap label."
            ),
            "width": WIDTH,
            "height": HEIGHT,
            "pixel_format": "rgb24",
            "fps": FPS,
            "duration_seconds": FRAME_COUNT / FPS,
            "frame_count": FRAME_COUNT,
            **source,
        },
        "fixed_command_contract": {
            "encoder": "h264_mf",
            "rate_control": "quality",
            "scenario": "archive",
            "hw_encoding": 0,
            "gop": GOP,
            "keyint_min": GOP,
            "force_key_frames": f"expr:eq(mod(n,{GOP}),0)",
            "closed_gop": True,
            "b_frames": 0,
            "pixel_format": "yuv420p",
            "profile_override": None,
            "preset_argument": None,
        },
        "candidates": candidates,
        "comparison": comparison,
        "decision": {
            "quality_tiers": {tier: quality for tier, quality in TIERS},
            "measurement_scope": (
                "Byte size, decoded PSNR, stream metadata, and forced-keyframe behavior; "
                "no unrecorded visual-review claim is used."
            ),
            "reason": (
                "The frozen 60/72/80 mapping produces strictly increasing byte size and "
                "decoded PSNR on the deterministic detail canary while preserving a "
                "distinct middle tier."
            ),
        },
        "external_artifacts": {
            "root_relative_to_workspace": DEFAULT_ARTIFACT_RELATIVE,
            "index": file_record(index_path, relative_to=artifact_root),
            "files": files,
            "repository_bloat_policy": (
                "Large RGB/video/log artifacts remain under 08_产出与验收; only this "
                "reproducibility manifest and generator are stored in the repository."
            ),
        },
    }
    atomic_write_json(manifest_path, evidence)
    return evidence


def verify_static(manifest_path: Path) -> dict[str, Any]:
    evidence = json.loads(manifest_path.read_text(encoding="utf-8"))
    if evidence.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceError("quality evidence schema mismatch")
    if evidence.get("evidence_id") != EVIDENCE_ID:
        raise EvidenceError("quality evidence identity mismatch")

    script = evidence["generator"]
    script_path = REPO_ROOT / script["path"]
    if file_record(script_path) != {
        "path": script_path.name,
        "bytes": script["bytes"],
        "sha256": script["sha256"],
    }:
        raise EvidenceError("generator script identity mismatch")

    lock = evidence["runtime"]["ffmpeg_lock"]
    if FFMPEG_LOCK.stat().st_size != lock["bytes"] or sha256_file(FFMPEG_LOCK) != lock[
        "sha256"
    ]:
        raise EvidenceError("FFmpeg lock identity mismatch")
    for record in evidence["runtime"]["runtime_files"]:
        path = FFMPEG_DIR / record["path"]
        if file_record(path) != record:
            raise EvidenceError(f"FFmpeg runtime identity mismatch: {path}")
    for record in evidence["font_audit"]["files"]:
        path = REPO_ROOT / record["path"]
        if file_record(path, relative_to=REPO_ROOT) != record:
            raise EvidenceError(f"font audit identity mismatch: {path}")

    source_digest = hashlib.sha256()
    frame_hashes = []
    for index in range(FRAME_COUNT):
        frame = render_frame(index)
        source_digest.update(frame)
        frame_hashes.append(sha256_bytes(frame))
    if source_digest.hexdigest().upper() != evidence["source"]["sha256"]:
        raise EvidenceError("deterministic source SHA-256 mismatch")
    if frame_hashes != evidence["source"]["frame_sha256"]:
        raise EvidenceError("deterministic frame SHA-256 list mismatch")

    for candidate, (tier, quality) in zip(evidence["candidates"], TIERS, strict=True):
        if candidate["tier"] != tier or candidate["quality"] != quality:
            raise EvidenceError("quality tier order mismatch")
        if candidate["encode"]["command_tokens"] != normalized_tokens(
            encode_tokens(quality, candidate["output"]["path"]), FFMPEG
        ):
            raise EvidenceError(f"encode command mismatch for quality {quality}")
        if sha256_bytes(candidate["encode"]["raw_stderr"].encode("utf-8")) != candidate[
            "encode"
        ]["stderr_sha256"]:
            raise EvidenceError(f"encode stderr hash mismatch for quality {quality}")
        for section in ("probe", "keyframes"):
            raw = candidate[section]["raw_stdout"]
            if sha256_bytes(raw.encode("utf-8")) != candidate[section]["stdout_sha256"]:
                raise EvidenceError(f"{section} raw output hash mismatch for quality {quality}")
            if json.loads(raw) != candidate[section]["raw_json"]:
                raise EvidenceError(f"{section} raw JSON mismatch for quality {quality}")
        psnr = candidate["psnr"]
        if sha256_bytes(psnr["raw_stderr"].encode("utf-8")) != psnr["stderr_sha256"]:
            raise EvidenceError(f"PSNR stderr hash mismatch for quality {quality}")
        if sha256_bytes(psnr["raw_stats"].encode("utf-8")) != psnr["stats_sha256"]:
            raise EvidenceError(f"PSNR stats hash mismatch for quality {quality}")
        if len(psnr["raw_stats"].splitlines()) != FRAME_COUNT:
            raise EvidenceError(f"PSNR frame evidence mismatch for quality {quality}")

    comparison = evidence["comparison"]
    if comparison["command_tokens"] != normalized_tokens(
        comparison_tokens(comparison["output"]["path"]), FFMPEG
    ):
        raise EvidenceError("comparison command mismatch")
    if comparison["source_frame_sha256"] != evidence["source"]["frame_sha256"][FPS]:
        raise EvidenceError("comparison source frame identity mismatch")
    if sha256_bytes(comparison["raw_stderr"].encode("utf-8")) != comparison[
        "stderr_sha256"
    ]:
        raise EvidenceError("comparison stderr hash mismatch")

    averages = [item["psnr"]["summary"]["average"] for item in evidence["candidates"]]
    sizes = [item["output"]["bytes"] for item in evidence["candidates"]]
    if averages != sorted(averages) or sizes != sorted(sizes):
        raise EvidenceError("quality measurements are not monotonic")
    return evidence


def verify_artifacts(evidence: dict[str, Any], artifact_root: Path) -> None:
    index = evidence["external_artifacts"]["index"]
    index_path = artifact_root / index["path"]
    if file_record(index_path, relative_to=artifact_root) != index:
        raise EvidenceError("external artifact index mismatch")
    external_index = json.loads(index_path.read_text(encoding="utf-8"))
    if external_index["files"] != evidence["external_artifacts"]["files"]:
        raise EvidenceError("external artifact index content mismatch")
    for record in evidence["external_artifacts"]["files"]:
        path = artifact_root / record["path"]
        if file_record(path, relative_to=artifact_root) != record:
            raise EvidenceError(f"external artifact mismatch: {path}")


def reproduce(evidence: dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix="shiyi-mf-quality-") as temporary:
        root = Path(temporary)
        frames, source = build_source_frames()
        if source["sha256"] != evidence["source"]["sha256"]:
            raise EvidenceError("reproduced source hash mismatch")
        for candidate in evidence["candidates"]:
            tier = candidate["tier"]
            quality = candidate["quality"]
            reproduced = generate_candidate(tier, quality, root, frames)
            if reproduced["output"] != candidate["output"]:
                raise EvidenceError(f"reproduced output mismatch for quality {quality}")
            if reproduced["probe"]["raw_json"] != candidate["probe"]["raw_json"]:
                raise EvidenceError(f"reproduced probe mismatch for quality {quality}")
            if reproduced["keyframes"]["raw_json"] != candidate["keyframes"]["raw_json"]:
                raise EvidenceError(f"reproduced keyframe mismatch for quality {quality}")
            if reproduced["psnr"]["summary"] != candidate["psnr"]["summary"]:
                raise EvidenceError(f"reproduced PSNR mismatch for quality {quality}")
        comparison = generate_comparison(root, frames)
        if comparison["output"] != evidence["comparison"]["output"]:
            raise EvidenceError("reproduced comparison image mismatch")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate_parser = subparsers.add_parser("generate")
    generate_parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    generate_parser.add_argument(
        "--artifact-root", type=Path, default=WORKSPACE_ROOT / DEFAULT_ARTIFACT_RELATIVE
    )
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    verify_parser.add_argument("--artifact-root", type=Path)
    verify_parser.add_argument("--reproduce", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "generate":
        evidence = generate_evidence(args.manifest.resolve(), args.artifact_root.resolve())
        print(
            json.dumps(
                {
                    "status": "generated",
                    "evidence_id": evidence["evidence_id"],
                    "manifest": str(args.manifest.resolve()),
                    "artifact_root": str(args.artifact_root.resolve()),
                },
                ensure_ascii=False,
            )
        )
        return 0
    evidence = verify_static(args.manifest.resolve())
    artifact_root = args.artifact_root
    if artifact_root is None:
        artifact_root = WORKSPACE_ROOT / evidence["external_artifacts"][
            "root_relative_to_workspace"
        ]
    verify_artifacts(evidence, artifact_root.resolve())
    if args.reproduce:
        reproduce(evidence)
    print(
        json.dumps(
            {
                "status": "verified",
                "evidence_id": evidence["evidence_id"],
                "reproduced": bool(args.reproduce),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvidenceError as exc:
        print(f"quality evidence error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
