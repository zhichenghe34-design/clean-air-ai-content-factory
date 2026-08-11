from __future__ import annotations

from typing import Any


DEFAULT_VOICE_ENGINE = "edge_tts"
DEFAULT_VOICE_CHUNK_MAX_CHARS = 90
VOICE_CHUNK_MIN_CHARS = 48
VOICE_CHUNK_MAX_CHARS = 140

VOICE_ENGINE_ALIASES = {
    "edge_tts": "edge_tts",
    "edge-tts": "edge_tts",
    "voxcpm2": "voxcpm2",
    "qwen3_tts": "qwen3_tts",
    "qwen3-tts": "qwen3_tts",
    "gpt_sovits": "gpt_sovits",
    "gpt-sovits": "gpt_sovits",
}
WORKBENCH_VOICE_ENGINES = frozenset({"voxcpm2", "qwen3_tts", "gpt_sovits"})
NATURAL_VOICE_ENGINES = frozenset({*WORKBENCH_VOICE_ENGINES, "edge_tts"})


def normalize_voice_engine(value: Any) -> str:
    key = str(value or DEFAULT_VOICE_ENGINE).strip().lower()
    normalized = VOICE_ENGINE_ALIASES.get(key)
    if normalized is None:
        raise ValueError("voice_engine不在允许范围内")
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
