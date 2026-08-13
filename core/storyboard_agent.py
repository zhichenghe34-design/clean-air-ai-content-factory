from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from core.motion_director import NARRATION_PAIRS, derive_motion_segments, split_narration_units
from core.voice_contract import estimate_voice_scene_pacing


STORYBOARD_SCHEMA_VERSION = 1
CONTENT_MODES = frozenset({"educational", "marketing"})
LAYOUTS = frozenset({
    "claim_contrast",
    "condition_map",
    "boundary_list",
    "process_flow",
    "evidence_cards",
    "final_checklist",
    "explain_points",
})
LAYOUT_TO_VISUAL_KIND = {
    "claim_contrast": "low-reading",
    "condition_map": "measurement-conditions",
    "boundary_list": "single-reading",
    "process_flow": "retest-process",
    "evidence_cards": "product-conditions",
    "final_checklist": "final-checklist",
    "explain_points": "explain-points",
}
LAYOUT_ITEM_BOUNDS = {
    # These bounds are part of the renderer contract, not a creative hint.
    # Layouts with too few items leave an entire row or column empty; layouts
    # with too many items overflow the 9:16 safe area.
    "claim_contrast": (2, 4),
    "condition_map": (2, 4),
    "boundary_list": (3, 5),
    "process_flow": (3, 4),
    "evidence_cards": (3, 3),
    "final_checklist": (2, 4),
    "explain_points": (1, 5),
}
FORBIDDEN_PLACEHOLDERS = frozenset({"第一项", "第二项", "第三项", "第四项", "问题", "依据", "边界", "行动"})


class StoryboardError(ValueError):
    pass


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def _phrase_key(value: Any) -> str:
    """Compare narration-bound display phrases while ignoring punctuation only."""

    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]", "", str(value or ""))


