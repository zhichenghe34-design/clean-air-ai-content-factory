from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import wave
from array import array
from collections.abc import Mapping
from pathlib import Path
from typing import Any


DEFAULT_VOICE_ENGINE = "edge_tts"
DEFAULT_VOICE_NAME = "zh-CN-YunxiNeural"
DEFAULT_VOICE_LABEL = "普通中文播报"
# Fixed product pacing.  The released -15% profile was judged too slow in the
# unattended product sample.  -2% is approximately 1.15x that profile while
# retaining the same neutral Yunxi broadcast voice.  It remains a product
# constant rather than a user-facing voice customization option.
DEFAULT_VOICE_RATE = "-2%"
DEFAULT_VOICE_CHUNK_MAX_CHARS = 90
VOICE_CHUNK_MIN_CHARS = 48
VOICE_CHUNK_MAX_CHARS = 140

# The final PCM gate remains authoritative because Edge prosody varies with
# text.  This deterministic estimate exists so both local and Provider
# storyboards can reject or split an over-dense caption before making a TTS
# request.  The duration scale compares the old verified -15% profile with the
# new -2% profile; the fixed allowance models leading/trailing segment prosody
# observed in the packaged Yunxi runtime.
VOICE_REFERENCE_RATE_SCALE = 0.85 / 0.98
VOICE_SCENE_FIXED_ALLOWANCE_SECONDS = 0.75
VOICE_SCENE_MAX_DELIVERED_CHARACTERS_PER_SECOND = 4.05
VOICE_SEGMENT_CONTRACT_VERSION = 1
VOICE_SCENE_PAUSE_SECONDS = 0.35
# A normal Edge neural-voice PCM signal is orders of magnitude above this.
# Eight s16 samples are about -72 dBFS, so this rejects empty/near-empty WAVs
# without mistaking natural leading, trailing, or inter-sentence pauses for a
# failed synthesis.
VOICE_PCM_MIN_RMS = 8.0
VOICE_PCM_SCAN_FRAMES = 64 * 1024


