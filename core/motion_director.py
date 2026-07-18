from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "agent-skills" / "produce-dynamic-health-video"
TEMPLATE_FILE = SKILL_ROOT / "assets" / "composition-template.html"

VISUAL_SEQUENCE = (
    "stat-ring",
    "magnifier",
    "liquid-chamber",
    "clock-wave",
    "report-scan",
    "compare",
    "orbit-summary",
)

MOTION_RECIPES = {
    "stat-ring": ("数字计数与圆环描边", "呼吸光晕", "lime-wipe"),
    "magnifier": ("放大镜沿证据字段扫描", "小字逐行显现", "green-wipe"),
    "liquid-chamber": ("液位上升与空间框展开", "剂量/体积标签弹入", "lime-wipe"),
    "clock-wave": ("指针旋转与波形脉冲", "时间标签滑入", "green-wipe"),
    "report-scan": ("报告面板升起与扫描线下移", "字段逐行显现", "green-wipe"),
    "compare": ("左右场景对向入场", "不等号脉冲", "orange-wipe"),
    "orbit-summary": ("要点围绕核心结论汇聚", "核心光环呼吸", "lime-wipe"),
}


class MotionPlanError(ValueError):
    pass


def derive_motion_segments(topic: str, script: str, target_count: int = 7) -> list[dict[str, str]]:
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
        rules = [
            (("气味", "鼻子", "嗅觉"), "嗅觉只是线索", "别让感受替代检测"),
            (("剂量", "空间", "体积"), "剂量 × 空间", "先看使用和空间条件"),
            (("时间", "浓度", "温度", "湿度"), "时间与环境条件", "结果取决于过程"),
            (("检测", "报告", "方法", "来源"), "方法 × 报告来源", "结论必须可追溯"),
            (("不能直接", "不等同", "整屋", "真实"), "实验条件 ≠ 真实场景", "不要直接外推"),
            (("至少", "找齐", "缺少", "条件"), "条件缺一不可", "判断前先补齐信息"),
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
            kicker = "最终判断原则"
            title = title if len(title) <= 16 else str(topic)[:16]
        segments.append({"kicker": kicker, "title": title, "caption": caption[:62]})
    return segments


def build_motion_plan(
    topic: str,
    audience: str,
    segments: list[dict[str, Any]],
    duration_seconds: float,
) -> dict[str, Any]:
    if not 4 <= len(segments) <= 8:
        raise MotionPlanError("动态短视频必须包含4到8个场景")
    duration = float(duration_seconds)
    if duration <= 0:
        raise MotionPlanError("音频时长必须大于0")

    weights = [max(8, len(str(item.get("caption", "")))) for item in segments]
    total_weight = sum(weights)
    cursor = 0.0
    scenes: list[dict[str, Any]] = []
    for index, (segment, weight) in enumerate(zip(segments, weights), start=1):
        end = duration if index == len(segments) else cursor + duration * weight / total_weight
        visual = "orbit-summary" if index == len(segments) else VISUAL_SEQUENCE[min(index - 1, len(VISUAL_SEQUENCE) - 2)]
        primary, secondary, transition = MOTION_RECIPES[visual]
        scenes.append(
            {
                "id": f"scene-{index:02d}",
                "index": index,
                "start": round(cursor, 3),
                "end": round(end, 3),
                "kicker": str(segment.get("kicker") or f"要点 {index:02d}"),
                "title": str(segment.get("title") or topic),
                "caption": str(segment.get("caption") or "").strip(),
                "visual_type": visual,
                "primary_motion": primary,
                "secondary_motion": secondary,
                "transition": transition,
                "entrance_lead_seconds": 0.22,
            }
        )
        cursor = end

    plan = {
        "schema_version": "1.0",
        "topic": str(topic).strip(),
        "audience": str(audience).strip(),
        "duration_seconds": round(duration, 3),
        "format": {"width": 1080, "height": 1920, "fps": 30, "aspect_ratio": "9:16"},
        "design_system": {
            "background": "#071713",
            "foreground": "#F1EFE7",
            "accent": "#C8E35B",
            "secondary": "#1F5A46",
            "risk": "#DF744F",
            "style": "evidence-led motion graphics, not slide cards",
        },
        "director_rules": [
            "每个场景必须有主体运动和辅助运动",
            "下一场主体在遮罩离场前开始入场，禁止转场后空场",
            "同一条视频至少使用三种不同视觉语法",
            "字幕最多两行，强调词作为不可拆分短语",
            "结尾必须将分散条件汇聚为一个可执行判断原则",
            "不把实验条件动画表现成具体产品功效证明",
        ],
        "scenes": scenes,
    }
    validate_motion_plan(plan)
    return plan


def validate_motion_plan(plan: dict[str, Any]) -> dict[str, Any]:
    scenes = plan.get("scenes")
    if not isinstance(scenes, list) or not 4 <= len(scenes) <= 8:
        raise MotionPlanError("motion_plan.scenes必须包含4到8项")
    errors: list[str] = []
    visual_types: set[str] = set()
    previous_end = 0.0
    for index, scene in enumerate(scenes, start=1):
        visual = str(scene.get("visual_type", ""))
        visual_types.add(visual)
        if visual not in MOTION_RECIPES:
            errors.append(f"场景{index}使用未知视觉类型：{visual}")
        start, end = float(scene.get("start", -1)), float(scene.get("end", -1))
        if abs(start - previous_end) > 0.02 or end <= start:
            errors.append(f"场景{index}时间轴不连续")
        previous_end = end
        if not str(scene.get("primary_motion", "")).strip() or not str(scene.get("secondary_motion", "")).strip():
            errors.append(f"场景{index}缺少双层运动")
        if len(str(scene.get("caption", ""))) > 62:
            errors.append(f"场景{index}字幕过长，可能超过两行")
    if len(visual_types) < 3:
        errors.append("整条视频至少需要三种不同视觉语法")
    if scenes[-1].get("visual_type") != "orbit-summary":
        errors.append("结尾必须使用orbit-summary汇聚结论")
    expected_duration = float(plan.get("duration_seconds", 0))
    if abs(previous_end - expected_duration) > 0.05:
        errors.append("场景总时长与音频时长不一致")
    if errors:
        raise MotionPlanError("；".join(errors))
    return {
        "ok": True,
        "scene_count": len(scenes),
        "visual_family_count": len(visual_types),
        "no_static_only_scenes": True,
        "timeline_continuous": True,
    }


def build_motion_project(project_dir: Path, plan: dict[str, Any], voice_path: Path | None = None) -> dict[str, Any]:
    validation = validate_motion_plan(plan)
    if not TEMPLATE_FILE.exists():
        raise FileNotFoundError(f"缺少受信动画模板：{TEMPLATE_FILE}")
    project_dir = Path(project_dir)
    assets_dir = project_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    has_audio = bool(voice_path and Path(voice_path).exists())
    if has_audio:
        shutil.copy2(Path(voice_path), assets_dir / "voice.wav")
    html = TEMPLATE_FILE.read_text(encoding="utf-8")
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
                "name": "clean-air-motion-output",
                "private": True,
                "type": "module",
                "scripts": {
                    "check": "npx --yes hyperframes@0.7.62 check",
                    "render": "npx --yes hyperframes@0.7.62 render",
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (project_dir / "hyperframes.json").write_text(
        json.dumps({"name": "clean-air-motion-output", "entry": "index.html"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (project_dir / "meta.json").write_text(
        json.dumps(
            {
                "name": "clean-air-motion-output",
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
    return {"project_dir": str(project_dir), "has_audio": has_audio, "validation": validation}
