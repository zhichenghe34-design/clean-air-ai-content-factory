from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGED_MOTION_ASSET_ROOT = REPO_ROOT / "product-assets" / "motion"
SOURCE_MOTION_ASSET_ROOT = (
    REPO_ROOT
    / "agent-skills"
    / "produce-dynamic-health-video"
    / "assets"
)
MOTION_ASSET_ROOT = (
    PACKAGED_MOTION_ASSET_ROOT
    if PACKAGED_MOTION_ASSET_ROOT.is_dir()
    else SOURCE_MOTION_ASSET_ROOT
)
DEFAULT_PACK_PATH = (
    MOTION_ASSET_ROOT / "animation-pack-v1.json"
)

PACK_SCHEMA_VERSION = "1.0"
RECEIPT_SCHEMA_VERSION = "1.0"
PACK_FIELDS = {
    "schema_version",
    "pack_id",
    "version",
    "sha256",
    "renderer",
    "renderer_families",
    "blocks",
}
FAMILY_FIELDS = {"id", "renderer", "description"}
BLOCK_FIELDS = {
    "id",
    "label",
    "renderer_family",
    "allowed_pack_modes",
    "semantic_tags",
    "duration_seconds",
    "text_limits",
    "motion",
}
DURATION_FIELDS = {"minimum", "maximum"}
TEXT_LIMIT_FIELDS = {"kicker", "title", "caption"}
MOTION_FIELDS = {"primary", "secondary", "transition"}
ALLOWED_RENDERER = "hyperframes-waapi-v1"
ALLOWED_PACK_MODES = {"generic", "legacy_clean_air"}
ALLOWED_TRANSITIONS = {"lime-wipe", "green-wipe", "orange-wipe"}
ALLOWED_SEMANTIC_TAGS = {
    "action",
    "audience",
    "boundary",
    "comparison",
    "document",
    "evidence",
    "hook",
    "metric",
    "process",
    "risk",
    "source",
    "space",
    "summary",
    "timeline",
}
_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")
_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_SHA_RE = re.compile(r"[0-9a-f]{64}")


