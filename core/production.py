from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from core.provider import ProviderError


REPO_ROOT = Path(__file__).resolve().parents[1]


def _configured_path(env_name: str, default: str | Path) -> Path:
    return Path(os.getenv(env_name, str(default))).expanduser()


def _tool_path(env_name: str, command_name: str) -> Path:
    configured = os.getenv(env_name)
    if configured:
        return Path(configured).expanduser()
    discovered = shutil.which(command_name)
    return Path(discovered) if discovered else Path(command_name)


def _tool_available(path: Path) -> bool:
    return path.exists() or shutil.which(str(path)) is not None


PATTERN_FILE = _configured_path("PATTERN_FILE", REPO_ROOT / "examples" / "pattern_cards.jsonl")
VOICE_WORKBENCH = _configured_path("VOICE_WORKBENCH", REPO_ROOT / "integrations" / "voice_workbench.py")
VOICE_REFERENCE = _configured_path("VOICE_REFERENCE", REPO_ROOT / "assets" / "voice-reference.wav")
FFMPEG = _tool_path("FFMPEG_PATH", "ffmpeg")
FFPROBE = _tool_path("FFPROBE_PATH", "ffprobe")
FONT_REGULAR = _configured_path("FONT_REGULAR", r"C:\Windows\Fonts\msyh.ttc")
FONT_BOLD = _configured_path("FONT_BOLD", r"C:\Windows\Fonts\msyhbd.ttc")

DEFAULT_INPUT: dict[str, Any] = {
    "topic": "99%除醛率为什么必须看检测条件？",
    "audience": "新房装修、租房、母婴家庭",
    "target_duration_seconds": 52,
    "pattern_card_ids": ["03", "06"],
    "voice_engine": "voxcpm2",
    "aspect_ratio": "9:16",
}

DEFAULT_SCRIPT = (
    "看到“除醛率百分之九十九”，先别急着下结论。真正决定这个数字能不能参考的，往往是旁边那行小字。"
    "第一，看用了多少产品；第二，看测试舱有多大；第三，看作用了多久；第四，看初始浓度和检测方法。"
    "小空间、大剂量、长时间得出的结果，不能直接等同于你家的整屋效果。"
    "判断一份除醛数据，至少要找齐六项：剂量、空间体积、作用时间、初始浓度、检测方法，以及报告来源。"
    "缺少任何一项，都只能把它当作线索，不能当成入住保证。"
    "真正有用的内容，不是把数字喊得更大，而是把条件讲清楚。"
    "涉及具体产品时，还要回到它自己的检测报告，并由专业人员结合真实房屋情况判断。"
)

LOCAL_VARIANTS = [
    {
        "id": "A",
        "hook_type": "反常识",
        "script": DEFAULT_SCRIPT,
        "reason": "大字主张与小字条件形成反差，证据边界最清楚。",
    },
    {
        "id": "B",
        "hook_type": "问题悬念",
        "script": (
            "同样写着除醛率百分之九十九，为什么两个结果可能完全不是一回事？"
            "因为百分比后面还有测试条件。产品用了多少、测试空间多大、作用了多久、初始浓度是多少、怎样检测，都会改变结果。"
            "在小舱里用较大剂量测试，不能直接等同于普通家庭的整屋使用。"
            "看报告时，请同时寻找剂量、体积、时间、初始浓度、检测方法和报告来源。"
            "条件不完整，就先把数字当线索，不要把它理解成入住保证。具体产品仍应回到自己的检测报告和真实使用场景。"
        ),
        "reason": "先抛出同数不同义的问题，适合科普账号。",
    },
    {
        "id": "C",
        "hook_type": "具体痛点",
        "script": (
            "买除醛产品最怕什么？不是没有数字，而是只看见百分之九十九，却没看见它怎么测出来。"
            "判断这类数据，先检查六件事：用了多少产品，空间有多大，作用多久，初始浓度多少，用什么方法检测，以及报告来自哪里。"
            "测试舱越小、剂量越大、时间越长，结果越不能直接照搬到整屋。"
            "所以一个数字能不能参考，要看它的条件能不能对应你的房间。"
            "没有完整条件时，不把它当作效果保证；涉及具体产品时，再回到检测报告和实际房屋情况判断。"
        ),
        "reason": "从购买焦虑切入，承接清晰。",
    },
    {
        "id": "D",
        "hook_type": "清单承诺",
        "script": (
            "十秒教你看懂除醛率百分之九十九后面的门道。"
            "先找剂量，再找测试空间；再看作用时间、初始浓度和检测方法，最后确认报告来源。"
            "这六项缺一项，结论就少一个边界。尤其是小空间、大剂量、长时间的结果，不能直接等同于家庭整屋。"
            "这不是说数字一定没用，而是要知道它在什么条件下成立。"
            "具体产品的判断，应回到对应检测报告，并结合真实面积、通风和使用方式。看清条件，比只记住一个百分比更有用。"
        ),
        "reason": "用六项清单提供即时奖励，但避免绝对承诺。",
    },
]