def _safe_text(value: Any, *, field: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        raise StoryboardError(f"{field}无效")
    closing = set(NARRATION_PAIRS.values())
    stack: list[str] = []
    for character in text:
        if stack and character == stack[-1]:
            stack.pop()
        elif character in NARRATION_PAIRS:
            stack.append(NARRATION_PAIRS[character])
        elif character in closing:
            raise StoryboardError(f"{field}引号或括号不成对")
    if stack:
        raise StoryboardError(f"{field}引号或括号不成对")
    return text


def _script_sha256(script: str) -> str:
    return hashlib.sha256(str(script).encode("utf-8")).hexdigest().upper()


def _narration_clauses(caption: str) -> list[str]:
    """Split narration without cutting punctuation inside paired quotes.

    A question such as ``对于“数值低，就能入住吗？”，不能只凭……``
    contains the visual headline inside Chinese quotation marks.  A plain
    punctuation split turns it into the broken card ``对于“数值低``.  Keep a
    quoted span together, then remove only the surrounding discourse wrapper
    when the whole clause is a quoted subject.
    """

    clauses: list[str] = []
    for unit in split_narration_units(
        caption,
        punctuation="，。！？；：,!?;:",
        keep_punctuation=False,
    ):
        raw = unit.strip("，。！？；：,!?;: ")
        if not raw:
            continue
        quoted = re.fullmatch(r"(?:对于|关于)?[“‘\"'](.+)[”’\"']", raw)
        value = quoted.group(1).strip() if quoted else raw
        if value:
            clauses.append(value)
    return clauses


def _local_kicker(title: str) -> str:
    """Return a complete narration phrase; never slice at a character count."""

    for phrase in split_narration_units(
        title,
        punctuation="，。！？；：,!?;:",
        keep_punctuation=False,
    ):
        value = phrase.strip()
        if value and value[0] in NARRATION_PAIRS and value[-1] == NARRATION_PAIRS[value[0]]:
            value = value[1:-1].strip()
        if 4 <= len(_phrase_key(value)) <= 18:
            return value
    return title


def _is_source_attribution_leadin(caption: str) -> bool:
    """Recognize a standalone, named-source lead-in before its quoted claim.

    The voice director can legitimately split ``某来源称：`` from the complete
    sentence that follows it.  That pair needs two sparse renderer blocks, but
    an arbitrary unfinished colon fragment must not gain the same exception.
    Require both a source-shaped name and an attribution verb immediately
    before the Chinese colon.
    """

    text = str(caption or "").strip()
    if not text.endswith("："):
        return False
    stem = text[:-1].strip()
    attribution = re.search(
        r"(?:称|指出|报道|显示|提到|披露|写道|说明|表明|介绍)$",
        stem,
    )
    if not attribution:
        return False
    source_name = stem[:attribution.start()].strip()
    return bool(source_name and re.search(
        r"[0-9A-Za-z\u3400-\u9fff]{2,}"
        r"(?:网|报|台|刊|杂志|期刊|机构|部门|委员会|协会|学会|大学|学院|实验室|研究院|研究所|出版社|新闻|媒体)",
        source_name,
    ))


def validate_storyboard(
    value: Mapping[str, Any],
    script: str,
    *,
    source: str,
    model: str = "",
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StoryboardError("导演蓝图必须是JSON对象")
    allowed_top = {
        "schema_version", "content_mode", "narrative_arc", "scenes",
        "source", "model", "script_sha256", "mechanical_review",
    }
    if set(value) - allowed_top:
        raise StoryboardError("导演蓝图包含未授权顶层字段")
    if int(value.get("schema_version", 0)) != STORYBOARD_SCHEMA_VERSION:
        raise StoryboardError("导演蓝图版本不受支持")
    if value.get("script_sha256") and str(value.get("script_sha256")) != _script_sha256(script):
        raise StoryboardError("导演蓝图绑定的脚本哈希已失效")
    content_mode = str(value.get("content_mode", "")).strip()
    if content_mode not in CONTENT_MODES:
        raise StoryboardError("content_mode必须是educational或marketing")
    narrative_arc = _safe_text(
        value.get("narrative_arc") or "按旁白顺序完整展示",
        field="narrative_arc",
        maximum=120,
    )
    raw_scenes = value.get("scenes")
    if not isinstance(raw_scenes, list) or not 4 <= len(raw_scenes) <= 8:
        raise StoryboardError("导演蓝图必须包含4到8幕")

    normalized_scenes: list[dict[str, Any]] = []
    previous_layout = ""
    previous_item_count = 0
    previous_sentence_count = 0
    previous_caption_complete = False
    previous_source_attribution_leadin = False
    captions: list[str] = []
    for index, raw in enumerate(raw_scenes, start=1):
        if not isinstance(raw, Mapping):
            raise StoryboardError(f"场景{index}必须是对象")
        allowed_scene = {"id", "caption", "kicker", "title", "summary", "layout", "items", "focus_order"}
        if set(raw) - allowed_scene:
            raise StoryboardError(f"场景{index}包含未授权字段")
        if raw.get("id") not in (None, f"scene-{index:02d}"):
            raise StoryboardError(f"场景{index}.id与顺序不一致")
        caption = _safe_text(raw.get("caption"), field=f"场景{index}.caption", maximum=90)
        voice_pacing = estimate_voice_scene_pacing(caption)
        if voice_pacing["blocked"]:
            raise StoryboardError(
                f"场景{index}.caption固定{voice_pacing['voice_rate']}大众播报预计"
                f"{voice_pacing['spoken_characters_per_second']:.2f}字/秒，超过"
                f"{voice_pacing['maximum_spoken_characters_per_second']:.2f}字/秒；"
                "请在旁白已有的安全标点处分幕，不能删字、增字或从引号括号中间截断"
            )
        phrase_caption = _phrase_key(caption)
        layout = str(raw.get("layout", "")).strip()
        if layout not in LAYOUTS:
            raise StoryboardError(f"场景{index}布局不在白名单")
        raw_items = raw.get("items")
        if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 5:
            raise StoryboardError(f"场景{index}.items必须包含1到5项")
        items = [_safe_text(item, field=f"场景{index}.items", maximum=30) for item in raw_items]
        minimum_items, maximum_items = LAYOUT_ITEM_BOUNDS[layout]
        if not minimum_items <= len(items) <= maximum_items:
            raise StoryboardError(
                f"场景{index}布局{layout}需要{minimum_items}到{maximum_items}项信息，"
                f"当前只有{len(items)}项，会产生大面积空白或溢出"
            )
        sentence_count = len(split_narration_units(
            caption,
            punctuation="。！？!?",
            keep_punctuation=True,
        ))
        caption_complete = caption.rstrip().endswith(("。", "！", "？", "!", "?"))
        allowed_complete_sentence_repeat = (
            layout == previous_layout == "explain_points"
            and previous_item_count == len(items) == 1
            and previous_sentence_count == sentence_count == 1
            and previous_caption_complete
            and caption_complete
        )
        allowed_attribution_claim_repeat = (
            layout == previous_layout == "explain_points"
            and previous_item_count == len(items) == 1
            and previous_source_attribution_leadin
            and sentence_count == 1
            and caption_complete
        )
        if layout == previous_layout and not (
            allowed_complete_sentence_repeat or allowed_attribution_claim_repeat
        ):
            raise StoryboardError(f"场景{index}与上一幕重复同一信息结构")
        previous_layout = layout
        previous_item_count = len(items)
        previous_sentence_count = sentence_count
        previous_caption_complete = caption_complete
        previous_source_attribution_leadin = _is_source_attribution_leadin(caption)
        if len(set(map(_compact, items))) != len(items):
            raise StoryboardError(f"场景{index}.items存在重复")
        for item in items:
            if not _phrase_key(item) or _phrase_key(item) not in phrase_caption:
                raise StoryboardError(f"场景{index}图上文字不是当前旁白的逐字短语")
            if item in FORBIDDEN_PLACEHOLDERS:
                raise StoryboardError(f"场景{index}使用通用占位词")
        # Provider output does not need to invent another wording layer.  The
        # visible header is derived from narration-bound phrases.  Stored
        # normalized artifacts may carry those fields, but they are rechecked.
        kicker = _safe_text(raw.get("kicker") or _local_kicker(items[0]), field=f"场景{index}.kicker", maximum=30)
        title = _safe_text(raw.get("title") or items[0], field=f"场景{index}.title", maximum=30)
        summary_source = items[1] if len(items) > 1 else items[0]
        summary = _safe_text(raw.get("summary") or summary_source, field=f"场景{index}.summary", maximum=30)
        for field_name, field_value in (("kicker", kicker), ("title", title), ("summary", summary)):
            if not _phrase_key(field_value) or _phrase_key(field_value) not in phrase_caption:
                raise StoryboardError(f"场景{index}.{field_name}不是当前旁白的逐字短语")
        if layout == "claim_contrast":
            primary, secondary = items[:2]
            if any(mark in primary + secondary for mark in "!?！？") or re.match(
                r"^(?:不能|不可|无法|并不|不等于|未必|不是)", secondary,
            ):
                raise StoryboardError(f"场景{index}.claim_contrast两端不是可直接对比的完整主张")
        focus_order = raw.get("focus_order")
        expected_order = list(range(len(items)))
        if not isinstance(focus_order, list) or sorted(focus_order) != expected_order:
            raise StoryboardError(f"场景{index}.focus_order必须逐项且不重复")
        captions.append(caption)
        normalized_scenes.append({
            "id": f"scene-{index:02d}",
            "caption": caption,
            "kicker": kicker,
            "title": title,
            "summary": summary,
            "layout": layout,
            "items": items,
            "focus_order": list(focus_order),
        })

    if _compact("".join(captions)) != _compact(script):
        raise StoryboardError("所有场景旁白未按原顺序完整覆盖最终脚本")
    if content_mode == "educational" and normalized_scenes[-1]["layout"] != "final_checklist":
        raise StoryboardError("科普蓝图最后一幕必须汇总可核对清单")
    return {
        "schema_version": STORYBOARD_SCHEMA_VERSION,
        "source": str(source).strip() or "unknown",
        "model": str(model).strip(),
        "script_sha256": _script_sha256(script),
        "content_mode": content_mode,
        "narrative_arc": narrative_arc,
        "scenes": normalized_scenes,
        "mechanical_review": {
            "status": "passed",
            "script_fully_covered": True,
            "all_visual_items_are_verbatim_caption_phrases": True,
            "layout_whitelist_only": True,
            "layout_item_density_compatible": True,
            "voice_scene_density_compatible": True,
            # Separate one-item explain scenes use deterministic alternate
            # renderer blocks, so the narrow exception above is not recorded
            # as a repeated information structure.
            "adjacent_layout_repeat": False,
            "coordinates_css_and_code_allowed": False,
        },
    }


def _coalesce_verbatim_items(caption: str, items: list[str], maximum: int) -> list[str]:
    """Fit verbatim visual phrases without dropping the narration tail."""

    values = [str(item).strip() for item in items if str(item).strip()]
    while len(values) > maximum:
        candidates: list[tuple[int, int, str]] = []
        cursor = 0
        positions: list[tuple[int, int]] = []
        for value in values:
            start = caption.find(value, cursor)
            if start < 0:
                raise StoryboardError("图上短语未按旁白顺序绑定")
            end = start + len(value)
            positions.append((start, end))
            cursor = end
        for index in range(len(values) - 1):
            combined = caption[positions[index][0]:positions[index + 1][1]].strip("，。！？；：、,!?;: ")
            if combined and len(combined) <= 30:
                candidates.append((len(combined), index, combined))
        if not candidates:
            raise StoryboardError("旁白信息无法在画面容量内逐字完整展示")
        _, index, combined = min(candidates)
        values[index:index + 2] = [combined]
    return values


def _verbatim_items(caption: str) -> list[str]:
    pieces: list[str] = []
    for clause in _narration_clauses(caption):
        if len(re.sub(r"[^0-9A-Za-z\u3400-\u9fff]", "", clause)) < 4:
            continue
        # Preserve complete, narration-bound phrases.  The former fallback
        # selected the shortest fragment and then cut it at an arbitrary
        # character count, which produced cards such as “功效” and titles
        # ending in the middle of “材料”.
        # A short sentence can still contain a dense, narration-bound list.
        # Keeping that whole list in one card creates the large empty panel the
        # product is supposed to prevent.  Split only on separators already in
        # the caption, retaining every visible word verbatim.
        if "、" in clause:
            list_parts = [part.strip() for part in clause.split("、") if part.strip()]
            # Do not turn compact nouns such as "功效、价格" into isolated,
            # context-free labels.  Enumeration expansion is safe only when
            # every existing fragment is already a complete visible phrase.
            if len(list_parts) >= 2 and all(len(_phrase_key(part)) >= 4 for part in list_parts):
                if len(list_parts) < 5:
                    expanded: list[str] = []
                    for part in list_parts:
                        remaining_slots = 5 - len(expanded)
                        pair = [
                            value.strip()
                            for value in re.split(r"(?:以及|并且)", part, maxsplit=1)
                            if len(_phrase_key(value)) >= 4
                        ]
                        if len(pair) == 2 and remaining_slots >= 2:
                            expanded.extend(pair)
                        else:
                            expanded.append(part)
                    list_parts = expanded
                pieces.extend(list_parts)
                continue
        if "、" in clause and len(clause.split("、")) >= 4 and len(clause) <= 30:
            members = clause.split("、")
            pivot = (len(members) + 1) // 2
            pieces.extend(("、".join(members[:pivot]), "、".join(members[pivot:])))
            continue
        if len(clause) <= 30:
            pieces.append(clause)
            continue
        # Pack Chinese list members into contiguous phrases instead of
        # emitting meaningless one-word cards such as “功效” or “价格”.
        list_parts = clause.split("、")
        if len(list_parts) > 1:
            buffer = ""
            for part in list_parts:
                candidate = f"{buffer}、{part}" if buffer else part
                if len(candidate) <= 30:
                    buffer = candidate
                else:
                    if buffer:
                        pieces.append(buffer)
                    buffer = part
            if buffer:
                pieces.append(buffer)
            continue
        subpieces = [part.strip() for part in re.split(r"(?:而且|但是|然后|最后|继续|同时)", clause) if part.strip()]
        pieces.extend(part for part in subpieces if len(part) <= 30)
    compact: list[str] = []
    for piece in pieces:
        value = piece
        if value and value not in compact:
            compact.append(value)
    return _coalesce_verbatim_items(caption, compact or [caption], 5)


def build_local_storyboard(
    topic: str,
    script: str,
    capability_pack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    segments = list(
        derive_motion_segments(
            topic,
            script,
            target_count=7,
            capability_pack=capability_pack,
            enforce_voice_pacing=True,
        )
    )

    # A one-phrase scene has only one safe renderer.  Merge it with a nearby
    # non-final scene when the combined narration still fits the frozen caption
    # contract; otherwise consecutive sparse sentences would either repeat the
    # same empty-looking layout or force an unrelated A ≠ B diagram.
    while len(segments) > 4:
        candidates: list[tuple[int, int, int]] = []
        for sparse_index, segment in enumerate(segments[:-1]):
            if len(_verbatim_items(str(segment["caption"]))) != 1:
                continue
            for neighbour in (sparse_index - 1, sparse_index + 1):
                if neighbour < 0 or neighbour >= len(segments) - 1:
                    continue
                left, right = sorted((sparse_index, neighbour))
                caption = str(segments[left]["caption"]) + str(segments[right]["caption"])
                items = _verbatim_items(caption)
                if (
                    len(caption) <= 62
                    and 2 <= len(items) <= 5
                    # Keep two complete spoken sentences as two TTS scenes.
                    # The faster fixed voice has materially different leading
                    # and trailing prosody when separate sentences are sent in
                    # one Edge request; the final PCM gate measured a merge of
                    # this shape above the delivery-density limit even though
                    # each sentence was safe on its own.
                    and len(split_narration_units(
                        caption,
                        punctuation="。！？!?",
                        keep_punctuation=True,
                    )) <= 1
                    and not estimate_voice_scene_pacing(caption)["blocked"]
                ):
                    candidates.append((len(items), left, right))
        if not candidates:
            break
        _, left, right = min(candidates)
        segments[left] = {**segments[left], "caption": str(segments[left]["caption"]) + str(segments[right]["caption"])}
        del segments[right]
    raw_scenes: list[dict[str, Any]] = []
    previous_layout = ""

    def final_checklist_items(caption: str, items: list[str]) -> list[str]:
        if len(items) >= 2:
            return _coalesce_verbatim_items(caption, items, 4)
        # A final checklist cannot render a single generic block.  Split only
        # on punctuation or conjunctions that already exist in the narration;
        # never invent another wording layer merely to fill the layout.
        phrase = items[0]
        pieces = [
            part.strip()
            for part in re.split(r"[、，；：]|(?:以及|并且|然后)", phrase)
            if len(_phrase_key(part)) >= 4
        ]
        if len(pieces) >= 2:
            return _coalesce_verbatim_items(caption, pieces, 4)
        raise StoryboardError("本地兜底最后一幕缺少可逐字拆分的行动清单")

    def choose_layout(item_count: int, *, final: bool) -> str:
        if final:
            if 2 <= item_count <= 4:
                return "final_checklist"
            raise StoryboardError("本地兜底最后一幕需要2到4项旁白短语，不能生成空清单")
        candidates = {
            # A complete single-sentence scene is legitimate after the voice
            # director separates two independently paced sentences.  Alternate
            # its information structure so adjacent short sentences do not
            # render as identical cards.
            1: ["explain_points"],
            # Two narration phrases do not automatically form an A ≠ B claim.
            # The renderer labels the right side as “cannot directly infer”, so
            # blindly choosing claim_contrast reverses negated sentences such as
            # “只是线索，不能替代规范检测”.
            2: ["explain_points", "condition_map"],
            3: ["condition_map", "boundary_list", "process_flow", "evidence_cards", "explain_points"],
            4: ["process_flow", "claim_contrast", "condition_map", "boundary_list", "explain_points"],
            5: ["boundary_list", "explain_points"],
        }[item_count]
        return next((candidate for candidate in candidates if candidate != previous_layout), candidates[0])

    for index, segment in enumerate(segments):
        caption = str(segment["caption"])
        items = _verbatim_items(caption)
        # Visible card copy is capped at 30 characters.  A quote-aware clause
        # may remain longer (for example a complete question plus its reason),
        # so split only over punctuation that already exists in that clause.
        expanded_items: list[str] = []
        for item in items:
            if len(item) <= 30:
                expanded_items.append(item)
                continue
            pieces = _narration_clauses(item)
            if len(pieces) < 2:
                quoted_parts = re.fullmatch(r"([^“]*“[^”]+”)(.+)", item)
                pieces = (
                    [quoted_parts.group(1).strip(), quoted_parts.group(2).strip()]
                    if quoted_parts
                    else pieces
                )
            if len(pieces) < 2 or any(len(piece) > 30 for piece in pieces):
                raise StoryboardError("本地兜底旁白缺少可安全展示的完整短语")
            expanded_items.extend(pieces)
        items = _coalesce_verbatim_items(caption, expanded_items, 5)
        if index == len(segments) - 1:
            items = final_checklist_items(caption, items)
            layout = choose_layout(len(items), final=True)
        else:
            layout = choose_layout(len(items), final=False)
        previous_layout = layout
        raw_scenes.append({
            "caption": caption,
            "kicker": _local_kicker(items[0]),
            "title": items[0],
            "summary": items[1] if len(items) > 1 else items[0],
            "layout": layout,
            "items": items,
            "focus_order": list(range(len(items))),
        })
    return validate_storyboard(
        {
            "schema_version": STORYBOARD_SCHEMA_VERSION,
            "content_mode": "educational",
            "narrative_arc": "完整问题→逐项解释→条件边界→行动清单",
            "scenes": raw_scenes,
        },
        script,
        source="local_deterministic_storyboard",
    )


def storyboard_to_motion_segments(storyboard: Mapping[str, Any]) -> list[dict[str, Any]]:
    scenes = storyboard.get("scenes")
    if not isinstance(scenes, list):
        raise StoryboardError("导演蓝图缺少场景")
    return [
        {
            "kicker": scene["kicker"],
            "title": scene["title"],
            "caption": scene["caption"],
            "visual_content": {
                "kind": LAYOUT_TO_VISUAL_KIND[scene["layout"]],
                "headline": scene["title"],
                "summary": scene["summary"],
                "items": list(scene["items"]),
                "focus_order": list(scene["focus_order"]),
                "layout": scene["layout"],
                "storyboard_source": storyboard.get("source", "unknown"),
            },
        }
        for scene in scenes
    ]
