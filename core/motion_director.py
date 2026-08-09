from __future__ import annotations

import html as html_lib
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from core.animation_registry import (
    AnimationRegistry,
    AnimationRegistryError,
    DEFAULT_PACK_PATH,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "agent-skills" / "produce-dynamic-health-video"
TEMPLATE_FILE = SKILL_ROOT / "assets" / "composition-template.html"
ANIMATION_PACK_FILE = DEFAULT_PACK_PATH
FONT_REGULAR_FILE = REPO_ROOT / "docs" / "fonts" / "NotoSansSC-Regular.ttf"
FONT_BOLD_FILE = REPO_ROOT / "docs" / "fonts" / "NotoSansSC-Bold.ttf"

LEGACY_VISUAL_SEQUENCE = (
    "stat-ring",
    "magnifier",
    "liquid-chamber",
    "clock-wave",
    "report-scan",
    "compare",
    "orbit-summary",
)

GENERIC_VISUAL_SEQUENCE = (
    "signal-grid",
    "focus-lens",
    "process-flow",
    "timeline-pulse",
    "source-stack",
    "option-compare",
    "orbit-summary",
)

# Backward-compatible public name used by older integrations.
VISUAL_SEQUENCE = LEGACY_VISUAL_SEQUENCE

_TAG_KEYWORDS = {
    "audience": ("客户", "用户", "受众", "家庭", "人群"),
    "metric": ("数据", "数字", "指标", "比例", "价格", "剂量", "浓度", "%"),
    "evidence": ("证据", "依据", "核验", "检测", "事实"),
    "source": ("来源", "报告", "资料", "出处", "官方"),
    "document": ("报告", "资料", "文件", "字段", "记录"),
    "process": ("步骤", "流程", "先", "再", "过程", "执行"),
    "timeline": ("时间", "阶段", "周期", "长期", "短期", "先后"),
    "comparison": ("对比", "比较", "区别", "不等于", "不能直接", "方案"),
    "boundary": ("边界", "限制", "条件", "适用", "未知", "不能"),
    "risk": ("风险", "错误", "误区", "警惕", "不安全"),
    "space": ("空间", "体积", "房间", "整屋", "场景"),
    "action": ("行动", "选择", "建议", "下一步", "确认"),
}


def _registry() -> AnimationRegistry:
    try:
        return AnimationRegistry.load(ANIMATION_PACK_FILE)
    except AnimationRegistryError as exc:
        raise MotionPlanError(f"可信动画积木注册表无效：{exc}") from exc


def _semantic_tags(text: str, index: int, total: int) -> list[str]:
    tags: list[str] = []
    if index == 1:
        tags.append("hook")
    for tag, keywords in _TAG_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            tags.append(tag)
    role_fallback = ("evidence", "process", "timeline", "source", "comparison", "action")
    if index < total:
        tags.append(role_fallback[(index - 1) % len(role_fallback)])
    else:
        tags.extend(("summary", "action", "boundary"))
    return list(dict.fromkeys(tags))


def _scene_durations(weights: list[int], duration: float) -> list[float]:
    """Keep semantic weighting without producing unusably short scenes."""

    average = sum(weights) / len(weights)
    factors = [max(0.8, min(1.2, 0.8 + 0.4 * weight / average)) for weight in weights]
    scale = duration / sum(factors)
    values = [factor * scale for factor in factors]
    # With 4-8 scenes and the release duration contract this should always hold.
    if any(value < 3.5 or value > 20.0 for value in values):
        values = [duration / len(weights)] * len(weights)
    return values


def _pack_snapshot(capability_pack: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(capability_pack, dict):
        return {}
    snapshot = capability_pack.get("snapshot")
    return dict(snapshot) if isinstance(snapshot, dict) else dict(capability_pack)


def _is_legacy_pack(capability_pack: dict[str, Any] | None) -> bool:
    return bool(isinstance(capability_pack, dict) and capability_pack.get("id") == "legacy-clean-air-v2")


def _visual_direction(capability_pack: dict[str, Any] | None) -> dict[str, Any]:
    value = _pack_snapshot(capability_pack).get("visual_direction")
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        keywords = [str(item).strip() for item in value if str(item).strip()][:8]
        return {"style": " · ".join(keywords), "keywords": keywords} if keywords else {}
    if isinstance(value, str) and value.strip():
        return {"style": value.strip()}
    return {}


def _safe_hex_color(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text.upper() if re.fullmatch(r"#[0-9A-Fa-f]{6}", text) else fallback


class MotionPlanError(ValueError):
    pass


def derive_motion_segments(
    topic: str,
    script: str,
    target_count: int = 7,
    capability_pack: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    text = re.sub(r"\s+", "", str(script or "").strip())
    if not text:
        raise MotionPlanError("脚本为空，无法生成动态场景")
    raw_units = [item.strip("，,。！？!?；;") for item in re.split(r"(?<=[。！？!?；;])", text) if item.strip()]
    units: list[str] = []
    for unit in raw_units or [text]:
        if len(unit) <= 58:
            units.append(unit)
            continue
        parts = [part for part in re.split(r"(?<=[，,：:])", unit) if part]
        current = ""
        for part in parts:
            if current and len(current + part) > 58:
                units.append(current.strip("，,：:"))
                current = part
            else:
                current += part
        if current:
            units.append(current.strip("，,：:"))

    while len(units) < 4:
        longest_index = max(range(len(units)), key=lambda idx: len(units[idx]))
        value = units.pop(longest_index)
        midpoint = max(1, len(value) // 2)
        split_at = max(value.rfind("，", 0, midpoint + 1), value.rfind(",", 0, midpoint + 1))
        if split_at <= 0:
            split_at = midpoint
        units[longest_index:longest_index] = [value[:split_at].strip("，,"), value[split_at:].strip("，,")]

    target = max(4, min(8, int(target_count)))
    if len(units) > target:
        groups: list[list[str]] = [[] for _ in range(target)]
        for index, unit in enumerate(units):
            groups[min(target - 1, index * target // len(units))].append(unit)
        units = ["，".join(group)[:62] for group in groups if group]

    def title_for(caption: str, index: int) -> tuple[str, str]:
        if _is_legacy_pack(capability_pack):
            rules = [
                (("气味", "鼻子", "嗅觉"), "嗅觉只是线索", "别让感受替代检测"),
                (("剂量", "空间", "体积"), "剂量 × 空间", "先看使用和空间条件"),
                (("时间", "浓度", "温度", "湿度"), "时间与环境条件", "结果取决于过程"),
                (("检测", "报告", "方法", "来源"), "方法 × 报告来源", "结论必须可追溯"),
                (("不能直接", "不等同", "整屋", "真实"), "实验条件 ≠ 真实场景", "不要直接外推"),
                (("至少", "找齐", "缺少", "条件"), "条件缺一不可", "判断前先补齐信息"),
            ]
        else:
            rules = [
                (("客户", "用户", "受众", "需求"), "从真实需求出发", "先明确对象和场景"),
                (("数据", "数字", "指标", "价格"), "数据先核验", "别让数字替代依据"),
                (("证据", "来源", "资料", "依据"), "来源可追溯", "结论必须有出处"),
                (("步骤", "流程", "先", "再"), "按步骤推进", "把复杂任务拆开"),
                (("方案", "选择", "对比", "适用"), "比较适用场景", "不把一种方案外推到全部"),
                (("风险", "限制", "边界", "未知"), "说清适用边界", "未知项保持未知"),
            ]
        for keywords, title, kicker in rules:
            if any(keyword in caption for keyword in keywords):
                return title, kicker
        clean = re.sub(r"[“”‘’\"']", "", caption)
        return clean[:14] + ("…" if len(clean) > 14 else ""), ("核心问题" if index == 1 else "关键判断")

    segments: list[dict[str, str]] = []
    for index, caption in enumerate(units, start=1):
        title, kicker = title_for(caption, index)
        if index == len(units):
            kicker = "最终判断原则" if _is_legacy_pack(capability_pack) else "下一步行动"
            title = title if len(title) <= 16 else str(topic)[:16]
        segments.append({"kicker": kicker, "title": title, "caption": caption[:62]})
    return segments


def build_motion_plan(
    topic: str,
    audience: str,
    segments: list[dict[str, Any]],
    duration_seconds: float,
    capability_pack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not 4 <= len(segments) <= 8:
        raise MotionPlanError("动态短视频必须包含4到8个场景")
    duration = float(duration_seconds)
    if duration <= 0:
        raise MotionPlanError("音频时长必须大于0")

    weights = [max(8, len(str(item.get("caption", "")))) for item in segments]
    scene_durations = _scene_durations(weights, duration)
    cursor = 0.0
    scenes: list[dict[str, Any]] = []
    receipt_selections: list[dict[str, Any]] = []
    legacy = _is_legacy_pack(capability_pack)
    visual_sequence = LEGACY_VISUAL_SEQUENCE if legacy else GENERIC_VISUAL_SEQUENCE
    pack_mode = "legacy_clean_air" if legacy else "generic"
    registry = _registry()
    previous_block_id: str | None = None
    for index, (segment, scene_duration) in enumerate(zip(segments, scene_durations), start=1):
        end = duration if index == len(segments) else cursor + scene_duration
        safe_kicker = str(segment.get("kicker") or f"要点 {index:02d}").strip()
        safe_title = str(segment.get("title") or topic).strip()
        safe_caption = str(segment.get("caption") or "").strip()
        tags = _semantic_tags(" ".join((safe_kicker, safe_title, safe_caption)), index, len(segments))
        legacy_space_scene = legacy and index == min(3, len(segments) - 1)
        if legacy_space_scene:
            tags = list(dict.fromkeys(tags + ["space", "metric", "boundary"]))
        preferred = (
            ("orbit-summary",)
            if index == len(segments)
            else visual_sequence[index - 1 :] + visual_sequence[: index - 1]
        )
        try:
            excluded_ids = (
                tuple(item["id"] for item in registry.pack["blocks"] if item["id"] != "orbit-summary")
                if index == len(segments)
                else tuple(item["id"] for item in registry.pack["blocks"] if item["id"] != "liquid-chamber")
                if legacy_space_scene
                else ("orbit-summary", "liquid-chamber")
                if legacy
                else ("orbit-summary",)
            )
            block, matched_tags = registry.select(
                semantic_tags=tags,
                pack_mode=pack_mode,
                duration_seconds=round(end - cursor, 3),
                previous_block_id=previous_block_id,
                preferred_ids=preferred,
                excluded_ids=excluded_ids,
            )
        except AnimationRegistryError as exc:
            raise MotionPlanError(f"场景{index}没有可用的可信动画积木：{exc}") from exc
        if index == len(segments) and block["id"] != "orbit-summary":
            raise MotionPlanError("结尾场景必须绑定可信orbit-summary积木")
        limits = block["text_limits"]
        for field, value in (("kicker", safe_kicker), ("title", safe_title), ("caption", safe_caption)):
            if not value or len(value) > int(limits[field]):
                raise MotionPlanError(f"场景{index}的{field}超出可信积木文字限制")
        visual = block["id"]
        motion = block["motion"]
        scenes.append(
            {
                "id": f"scene-{index:02d}",
                "index": index,
                "start": round(cursor, 3),
                "end": round(end, 3),
                "kicker": safe_kicker,
                "title": safe_title,
                "caption": safe_caption,
                "visual_type": visual,
                "renderer_family": block["renderer_family"],
                "semantic_tags": tags,
                "primary_motion": motion["primary"],
                "secondary_motion": motion["secondary"],
                "transition": motion["transition"],
                "entrance_lead_seconds": 0.22,
            }
        )
        receipt_selections.append(
            {
                "scene_id": f"scene-{index:02d}",
                "block_id": visual,
                "renderer_family": block["renderer_family"],
                "semantic_tags": tags,
                "matched_tags": matched_tags,
                "duration_seconds": round(end - cursor, 3),
            }
        )
        previous_block_id = visual
        cursor = end

    snapshot = _pack_snapshot(capability_pack)
    visual_direction = _visual_direction(capability_pack)
    accent = _safe_hex_color(visual_direction.get("accent_color"), "#C8E35B")
    brand_name = str(
        visual_direction.get("brand_name")
        or snapshot.get("label")
        or ("净界AI内容工厂" if legacy else "Evidence Motion")
    ).strip()[:48]
    project_name = "clean-air-motion-output" if legacy else "evidence-motion-output"
    style = str(visual_direction.get("style") or (
        "evidence-led clean-air motion graphics, not slide cards"
        if legacy else "evidence-led adaptable motion graphics, not slide cards"
    )).strip()
    keywords = visual_direction.get("keywords")
    if isinstance(keywords, str):
        keywords = [keywords]
    if not isinstance(keywords, list):
        keywords = []
    director_rules = [
        "每个场景必须有主体运动和辅助运动",
        "下一场主体在遮罩离场前开始入场，禁止转场后空场",
        "同一条视频至少使用三种不同视觉语法",
        "字幕最多两行，强调词作为不可拆分短语",
        "结尾必须将分散信息汇聚为一个可执行判断原则",
        "画面只表达脚本已有信息，不把视觉隐喻当作事实证明",
    ]
    if legacy:
        director_rules[-1] = "不把实验条件动画表现成具体产品功效证明"
    plan = {
        "schema_version": "1.0",
        "topic": str(topic).strip(),
        "audience": str(audience).strip(),
        "duration_seconds": round(duration, 3),
        "format": {"width": 1080, "height": 1920, "fps": 30, "aspect_ratio": "9:16"},
        "project": {
            "name": project_name,
            "brand_name": brand_name,
            "brand_mark": ("时" if legacy else (brand_name[:1].upper() or "E")),
            "legacy": legacy,
        },
        "capability_pack": {
            "id": str((capability_pack or {}).get("id", "")) if isinstance(capability_pack, dict) else "",
            "version": (capability_pack or {}).get("version") if isinstance(capability_pack, dict) else None,
            "sha256": str((capability_pack or {}).get("sha256", "")) if isinstance(capability_pack, dict) else "",
        },
        "design_system": {
            "background": "#071713",
            "foreground": "#F1EFE7",
            "accent": accent,
            "secondary": "#1F5A46",
            "risk": "#DF744F",
            "style": style,
            "keywords": [str(value)[:40] for value in keywords[:8]],
        },
        "director_rules": director_rules,
        "scenes": scenes,
        "animation_pack_mode": pack_mode,
        "animation_registry": registry.summary(),
        "selection_receipt": registry.build_receipt(receipt_selections, pack_mode),
    }
    validate_motion_plan(plan)
    return plan


def validate_motion_plan(plan: dict[str, Any]) -> dict[str, Any]:
    scenes = plan.get("scenes")
    if not isinstance(scenes, list) or not 4 <= len(scenes) <= 8:
        raise MotionPlanError("motion_plan.scenes必须包含4到8项")
    errors: list[str] = []
    renderer_families: set[str] = set()
    previous_end = 0.0
    previous_visual = ""
    registry = _registry()
    pack_mode = plan.get("animation_pack_mode")
    project = plan.get("project") if isinstance(plan.get("project"), dict) else {}
    capability_identity = plan.get("capability_pack") if isinstance(plan.get("capability_pack"), dict) else {}
    project_legacy = project.get("legacy") is True
    capability_legacy = capability_identity.get("id") == "legacy-clean-air-v2"
    if project_legacy != capability_legacy:
        errors.append("项目legacy标记与能力包身份不一致")
    expected_pack_mode = "legacy_clean_air" if capability_legacy else "generic"
    if pack_mode != expected_pack_mode:
        errors.append("animation_pack_mode与项目能力包身份不一致")
    if plan.get("animation_registry") != registry.summary():
        errors.append("动画注册表身份与当前可信版本不一致")
    for index, scene in enumerate(scenes, start=1):
        visual = str(scene.get("visual_type", ""))
        try:
            block = registry.get(visual)
        except AnimationRegistryError:
            errors.append(f"场景{index}使用未知视觉类型：{visual}")
            block = None
        if visual == previous_visual:
            errors.append(f"场景{index}与上一场重复同一动画积木")
        previous_visual = visual
        family = str(scene.get("renderer_family", ""))
        renderer_families.add(family)
        start, end = float(scene.get("start", -1)), float(scene.get("end", -1))
        if abs(start - previous_end) > 0.02 or end <= start:
            errors.append(f"场景{index}时间轴不连续")
        previous_end = end
        if not str(scene.get("primary_motion", "")).strip() or not str(scene.get("secondary_motion", "")).strip():
            errors.append(f"场景{index}缺少双层运动")
        if block is not None:
            if pack_mode not in block["allowed_pack_modes"]:
                errors.append(f"场景{index}的动画积木不允许用于当前animation_pack_mode")
            if family != block["renderer_family"]:
                errors.append(f"场景{index}renderer family与积木登记不一致")
            scene_duration = end - start
            bounds = block["duration_seconds"]
            if not float(bounds["minimum"]) <= scene_duration <= float(bounds["maximum"]):
                errors.append(f"场景{index}时长超出积木范围")
            for field, value in (("kicker", scene.get("kicker", "")), ("title", scene.get("title", "")), ("caption", scene.get("caption", ""))):
                if not isinstance(value, str) or not value.strip() or len(value) > int(block["text_limits"][field]):
                    errors.append(f"场景{index}的{field}不符合积木文字限制")
            tags = scene.get("semantic_tags")
            if not isinstance(tags, list) or not tags or any(not isinstance(tag, str) for tag in tags):
                errors.append(f"场景{index}缺少有效语义标签")
            if (
                scene.get("primary_motion") != block["motion"]["primary"]
                or scene.get("secondary_motion") != block["motion"]["secondary"]
                or scene.get("transition") != block["motion"]["transition"]
            ):
                errors.append(f"场景{index}运动声明与可信积木不一致")
    if len(renderer_families) < 3:
        errors.append("整条视频至少需要三种不同renderer family")
    if scenes[-1].get("visual_type") != "orbit-summary":
        errors.append("结尾必须使用orbit-summary汇聚结论")
    expected_duration = float(plan.get("duration_seconds", 0))
    if abs(previous_end - expected_duration) > 0.05:
        errors.append("场景总时长与音频时长不一致")
    try:
        registry.validate_receipt(plan.get("selection_receipt"), scenes, str(pack_mode))
    except AnimationRegistryError as exc:
        errors.append(f"动画选择凭证无效：{exc}")
    if errors:
        raise MotionPlanError("；".join(errors))
    return {
        "ok": True,
        "scene_count": len(scenes),
        "visual_family_count": len(renderer_families),
        "no_static_only_scenes": True,
        "timeline_continuous": True,
        "registry_sha256": registry.summary()["sha256"],
        "selection_receipt_sha256": plan["selection_receipt"]["sha256"],
    }


def build_motion_project(
    project_dir: Path,
    plan: dict[str, Any],
    voice_path: Path | None = None,
    capability_pack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validation = validate_motion_plan(plan)
    if not TEMPLATE_FILE.exists():
        raise FileNotFoundError(f"缺少受信动画模板：{TEMPLATE_FILE}")
    for font_path in (FONT_REGULAR_FILE, FONT_BOLD_FILE):
        if not font_path.is_file() or font_path.is_symlink():
            raise FileNotFoundError(f"缺少已核验离线字体：{font_path.name}")
    project_dir = Path(project_dir)
    assets_dir = project_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FONT_REGULAR_FILE, assets_dir / FONT_REGULAR_FILE.name)
    shutil.copy2(FONT_BOLD_FILE, assets_dir / FONT_BOLD_FILE.name)

    has_audio = bool(voice_path and Path(voice_path).exists())
    if has_audio:
        shutil.copy2(Path(voice_path), assets_dir / "voice.wav")
    html = TEMPLATE_FILE.read_text(encoding="utf-8")
    project = plan.get("project") if isinstance(plan.get("project"), dict) else {}
    legacy = bool(project.get("legacy")) or _is_legacy_pack(capability_pack)
    project_name = str(project.get("name") or ("clean-air-motion-output" if legacy else "evidence-motion-output"))
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", project_name):
        project_name = "clean-air-motion-output" if legacy else "evidence-motion-output"
    brand_name = str(project.get("brand_name") or ("净界AI内容工厂" if legacy else "Evidence Motion"))[:48]
    brand_mark = str(project.get("brand_mark") or ("时" if legacy else "E"))[:2]
    accent = _safe_hex_color((plan.get("design_system") or {}).get("accent"), "#C8E35B")
    html = html.replace("#C8E35B", accent)
    html = html.replace("clean-air-motion-output", project_name)
    html = html.replace(
        "<i>时</i><span>净界AI内容工厂</span>",
        f"<i>{html_lib.escape(brand_mark)}</i><span>{html_lib.escape(brand_name)}</span>",
    )
    if not legacy:
        legacy_report_branch = (
            'if (type === "report-scan") return `<div class="report"><h2>检测报告</h2>'
        )
        generic_process_branch = (
            'if (type === "process-flow") return `<div class="report"><h2>执行路径</h2>'
            '<div class="row"><span>第一步</span><b>问题</b></div>'
            '<div class="row"><span>第二步</span><b>依据</b></div>'
            '<div class="row"><span>第三步</span><b>边界</b></div>'
            '<div class="row"><span>第四步</span><b>行动</b></div>'
            '<div class="scan" data-layout-allow-overflow data-layout-allow-occlusion></div></div>`;\n      '
            + legacy_report_branch
        )
        html = html.replace(legacy_report_branch, generic_process_branch)
        aliases = {
            "stat-ring": "signal-grid",
            "magnifier": "focus-lens",
            "clock-wave": "timeline-pulse",
            "report-scan": "source-stack",
            "compare": "option-compare",
        }
        for template_type, generic_type in aliases.items():
            html = html.replace(
                f'type === "{template_type}"',
                f'(type === "{template_type}" || type === "{generic_type}")',
            )
        html = html.replace(
            '(type === "report-scan" || type === "source-stack")',
            '(type === "report-scan" || type === "source-stack" || type === "process-flow")',
        )
        semantic_replacements = {
            "空间体积": "使用场景",
            "检测报告": "资料来源",
            "检测方法": "核验方法",
            "测试条件": "适用条件",
            "报告来源": "信息来源",
            "实验条件": "已有依据",
            "真实家庭": "实际场景",
            '["条件一","条件二","条件三","条件四","条件五","条件六"]': '["依据","对象","范围","时间","场景","限制"]',
        }
        for old, new in semantic_replacements.items():
            html = html.replace(old, new)
    encoded_plan = json.dumps(plan, ensure_ascii=False).replace("</", "<\\/")
    html = html.replace("__MOTION_PLAN_JSON__", encoded_plan)
    html = html.replace("__DURATION__", f'{float(plan["duration_seconds"]):.3f}')
    html = html.replace(
        "__AUDIO_ELEMENT__",
        '<audio id="narration" class="clip" data-start="0" data-duration="{:.3f}" data-track-index="10" data-volume="1" src="assets/voice.wav"></audio>'.format(plan["duration_seconds"])
        if has_audio
        else "",
    )
    (project_dir / "index.html").write_text(html, encoding="utf-8")
    (project_dir / "motion_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    (project_dir / "package.json").write_text(
        json.dumps(
            {
                "name": project_name,
                "private": True,
                "type": "module",
                "scripts": {
                    "check": "hyperframes check",
                    "render": "hyperframes render",
                },
                "devDependencies": {"hyperframes": "0.7.86"},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (project_dir / "hyperframes.json").write_text(
        json.dumps({"name": project_name, "entry": "index.html"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (project_dir / "meta.json").write_text(
        json.dumps(
            {
                "name": project_name,
                "duration": plan["duration_seconds"],
                "width": 1080,
                "height": 1920,
                "fps": 30,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    first_scene = plan["scenes"][0]["id"]
    caption_assertions = [
        {"kind": "staysInFrame", "selector": f'#{scene["id"]} .caption'}
        for scene in plan["scenes"]
    ]
    (project_dir / "index.motion.json").write_text(
        json.dumps(
            {
                "duration": plan["duration_seconds"],
                "assertions": [
                    {"kind": "appearsBy", "selector": f"#{first_scene} h1", "bySec": 0.8},
                    {"kind": "keepsMoving", "withinSelector": "#scenes", "maxStaticSec": 2.5},
                ] + caption_assertions,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "project_dir": str(project_dir),
        "project_name": project_name,
        "brand_name": brand_name,
        "has_audio": has_audio,
        "validation": validation,
        "animation_registry": dict(plan["animation_registry"]),
        "font_sha256": {
            FONT_REGULAR_FILE.name: hashlib.sha256((assets_dir / FONT_REGULAR_FILE.name).read_bytes()).hexdigest(),
            FONT_BOLD_FILE.name: hashlib.sha256((assets_dir / FONT_BOLD_FILE.name).read_bytes()).hexdigest(),
        },
    }
