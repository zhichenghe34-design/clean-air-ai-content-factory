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

from core.motion_director import build_motion_plan, build_motion_project, derive_motion_segments
from core.provider import BudgetLedger, ProviderError
from core.web_agent import WebResearchAgent
from core.web_tools import TrustedWebToolRegistry


REPO_ROOT = Path(__file__).resolve().parents[1]


def _configured_path(env_name: str, default: str | Path) -> Path:
    return Path(os.getenv(env_name, str(default))).expanduser()


def _tool_path(env_name: str, command_name: str) -> Path:
    configured = os.getenv(env_name)
    if configured:
        return Path(configured).expanduser()
    discovered = shutil.which(command_name)
    if discovered:
        return Path(discovered)
    return Path(command_name)


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
    "render_mode": "animated",
    "require_animation": False,
    "enable_web_research": True,
    "source_urls": [],
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

UNSUPPORTED_GENERALIZATIONS = [
    "很多产品宣称", "好几罐", "远远超过国标", "商家常拿", "极限实验数据",
]

MEDICAL_CLAIM_PATTERNS = [
    r"(?:治疗|治愈|预防)(?:癌症|白血病|哮喘|疾病)",
    r"(?:甲醛|除醛产品).{0,12}(?:导致|诱发|造成).{0,8}(?:癌症|白血病|哮喘|肺损伤|不孕)",
    r"(?:孕妇|婴儿|儿童|母婴).{0,10}(?:绝对安全|没有风险|放心入住)",
]


class ScriptRevisionRequired(RuntimeError):
    workflow_status = "awaiting_script_revision"


def estimate_narration_duration(script: str) -> dict[str, Any]:
    value = str(script or "").strip()
    spoken = len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", value))
    short_pauses = len(re.findall(r"[，、；：,;:]", value))
    long_pauses = len(re.findall(r"[。！？!?]", value))
    seconds = spoken / 4.2 + short_pauses * 0.16 + long_pauses * 0.38
    return {
        "spoken_characters": spoken,
        "estimated_seconds": round(seconds, 2),
        "accepted_range_seconds": [35, 75],
        "target_range_seconds": [45, 60],
    }


