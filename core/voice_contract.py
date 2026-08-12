from __future__ import annotations

from typing import Any


DEFAULT_VOICE_ENGINE = "edge_tts"
DEFAULT_VOICE_NAME = "zh-CN-YunxiNeural"
DEFAULT_VOICE_LABEL = "普通中文播报"
# Fixed product pacing: the former Edge default (+0%) measured 249 spoken
# Chinese characters in 54.7 seconds and was judged visibly rushed.  -15% was
# verified with the packaged runtime and keeps a 180-195 character script in
# the 45-60 second delivery window without post-speeding.
DEFAULT_VOICE_RATE = "-15%"
DEFAULT_VOICE_CHUNK_MAX_CHARS = 90
VOICE_CHUNK_MIN_CHARS = 48
VOICE_CHUNK_MAX_CHARS = 140

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