class AnimationRegistryError(ValueError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _strict_fields(value: Any, expected: set[str], location: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise AnimationRegistryError(f"{location}字段必须严格匹配白名单")
    return value


def _safe_text(value: Any, *, location: str, minimum: int = 1, maximum: int = 160) -> str:
    if not isinstance(value, str):
        raise AnimationRegistryError(f"{location}必须是字符串")
    text = value.strip()
    if not minimum <= len(text) <= maximum or any(ord(char) < 32 for char in text):
        raise AnimationRegistryError(f"{location}长度或字符无效")
    return text


def _safe_id(value: Any, *, location: str) -> str:
    text = _safe_text(value, location=location, maximum=64)
    if not _ID_RE.fullmatch(text):
        raise AnimationRegistryError(f"{location}格式无效")
    return text


def _bounded_number(value: Any, *, location: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnimationRegistryError(f"{location}必须是数字")
    number = float(value)
    if not minimum <= number <= maximum:
        raise AnimationRegistryError(f"{location}超出允许范围")
    return number


def validate_animation_pack(raw: Any) -> dict[str, Any]:
    pack = copy.deepcopy(_strict_fields(raw, PACK_FIELDS, "animation_pack"))
    if pack["schema_version"] != PACK_SCHEMA_VERSION:
        raise AnimationRegistryError("animation_pack schema版本不受支持")
    pack["pack_id"] = _safe_id(pack["pack_id"], location="pack_id")
    if not isinstance(pack["version"], str) or not _VERSION_RE.fullmatch(pack["version"]):
        raise AnimationRegistryError("animation_pack版本格式无效")
    if pack["renderer"] != ALLOWED_RENDERER:
        raise AnimationRegistryError("animation_pack renderer不受信任")
    if not isinstance(pack["sha256"], str) or not _SHA_RE.fullmatch(pack["sha256"]):
        raise AnimationRegistryError("animation_pack sha256格式无效")

    families = pack["renderer_families"]
    if not isinstance(families, list) or not 5 <= len(families) <= 24:
        raise AnimationRegistryError("renderer_families数量无效")
    family_ids: set[str] = set()
    for index, family in enumerate(families):
        family = _strict_fields(family, FAMILY_FIELDS, f"renderer_families[{index}]")
        family_id = _safe_id(family["id"], location=f"renderer_families[{index}].id")
        if family_id in family_ids:
            raise AnimationRegistryError("renderer_family id重复")
        family_ids.add(family_id)
        if family["renderer"] != ALLOWED_RENDERER:
            raise AnimationRegistryError("renderer_family使用了非白名单renderer")
        _safe_text(family["description"], location=f"renderer_families[{index}].description")

    blocks = pack["blocks"]
    if not isinstance(blocks, list) or not 12 <= len(blocks) <= 128:
        raise AnimationRegistryError("animation blocks必须包含12到128项")
    block_ids: set[str] = set()
    for index, block in enumerate(blocks):
        block = _strict_fields(block, BLOCK_FIELDS, f"blocks[{index}]")
        block_id = _safe_id(block["id"], location=f"blocks[{index}].id")
        if block_id in block_ids:
            raise AnimationRegistryError("animation block id重复")
        block_ids.add(block_id)
        _safe_text(block["label"], location=f"blocks[{index}].label", maximum=48)
        family_id = _safe_id(block["renderer_family"], location=f"blocks[{index}].renderer_family")
        if family_id not in family_ids:
            raise AnimationRegistryError("animation block引用了未知renderer_family")

        modes = block["allowed_pack_modes"]
        if (
            not isinstance(modes, list)
            or not modes
            or len(modes) != len(set(modes))
            or not set(modes).issubset(ALLOWED_PACK_MODES)
        ):
            raise AnimationRegistryError("allowed_pack_modes无效")
        tags = block["semantic_tags"]
        if (
            not isinstance(tags, list)
            or not 1 <= len(tags) <= 8
            or len(tags) != len(set(tags))
            or not set(tags).issubset(ALLOWED_SEMANTIC_TAGS)
        ):
            raise AnimationRegistryError("semantic_tags无效")

        duration = _strict_fields(block["duration_seconds"], DURATION_FIELDS, "duration_seconds")
        minimum = _bounded_number(duration["minimum"], location="duration.minimum", minimum=2.0, maximum=20.0)
        maximum = _bounded_number(duration["maximum"], location="duration.maximum", minimum=2.0, maximum=24.0)
        if minimum > maximum:
            raise AnimationRegistryError("duration最小值不能大于最大值")
        limits = _strict_fields(block["text_limits"], TEXT_LIMIT_FIELDS, "text_limits")
        for key, ceiling in (("kicker", 40), ("title", 48), ("caption", 80)):
            value = limits[key]
            if isinstance(value, bool) or not isinstance(value, int) or not 4 <= value <= ceiling:
                raise AnimationRegistryError(f"text_limits.{key}无效")
        motion = _strict_fields(block["motion"], MOTION_FIELDS, "motion")
        _safe_text(motion["primary"], location="motion.primary", maximum=80)
        _safe_text(motion["secondary"], location="motion.secondary", maximum=80)
        if motion["transition"] not in ALLOWED_TRANSITIONS:
            raise AnimationRegistryError("motion.transition不受支持")

    payload = {key: value for key, value in pack.items() if key != "sha256"}
    if canonical_sha256(payload) != pack["sha256"]:
        raise AnimationRegistryError("animation_pack哈希不匹配")
    return pack


def load_animation_pack(path: Path | str = DEFAULT_PACK_PATH) -> dict[str, Any]:
    candidate = Path(path)
    try:
        raw = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnimationRegistryError("无法读取可信动画积木注册表") from exc
    return validate_animation_pack(raw)


class AnimationRegistry:
    def __init__(self, pack: dict[str, Any]):
        self.pack = validate_animation_pack(pack)
        self._blocks = {block["id"]: block for block in self.pack["blocks"]}

    @classmethod
    def load(cls, path: Path | str = DEFAULT_PACK_PATH) -> "AnimationRegistry":
        return cls(load_animation_pack(path))

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": self.pack["schema_version"],
            "pack_id": self.pack["pack_id"],
            "version": self.pack["version"],
            "sha256": self.pack["sha256"],
            "renderer": self.pack["renderer"],
            "block_count": len(self._blocks),
            "renderer_family_count": len(self.pack["renderer_families"]),
        }

    def get(self, block_id: str) -> dict[str, Any]:
        try:
            return copy.deepcopy(self._blocks[block_id])
        except KeyError as exc:
            raise AnimationRegistryError(f"未知动画积木：{block_id}") from exc

    def select(
        self,
        *,
        semantic_tags: Iterable[str],
        pack_mode: str,
        duration_seconds: float,
        previous_block_id: str | None = None,
        preferred_ids: Iterable[str] = (),
        excluded_ids: Iterable[str] = (),
        used_renderer_families: Iterable[str] = (),
    ) -> tuple[dict[str, Any], list[str]]:
        if pack_mode not in ALLOWED_PACK_MODES:
            raise AnimationRegistryError("pack_mode无效")
        if isinstance(semantic_tags, (str, bytes)):
            raise AnimationRegistryError("semantic_tags必须是合法标签列表")
        tags = list(semantic_tags)
        if (
            not tags
            or any(not isinstance(tag, str) or tag not in ALLOWED_SEMANTIC_TAGS for tag in tags)
            or len(tags) != len(set(tags))
        ):
            raise AnimationRegistryError("semantic_tags必须是无重复的合法标签列表")
        duration = _bounded_number(duration_seconds, location="scene.duration", minimum=0.001, maximum=120.0)
        preferred_order = {block_id: index for index, block_id in enumerate(preferred_ids)}
        excluded = set(excluded_ids)
        used_families = set(used_renderer_families)
        known_families = {family["id"] for family in self.pack["renderer_families"]}
        if not used_families.issubset(known_families):
            raise AnimationRegistryError("used_renderer_families包含未知renderer family")
        candidates: list[tuple[tuple[int, int, int, int, str], dict[str, Any], list[str]]] = []
        for block in self.pack["blocks"]:
            bounds = block["duration_seconds"]
            if pack_mode not in block["allowed_pack_modes"] or not bounds["minimum"] <= duration <= bounds["maximum"]:
                continue
            if block["id"] == previous_block_id or block["id"] in excluded:
                continue
            matched = sorted(set(tags).intersection(block["semantic_tags"]))
            if not matched:
                continue
            preference = preferred_order.get(block["id"], 10_000)
            score = (
                int(block["renderer_family"] not in used_families),
                len(matched),
                int("summary" in matched),
                -preference,
                block["id"],
            )
            candidates.append((score, block, matched))
        if not candidates:
            raise AnimationRegistryError("没有满足语义、时长和相邻去重约束的可信动画积木")
        _score, selected, matched = max(candidates, key=lambda item: item[0])
        return copy.deepcopy(selected), matched

    def build_receipt(self, selections: list[dict[str, Any]], animation_pack_mode: str) -> dict[str, Any]:
        if animation_pack_mode not in ALLOWED_PACK_MODES:
            raise AnimationRegistryError("animation_pack_mode无效")
        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "registry": self.summary(),
            "animation_pack_mode": animation_pack_mode,
            "strategy": "semantic-whitelist-v1",
            "selections": copy.deepcopy(selections),
            "sha256": "",
        }
        receipt["sha256"] = canonical_sha256({key: value for key, value in receipt.items() if key != "sha256"})
        return receipt

    def validate_receipt(
        self,
        receipt: Any,
        scenes: list[dict[str, Any]],
        animation_pack_mode: str,
    ) -> dict[str, Any]:
        if animation_pack_mode not in ALLOWED_PACK_MODES:
            raise AnimationRegistryError("animation_pack_mode无效")
        if not isinstance(receipt, dict) or set(receipt) != {
            "schema_version", "registry", "animation_pack_mode", "strategy", "selections", "sha256"
        }:
            raise AnimationRegistryError("selection_receipt字段无效")
        if receipt["schema_version"] != RECEIPT_SCHEMA_VERSION or receipt["strategy"] != "semantic-whitelist-v1":
            raise AnimationRegistryError("selection_receipt版本或策略无效")
        if receipt["registry"] != self.summary():
            raise AnimationRegistryError("selection_receipt注册表身份不匹配")
        if receipt["animation_pack_mode"] != animation_pack_mode:
            raise AnimationRegistryError("selection_receipt动画包模式不匹配")
        expected_hash = canonical_sha256({key: value for key, value in receipt.items() if key != "sha256"})
        if receipt["sha256"] != expected_hash:
            raise AnimationRegistryError("selection_receipt哈希不匹配")
        selections = receipt["selections"]
        if not isinstance(selections, list) or len(selections) != len(scenes):
            raise AnimationRegistryError("selection_receipt场景数量不匹配")
        previous: str | None = None
        for index, (selection, scene) in enumerate(zip(selections, scenes), start=1):
            expected_fields = {
                "scene_id", "block_id", "renderer_family", "semantic_tags", "matched_tags", "duration_seconds"
            }
            selection = _strict_fields(selection, expected_fields, f"selection[{index}]")
            block = self.get(selection["block_id"])
            if animation_pack_mode not in block["allowed_pack_modes"]:
                raise AnimationRegistryError("动画积木不允许用于当前animation_pack_mode")
            if selection["scene_id"] != scene.get("id") or selection["block_id"] != scene.get("visual_type"):
                raise AnimationRegistryError("selection_receipt与场景身份不一致")
            if selection["renderer_family"] != block["renderer_family"] or selection["renderer_family"] != scene.get("renderer_family"):
                raise AnimationRegistryError("selection_receipt renderer_family不一致")
            if selection["block_id"] == previous:
                raise AnimationRegistryError("相邻场景不能重复同一动画积木")
            semantic_tags = selection["semantic_tags"]
            if (
                not isinstance(semantic_tags, list)
                or not semantic_tags
                or len(semantic_tags) != len(set(semantic_tags))
                or any(not isinstance(tag, str) or tag not in ALLOWED_SEMANTIC_TAGS for tag in semantic_tags)
            ):
                raise AnimationRegistryError("selection_receipt semantic_tags无效")
            if semantic_tags != scene.get("semantic_tags"):
                raise AnimationRegistryError("selection_receipt语义标签与场景不一致")
            matched_tags = selection["matched_tags"]
            expected_matched_tags = sorted(set(semantic_tags).intersection(block["semantic_tags"]))
            if (
                not isinstance(matched_tags, list)
                or not matched_tags
                or len(matched_tags) != len(set(matched_tags))
                or any(not isinstance(tag, str) or tag not in ALLOWED_SEMANTIC_TAGS for tag in matched_tags)
                or matched_tags != expected_matched_tags
            ):
                raise AnimationRegistryError("selection_receipt matched_tags不是场景与积木标签的精确交集")
            duration = round(float(scene["end"]) - float(scene["start"]), 3)
            if abs(float(selection["duration_seconds"]) - duration) > 0.002:
                raise AnimationRegistryError("selection_receipt时长与场景不一致")
            previous = selection["block_id"]
        return {
            "ok": True,
            "animation_pack_mode": animation_pack_mode,
            "selection_count": len(selections),
            "sha256": receipt["sha256"],
        }