def build_local_variants(topic: str, audience: str) -> list[dict[str, Any]]:
    topic = str(topic).strip()
    audience = str(audience).strip()
    safe_topic = re.sub(r"\d+(?:\.\d+)?\s*%|百分之[零一二三四五六七八九十百]+", "高比例", topic)
    safe_topic = re.sub(r"\d+(?:\.\d+)?\s*(?:m[³3]|立方米|平方米|mg(?:/m[³3])?|毫克(?:每立方米)?|罐|倍|小时|分钟|年)", "具体条件", safe_topic, flags=re.IGNORECASE)
    core = (
        f"你问的是“{safe_topic}”。先把肉眼或鼻子感受到的现象，和能够证明室内甲醛水平的证据分开。"
        "气味、颜色变化和短时间体感都只能提供线索，不能单独替代规范检测。"
        "判断一条除醛信息，先核对它讨论的对象和使用场景，再看剂量、空间体积、作用时间、初始浓度、检测方法以及报告来源。"
        "实验条件与真实房间不同，结论就不能直接照搬；缺少来源和适用边界，也不能把宣传话术理解成入住保证。"
        f"对{audience}来说，更稳妥的做法是保存完整报告、持续通风，并在重要入住决策前结合真实房屋情况请专业人员判断。"
    )
    openings = [
        ("A", "问题拆解", "这个问题不能只凭一个表面现象下结论。"),
        ("B", "证据清单", "先记住一个原则：现象是线索，检测才是证据。"),
        ("C", "风险提醒", "最容易误判的地方，是把感受直接当成检测结论。"),
        ("D", "行动建议", "遇到这类问题，可以按证据、条件、场景三步判断。"),
    ]
    return [
        {"id": item_id, "hook_type": hook, "script": opening + core, "reason": f"围绕实际选题“{topic}”生成的本地安全模板。"}
        for item_id, hook, opening in openings
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


def review_script(script: str, approved_findings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    hits = [phrase for phrase in BANNED_PHRASES if phrase in script]
    generalizations = [phrase for phrase in UNSUPPORTED_GENERALIZATIONS if phrase in script]
    percentages = re.findall(r"\d+(?:\.\d+)?\s*%|百分之[零一二三四五六七八九十百]+", script)
    measurements = re.findall(
        r"\d+(?:\.\d+)?\s*(?:m[³3]|立方米|平方米|mg(?:/m[³3])?|毫克(?:每立方米)?|罐|倍|小时|分钟|年)",
        script,
        flags=re.IGNORECASE,
    )
    conditions = [name for name in ("剂量", "空间", "作用时间", "初始浓度", "检测方法", "报告来源") if name in script]
    medical_claims = [match.group(0) for pattern in MEDICAL_CLAIM_PATTERNS for match in re.finditer(pattern, script)]
    warnings: list[dict[str, Any]] = []
    if percentages:
        warnings.append({
            "type": "numeric_claim_context",
            "level": "review",
            "message": "出现百分比，但脚本将其作为待核查的广告主张而非产品承诺。",
            "matches": percentages,
        })
    if measurements:
        warnings.append({
            "type": "unsupported_measurement",
            "level": "block" if approved_findings is None else "review",
            "message": "通用科普稿包含具体测量数字，必须匹配已批准证据并接受人工复核。",
            "matches": measurements,
        })
    if approved_findings is not None and (percentages or measurements):
        evidence_text = json.dumps(approved_findings, ensure_ascii=False)
        unsupported = [value for value in percentages + measurements if re.sub(r"\s+", "", value) not in re.sub(r"\s+", "", evidence_text)]
        if unsupported:
            warnings.append({
                "type": "unsupported_efficacy_number",
                "level": "block",
                "message": "具体功效或测量数字没有已批准finding的证据支持。",
                "matches": unsupported,
            })
    if (percentages or measurements) and len(conditions) < 6:
        warnings.append({
            "type": "missing_conditions",
            "level": "block",
            "message": "数值功效语境没有覆盖六项内部审核条件。",
            "present": conditions,
        })
    for phrase in hits:
        warnings.append({"type": "banned_phrase", "level": "block", "message": f"命中高风险表达：{phrase}"})
    for phrase in generalizations:
        warnings.append({"type": "unsupported_generalization", "level": "block", "message": f"命中无来源行业泛化：{phrase}"})
    for phrase in medical_claims:
        warnings.append({"type": "unsupported_medical_causality", "level": "block", "message": f"命中无证据医学因果：{phrase}"})
    blocked = any(item["level"] == "block" for item in warnings)
    status = "blocked" if blocked else ("needs_human" if warnings else "passed")
    return {
        "status": status,
        "blocked": blocked,
        "warnings": warnings,
        "conditions_present": conditions,
        "human_confirmation_required": True,
        "scope": "通用科普，不构成具体产品功效结论",
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


class ProductionRunner:
    def __init__(
        self,
        provider: Any | None = None,
        research_config: dict[str, Any] | None = None,
        budget: BudgetLedger | None = None,
        voice_adapter: Any | None = None,
        render_adapter: Any | None = None,
    ):
        self.provider = provider
        self.research_config = research_config or {}
        self.budget = budget or getattr(provider, "budget", None) or BudgetLedger(
            int(self.research_config.get("max_provider_calls_per_job", 7))
        )
        if self.provider is not None:
            self.provider.budget = self.budget
        self.voice_adapter = voice_adapter
        self.render_adapter = render_adapter

    def run(self, folder: Path, production_input: dict[str, Any] | None = None) -> dict[str, Any]:
        raise RuntimeError("v2生产线必须通过JobStore分阶段运行并完成人工门禁")

    def run_research_stage(self, folder: Path, production_input: dict[str, Any] | None = None) -> dict[str, Any]:
        folder.mkdir(parents=True, exist_ok=True)
        config = dict(DEFAULT_INPUT)
        config.update(production_input or {})
        research = self._run_research(folder, config)
        atomic_json(folder / "research.json", research)
        insight = self._build_insight(config, research)
        atomic_json(folder / "insight.json", insight)
        return {"research": research, "insight": insight, "budget": self.budget.snapshot()}

    def run_content_stage(
        self,
        folder: Path,
        production_input: dict[str, Any] | None,
        research_approval: dict[str, Any],
    ) -> dict[str, Any]:
        folder.mkdir(parents=True, exist_ok=True)
        config = dict(DEFAULT_INPUT)
        config.update(production_input or {})
        research = json.loads((folder / "research.json").read_text(encoding="utf-8"))
        approved_ids = {
            str(item.get("finding_id"))
            for item in research_approval.get("findings", [])
            if item.get("decision") == "approved"
        }
        research["script_eligible_findings"] = [
            item for item in research.get("findings", []) if str(item.get("finding_id")) in approved_ids
        ]
        approved_findings = list(research["script_eligible_findings"])
        insight = self._build_insight(config, research)
        atomic_json(folder / "insight.json", insight)
        variants, provider_report = self._generate_variants(config, insight)
        provider_report["budget"] = self.budget.snapshot()
        provider_report["tool_calls"] = len(research.get("tool_trace", []))
        atomic_json(folder / "script_variants.json", {"variants": variants, "provider": provider_report})
        approved_path = folder / "approved_script.json"
        safe_candidates = [item for item in variants if not review_script(str(item.get("script", "")), approved_findings)["blocked"]]
        approved = dict((safe_candidates or variants)[0])
        approved.update({"selected_by": "local_compliance_prefilter", "selected_at": datetime.now().astimezone().isoformat(timespec="seconds")})
        atomic_json(approved_path, approved)
        review = review_script(str(approved["script"]), approved_findings)
        if review["blocked"] and self.provider is not None and provider_report.get("source") == "DeepSeek":
            try:
                original_script = str(approved["script"])
                repair = self.provider.repair_content_script(original_script, review, insight)
                provider_report["repair_source"] = "DeepSeek"
                repaired_script = str(repair.get("script", "")).strip()
                if repaired_script:
                    approved.update({"script": repaired_script, "selected_by": "DeepSeek_repair_then_local_rules", "original_blocked_script": original_script, "repair_changes": repair.get("changes", [])})
                    atomic_json(approved_path, approved)
                    review = review_script(repaired_script, approved_findings)
                    review["repair"] = {"applied": True, "changes": repair.get("changes", [])}
            except ProviderError as exc:
                review["repair_error"] = str(exc)
        elif self.provider is not None and provider_report.get("source") == "DeepSeek":
            try:
                model_review = self.provider.review_content_script(approved["script"], review)
                normalized_status = "blocked" if model_review.get("status") == "blocked" else "needs_human"
                review["model_review"] = {**model_review, "status": normalized_status}
                if normalized_status == "blocked":
                    review["warnings"].append({"type": "model_compliance_block", "level": "block", "message": "模型预审发现需阻断的风险，请改用保守脚本。"})
                    review["status"] = "blocked"
                    review["blocked"] = True
                provider_report["review_source"] = "DeepSeek"
            except ProviderError as exc:
                review["model_review_error"] = str(exc)
        if review["blocked"]:
            unsafe_script = str(approved.get("script", ""))
            safe_template = build_local_variants(str(config["topic"]), str(config["audience"]))[0]
            approved.update({
                "script": safe_template["script"],
                "selected_by": "trusted_topic_aware_safety_template",
                "unsafe_candidate_preserved": unsafe_script,
            })
            atomic_json(approved_path, approved)
            previous_warnings = review.get("warnings", [])
            review = review_script(str(approved["script"]), approved_findings)
            review["safety_fallback"] = {
                "applied": True,
                "reason": "候选仍命中本地阻断规则，改用与选题相关的安全模板",
                "previous_warnings": previous_warnings,
            }
        provider_report["budget"] = self.budget.snapshot()
        atomic_json(folder / "script_variants.json", {"variants": variants, "provider": provider_report})
        atomic_json(folder / "review.json", review)
        return {"review": review, "approved_script": approved, "provider": provider_report}

    def run_render_stage(
        self,
        folder: Path,
        production_input: dict[str, Any] | None,
        approvals: dict[str, Any],
    ) -> dict[str, Any]:
        started = time.monotonic()
        started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        config = dict(DEFAULT_INPUT)
        config.update(production_input or {})
        if approvals.get("research", {}).get("status") != "approved" or approvals.get("compliance", {}).get("status") != "approved":
            raise RuntimeError("研究与合规人工门禁尚未全部批准")
        approved = json.loads((folder / "approved_script.json").read_text(encoding="utf-8"))
        review = json.loads((folder / "review.json").read_text(encoding="utf-8"))
        if review.get("status") == "blocked" or review.get("blocked"):
            raise RuntimeError("合规审核仍处于阻断状态")
        voice_report = self.voice_adapter(folder, approved["script"], config) if self.voice_adapter else self._synthesize_voice(folder, approved["script"], config)
        if not (folder / "voice.wav").is_file():
            raise RuntimeError("配音适配器没有生成voice.wav")
        voice_report.update(self._normalize_voice_duration(folder, float(config.get("target_duration_seconds", 52))))
        segments = self._segments(config, str(approved["script"]))
        duration = self._audio_duration(folder / "voice.wav")
        captions = self._write_captions(folder, segments, duration)
        motion_plan = build_motion_plan(config["topic"], config["audience"], segments, duration)
        atomic_json(folder / "motion_plan.json", motion_plan)
        render_mode = str(config.get("render_mode", "animated"))
        if self.render_adapter:
            render_report = self.render_adapter(folder, motion_plan, config)
            if not (folder / "final.mp4").is_file():
                raise RuntimeError("渲染适配器没有生成final.mp4")
        elif render_mode == "animated":
            try:
                render_report = self._render_animated_video(folder, motion_plan, config)
            except Exception as exc:
                (folder / "animation_fallback.log").write_text(str(exc), encoding="utf-8")
                if config.get("require_animation"):
                    raise
                render_report = self._render_video(folder, segments, captions, duration)
                render_report.update({"mode": "static_fallback", "fallback_reason": str(exc)})
        else:
            render_report = self._render_video(folder, segments, captions, duration)
            render_report["mode"] = "static_requested"

        elapsed = round(time.monotonic() - started, 2)
        variants_payload = json.loads((folder / "script_variants.json").read_text(encoding="utf-8"))
        variants = [item for item in variants_payload.get("variants", []) if isinstance(item, dict)]
        provider_report = dict(variants_payload.get("provider", {}))
        provider_report["budget"] = self.budget.snapshot()
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
                "research.json", "insight.json", "script_variants.json", "approved_script.json", "review.json",
                "voice.wav", "captions.srt", "motion_plan.json", "final.mp4", "run_report.json",
            ],
        }
        atomic_json(folder / "run_report.json", report)
        return report

    def _run_research(self, folder: Path, config: dict[str, Any]) -> dict[str, Any]:
        enabled = bool(self.research_config.get("enabled", True)) and bool(config.get("enable_web_research", True))
        if not enabled:
            return {"status": "disabled", "summary": "本任务未启用联网调研", "findings": [], "content_patterns": [], "evidence_gaps": [], "sources": [], "tool_trace": [], "model_calls": 0}
        if self.provider is None or not getattr(self.provider, "api_key", ""):
            return {"status": "offline", "summary": "未配置API Key，跳过Flash工具调度并使用本地范式", "findings": [], "content_patterns": [], "evidence_gaps": ["未运行联网调研"], "sources": [], "tool_trace": [], "model_calls": 0}
        try:
            source_urls = [str(value) for value in config.get("source_urls", []) if str(value).strip()]
            registry = TrustedWebToolRegistry(folder / "research", self.research_config, seed_urls=source_urls)
            agent = WebResearchAgent(
                self.provider,
                registry,
                max_model_turns=int(self.research_config.get("max_model_turns", 5)),
            )
            return agent.run(str(config["topic"]), str(config["audience"]), source_urls)
        except Exception as exc:
            return {
                "status": "failed",
                "summary": "联网调研失败，已降级到本地范式",
                "findings": [],
                "content_patterns": [],
                "evidence_gaps": [str(exc)],
                "sources": [],
                "tool_trace": [],
                "model_calls": 0,
                "budget": self.budget.snapshot(),
            }

    @staticmethod
    def _build_insight(config: dict[str, Any], research: dict[str, Any] | None = None) -> dict[str, Any]:
        ids = {str(value) for value in config.get("pattern_card_ids", [])}
        selected = [card for card in load_pattern_cards() if str(card.get("item_id")) in ids]
        research_for_script = dict(research or {})
        if research_for_script:
            research_for_script["findings"] = list(research_for_script.get("script_eligible_findings", []))
            research_for_script.pop("script_eligible_findings", None)
            research_for_script.pop("tool_trace", None)
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
            "web_research": research_for_script,
        }

    def _generate_variants(self, config: dict[str, Any], insight: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        report: dict[str, Any] = {"source": "local_deterministic", "fallback_used": False}
        if self.provider is None or not getattr(self.provider, "api_key", ""):
            return build_local_variants(str(config["topic"]), str(config["audience"])), report
        try:
            variants = self.provider.generate_content_scripts(config, insight)
            if not isinstance(variants, list) or len(variants) != 4:
                raise ProviderError("脚本接口没有返回4个候选")
            report.update({"source": "DeepSeek"})
            return variants, report
        except ProviderError as exc:
            report.update({"fallback_used": True, "fallback_reason": str(exc)})
            return build_local_variants(str(config["topic"]), str(config["audience"])), report

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
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800, check=True)
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
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, check=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write((result.stdout or "") + "\n" + (result.stderr or ""))
            return {"engine": "windows_sapi", "fallback": True, "fallback_reason": str(exc)}

    @staticmethod
    def _segments(config: dict[str, Any], script: str) -> list[dict[str, str]]:
        provided = config.get("motion_scenes")
        if isinstance(provided, list) and 4 <= len(provided) <= 8 and all(isinstance(item, dict) for item in provided):
            return [
                {
                    "kicker": str(item.get("kicker") or f"要点 {index:02d}"),
                    "title": str(item.get("title") or config["topic"]),
                    "caption": str(item.get("caption") or ""),
                }
                for index, item in enumerate(provided, start=1)
            ]
        return derive_motion_segments(str(config["topic"]), script, target_count=7)

    @staticmethod
    def _audio_duration(path: Path) -> float:
        command = [str(FFPROBE), "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", str(path)]
        return float(subprocess.check_output(command, text=True).strip())

    @classmethod
    def _normalize_voice_duration(cls, folder: Path, target_seconds: float) -> dict[str, Any]:
        voice = folder / "voice.wav"
        duration = cls._audio_duration(voice)
        if 45.0 <= duration <= 60.0:
            return {"duration_seconds": round(duration, 3), "tempo_adjusted": False}
        desired = min(58.0, max(45.0, target_seconds))
        tempo = duration / desired
        if not 0.75 <= tempo <= 1.5:
            raise ScriptRevisionRequired(
                f"配音时长{duration:.2f}秒，需要{tempo:.2f}倍变速，超出0.75到1.5的安全范围"
            )
        if not _tool_available(FFMPEG):
            raise FileNotFoundError("未找到FFmpeg，无法将配音调整到45-60秒")
        original = folder / "voice.original.wav"
        if not original.exists():
            shutil.copy2(voice, original)
        adjusted = folder / "voice.adjusted.wav"
        command = [str(FFMPEG), "-y", "-i", str(original), "-filter:a", f"atempo={tempo:.6f}", str(adjusted)]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        if result.returncode or not adjusted.is_file():
            raise RuntimeError(f"配音时长调整失败: {(result.stderr or result.stdout or '')[-1000:]}")
        adjusted.replace(voice)
        final_duration = cls._audio_duration(voice)
        return {
            "duration_seconds": round(final_duration, 3),
            "tempo_adjusted": True,
            "original_duration_seconds": round(duration, 3),
            "tempo_factor": round(tempo, 4),
            "original_file": "voice.original.wav",
        }

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
            card = (cards_dir / f"scene_{index:02d}.png").resolve().as_posix()
            concat_lines.append(f"file '{card}'")
            concat_lines.append(f"duration {max(0.04, caption['end'] - caption['start']):.3f}")
        concat_lines.append(f"file '{(cards_dir / f'scene_{len(segments):02d}.png').resolve().as_posix()}'")
        concat_path = folder / "video_concat.txt"
        concat_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

        output = folder / "final.mp4"
        command = [
            str(FFMPEG), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_path),
            "-i", str(folder / "voice.wav"), "-vf", "fps=30,format=yuv420p",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(output),
        ]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        (folder / "render.log").write_text((result.stdout or "") + "\n" + (result.stderr or ""), encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"FFmpeg合成失败: {(result.stderr or result.stdout or '')[-1200:]}")
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
            "file": "final.mp4",
            "duration_seconds": round(final_duration, 3),
            "video_codec": video_stream.get("codec_name"),
            "audio_codec": audio_stream.get("codec_name"),
            "width": video_stream.get("width"),
            "height": video_stream.get("height"),
            "fps": video_stream.get("r_frame_rate"),
            "subtitle_mode": "burned_into_scene_cards_and_sidecar_srt",
        }

    def _render_animated_video(self, folder: Path, motion_plan: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if not npm:
            raise FileNotFoundError("未找到npm，无法运行受信HyperFrames动画渲染器")
        executable = REPO_ROOT / "node_modules" / ".bin" / ("hyperframes.cmd" if os.name == "nt" else "hyperframes")
        if not executable.is_file():
            raise FileNotFoundError("HyperFrames适配器未安装；请先在项目根目录执行npm ci，运行时禁止自动下载")
        project_dir = folder / "animation_project"
        build_report = build_motion_project(project_dir, motion_plan, folder / "voice.wav")
        env = os.environ.copy()
        env["PATH"] = str(executable.parent) + os.pathsep + env.get("PATH", "")
        if FFMPEG.exists():
            env["PATH"] = str(FFMPEG.parent) + os.pathsep + env.get("PATH", "")

        check = subprocess.run(
            [npm, "run", "check"], cwd=project_dir, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        (folder / "animation_check.log").write_text((check.stdout or "") + "\n" + (check.stderr or ""), encoding="utf-8")
        if check.returncode:
            raise RuntimeError(f"动画工程检查失败: {(check.stderr or check.stdout or '')[-1200:]}")
        output = project_dir / "renders" / "final.mp4"
        quality = str(config.get("animation_quality", "standard"))
        render = subprocess.run(
            [npm, "run", "render", "--", "--output", "renders/final.mp4", "--quality", quality, "--workers", "2", "--strict"],
            cwd=project_dir, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800,
        )
        (folder / "animation_render.log").write_text((render.stdout or "") + "\n" + (render.stderr or ""), encoding="utf-8")
        if render.returncode:
            raise RuntimeError(f"动画渲染失败: {(render.stderr or render.stdout or '')[-1200:]}")
        if not output.exists():
            raise RuntimeError("HyperFrames命令完成但没有生成final.mp4")
        shutil.copy2(output, folder / "final.mp4")

        probe_command = [str(FFPROBE), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(folder / "final.mp4")]
        probe = json.loads(subprocess.check_output(probe_command, text=True, encoding="utf-8"))
        video_stream = next(stream for stream in probe["streams"] if stream.get("codec_type") == "video")
        audio_stream = next(stream for stream in probe["streams"] if stream.get("codec_type") == "audio")
        final_duration = float(probe["format"]["duration"])
        if video_stream.get("width") != 1080 or video_stream.get("height") != 1920:
            raise RuntimeError("动画成片分辨率不符合1080x1920")
        if not (45 <= final_duration <= 60):
            raise RuntimeError(f"动画成片时长{final_duration:.2f}秒，不在45-60秒范围内")
        return {
            "ok": True,
            "mode": "animated_hyperframes",
            "file": "final.mp4",
            "project_dir": "animation_project",
            "duration_seconds": round(final_duration, 3),
            "video_codec": video_stream.get("codec_name"),
            "audio_codec": audio_stream.get("codec_name"),
            "width": video_stream.get("width"),
            "height": video_stream.get("height"),
            "fps": video_stream.get("r_frame_rate"),
            "motion_validation": build_report["validation"],
            "subtitle_mode": "animated_caption_overlay_and_sidecar_srt",
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