def estimate_voice_scene_pacing(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    spoken = len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", text))
    short_pauses = len(re.findall(r"[，、；：,;:]", text))
    long_pauses = len(re.findall(r"[。！？!?]", text))
    reference_seconds = spoken / 4.2 + short_pauses * 0.16 + long_pauses * 0.38
    estimated_seconds = reference_seconds * VOICE_REFERENCE_RATE_SCALE
    if spoken:
        estimated_seconds += VOICE_SCENE_FIXED_ALLOWANCE_SECONDS
    delivered_rate = spoken / max(estimated_seconds, 0.001)
    return {
        "spoken_characters": spoken,
        "estimated_seconds": round(estimated_seconds, 3),
        "spoken_characters_per_second": round(delivered_rate, 3),
        "maximum_spoken_characters_per_second": VOICE_SCENE_MAX_DELIVERED_CHARACTERS_PER_SECOND,
        "blocked": bool(spoken and delivered_rate > VOICE_SCENE_MAX_DELIVERED_CHARACTERS_PER_SECOND),
        "voice_rate": DEFAULT_VOICE_RATE,
    }


def voice_segments_digest(segments: Any) -> str:
    captions = [
        str(item.get("caption") or "").strip()
        for item in (segments if isinstance(segments, list) else [])
        if isinstance(item, Mapping)
    ]
    payload = json.dumps(captions, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest().upper()


def _stream_s16le_signal_metrics(audio: Any) -> tuple[int, float, int]:
    """Read PCM samples in bounded chunks and return peak, RMS, sample count."""

    peak = 0
    squared_sum = 0
    sample_count = 0
    while payload := audio.readframes(VOICE_PCM_SCAN_FRAMES):
        samples = array("h")
        samples.frombytes(payload)
        if sys.byteorder != "little":
            samples.byteswap()
        if not samples:
            continue
        peak = max(peak, max(abs(sample) for sample in samples))
        squared_sum += sum(sample * sample for sample in samples)
        sample_count += len(samples)
    rms = math.sqrt(squared_sum / sample_count) if sample_count else 0.0
    return peak, rms, sample_count


def fixed_voice_delivery_violations(
    identity: Any,
    *,
    script: str,
    voice_path: str | Path,
    motion_plan: Any,
) -> list[str]:
    """Validate the immutable motion-delivery voice identity from one SSOT."""

    if not isinstance(identity, Mapping):
        return ["missing_fixed_voice_identity"]
    violations: list[str] = []
    expected = {
        "schema_version": 3,
        "engine": DEFAULT_VOICE_ENGINE,
        "requested_engine": DEFAULT_VOICE_ENGINE,
        "voice": DEFAULT_VOICE_NAME,
        "voice_rate": DEFAULT_VOICE_RATE,
        "voice_selection_exposed": False,
        "segment_contract_version": VOICE_SEGMENT_CONTRACT_VERSION,
        "segment_aligned": True,
        "fallback": False,
        "natural_voice": True,
        "quality_eligible": True,
        "tempo_adjusted": False,
        "duration_source": "scene_voice_segments",
        "pacing_status": "passed",
        "maximum_spoken_characters_per_second": VOICE_SCENE_MAX_DELIVERED_CHARACTERS_PER_SECOND,
    }
    for field, value in expected.items():
        if identity.get(field) != value:
            violations.append(f"invalid_{field}")
    chunk_max_chars = identity.get("voice_chunk_max_chars")
    try:
        normalized_chunk_max_chars = normalize_voice_chunk_max_chars(chunk_max_chars)
    except (TypeError, ValueError):
        normalized_chunk_max_chars = None
    if not isinstance(chunk_max_chars, int) or normalized_chunk_max_chars != chunk_max_chars:
        violations.append("invalid_voice_chunk_max_chars")
    expected_script_sha256 = hashlib.sha256(str(script).encode("utf-8")).hexdigest().upper()
    if str(identity.get("script_sha256", "")).upper() != expected_script_sha256:
        violations.append("script_hash_mismatch")
    resolved_voice_path = Path(voice_path)
    actual_voice_sha256 = ""
    actual_voice_duration = 0.0
    try:
        digest = hashlib.sha256()
        with resolved_voice_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        actual_voice_sha256 = digest.hexdigest().upper()
        with wave.open(str(resolved_voice_path), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            frame_count = audio.getnframes()
            compression = audio.getcomptype()
            valid_wav_contract = not (
                channels != 1
                or sample_width != 2
                or sample_rate != 48000
                or compression != "NONE"
                or frame_count <= 0
                or resolved_voice_path.stat().st_size < frame_count * channels * sample_width + 44
            )
            if valid_wav_contract:
                peak, rms, scanned_samples = _stream_s16le_signal_metrics(audio)
            else:
                peak, rms, scanned_samples = 0, 0.0, 0
        if not valid_wav_contract or scanned_samples != frame_count:
            violations.append("invalid_voice_wav_contract")
        else:
            actual_voice_duration = frame_count / sample_rate
            if peak <= 0 or rms <= VOICE_PCM_MIN_RMS:
                violations.append("silent_or_near_silent_voice_wav")
    except (OSError, EOFError, ValueError, wave.Error):
        violations.append("invalid_voice_wav_contract")
    if str(identity.get("voice_sha256", "")).upper() != actual_voice_sha256:
        violations.append("voice_hash_mismatch")

    try:
        duration = float(identity.get("duration_seconds"))
    except (TypeError, ValueError):
        duration = 0.0
        violations.append("invalid_total_duration")
    else:
        if not math.isfinite(duration) or not 45.0 <= duration <= 60.0:
            violations.append("invalid_total_duration")
        if actual_voice_duration <= 0 or abs(duration - actual_voice_duration) > 0.01:
            violations.append("voice_wav_duration_mismatch")
    expected_overall_spoken = len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", str(script)))
    try:
        reported_overall_spoken = int(identity.get("spoken_characters"))
    except (TypeError, ValueError):
        violations.append("invalid_overall_spoken_characters")
    else:
        if reported_overall_spoken != expected_overall_spoken:
            violations.append("invalid_overall_spoken_characters")
    try:
        overall_rate = float(identity.get("spoken_characters_per_second"))
    except (TypeError, ValueError):
        violations.append("invalid_overall_voice_density")
    else:
        if (
            not math.isfinite(overall_rate)
            or not 0 < overall_rate <= VOICE_SCENE_MAX_DELIVERED_CHARACTERS_PER_SECOND
        ):
            violations.append("invalid_overall_voice_density")
        expected_overall_rate = expected_overall_spoken / max(duration, 0.001)
        if abs(overall_rate - expected_overall_rate) > 0.0015:
            violations.append("overall_voice_density_mismatch")

    scenes = identity.get("scene_segments")
    if not isinstance(scenes, list) or not 4 <= len(scenes) <= 8:
        violations.append("invalid_scene_segments")
        scenes = []
    if identity.get("segment_count") != len(scenes):
        violations.append("segment_count_mismatch")
    if str(identity.get("segments_sha256", "")).upper() != voice_segments_digest(scenes):
        violations.append("segments_hash_mismatch")
    if re.sub(
        r"\s+",
        "",
        "".join(str(item.get("caption") or "") for item in scenes if isinstance(item, Mapping)),
    ) != re.sub(
        r"\s+", "", str(script)
    ):
        violations.append("scene_script_binding_mismatch")

    cursor = 0.0
    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, Mapping) or scene.get("index") != index:
            violations.append("invalid_scene_timeline")
            continue
        try:
            start = float(scene.get("start_seconds"))
            spoken_end = float(scene.get("spoken_end_seconds"))
            end = float(scene.get("end_seconds"))
            spoken_duration = float(scene.get("spoken_duration_seconds"))
            pause_after = float(scene.get("pause_after_seconds"))
            reported_spoken = int(scene.get("spoken_characters"))
            scene_rate = float(scene.get("spoken_characters_per_second"))
        except (TypeError, ValueError):
            violations.append("invalid_scene_timeline")
            continue
        caption = str(scene.get("caption") or "")
        expected_spoken = len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", caption))
        expected_pause = VOICE_SCENE_PAUSE_SECONDS if index < len(scenes) else 0.0
        measured_spoken_duration = spoken_end - start
        expected_scene_rate = expected_spoken / max(measured_spoken_duration, 0.001)
        if (
            not math.isfinite(start)
            or not math.isfinite(spoken_end)
            or not math.isfinite(end)
            or not math.isfinite(spoken_duration)
            or not math.isfinite(pause_after)
            or start < 0
            or spoken_end <= start
            or end < spoken_end
            or abs(start - cursor) > 0.01
            or abs(spoken_duration - measured_spoken_duration) > 0.002
            or abs(pause_after - expected_pause) > 0.002
            or abs((end - spoken_end) - expected_pause) > 0.002
        ):
            violations.append("invalid_scene_timeline")
        if reported_spoken != expected_spoken:
            violations.append("invalid_scene_spoken_characters")
        if (
            not math.isfinite(scene_rate)
            or not 0 < scene_rate <= VOICE_SCENE_MAX_DELIVERED_CHARACTERS_PER_SECOND
        ):
            violations.append("scene_voice_density_exceeded")
        if abs(scene_rate - expected_scene_rate) > 0.0015:
            violations.append("scene_voice_density_mismatch")
        if expected_scene_rate > VOICE_SCENE_MAX_DELIVERED_CHARACTERS_PER_SECOND:
            violations.append("scene_voice_density_exceeded")
        cursor = end
    if scenes and (
        not math.isfinite(cursor)
        or not 45.0 <= cursor <= 60.0
        or abs(cursor - duration) > 0.05
    ):
        violations.append("scene_timeline_duration_mismatch")
    if scenes and (actual_voice_duration <= 0 or abs(cursor - actual_voice_duration) > 0.01):
        violations.append("voice_wav_duration_mismatch")

    plan_scenes = motion_plan.get("scenes") if isinstance(motion_plan, Mapping) else None
    if not isinstance(plan_scenes, list) or len(plan_scenes) != len(scenes):
        violations.append("motion_plan_scene_count_mismatch")
        plan_scenes = []
    for index, (scene, plan_scene) in enumerate(zip(scenes, plan_scenes), start=1):
        if not isinstance(scene, Mapping) or not isinstance(plan_scene, Mapping):
            violations.append("motion_plan_scene_binding_mismatch")
            continue
        caption = str(scene.get("caption") or "")
        text_sha256 = hashlib.sha256(caption.encode("utf-8")).hexdigest().upper()
        try:
            plan_start = float(plan_scene.get("start"))
            plan_end = float(plan_scene.get("end"))
            voice_start = float(scene.get("start_seconds"))
            voice_end = float(scene.get("end_seconds"))
        except (TypeError, ValueError):
            violations.append("motion_plan_scene_binding_mismatch")
            continue
        if (
            not all(math.isfinite(value) for value in (plan_start, plan_end, voice_start, voice_end))
            or plan_start < 0
            or plan_end <= plan_start
            or voice_start < 0
            or voice_end <= voice_start
            or plan_scene.get("id") != scene.get("id")
            or str(plan_scene.get("caption") or "") != caption
            or str(scene.get("text_sha256", "")).upper() != text_sha256
            or abs(plan_start - voice_start) > 0.01
            or abs(plan_end - voice_end) > 0.01
        ):
            violations.append("motion_plan_scene_binding_mismatch")
    return list(dict.fromkeys(violations))

VOICE_ENGINE_ALIASES = {
    "edge_tts": "edge_tts",
    "edge-tts": "edge_tts",
}
WORKBENCH_VOICE_ENGINES = frozenset()
NATURAL_VOICE_ENGINES = frozenset({DEFAULT_VOICE_ENGINE})


def normalize_voice_engine(value: Any) -> str:
    key = str(value or DEFAULT_VOICE_ENGINE).strip().lower()
    normalized = VOICE_ENGINE_ALIASES.get(key)
    if normalized is None:
        raise ValueError("产品只允许固定的普通中文播报声，不提供音色选择")
    return normalized


def normalize_voice_chunk_max_chars(value: Any) -> int:
    if value is None or value == "":
        return DEFAULT_VOICE_CHUNK_MAX_CHARS
    if isinstance(value, bool):
        raise ValueError("voice_chunk_max_chars必须是整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("voice_chunk_max_chars必须是整数") from exc
    if not VOICE_CHUNK_MIN_CHARS <= parsed <= VOICE_CHUNK_MAX_CHARS:
        raise ValueError(
            f"voice_chunk_max_chars必须在{VOICE_CHUNK_MIN_CHARS}到{VOICE_CHUNK_MAX_CHARS}之间"
        )
    return parsed