BANNED_PHRASES = [
    "绝对安全", "彻底去除", "完全去除", "零甲醛", "立即入住", "母婴零风险",
    "百分百安全", "永久有效", "国家级", "最高级", "最佳",
]


def atomic_json(path: Path, data: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def load_pattern_cards() -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for line in PATTERN_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cards.append(json.loads(line))
    return cards


def review_script(script: str) -> dict[str, Any]:
    hits = [phrase for phrase in BANNED_PHRASES if phrase in script]
    percentages = re.findall(r"\d+(?:\.\d+)?\s*%|百分之[零一二三四五六七八九十百]+", script)
    conditions = [name for name in ("剂量", "空间", "作用时间", "初始浓度", "检测方法", "报告来源") if name in script]
    warnings: list[dict[str, Any]] = []
    if percentages:
        warnings.append({
            "type": "numeric_claim_context",
            "level": "review",
            "message": "出现百分比，但脚本将其作为待核查的广告主张而非产品承诺。",
            "matches": percentages,
        })
    if len(conditions) < 6:
        warnings.append({
            "type": "missing_conditions",
            "level": "block",
            "message": "数值功效语境没有覆盖六项内部审核条件。",
            "present": conditions,
        })
    for phrase in hits:
        warnings.append({"type": "banned_phrase", "level": "block", "message": f"命中高风险表达：{phrase}"})
    blocked = any(item["level"] == "block" for item in warnings)
    return {
        "status": "blocked" if blocked else "human_review_required",
        "blocked": blocked,
        "warnings": warnings,
        "conditions_present": conditions,
        "human_confirmation_required": True,
        "scope": "通用科普，不构成具体产品功效结论",
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


class ProductionRunner:
    def __init__(self, provider: Any | None = None):
        self.provider = provider

    def run(self, folder: Path, production_input: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.monotonic()
        started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        folder.mkdir(parents=True, exist_ok=True)
        config = dict(DEFAULT_INPUT)
        config.update(production_input or {})

        insight = self._build_insight(config)
        atomic_json(folder / "insight.json", insight)

        variants, provider_report = self._generate_variants(config, insight)
        atomic_json(folder / "script_variants.json", {"variants": variants, "provider": provider_report})

        approved_path = folder / "approved_script.json"
        if approved_path.exists():
            approved = json.loads(approved_path.read_text(encoding="utf-8"))
        else:
            approved = dict(variants[0])
            approved.update({"approved_by": "demo_default", "approved_at": datetime.now().astimezone().isoformat(timespec="seconds")})
            atomic_json(approved_path, approved)

        review = review_script(str(approved["script"]))
        if self.provider is not None and provider_report.get("api_calls", 0) == 1:
            try:
                model_review = self.provider.review_content_script(approved["script"], review)
                review["model_review"] = model_review
                provider_report["api_calls"] = 2
                provider_report["review_source"] = "DeepSeek"
                atomic_json(folder / "script_variants.json", {"variants": variants, "provider": provider_report})
            except ProviderError as exc:
                review["model_review_error"] = str(exc)
        atomic_json(folder / "review.json", review)
        if review["blocked"]:
            raise RuntimeError("合规审核阻止成片：请修改 approved_script.json 后重试")

        voice_report = self._synthesize_voice(folder, approved["script"], config)
        segments = self._segments()
        duration = self._audio_duration(folder / "voice.wav")
        captions = self._write_captions(folder, segments, duration)
        render_report = self._render_video(folder, segments, captions, duration)

        elapsed = round(time.monotonic() - started, 2)
        report = {
            "status": "complete",
            "topic": config["topic"],
            "started_at": started_at,
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "wall_clock_seconds": elapsed,
            "provider": provider_report,
            "voice": voice_report,
            "render": render_report,
            "compliance": review,
            "adoption_proxy": {
                "candidate_count": len(variants),
                "provisionally_usable_count": sum(not review_script(item["script"])["blocked"] for item in variants),
                "definition": "未命中阻断项且结构完整；尚未经过企业运营团队验证",
            },
            "artifacts": [
                "insight.json", "script_variants.json", "approved_script.json", "review.json",
                "voice.wav", "captions.srt", "final.mp4", "run_report.json",
            ],
        }
        atomic_json(folder / "run_report.json", report)
        return report

    @staticmethod
    def _build_insight(config: dict[str, Any]) -> dict[str, Any]:
        ids = {str(value) for value in config.get("pattern_card_ids", [])}
        selected = [card for card in load_pattern_cards() if str(card.get("item_id")) in ids]
        return {
            "topic": config["topic"],
            "audience": config["audience"],
            "selected_pattern_cards": selected,
            "pattern": "大字主张→小字条件→条件换算→现实场景反差→行动建议",
            "source_boundary": "公开视频仅用于学习内容结构，不作为产品功效证据",
            "evidence_requirements": ["剂量", "空间体积", "作用时间", "初始浓度", "检测方法", "报告来源"],
            "official_references": [
                {"name": "GB/T 18883-2022 室内空气质量标准", "url": "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=6188E23AE55E8F557043401FC2EDC436"},
                {"name": "中华人民共和国广告法", "url": "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/fgs/art/2023/art_5474cf75173c45d6a0379730fb4e8d97.html"},
            ],
        }

    def _generate_variants(self, config: dict[str, Any], insight: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        report: dict[str, Any] = {"source": "local_deterministic", "api_calls": 0, "fallback_used": False}
        if self.provider is None or not getattr(self.provider, "api_key", ""):
            return [dict(item) for item in LOCAL_VARIANTS], report
        try:
            variants = self.provider.generate_content_scripts(config, insight)
            if not isinstance(variants, list) or len(variants) != 4:
                raise ProviderError("脚本接口没有返回4个候选")
            report.update({"source": "DeepSeek", "api_calls": 1})
            return variants, report
        except ProviderError as exc:
            report.update({"fallback_used": True, "fallback_reason": str(exc)})
            return [dict(item) for item in LOCAL_VARIANTS], report

    @staticmethod
    def _synthesize_voice(folder: Path, script: str, config: dict[str, Any]) -> dict[str, Any]:
        text_path = folder / "narration.txt"
        previous_text = text_path.read_text(encoding="utf-8") if text_path.exists() else None
        existing_voice = folder / "voice.wav"
        if previous_text == script and existing_voice.exists() and existing_voice.stat().st_size > 44:
            return {"engine": config.get("voice_engine", "voxcpm2"), "fallback": False, "reused": True, "reason": "脚本未变化，复用已通过QC的配音"}
        text_path.write_text(script, encoding="utf-8")
        voice_dir = folder / "voice_parts"
        command = [
            sys.executable, str(VOICE_WORKBENCH), "--engine", str(config.get("voice_engine", "voxcpm2")),
            "--text-file", str(text_path), "--reference-audio", str(VOICE_REFERENCE),
            "--output-dir", str(voice_dir), "--max-chars", "260",
        ]
        log_path = folder / "voice_generation.log"
        try:
            if not VOICE_WORKBENCH.exists() or not VOICE_REFERENCE.exists():
                raise FileNotFoundError("Local Voice Workbench或参考音频不存在")
            result = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=True)
            log_path.write_text((result.stdout or "") + "\n" + (result.stderr or ""), encoding="utf-8")
            merged = voice_dir / "merged.wav"
            if not merged.exists():
                raise RuntimeError("语音工作台未生成 merged.wav")
            shutil.copy2(merged, folder / "voice.wav")
            qc = json.loads((voice_dir / "qc_report.json").read_text(encoding="utf-8"))
            return {"engine": config.get("voice_engine", "voxcpm2"), "fallback": False, "qc": qc}
        except Exception as exc:
            log_path.write_text(f"Primary voice failed: {exc}\n", encoding="utf-8")
            fallback = Path(__file__).resolve().parent / "sapi_tts.ps1"
            command = [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(fallback),
                "-TextFile", str(text_path), "-OutputFile", str(folder / "voice.wav"),
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write((result.stdout or "") + "\n" + (result.stderr or ""))
            return {"engine": "windows_sapi", "fallback": True, "fallback_reason": str(exc)}

    @staticmethod
    def _segments() -> list[dict[str, str]]:
        return [
            {"kicker": "别只看大字", "title": "除醛率 99%？", "caption": "真正决定这个数字能不能参考的，往往是旁边那行小字。"},
            {"kicker": "条件 01-02", "title": "剂量 × 空间", "caption": "用了多少产品？测试舱有多大？"},
            {"kicker": "条件 03-04", "title": "时间 × 浓度", "caption": "作用了多久？初始浓度是多少？"},
            {"kicker": "条件 05-06", "title": "方法 × 来源", "caption": "怎样检测？报告来自哪里？"},
            {"kicker": "现实场景", "title": "小舱 ≠ 整屋", "caption": "小空间、大剂量、长时间的结果，不能直接等同家庭整屋效果。"},
            {"kicker": "判断原则", "title": "条件比数字更重要", "caption": "具体产品应回到自己的检测报告，并结合真实房屋情况判断。"},
        ]

    @staticmethod
    def _audio_duration(path: Path) -> float:
        command = [str(FFPROBE), "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", str(path)]
        return float(subprocess.check_output(command, text=True).strip())

    @staticmethod
    def _srt_time(seconds: float) -> str:
        milliseconds = max(0, round(seconds * 1000))
        hours, milliseconds = divmod(milliseconds, 3_600_000)
        minutes, milliseconds = divmod(milliseconds, 60_000)
        secs, milliseconds = divmod(milliseconds, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"

    def _write_captions(self, folder: Path, segments: list[dict[str, str]], duration: float) -> list[dict[str, Any]]:
        weights = [max(1, len(item["caption"])) for item in segments]
        total = sum(weights)
        cursor = 0.0
        output: list[dict[str, Any]] = []
        lines: list[str] = []
        for index, (segment, weight) in enumerate(zip(segments, weights), start=1):
            end = duration if index == len(segments) else cursor + duration * weight / total
            item = dict(segment)
            item.update({"start": round(cursor, 3), "end": round(end, 3)})
            output.append(item)
            lines.extend([str(index), f"{self._srt_time(cursor)} --> {self._srt_time(end)}", segment["caption"], ""])
            cursor = end
        (folder / "captions.srt").write_text("\n".join(lines), encoding="utf-8")
        return output

    def _render_video(self, folder: Path, segments: list[dict[str, str]], captions: list[dict[str, Any]], duration: float) -> dict[str, Any]:
        if not _tool_available(FFMPEG) or not _tool_available(FFPROBE):
            raise FileNotFoundError("未找到FFmpeg或FFprobe；请加入PATH或设置FFMPEG_PATH/FFPROBE_PATH")
        cards_dir = folder / "cards"
        cards_dir.mkdir(exist_ok=True)
        for index, segment in enumerate(segments, start=1):
            self._draw_card(cards_dir / f"scene_{index:02d}.png", segment, index, len(segments))

        concat_lines: list[str] = []
        for index, caption in enumerate(captions, start=1):
            card = (cards_dir / f"scene_{index:02d}.png").as_posix()
            concat_lines.append(f"file '{card}'")
            concat_lines.append(f"duration {max(0.04, caption['end'] - caption['start']):.3f}")
        concat_lines.append(f"file '{(cards_dir / f'scene_{len(segments):02d}.png').as_posix()}'")
        concat_path = folder / "video_concat.txt"
        concat_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

        output = folder / "final.mp4"
        command = [
            str(FFMPEG), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_path),
            "-i", str(folder / "voice.wav"), "-vf", "fps=30,format=yuv420p",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(output),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=900, check=True)
        (folder / "render.log").write_text((result.stdout or "") + "\n" + (result.stderr or ""), encoding="utf-8")
        probe_command = [str(FFPROBE), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)]
        probe = json.loads(subprocess.check_output(probe_command, text=True, encoding="utf-8"))
        video_stream = next(stream for stream in probe["streams"] if stream.get("codec_type") == "video")
        audio_stream = next(stream for stream in probe["streams"] if stream.get("codec_type") == "audio")
        final_duration = float(probe["format"]["duration"])
        if video_stream.get("width") != 1080 or video_stream.get("height") != 1920:
            raise RuntimeError("成片分辨率不符合1080x1920")
        if not (45 <= final_duration <= 60):
            raise RuntimeError(f"成片时长{final_duration:.2f}秒，不在45-60秒范围内")
        return {
            "ok": True,
            "file": str(output),
            "duration_seconds": round(final_duration, 3),
            "video_codec": video_stream.get("codec_name"),
            "audio_codec": audio_stream.get("codec_name"),
            "width": video_stream.get("width"),
            "height": video_stream.get("height"),
            "fps": video_stream.get("r_frame_rate"),
            "subtitle_mode": "burned_into_scene_cards_and_sidecar_srt",
        }

    @staticmethod
    def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(str(path if path.exists() else FONT_REGULAR), size=size)

    @staticmethod
    def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
        lines: list[str] = []
        current = ""
        for char in text:
            candidate = current + char
            if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines

    def _draw_card(self, path: Path, segment: dict[str, str], index: int, count: int) -> None:
        width, height = 1080, 1920
        image = Image.new("RGB", (width, height), "#09261f")
        draw = ImageDraw.Draw(image)
        for y in range(height):
            ratio = y / height
            draw.line((0, y, width, y), fill=(9 + int(15 * ratio), 38 + int(26 * ratio), 31 + int(19 * ratio)))
        lime, cream, mint, orange = "#D7EF68", "#F6F3E8", "#9CCBB7", "#EF8058"
        draw.ellipse((-170, -150, 560, 580), fill="#123D32")
        draw.ellipse((700, 1320, 1330, 1980), fill="#173D34")
        for x in range(70, 1010, 90):
            draw.line((x, 0, x - 360, height), fill="#153D34", width=2)

        kicker_font = self._font(FONT_BOLD, 38)
        number_font = self._font(FONT_BOLD, 30)
        title_font = self._font(FONT_BOLD, 104 if len(segment["title"]) <= 10 else 76)
        caption_font = self._font(FONT_REGULAR, 50)
        small_font = self._font(FONT_REGULAR, 29)

        draw.rounded_rectangle((70, 92, 470, 158), radius=33, fill=lime)
        draw.text((100, 103), segment["kicker"], font=kicker_font, fill="#10362C")
        draw.text((845, 105), f"{index:02d}/{count:02d}", font=number_font, fill=mint)

        title_y = 430
        title_lines = self._wrap(draw, segment["title"], title_font, 880)
        for line in title_lines:
            draw.text((80, title_y), line, font=title_font, fill=cream)
            title_y += 135
        draw.rounded_rectangle((80, title_y + 38, 300, title_y + 54), radius=8, fill=orange)

        card_top = 1120
        draw.rounded_rectangle((70, card_top, 1010, 1610), radius=44, fill="#F4F1E6")
        draw.text((116, card_top + 58), "判断提示", font=small_font, fill="#4C6B60")
        caption_lines = self._wrap(draw, segment["caption"], caption_font, 830)
        y = card_top + 130
        for line in caption_lines:
            draw.text((116, y), line, font=caption_font, fill="#153B31")
            y += 78

        draw.text((78, 1770), "净界AI内容工厂", font=small_font, fill=mint)
        draw.text((680, 1770), "先看条件，再看数字", font=small_font, fill=cream)
        progress = 860 * index / count
        draw.rounded_rectangle((80, 1830, 1000, 1844), radius=7, fill="#31584B")
        draw.rounded_rectangle((80, 1830, 80 + progress, 1844), radius=7, fill=lime)
        image.save(path, quality=95)
