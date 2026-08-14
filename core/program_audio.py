from __future__ import annotations

import hashlib
import math
import subprocess
import wave
from pathlib import Path


DEFAULT_SAMPLE_RATE = 48_000
DEFAULT_AUDIO_CONTRACT = "shiyi_generic_narration_with_builtin_bgm_v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _run(command: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(
            f"标准节目音频命令失败({result.returncode})：{' '.join(command)}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result


def synthesize_builtin_bgm(
    output_path: Path,
    *,
    duration_seconds: float,
    variant: int,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> Path:
    """Create a restrained deterministic bed without downloaded music assets."""

    duration = float(duration_seconds)
    if not 1.0 <= duration <= 600.0:
        raise ValueError("背景音乐时长必须在1到600秒之间")
    if sample_rate != DEFAULT_SAMPLE_RATE:
        raise ValueError("正式背景音乐只允许48kHz")
    variant_index = int(variant) % 3
    chord_sets = (
        ((130.81, 164.81, 196.00, 246.94), (110.00, 146.83, 196.00, 220.00)),
        ((110.00, 130.81, 164.81, 196.00), (98.00, 146.83, 196.00, 246.94)),
        ((87.31, 130.81, 174.61, 220.00), (110.00, 164.81, 220.00, 261.63)),
    )
    chords = chord_sets[variant_index]
    frame_count = round(duration * sample_rate)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)
    with wave.open(str(output_path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        block_frames = sample_rate
        for block_start in range(0, frame_count, block_frames):
            block_end = min(frame_count, block_start + block_frames)
            frames = bytearray()
            for frame in range(block_start, block_end):
                t = frame / sample_rate
                chord = chords[int(t // 5) % len(chords)]
                fade = min(1.0, t / 1.2, max(0.0, duration - t) / 1.2)
                slow = 0.86 + 0.14 * math.sin(2 * math.pi * (0.052 + variant_index * 0.004) * t)
                pad_l = sum(
                    math.sin(2 * math.pi * frequency * t + index * 0.37)
                    for index, frequency in enumerate(chord)
                ) / len(chord)
                pad_r = sum(
                    math.sin(2 * math.pi * frequency * t + index * 0.37 + 0.14)
                    for index, frequency in enumerate(chord)
                ) / len(chord)
                beat = int(t / 0.625)
                local_beat = t - beat * 0.625
                note = chord[(beat + variant_index) % len(chord)] * 2
                pluck = math.exp(-7.5 * local_beat) * math.sin(2 * math.pi * note * local_beat)
                left = fade * (0.070 * slow * pad_l + 0.024 * pluck)
                right = fade * (0.070 * slow * pad_r + 0.022 * pluck)
                for value in (left, right):
                    integer = max(-32767, min(32767, round(value * 32767)))
                    frames.extend(int(integer).to_bytes(2, "little", signed=True))
            audio.writeframes(frames)
    if output_path.stat().st_size <= 44:
        raise RuntimeError("内置背景音乐生成失败")
    return output_path


def build_default_program_audio(
    narration_path: Path,
    bgm_path: Path,
    output_path: Path,
    *,
    ffmpeg_path: Path,
    duration_seconds: float,
    script_sha256: str,
) -> dict[str, object]:
    """Mix the sole product narration path with the built-in low-attention bed."""

    if not narration_path.is_file() or narration_path.stat().st_size <= 44:
        raise FileNotFoundError("缺少可用的普通中文播报音频")
    normalized_script_sha = str(script_sha256).strip().upper()
    if len(normalized_script_sha) != 64 or any(value not in "0123456789ABCDEF" for value in normalized_script_sha):
        raise ValueError("script_sha256无效")
    if not ffmpeg_path.is_file():
        raise FileNotFoundError("缺少随包FFmpeg，无法生成节目音频")
    duration = float(duration_seconds)
    variant = int(normalized_script_sha[:8], 16) % 3
    synthesize_builtin_bgm(bgm_path, duration_seconds=duration, variant=variant)
    output_path.unlink(missing_ok=True)
    filters = (
        "[0:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[narration][sidechain];"
        "[1:a]aresample=48000,aformat=channel_layouts=stereo,loudnorm=I=-29:TP=-4:LRA=4[bed];"
        "[bed][sidechain]sidechaincompress=threshold=0.025:ratio=8:attack=15:release=280[ducked];"
        f"[narration][ducked]amix=inputs=2:normalize=0:dropout_transition=0,"
        f"loudnorm=I=-16:TP=-1.2:LRA=7,asetpts=N/SR/TB,"
        f"apad=pad_dur={duration:.6f},atrim=duration={duration:.6f}[out]"
    )
    _run(
        [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(narration_path),
            "-i",
            str(bgm_path),
            "-filter_complex",
            filters,
            "-map",
            "[out]",
            "-ar",
            str(DEFAULT_SAMPLE_RATE),
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
    )
    if not output_path.is_file() or output_path.stat().st_size <= 44:
        raise RuntimeError("标准节目音频生成失败")
    with wave.open(str(output_path), "rb") as audio:
        actual_frames = audio.getnframes()
        actual_rate = audio.getframerate()
        actual_duration = actual_frames / actual_rate
    expected_frames = round(duration * DEFAULT_SAMPLE_RATE)
    if actual_rate != DEFAULT_SAMPLE_RATE or actual_frames != expected_frames:
        raise RuntimeError("标准节目音频未精确覆盖逐镜头旁白时间线")
    return {
        "schema_version": 1,
        "contract": DEFAULT_AUDIO_CONTRACT,
        "narration": "fixed_edge_tts_zh-CN-YunxiNeural",
        "voice_selection_exposed": False,
        "background_music": "deterministic_builtin_synthesis",
        "background_music_external_asset": False,
        "requested_duration_seconds": round(duration, 3),
        "duration_seconds": round(actual_duration, 6),
        "variant": variant,
        "script_sha256": normalized_script_sha,
        "narration_sha256": _sha256(narration_path),
        "bgm_sha256": _sha256(bgm_path),
        "program_audio_sha256": _sha256(output_path),
    }
