from __future__ import annotations

import hashlib
import json
import inspect
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import unicodedata
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, ImageStat

from core.motion_director import build_motion_plan, build_motion_project, derive_motion_segments
from core.program_audio import build_default_program_audio
from core.storyboard_agent import (
    StoryboardError,
    build_local_storyboard,
    storyboard_to_motion_segments,
    validate_storyboard,
)
from core.motion_runtime_contract import (
    H264_CODEC_STRATEGY,
    HYPERFRAMES_RENDERER,
    HYPERFRAMES_PATCHED_CLI_SHA256,
    HYPERFRAMES_PATCH_ID,
    HYPERFRAMES_PATCH_VERSION,
    HYPERFRAMES_VERSION,
    MOTION_ENGINE_NAME,
    SYSTEM_BROWSER_MINIMUM_MAJOR,
    SYSTEM_BROWSER_STRATEGY,
    h264_mf_video_args,
)
from core.production_engine import ENGINE_COMMIT, ENGINE_MODE, ENGINE_NAME, ENGINE_VERSION
from core.provider import BudgetLedger, ProviderError
from core.web_agent import (
    CLEAN_AIR_EXACT_TOPIC_MARKERS,
    EXACT_EVIDENCE_RULES,
    WebResearchAgent,
    _capability_pack_has_clean_air_scope,
)
from core.web_tools import TrustedWebToolRegistry
from core.voice_contract import (
    DEFAULT_VOICE_CHUNK_MAX_CHARS,
    DEFAULT_VOICE_ENGINE,
    DEFAULT_VOICE_LABEL,
    DEFAULT_VOICE_NAME,
    DEFAULT_VOICE_RATE,
    NATURAL_VOICE_ENGINES,
    normalize_voice_chunk_max_chars,
    normalize_voice_engine,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_MODES = frozenset({"motion", "footage", "hybrid", "simple"})
MOTION_ENGINE_MODE = "local_cli"
RENDER_PROVIDER_ENV_PREFIXES = (
    "AIHUBMIX_",
    "ANTHROPIC_",
    "DEEPSEEK_",
    "GEMINI_",
    "OPENAI_",
    "OPENROUTER_",
    "PRODUCER_",
)
RENDER_PROVIDER_ENV_NAMES = frozenset(
    {"AUTHORIZATION", "COOKIE", "HF_TOKEN", "MODEL_PROVIDER"}
)


def _hyperframes_subprocess_environment() -> dict[str, str]:
    """Keep the pinned render runtime while excluding Provider credentials."""

    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in RENDER_PROVIDER_ENV_NAMES
        and not key.upper().startswith(RENDER_PROVIDER_ENV_PREFIXES)
    }
    env.update(
        {
            "HYPERFRAMES_NO_UPDATE_CHECK": "1",
            "HYPERFRAMES_NO_SKILL_CHECK": "1",
            "HYPERFRAMES_TELEMETRY": "0",
            "NO_UPDATE_NOTIFIER": "1",
            "PRODUCER_TELEMETRY": "0",
            "PRODUCER_ENABLE_TELEMETRY": "false",
            "npm_config_offline": "true",
            "npm_config_update_notifier": "false",
            "SHIYI_HYPERFRAMES_CODEC_STRATEGY": H264_CODEC_STRATEGY,
        }
    )
    return env


def resolve_production_mode(
    production_input: dict[str, Any] | None,
    *,
    legacy_engine_adapter: bool = False,
) -> str:
    """Resolve the new mode contract while retaining narrow v0.2 compatibility."""

    values = production_input or {}
    mode = values.get("production_mode")
    if mode is None:
        if legacy_engine_adapter:
            # Before production_mode existed, explicitly injecting the coarse
            # production adapter was the only way to select footage rendering.
            return "footage"
        return "simple" if values.get("render_mode") == "simple" else "motion"
    if not isinstance(mode, str) or mode not in PRODUCTION_MODES:
        raise RuntimeError("生产模式无效")
    return mode


def production_mode_contract(mode: str) -> dict[str, Any]:
    """Return legacy fields canonicalized for a selected production mode."""

    if mode == "motion":
        return {"production_mode": mode, "render_mode": "animated", "require_animation": True}
    if mode in {"footage", "hybrid"}:
        return {"production_mode": mode, "render_mode": "animated", "require_animation": False}
    if mode == "simple":
        return {"production_mode": mode, "render_mode": "simple", "require_animation": False}
    raise RuntimeError("生产模式无效")


def _resolve_hyperframes_runtime() -> dict[str, Any]:
    """Resolve the pinned CLI plus the launcher-verified system Edge identity."""

    configured_node = os.environ.get("SHIYI_NODE_EXECUTABLE", "").strip()
    configured_cli = os.environ.get("SHIYI_HYPERFRAMES_CLI", "").strip()
    configured_browser = os.environ.get("HYPERFRAMES_BROWSER_PATH", "").strip()
    packaged = bool(configured_node or configured_cli or configured_browser) or (
        os.environ.get("SHIYI_PACKAGED_RUNTIME", "").strip() == "1"
    )
    if packaged:
        if not configured_node or not configured_cli or not configured_browser:
            raise FileNotFoundError("封包HyperFrames运行时不完整")
        node = Path(configured_node).expanduser()
        cli = Path(configured_cli).expanduser()
        browser = Path(configured_browser).expanduser()
        if not node.is_file() or not cli.is_file() or not browser.is_file():
            raise FileNotFoundError("封包HyperFrames运行时文件不可用")
        browser_strategy = os.environ.get("SHIYI_HYPERFRAMES_BROWSER_STRATEGY", "").strip()
        browser_version = os.environ.get("SHIYI_HYPERFRAMES_BROWSER_VERSION", "").strip()
        browser_minimum = os.environ.get("SHIYI_HYPERFRAMES_BROWSER_MINIMUM_MAJOR", "").strip()
        if browser_strategy != SYSTEM_BROWSER_STRATEGY or browser_minimum != str(SYSTEM_BROWSER_MINIMUM_MAJOR):
            raise ValueError("封包浏览器策略未绑定到受信系统Edge")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", browser_version):
            raise ValueError("封包系统Edge版本身份无效")
        if int(browser_version.split(".", 1)[0]) < SYSTEM_BROWSER_MINIMUM_MAJOR:
            raise ValueError(f"封包系统Edge版本低于最低要求 {SYSTEM_BROWSER_MINIMUM_MAJOR}")
        runtime_version = _verified_hyperframes_version(cli)
        return {
            "node": node,
            "cli": cli,
            "browser": browser,
            "runtime_source": "packaged",
            "version": runtime_version,
            "renderer": HYPERFRAMES_RENDERER,
            "codec_strategy": H264_CODEC_STRATEGY,
            "patch_id": HYPERFRAMES_PATCH_ID,
            "patch_version": HYPERFRAMES_PATCH_VERSION,
            "patched_cli_sha256": HYPERFRAMES_PATCHED_CLI_SHA256,
            "browser_strategy": browser_strategy,
            "browser_version": browser_version,
            "browser_minimum_major": SYSTEM_BROWSER_MINIMUM_MAJOR,
        }

    # Development mode may use only the already-installed, pinned repository
    # dependency.  It never downloads packages and never invokes npm/npx.
    discovered_node = shutil.which("node.exe" if os.name == "nt" else "node")
    cli = REPO_ROOT / "node_modules" / "hyperframes" / "bin" / "hyperframes.mjs"
    if not discovered_node or not cli.is_file():
        raise FileNotFoundError("开发模式HyperFrames运行时未安装；请离线执行项目锁定依赖安装")
    runtime_version = _verified_hyperframes_version(cli)
    return {
        "node": Path(discovered_node),
        "cli": cli,
        "browser": None,
        "runtime_source": "development_repo",
        "version": runtime_version,
        "renderer": HYPERFRAMES_RENDERER,
        "codec_strategy": H264_CODEC_STRATEGY,
        "patch_id": HYPERFRAMES_PATCH_ID,
        "patch_version": HYPERFRAMES_PATCH_VERSION,
        "patched_cli_sha256": HYPERFRAMES_PATCHED_CLI_SHA256,
        "browser_strategy": None,
        "browser_version": None,
        "browser_minimum_major": SYSTEM_BROWSER_MINIMUM_MAJOR,
    }


def _verified_hyperframes_version(cli: Path) -> str:
    """Bind the launcher to the exact patched executable, not just an npm version."""

    package_json = cli.parent.parent / "package.json"
    if not package_json.is_file():
        raise FileNotFoundError("HyperFrames运行时缺少package.json版本身份")
    try:
        package = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("HyperFrames运行时package.json无法解析") from exc
    actual_version = package.get("version") if isinstance(package, dict) else None
    if actual_version != HYPERFRAMES_VERSION:
        raise ValueError(
            f"HyperFrames运行时版本不匹配：要求{HYPERFRAMES_VERSION}"
        )
    executable_bundle = package_json.parent / "dist" / "cli.js"
    if not executable_bundle.is_file():
        raise FileNotFoundError("HyperFrames runtime is missing the patched executable bundle")
    actual_payload_sha256 = _file_sha256(executable_bundle)
    if actual_payload_sha256 != HYPERFRAMES_PATCHED_CLI_SHA256:
        raise ValueError(
            "HyperFrames runtime payload is not the reviewed Windows Media Foundation patch: "
            f"expected {HYPERFRAMES_PATCHED_CLI_SHA256}, got {actual_payload_sha256}"
        )
    return HYPERFRAMES_VERSION


def _hyperframes_commands(quality: str) -> tuple[list[str], list[str], dict[str, Any]]:
    runtime = _resolve_hyperframes_runtime()
    prefix = [str(runtime["node"]), str(runtime["cli"])]
    return (
        [*prefix, "check", "--strict"],
        [
            *prefix,
            "render",
            "--output",
            "renders/final.mp4",
            "--quality",
            quality,
            "--workers",
            "2",
            "--no-best-effort",
            "--strict",
        ],
        runtime,
    )


def _injected_render_adapter_identity(adapter: Any) -> dict[str, Any]:
    """Describe a test hook without allowing it to impersonate HyperFrames."""

    module = str(getattr(adapter, "__module__", type(adapter).__module__) or "").strip()
    name = str(
        getattr(adapter, "__qualname__", getattr(adapter, "__name__", type(adapter).__qualname__))
        or "render_adapter"
    ).strip()
    callable_name = ".".join(part for part in (module, name) if part)[:240]
    return {
        "kind": "injected_test_adapter",
        "callable": callable_name or "render_adapter",
        "formal_engine": False,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


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
FFMPEG = _tool_path("FFMPEG_PATH", "ffmpeg")
FFPROBE = _tool_path("FFPROBE_PATH", "ffprobe")
FONT_REGULAR = _configured_path("FONT_REGULAR", r"C:\Windows\Fonts\msyh.ttc")
FONT_BOLD = _configured_path("FONT_BOLD", r"C:\Windows\Fonts\msyhbd.ttc")

VOICE_TEMPO_MINIMUM = 0.90
VOICE_TEMPO_MAXIMUM = 1.12
DEFAULT_EDGE_TTS_VOICE = DEFAULT_VOICE_NAME
NARRATION_TARGET_MIN_SPOKEN_CHARACTERS = 180
NARRATION_TARGET_MAX_SPOKEN_CHARACTERS = 195
NARRATION_MAX_DELIVERED_CHARACTERS_PER_SECOND = 4.05
VOICE_SCENE_PAUSE_SECONDS = 0.35
VOICE_SEGMENT_CONTRACT_VERSION = 1

VISUAL_QC_SAMPLE_COUNT = 12
VISUAL_QC_MOTION_OFFSET_SECONDS = 0.40
VISUAL_QC_MIN_ACTIVE_PAIR_RATIO = 0.75
VISUAL_QC_MIN_FRAME_DIFFERENCE = 0.0035
VISUAL_QC_MIN_MOVING_PIXEL_RATIO = 0.035
VISUAL_QC_MOTION_PROFILES = {
    "sustained_video": {"minimum_active_pair_ratio": 0.75, "maximum_inactive_run": 2},
    # Information motion deliberately holds a completed diagram long enough to
    # read it.  HyperFrames still enforces a <=2.5s frozen interval, while this
    # sampled gate requires repeated, measurable internal motion without forcing
    # the whole page to shake or adding decorative background effects.
    "semantic_motion_graphics": {"minimum_active_pair_ratio": 0.25, "maximum_inactive_run": 4},
}
KNOWN_TEST_MATERIAL_SHA256 = {
    "9A37A318334DD6478E5BBE3B4447E97B437B92C22855F96640D2CF9DD1F9716D": "mpt_testsrc2_fixture",
}

DEFAULT_INPUT: dict[str, Any] = {
    "topic": "怎样把一个真实客户问题讲清楚？",
    "audience": "需要快速制作竖屏内容的业务团队",
    "target_duration_seconds": 52,
    "pattern_card_ids": [],
    "voice_engine": DEFAULT_VOICE_ENGINE,
    "voice_chunk_max_chars": DEFAULT_VOICE_CHUNK_MAX_CHARS,
    "aspect_ratio": "9:16",
    "production_mode": "motion",
    "render_mode": "animated",
    "require_animation": True,
    "enable_web_research": True,
    "source_urls": [],
}

LEGACY_CLEAN_AIR_INPUT: dict[str, Any] = {
    **DEFAULT_INPUT,
    "topic": "99%除醛率为什么必须看检测条件？",
    "audience": "新房装修、租房、母婴家庭",
    "pattern_card_ids": ["03", "06"],
    "capability_pack": {"id": "legacy-clean-air-v2", "version": 2},
}

LEGACY_DEFAULT_SCRIPT = (
    "看到“除醛率百分之九十九”，先别急着下结论。真正决定这个数字能不能参考的，往往是旁边那行小字。"
    "第一，看用了多少产品；第二，看测试舱有多大；第三，看作用了多久；第四，看初始浓度和检测方法。"
    "小空间、大剂量、长时间得出的结果，不能直接等同于你家的整屋效果。"
    "判断一份除醛数据，至少要找齐六项：剂量、空间体积、作用时间、初始浓度、检测方法，以及报告来源。"
    "缺少任何一项，都只能把它当作线索，不能当成入住保证。"
    "真正有用的内容，不是把数字喊得更大，而是把条件讲清楚。"
    "涉及具体产品时，还要回到它自己的检测报告，并由专业人员结合真实房屋情况判断。"
)

LEGACY_LOCAL_VARIANTS = [
    {
        "id": "A",
        "hook_type": "反常识",
        "script": LEGACY_DEFAULT_SCRIPT,
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

# Kept as public aliases for read-only v2 jobs and their committed evidence.
DEFAULT_SCRIPT = LEGACY_DEFAULT_SCRIPT
LOCAL_VARIANTS = LEGACY_LOCAL_VARIANTS

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

FINANCIAL_GUARANTEE_PATTERNS = [
    r"(?:保证|承诺|确保).{0,8}(?:收益|回报|赚钱|盈利)",
    r"(?:稳赚不赔|保本保收益|零风险收益|必赚|躺赚)",
]

LEGAL_GUARANTEE_PATTERNS = [
    r"(?:保证|承诺|确保|百分之百|100%).{0,8}(?:胜诉|赢官司)",
    r"(?:包赢官司|必然胜诉|一定胜诉)",
]

ABSOLUTE_GUARANTEE_PATTERNS = [
    r"(?:保证|承诺|确保).{0,8}(?:有效|成交|成功|达标|解决|见效)",
    r"(?:百分之百|100%)\s*(?:有效|成功|安全|保证)",
    r"(?:毫无风险|零风险|永不失败|一定能|必然会)",
]

TESTIMONIAL_CERTIFICATION_RANKING_PATTERNS = [
    r"(?:所有|全部|广大)?(?:用户|客户).{0,6}(?:一致好评|都说|亲测有效)",
    r"(?:权威|官方|国家|国际).{0,4}(?:认证|背书)",
    r"(?:销量|行业|全国|平台|品类).{0,4}(?:第一|冠军|领先)",
    r"(?:排名第一|第一品牌|唯一指定|首选品牌)",
]

GENERIC_NUMERIC_CLAIM_PATTERNS = [
    r"(?:[¥￥$]\s*)?\d+(?:\.\d+)?\s*(?:元|块|万元|亿元|美元|美金)(?:\b|起|以内|以上|以下)?",
    r"\d+(?:\.\d+)?\s*(?:单|客户|用户|粉丝|播放|销量|成交|业绩|营收|收入)(?:\b|以上|以下|起)?",
    r"(?:提升|增长|增加|降低|减少|转化率|成功率|准确率|复购率|完播率|去除率|收益率|回报率|ROI).{0,8}(?:\d+(?:\.\d+)?\s*%|百分之[零一二三四五六七八九十百]+)",
    r"(?:\d+(?:\.\d+)?\s*%|百分之[零一二三四五六七八九十百]+).{0,8}(?:提升|增长|增加|降低|减少|转化|成功|准确|复购|完播|去除|收益|回报)",
]

# This is intentionally conservative rather than an industry fact dictionary.
# It catches common declarative provenance, credential and outcome predicates;
# anything it catches must be bound to a strictly usable finding.
QUALITATIVE_FACT_PATTERNS = [
    r"[^，。！？；\n]{2,40}(?:来自|源自|产自|位于|成立于|创立于|总部设在|隶属于|获得了?|通过了?|荣获|采用了?|包含|含有|拥有)[^，。！？；\n]{1,60}",
    r"(?:产品|服务|方案|技术|设备|原料|食材|咖啡豆|成分|课程|平台|软件|系统|材料|品牌)[^，。！？；\n]{0,40}(?:能够|可以|有助于|导致|带来|提升|降低|改善|减少|增加|更(?:好|快|强|高|低|安全|健康|有效|稳定|优质|明亮))[^，。！？；\n]{0,40}",
    r"(?:因此|所以|从而)[^，。！？；\n]{0,30}(?:更|会|能|可以|有助于|导致|带来|提升|降低|改善)[^，。！？；\n]{0,40}",
    r"(?:品牌|公司|企业|门店|产品|服务)[^，。！？；\n]{0,30}(?:覆盖|服务了|认证|销量|客户数量|市场份额)[^，。！？；\n]{0,40}",
]

STRICT_FINDING_STATUSES = {
    "proven_for_limited_use", "supported_limited", "passed", "approved", "eligible", "evidence_bound",
}

SOURCE_PAGE_STATEMENT_ONLY = "source_page_statement_only"
SOURCE_PAGE_GENERIC_ATTRIBUTIONS = (
    "该来源页面称",
    "该页面称",
    "该来源称",
    "据该来源",
    "据该页面",
    "该报道认为",
    "该报道指出",
    "该报道提醒",
    "该文章称",
)

LEGACY_BRAND_TERMS = ("净界",)
CLEAN_AIR_DOMAIN_TERMS = CLEAN_AIR_EXACT_TOPIC_MARKERS


class ScriptRevisionRequired(RuntimeError):
    workflow_status = "awaiting_script_revision"


class VideoVisualQualityBlocked(RuntimeError):
    """Fail a render without publishing it when formal visual checks are unsafe."""


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


def review_narration_pacing(script: str) -> dict[str, Any]:
    estimate = estimate_narration_duration(script)
    spoken = int(estimate["spoken_characters"])
    sentence_count = len(re.findall(r"[^。！？!?]+[。！？!?]", str(script or "")))
    reasons: list[str] = []
    if spoken < NARRATION_TARGET_MIN_SPOKEN_CHARACTERS:
        reasons.append("narration_too_short_for_natural_45_second_delivery")
    if spoken > NARRATION_TARGET_MAX_SPOKEN_CHARACTERS:
        reasons.append("narration_too_dense_for_fixed_normal_voice")
    if not 45.0 <= float(estimate["estimated_seconds"]) <= 60.0:
        reasons.append("estimated_narration_duration_outside_45_60_seconds")
    if not 6 <= sentence_count <= 8:
        reasons.append("narration_requires_6_to_8_complete_sentences")
    return {
        **estimate,
        "status": "blocked" if reasons else "passed",
        "blocked": bool(reasons),
        "target_spoken_character_range": [
            NARRATION_TARGET_MIN_SPOKEN_CHARACTERS,
            NARRATION_TARGET_MAX_SPOKEN_CHARACTERS,
        ],
        "fixed_voice_rate": DEFAULT_VOICE_RATE,
        "sentence_count": sentence_count,
        "reasons": reasons,
    }


def _pack_snapshot(capability_pack: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(capability_pack, dict):
        return {}
    snapshot = capability_pack.get("snapshot")
    return dict(snapshot) if isinstance(snapshot, dict) else dict(capability_pack)


def _pack_id(capability_pack: dict[str, Any] | None) -> str:
    return str((capability_pack or {}).get("id", "")).strip() if isinstance(capability_pack, dict) else ""


def _pack_report(capability_pack: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(capability_pack, dict):
        return {"id": "", "version": None, "sha256": ""}
    return {
        "id": str(capability_pack.get("id", "")),
        "version": capability_pack.get("version"),
        "sha256": str(capability_pack.get("sha256", "")),
    }


def _is_legacy_pack(capability_pack: dict[str, Any] | None) -> bool:
    return _pack_id(capability_pack) == "legacy-clean-air-v2"


def _trusted_pack_allows_clean_air_domain_terms(
    capability_pack: dict[str, Any] | None,
) -> bool:
    """Allow clean-air vocabulary only for a validated, topic-bound local pack."""

    snapshot = _pack_snapshot(capability_pack)
    goal = str(snapshot.get("goal", ""))
    return _capability_pack_has_clean_air_scope(capability_pack) and any(
        marker in goal for marker in CLEAN_AIR_EXACT_TOPIC_MARKERS
    )


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalized_learning_rules(learning_rules: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [dict(item) for item in (learning_rules or []) if isinstance(item, dict)]


def _call_with_optional_policy(callable_value: Any, *args: Any, **policy: Any) -> Any:
    """Pass v3 policy context while preserving adapters that still expose the v2 signature."""
    try:
        parameters = inspect.signature(callable_value).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_kwargs = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values())
    kwargs = policy if accepts_kwargs else {key: value for key, value in policy.items() if key in parameters}
    return callable_value(*args, **kwargs)


def _finding_is_strictly_usable(item: dict[str, Any]) -> bool:
    decision = str(item.get("decision", "")).strip().lower()
    if decision and decision not in {"approved", "approve", "accepted"}:
        return False
    if item.get("script_eligible") is False:
        return False
    strict_statuses = [
        str(item.get(name, "")).strip().lower()
        for name in ("strict_review_status", "final_verdict")
        if str(item.get(name, "")).strip()
    ]
    if strict_statuses and not any(status in STRICT_FINDING_STATUSES for status in strict_statuses):
        return False
    fallback_statuses = [
        str(item.get(name, "")).strip().lower()
        for name in ("review_status", "auto_review_status")
        if str(item.get(name, "")).strip()
    ]
    if not strict_statuses and fallback_statuses and not any(
        status in STRICT_FINDING_STATUSES for status in fallback_statuses
    ):
        return False
    evidence = item.get("evidence")
    return bool(item.get("claim") or item.get("allowed_use") or (isinstance(evidence, list) and evidence))


def _strict_findings(approved_findings: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in (approved_findings or [])
        if isinstance(item, dict) and _finding_is_strictly_usable(item)
    ]


def _finding_requires_source_attribution(item: dict[str, Any]) -> bool:
    if item.get("independent_fact_supported") is False:
        return True
    scopes = [item.get("claim_scope"), item.get("source_scope")]
    scopes.extend(
        entry.get("source_scope")
        for entry in item.get("evidence", [])
        if isinstance(entry, dict)
    )
    return any(str(value).strip() == SOURCE_PAGE_STATEMENT_ONLY for value in scopes)


def _compact_contract_text(value: Any) -> str:
    return re.sub(r"[\s，,。！？!?；;：:\"'“”‘’（）()]", "", str(value or ""))


def _source_attribution_contract(item: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Derive exact claim content and same-sentence attribution from approved metadata."""
    contents: set[str] = set()
    attributions = {
        normalized
        for marker in SOURCE_PAGE_GENERIC_ATTRIBUTIONS
        if (normalized := _compact_contract_text(marker))
    }

    source_label = str(item.get("source_label", "")).strip()
    normalized_label = _compact_contract_text(source_label)
    if normalized_label:
        attributions.add(normalized_label)
        for suffix in ("官方页面", "原始文章", "来源页面", "页面", "文章", "来源"):
            if normalized_label.endswith(suffix) and len(normalized_label) > len(suffix) + 1:
                attributions.add(normalized_label[: -len(suffix)])
        if "转载" in normalized_label:
            attributions.add(normalized_label[: normalized_label.index("转载") + len("转载")])

    for field in ("claim", "allowed_use"):
        statement = str(item.get(field, "")).strip()
        if not statement:
            continue
        body = statement
        split = re.split(r"[：:]", statement, maxsplit=1)
        if len(split) == 2:
            prefix, body = (part.strip() for part in split)
            if prefix and not prefix.startswith("可以"):
                attributions.add(_compact_contract_text(prefix))

        body = body.lstrip(" ，,：:")
        compact_body = _compact_contract_text(body)
        for marker in sorted(SOURCE_PAGE_GENERIC_ATTRIBUTIONS, key=len, reverse=True):
            compact_marker = _compact_contract_text(marker)
            if compact_body.startswith(compact_marker):
                attributions.add(compact_marker)
                compact_body = compact_body[len(compact_marker):]
                break
        if len(compact_body) >= 8:
            contents.add(compact_body)
    return contents, {value for value in attributions if value}


def _fixed_source_page_anchor_groups(item: dict[str, Any]) -> tuple[tuple[str, ...], ...]:
    """Return anchors only from the local fixed rule whose claim matches exactly."""
    claim = str(item.get("claim", "")).strip()
    if not claim:
        return ()
    for rule in EXACT_EVIDENCE_RULES:
        if str(rule.get("claim", "")).strip() != claim or rule.get("source_type") != "source_page":
            continue
        raw_groups = rule.get("attribution_anchor_groups")
        if not isinstance(raw_groups, (list, tuple)):
            return ()
        groups: list[tuple[str, ...]] = []
        for raw_group in raw_groups:
            if not isinstance(raw_group, (list, tuple)):
                return ()
            group = tuple(
                compact
                for value in raw_group
                if isinstance(value, str) and (compact := _compact_contract_text(value))
            )
            if not group:
                return ()
            groups.append(group)
        return tuple(groups)
    return ()


def _source_page_attribution_violations(
    script: str,
    strict_findings: list[dict[str, Any]],
) -> list[str]:
    clauses = [
        _compact_contract_text(value)
        for value in re.split(r"[。！？；;\n]+", script)
        if _compact_contract_text(value)
    ]
    violations: list[str] = []
    for index, item in enumerate(strict_findings, start=1):
        if not _finding_requires_source_attribution(item):
            continue
        contents, attributions = _source_attribution_contract(item)
        anchor_groups = _fixed_source_page_anchor_groups(item)
        if not contents and not anchor_groups:
            continue
        missing = any(
            (
                any(content in clause or (len(clause) >= 8 and clause in content) for content in contents)
                or bool(anchor_groups) and all(
                    any(anchor in clause for anchor in group)
                    for group in anchor_groups
                )
            )
            and not any(marker in clause for marker in attributions)
            for clause in clauses
        )
        if missing:
            finding_id = str(item.get("finding_id", "")).strip()
            violations.append(finding_id or f"approved-finding-{index}")
    return list(dict.fromkeys(violations))


def _finding_statement(item: dict[str, Any], max_chars: int = 96) -> str:
    value = str(item.get("claim") or item.get("safe_scope") or item.get("allowed_use") or "").strip()
    if not value:
        for evidence in item.get("evidence", []):
            if isinstance(evidence, dict):
                value = str(evidence.get("excerpt") or evidence.get("quote") or "").strip()
                if value:
                    break
    value = re.sub(r"\s+", "", value).strip("。；;，,")
    if len(value) > max_chars:
        value = value[:max_chars].rstrip("，,；;：:") + "，具体范围仍以原证据为准"
    return value


def _additional_banned_phrases(
    capability_pack: dict[str, Any] | None,
    learning_rules: list[dict[str, Any]] | None,
) -> list[tuple[str, str, str]]:
    output: list[tuple[str, str, str]] = []
    snapshot = _pack_snapshot(capability_pack)
    for field in ("avoided_terms", "prohibited_claims"):
        output.extend((phrase, "capability_pack", "") for phrase in _string_list(snapshot.get(field)))
    for rule in _normalized_learning_rules(learning_rules):
        rule_id = str(rule.get("rule_id", "")).strip()
        for field in ("banned_phrases", "avoided_terms", "terms"):
            output.extend((phrase, "learning_rule", rule_id) for phrase in _string_list(rule.get(field)))
        instruction = str(rule.get("instruction", "")).strip()
        quoted = re.findall(r"[“‘\"']([^”’\"']{1,40})[”’\"']", instruction)
        output.extend((phrase.strip(), "learning_rule", rule_id) for phrase in quoted if phrase.strip())
        for match in re.finditer(
            r"(?:不要(?:再)?(?:出现|使用|说)?|禁止(?:使用|出现)?|避免(?:使用|出现)?|禁用|不得(?:使用|出现)?)[：:\s]*([^，。；;]{1,40})",
            instruction,
        ):
            phrase = re.sub(r"^(?:词|表述|说法)[：:\s]*", "", match.group(1)).strip(" “”—-：:")
            if phrase:
                output.append((phrase, "learning_rule", rule_id))
    deduplicated: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for phrase, source, rule_id in output:
        if phrase and phrase not in seen:
            seen.add(phrase)
            deduplicated.append((phrase, source, rule_id))
    return deduplicated


def _sanitize_topic(
    topic: str,
    capability_pack: dict[str, Any] | None,
    learning_rules: list[dict[str, Any]] | None,
) -> str:
    value = str(topic or "").strip()
    value = re.sub(r"\d+(?:\.\d+)?\s*%|百分之[零一二三四五六七八九十百]+", "相关比例主张", value)
    value = re.sub(r"(?:[¥￥$]\s*)?\d+(?:\.\d+)?\s*(?:元|块|万元|亿元|美元|美金)", "相关价格主张", value)
    for phrase, _, _ in _additional_banned_phrases(capability_pack, learning_rules):
        value = value.replace(phrase, "相关表述")
    return value.strip() or "当前业务问题"


def _pad_safe_script(script: str, minimum_seconds: float = 45.0) -> str:
    additions = (
        "有疑问就回到原始材料重新核对。",
        "复核时还要保留来源和修改记录，避免把未确认信息重新带回正文。",
        "如果关键材料仍然缺失，就明确标注未知，不用听起来确定的话替代证据。",
        "发布前再由工作人员检查一次对象、语境和限制，确认表达没有超出材料边界。",
    )
    value = script
    for addition in additions:
        estimate = estimate_narration_duration(value)
        if (
            estimate["estimated_seconds"] >= minimum_seconds
            and estimate["spoken_characters"] >= NARRATION_TARGET_MIN_SPOKEN_CHARACTERS
        ):
            break
        # Extend the final sentence instead of creating a ninth sentence.  The
        # pacing contract deliberately limits short-form narration to 6-8
        # complete sentences, so safe padding must not break that structure.
        terminal = value[-1:] if value[-1:] in "。！？!?" else ""
        clause = addition.rstrip("。！？!?")
        candidate = (
            value[:-1] + "；" + clause + terminal
            if terminal
            else value + "；" + clause + "。"
        )
        if estimate_narration_duration(candidate)["spoken_characters"] > NARRATION_TARGET_MAX_SPOKEN_CHARACTERS:
            break
        value = candidate
    return value


def _spoken_text_edge(value: str, budget: int, *, from_end: bool = False) -> str:
    characters = list(str(value or "").strip())
    if from_end:
        characters.reverse()
    result: list[str] = []
    spoken = 0
    for character in characters:
        consumes_budget = bool(re.fullmatch(r"[\u3400-\u9fffA-Za-z0-9]", character))
        if consumes_budget and spoken >= budget:
            break
        result.append(character)
        if consumes_budget:
            spoken += 1
    if from_end:
        result.reverse()
    return "".join(result).strip(" ，,；;：:")


def _summarize_spoken_text(value: str, maximum_spoken_characters: int) -> str:
    """Preserve ordinary subjects verbatim and keep both qualifiers of long ones."""

    cleaned = str(value or "").strip()
    if int(estimate_narration_duration(cleaned)["spoken_characters"]) <= maximum_spoken_characters:
        return cleaned
    tail_budget = max(8, maximum_spoken_characters * 2 // 5)
    head_budget = maximum_spoken_characters - tail_budget
    head = _spoken_text_edge(cleaned, head_budget)
    tail = _spoken_text_edge(cleaned, tail_budget, from_end=True)
    return f"{head}……{tail}".strip()


def _compact_local_process_subject(topic: str, audience: str) -> tuple[str, str]:
    """Bind dynamic subject fields to the fixed 45-60 second narration budget.

    The local process-only fallback is deterministic, so repeating an oversized
    topic cannot produce a better candidate. Preserve normal business inputs in
    full and compact only unusually long values before composing the script.
    """

    # A valid topic may contain its decisive limitation or negation at the end.
    # Keep every ordinary (<=56 spoken characters) topic byte-for-byte. Longer
    # inputs retain both the opening subject and closing qualifier.
    safe_topic = _summarize_spoken_text(topic, 56) or "当前业务问题"
    safe_audience = _summarize_spoken_text(audience, 24) or "相关业务人员"
    return safe_topic, safe_audience


def _fit_local_process_script(script: str) -> str:
    """Add only process safeguards until the fixed narration gate passes."""

    additions = (
        "保留原始材料",
        "记录核验时间",
        "标明适用对象",
        "写清适用范围",
        "把未知项明确标注",
        "不拿经验替代证据",
        "发布前再次逐项复核",
        "发现材料变化就重新判断",
        "让每次修改都能回查",
        "没有依据就不补写",
        "核对每项表达是否超出材料边界",
        "无法确认的内容继续保持未知状态",
    )
    terminal = script[-1:] if script[-1:] in "。！？!?" else "。"
    stem = script[:-1] if script[-1:] in "。！？!?" else script
    best: tuple[tuple[int, int, int], str] | None = None
    for mask in range(1 << len(additions)):
        selected = [addition for index, addition in enumerate(additions) if mask & (1 << index)]
        candidate = stem + ("；" if selected else "") + "；".join(selected) + terminal
        pacing = review_narration_pacing(candidate)
        if pacing["blocked"]:
            continue
        key = (int(pacing["spoken_characters"]) - NARRATION_TARGET_MIN_SPOKEN_CHARACTERS, len(selected), mask)
        if best is None or key < best[0]:
            best = (key, candidate)
    return best[1] if best is not None else script


def _build_legacy_local_variants(
    topic: str,
    audience: str,
    approved_findings: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    safe_topic = re.sub(r"\d+(?:\.\d+)?\s*%|百分之[零一二三四五六七八九十百]+", "高比例", topic)
    safe_topic = re.sub(
        r"\d+(?:\.\d+)?\s*(?:m[³3]|立方米|平方米|mg(?:/m[³3])?|毫克(?:每立方米)?|罐|倍|小时|分钟|年)",
        "具体条件",
        safe_topic,
        flags=re.IGNORECASE,
    )
    # The first deterministic topic card deliberately adds a UI-oriented
    # "先讲清楚：" label.  It is not part of the subject and must not consume
    # the fixed 180-195 character narration budget when the same card enters
    # the fully unattended path.
    safe_topic = re.sub(r"^(?:先讲清楚|先说明)\s*[：:]\s*", "", safe_topic).strip()
    # An explicit audience belongs to the separate audience field.  Remove
    # only the leading UI phrasing and keep the actual question verbatim.
    safe_topic = re.sub(
        r"^(?:主要)?面向[^，。；;]{2,24}[，,]\s*(?:讲清|说明|解释)",
        "",
        safe_topic,
    ).strip()
    evidence_rows = _strict_findings(approved_findings)
    if evidence_rows:
        source_priority = {"media_original": 0, "government_law": 1, "government_standard_metadata": 2}
        evidence_rows.sort(key=lambda item: source_priority.get(
            str((item.get("evidence") or [{}])[0].get("source_type", "")), 9
        ))
        statements = [_finding_statement(item, max_chars=52) for item in evidence_rows]
        statements = [value for value in statements if value][:1]
        evidence_copy = "；".join(statements).rstrip("。") + "。"
        core = evidence_copy + (
            "这项内容只适用于原来源的对象和范围，不能外推到所有产品或场景。"
            "判断除醛信息，还要核对剂量、空间体积、作用时间、初始浓度、检测方法和报告来源。"
            "实验条件与真实房间不同，结论不能直接照搬；证据不完整，也不能理解成入住保证。"
            "最后保留原始报告和核对记录，再结合真实房屋情况判断。"
        )
        openings = [
            ("A", "证据反查", "看到一条高比例除醛率宣传，先问一句：这个数字是在什么条件下得到的？"),
            ("B", "条件对照", "同样是一个百分比，换了测试条件，含义可能完全不同。"),
            ("C", "风险提醒", "先别急着记住漂亮数字，先看它后面的测试条件。"),
            ("D", "三步检查", "判断除醛率，可以按来源、条件、适用范围三步检查。"),
        ]
        finding_ids = [str(item.get("finding_id", "")) for item in evidence_rows if item.get("finding_id")]
        return [
            {
                "id": item_id,
                "hook_type": hook,
                "script": _pad_safe_script(opening + core),
                "reason": "只组合阶段审查批准且通过严格反证审核的证据原意。",
                "source": "local_evidence_bound",
                "evidence_finding_ids": finding_ids,
            }
            for item_id, hook, opening in openings
        ]
    safe_topic = safe_topic[:46]
    safe_audience = audience[:18] or "装修后家庭"
    core = (
        "先分清气味线索和仪器读数，再核对室内甲醛证据。"
        "先核对检测时的门窗状态、仪器位置和持续时间。"
        "气味和体感只是线索，不能替代规范检测。"
        "再看剂量、空间体积、作用时间、初始浓度、检测方法和报告来源。"
        "实验条件与真实房间不同，结论不能直接照搬；缺少来源和适用边界，也不能理解成入住保证。"
        f"对{safe_audience}，建议保留报告、持续通风，重要决定前结合房屋情况请专业人员判断。"
    )
    openings = [
        ("A", "问题拆解", f"对于“{safe_topic}”，不能只凭一个低数值下结论。"),
        ("B", "证据清单", f"判断“{safe_topic}”，先分清线索和证据。"),
        ("C", "风险提醒", f"“{safe_topic}”最容易错在把感受直接当结论。"),
        ("D", "行动建议", f"面对“{safe_topic}”，可以按条件、证据和场景判断。"),
    ]
    return [
        {"id": item_id, "hook_type": hook, "script": _pad_safe_script(opening + core, minimum_seconds=45), "reason": f"围绕实际选题“{topic}”生成的本地安全模板。"}
        for item_id, hook, opening in openings
    ]


def build_local_variants(
    topic: str,
    audience: str,
    approved_findings: list[dict[str, Any]] | None = None,
    capability_pack: dict[str, Any] | None = None,
    learning_rules: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    topic = str(topic).strip()
    audience = str(audience).strip()
    if _is_legacy_pack(capability_pack) or _trusted_pack_allows_clean_air_domain_terms(capability_pack) or (
        capability_pack is None and any(term in topic for term in ("甲醛", "除醛", "测醛"))
    ):
        return _build_legacy_local_variants(topic, audience, approved_findings)

    safe_topic, safe_audience = _compact_local_process_subject(
        _sanitize_topic(topic, capability_pack, learning_rules),
        audience,
    )
    evidence_rows = _strict_findings(approved_findings)
    added_bans = [phrase for phrase, _, _ in _additional_banned_phrases(capability_pack, learning_rules)]
    evidence_rows = [
        row for row in evidence_rows
        if not any(phrase in _finding_statement(row) for phrase in added_bans)
    ]
    if evidence_rows:
        statements: list[str] = []
        used_rows: list[dict[str, Any]] = []
        for row in evidence_rows:
            statement = _finding_statement(row, max_chars=45)
            if statement:
                statements.append(statement)
                used_rows.append(row)
                break
        evidence_copy = "；".join(statements).rstrip("。") + "。"
        core = (
            f"这次讨论“{safe_topic}”。当前只使用阶段审查批准且严格通过的材料：{evidence_copy}"
            "这项结论只适用于原证据的对象、时间和范围，不能外推。"
            "其他说法继续核对来源和限制；未经批准的数字、功效、价格、业绩、保证、证言、认证和排名都不写入正文。"
            f"最后列出已证实、待确认和禁用内容，交给{safe_audience}复核。"
        )
        openings = [
            ("A", "证据反查", "先别急着下结论，先看哪些内容已经被证据支持。"),
            ("B", "边界核验", "同一句话换了对象或场景，适用范围可能完全不同。"),
            ("C", "风险提醒", "听起来确定，不等于已经得到可靠材料证明。"),
            ("D", "核验清单", "可以按来源、对象、条件和限制四步复核这件事。"),
        ]
        finding_ids = [str(item.get("finding_id", "")) for item in used_rows if item.get("finding_id")]
        return [
            {
                "id": item_id,
                "hook_type": hook,
                "script": _pad_safe_script(opening + core),
                "reason": "只使用阶段审查批准且通过严格反证审核的claim或有限表述。",
                "source": "local_evidence_bound",
                "evidence_finding_ids": finding_ids,
            }
            for item_id, hook, opening in openings
        ]
    core = (
        f"这次讨论“{safe_topic}”，面向{safe_audience}。"
        "先核对目标、对象、场景和限制。"
        "再把资料分成证据、假设和经验。"
        "无批准证据的功效、价格、业绩数字以及保证、证言、认证或排名，一律不写。"
        "最后列出结论、缺失材料和核验动作，保留来源、时间、范围与修改依据。"
    )
    openings = [
        ("A", "问题拆解", "先把问题说清楚。"),
        ("B", "核验流程", "有材料才下结论。"),
        ("C", "风险提醒", "别把经验当成事实。"),
        ("D", "行动建议", "按四步核验这件事。"),
    ]
    return [
        {
            "id": item_id,
            "hook_type": hook,
            "script": _fit_local_process_script(opening + core),
            "reason": f"围绕实际选题“{topic}”生成的通用核验流程；未补写任何行业事实。",
            "source": "local_process_only",
            "evidence_finding_ids": [],
        }
        for item_id, hook, opening in openings
    ]


def atomic_json(path: Path, data: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _visual_qc_duration(video_path: Path, ffprobe_path: Path) -> float:
    if not _tool_available(ffprobe_path):
        raise RuntimeError("未找到FFprobe，无法执行正式成片视觉门禁")
    result = subprocess.run(
        [
            str(ffprobe_path),
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    try:
        duration = float((result.stdout or "").strip())
    except ValueError as exc:
        raise RuntimeError("正式成片无法读取有效时长") from exc
    if result.returncode or not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("正式成片无法读取有效时长")
    return duration


def _extract_visual_qc_frames(
    video_path: Path,
    work_dir: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    sample_count: int = VISUAL_QC_SAMPLE_COUNT,
) -> tuple[list[Image.Image], list[float]]:
    if not _tool_available(ffmpeg_path):
        raise RuntimeError("未找到FFmpeg，无法执行正式成片视觉门禁")
    duration = _visual_qc_duration(video_path, ffprobe_path)
    timestamps = [
        min(max(duration - 0.05, 0.0), duration * (index + 0.5) / sample_count)
        for index in range(sample_count)
    ]
    frames: list[Image.Image] = []
    for index, timestamp in enumerate(timestamps, start=1):
        frame_path = work_dir / f"frame-{index:02d}.png"
        result = subprocess.run(
            [
                str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{timestamp:.6f}", "-i", str(video_path),
                "-frames:v", "1", "-vf", "scale=320:-2:flags=lanczos",
                str(frame_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if result.returncode or not frame_path.is_file() or frame_path.stat().st_size <= 0:
            raise RuntimeError("正式成片抽帧失败")
        with Image.open(frame_path) as image:
            frames.append(image.convert("RGB").copy())
    return frames, timestamps


def _extract_visual_motion_pairs(
    video_path: Path,
    work_dir: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    sample_count: int = VISUAL_QC_SAMPLE_COUNT,
    offset_seconds: float = VISUAL_QC_MOTION_OFFSET_SECONDS,
) -> tuple[list[tuple[Image.Image, Image.Image]], list[float]]:
    """Sample short frame pairs so scene changes cannot masquerade as animation."""

    if not _tool_available(ffmpeg_path):
        raise RuntimeError("未找到FFmpeg，无法执行动态密度门禁")
    duration = _visual_qc_duration(video_path, ffprobe_path)
    if duration <= offset_seconds + 0.1:
        raise RuntimeError("成片过短，无法执行动态密度门禁")
    latest_start = max(0.0, duration - offset_seconds - 0.05)
    timestamps = [
        min(latest_start, max(0.0, duration * (index + 0.5) / sample_count))
        for index in range(sample_count)
    ]
    pairs: list[tuple[Image.Image, Image.Image]] = []
    for index, timestamp in enumerate(timestamps, start=1):
        images: list[Image.Image] = []
        for suffix, pair_timestamp in (("a", timestamp), ("b", timestamp + offset_seconds)):
            frame_path = work_dir / f"motion-{index:02d}-{suffix}.png"
            result = subprocess.run(
                [
                    str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{pair_timestamp:.6f}", "-i", str(video_path),
                    "-frames:v", "1", "-vf", "scale=320:-2:flags=lanczos",
                    str(frame_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            if result.returncode or not frame_path.is_file() or frame_path.stat().st_size <= 0:
                raise RuntimeError("正式成片动态密度抽帧失败")
            with Image.open(frame_path) as image:
                images.append(image.convert("RGB").copy())
        pairs.append((images[0], images[1]))
    return pairs, timestamps


def _classify_test_bar_pixel(red: int, green: int, blue: int) -> int:
    targets = (
        red >= 170 and green <= 105 and blue <= 105,
        green >= 135 and red <= 115 and blue <= 115,
        red >= 165 and green >= 145 and blue <= 115,
        blue >= 155 and red <= 115 and green <= 135,
        red >= 160 and blue >= 145 and green <= 115,
        green >= 135 and blue >= 145 and red <= 115,
    )
    for index, matched in enumerate(targets):
        if matched:
            return index
    return -1


def _detect_test_color_bars(image: Image.Image) -> dict[str, Any]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    top = max(0, int(height * 0.04))
    bottom = max(top + 1, int(height * 0.58))
    sample = rgb.crop((0, top, width, bottom)).resize((240, 132), Image.Resampling.BILINEAR)
    pixels = sample.load()
    dominant_columns: list[int] = []
    for x in range(sample.width):
        counts = [0] * 6
        for y in range(sample.height):
            label = _classify_test_bar_pixel(*pixels[x, y])
            if label >= 0:
                counts[label] += 1
        best = max(range(6), key=counts.__getitem__)
        dominant_columns.append(best if counts[best] / sample.height >= 0.62 else -1)

    longest_runs = [0] * 6
    current = -1
    run_length = 0
    for label in [*dominant_columns, -2]:
        if label == current:
            run_length += 1
            continue
        if current >= 0:
            longest_runs[current] = max(longest_runs[current], run_length)
        current = label
        run_length = 1
    minimum_bar_width = max(10, round(sample.width * 0.055))
    detected_targets = [index for index, length in enumerate(longest_runs) if length >= minimum_bar_width]
    covered_columns = sum(
        1 for label in dominant_columns
        if label >= 0 and longest_runs[label] >= minimum_bar_width
    )
    coverage = covered_columns / sample.width
    score = round((len(detected_targets) / 6) * coverage, 4)
    return {
        "detected": len(detected_targets) >= 5 and coverage >= 0.52,
        "score": score,
        "detected_target_count": len(detected_targets),
        "vertical_coverage": round(coverage, 4),
    }


def _frame_perceptual_hash(image: Image.Image) -> int:
    width, height = image.size
    crop = image.crop((0, 0, width, max(1, int(height * 0.78))))
    small = crop.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    values = list(small.get_flattened_data()) if hasattr(small, "get_flattened_data") else list(small.getdata())
    result = 0
    bit = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            if values[offset + column] > values[offset + column + 1]:
                result |= 1 << bit
            bit += 1
    return result


def _normalized_frame_difference(left: Image.Image, right: Image.Image) -> float:
    def normalized(image: Image.Image) -> Image.Image:
        width, height = image.size
        return image.crop((0, 0, width, max(1, int(height * 0.78)))).convert("RGB").resize(
            (32, 32), Image.Resampling.BILINEAR
        )

    difference = ImageChops.difference(normalized(left), normalized(right))
    channel_means = ImageStat.Stat(difference).mean
    return sum(channel_means) / (len(channel_means) * 255.0)


def analyze_visual_motion_pairs(
    pairs: list[tuple[Image.Image, Image.Image]],
    timestamps: list[float],
    *,
    motion_profile: str = "sustained_video",
) -> dict[str, Any]:
    if len(pairs) != VISUAL_QC_SAMPLE_COUNT or len(timestamps) != len(pairs):
        raise ValueError(f"动态密度门禁必须恰好抽取{VISUAL_QC_SAMPLE_COUNT}组相邻帧")
    profile = VISUAL_QC_MOTION_PROFILES.get(str(motion_profile))
    if profile is None:
        raise ValueError("未知动态密度质检类型")
    minimum_active_pair_ratio = float(profile["minimum_active_pair_ratio"])
    maximum_inactive_run = int(profile["maximum_inactive_run"])
    rows: list[dict[str, Any]] = []
    active_flags: list[bool] = []
    for index, ((left, right), timestamp) in enumerate(zip(pairs, timestamps), start=1):
        def normalized(image: Image.Image) -> Image.Image:
            width, height = image.size
            return image.crop((0, 0, width, max(1, int(height * 0.78)))).convert("RGB").resize(
                (160, 250), Image.Resampling.BILINEAR
            )

        difference = ImageChops.difference(normalized(left), normalized(right))
        pixels = (
            list(difference.get_flattened_data())
            if hasattr(difference, "get_flattened_data")
            else list(difference.getdata())
        )
        normalized_difference = sum(sum(pixel) / 3 for pixel in pixels) / (len(pixels) * 255.0)
        moving_pixel_ratio = sum(1 for pixel in pixels if max(pixel) >= 10) / len(pixels)
        active = (
            normalized_difference >= VISUAL_QC_MIN_FRAME_DIFFERENCE
            and moving_pixel_ratio >= VISUAL_QC_MIN_MOVING_PIXEL_RATIO
        )
        active_flags.append(active)
        rows.append({
            "index": index,
            "timestamp_seconds": round(float(timestamp), 3),
            "offset_seconds": VISUAL_QC_MOTION_OFFSET_SECONDS,
            "normalized_frame_difference": round(normalized_difference, 6),
            "moving_pixel_ratio": round(moving_pixel_ratio, 6),
            "active": active,
        })

    longest_inactive_run = 0
    current_inactive_run = 0
    for active in active_flags:
        if active:
            current_inactive_run = 0
        else:
            current_inactive_run += 1
            longest_inactive_run = max(longest_inactive_run, current_inactive_run)
    active_pair_ratio = sum(active_flags) / len(active_flags)
    passed = active_pair_ratio >= minimum_active_pair_ratio and longest_inactive_run <= maximum_inactive_run
    return {
        "status": "passed" if passed else "needs_visual_review",
        "motion_profile": str(motion_profile),
        "active_pair_count": sum(active_flags),
        "sample_pair_count": len(active_flags),
        "active_pair_ratio": round(active_pair_ratio, 4),
        "longest_inactive_run": longest_inactive_run,
        "minimum_active_pair_ratio": minimum_active_pair_ratio,
        "maximum_inactive_run": maximum_inactive_run,
        "minimum_frame_difference": VISUAL_QC_MIN_FRAME_DIFFERENCE,
        "minimum_moving_pixel_ratio": VISUAL_QC_MIN_MOVING_PIXEL_RATIO,
        "pairs": rows,
    }


def analyze_visual_qc_frames(
    frames: list[Image.Image],
    timestamps: list[float],
    *,
    material_hashes: list[str] | None = None,
    material_sources_valid: bool = True,
) -> dict[str, Any]:
    if len(frames) != VISUAL_QC_SAMPLE_COUNT or len(timestamps) != len(frames):
        raise ValueError(f"视觉门禁必须恰好抽取{VISUAL_QC_SAMPLE_COUNT}帧")
    normalized_hashes = {
        str(value).strip().upper() for value in (material_hashes or [])
        if re.fullmatch(r"[0-9A-Fa-f]{64}", str(value).strip())
    }
    known_matches = [
        {"sha256": digest, "fixture": KNOWN_TEST_MATERIAL_SHA256[digest]}
        for digest in sorted(normalized_hashes & set(KNOWN_TEST_MATERIAL_SHA256))
    ]
    frame_rows: list[dict[str, Any]] = []
    color_bar_frames: list[int] = []
    perceptual_hashes: list[int] = []
    for index, (frame, timestamp) in enumerate(zip(frames, timestamps), start=1):
        bars = _detect_test_color_bars(frame)
        if bars["detected"]:
            color_bar_frames.append(index)
        perceptual_hashes.append(_frame_perceptual_hash(frame))
        frame_rows.append({
            "index": index,
            "timestamp_seconds": round(float(timestamp), 3),
            "test_color_bars": bars,
        })

    hamming_distances = [
        (left ^ right).bit_count()
        for left, right in zip(perceptual_hashes, perceptual_hashes[1:])
    ]
    frame_differences = [
        _normalized_frame_difference(left, right)
        for left, right in zip(frames, frames[1:])
    ]
    median_hamming = statistics.median(hamming_distances) if hamming_distances else 0
    median_difference = statistics.median(frame_differences) if frame_differences else 0.0
    unique_hashes = len(set(perceptual_hashes))
    extreme_repetition = (
        unique_hashes <= 2 and median_difference < 0.008
    ) or (
        max(hamming_distances or [0]) <= 2 and median_difference < 0.006
    )

    blocking_reasons: list[str] = []
    if not material_sources_valid:
        blocking_reasons.append("invalid_material_sources")
    if known_matches:
        blocking_reasons.append("known_test_fixture_material")
    if color_bar_frames:
        blocking_reasons.append("test_pattern_color_bars")
    review_reasons = ["extreme_visual_repetition"] if extreme_repetition else []
    status = "blocked" if blocking_reasons else ("needs_visual_review" if review_reasons else "passed")
    return {
        "status": status,
        "sample_count": len(frames),
        "blocking_reasons": blocking_reasons,
        "review_reasons": review_reasons,
        "checks": {
            "known_test_fixture": {
                "status": "blocked" if known_matches else "passed",
                "matches": known_matches,
            },
            "test_color_bars": {
                "status": "blocked" if color_bar_frames else "passed",
                "detected_frame_count": len(color_bar_frames),
                "detected_frame_indices": color_bar_frames,
            },
            "visual_repetition": {
                "status": "needs_visual_review" if extreme_repetition else "passed",
                "unique_perceptual_hashes": unique_hashes,
                "median_consecutive_hamming_distance": median_hamming,
                "median_normalized_frame_difference": round(median_difference, 6),
            },
        },
        "frames": frame_rows,
    }


def _build_visual_contact_sheet(
    frames: list[Image.Image],
    timestamps: list[float],
    destination: Path,
    status: str,
) -> None:
    columns, rows = 4, 3
    cell_width, cell_height = 240, 426
    header_height = 64
    sheet = Image.new("RGB", (columns * cell_width, header_height + rows * cell_height), "#101820")
    draw = ImageDraw.Draw(sheet)
    status_color = {"passed": "#77D970", "needs_visual_review": "#F5C451", "blocked": "#F26B5E"}.get(
        status, "#F26B5E"
    )
    draw.rectangle((0, 0, sheet.width, header_height), fill="#101820")
    draw.text((18, 17), f"FORMAL VISUAL QC | {status.upper()} | 12 SAMPLES", fill=status_color)
    for index, (frame, timestamp) in enumerate(zip(frames, timestamps)):
        column, row = index % columns, index // columns
        origin_x = column * cell_width
        origin_y = header_height + row * cell_height
        cell = Image.new("RGB", (cell_width, cell_height), "#06090C")
        contained = ImageOps.contain(frame.convert("RGB"), (cell_width, cell_height), Image.Resampling.LANCZOS)
        paste_x = (cell_width - contained.width) // 2
        paste_y = (cell_height - contained.height) // 2
        cell.paste(contained, (paste_x, paste_y))
        cell_draw = ImageDraw.Draw(cell)
        cell_draw.rectangle((0, cell_height - 28, 116, cell_height), fill="#101820")
        cell_draw.text((8, cell_height - 22), f"{index + 1:02d}  {timestamp:06.2f}s", fill="#FFFFFF")
        sheet.paste(cell, (origin_x, origin_y))
    temporary = destination.with_name(destination.name + ".tmp")
    sheet.save(temporary, format="PNG", optimize=True)
    temporary.replace(destination)


def _visual_material_hashes(path: Path | None) -> tuple[list[str], bool]:
    if path is None or not path.is_file():
        return [], True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return [], False
    sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(sources, list):
        return [], False
    hashes: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            return [], False
        digest = str(source.get("sha256", "")).strip().upper()
        if digest and not re.fullmatch(r"[0-9A-F]{64}", digest):
            return [], False
        if digest:
            hashes.append(digest)
    return hashes, True


def verify_video_visuals(
    video_path: Path,
    *,
    output_dir: Path | None = None,
    material_sources_path: Path | None = None,
    ffmpeg_path: Path | None = None,
    ffprobe_path: Path | None = None,
    motion_profile: str = "sustained_video",
) -> dict[str, Any]:
    video_path = Path(video_path)
    if not video_path.is_file() or video_path.stat().st_size <= 0:
        raise RuntimeError("正式成片不存在，无法执行视觉门禁")
    output_dir = Path(output_dir or video_path.parent)
    output_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_path = Path(ffmpeg_path or FFMPEG)
    ffprobe_path = Path(ffprobe_path or FFPROBE)
    material_hashes, material_sources_valid = _visual_material_hashes(material_sources_path)
    with tempfile.TemporaryDirectory(prefix=".visual-qc-", dir=output_dir) as work_name:
        frames, timestamps = _extract_visual_qc_frames(
            video_path,
            Path(work_name),
            ffmpeg_path,
            ffprobe_path,
        )
        report = analyze_visual_qc_frames(
            frames,
            timestamps,
            material_hashes=material_hashes,
            material_sources_valid=material_sources_valid,
        )
        motion_pairs, motion_timestamps = _extract_visual_motion_pairs(
            video_path,
            Path(work_name),
            ffmpeg_path,
            ffprobe_path,
        )
        motion_density = analyze_visual_motion_pairs(
            motion_pairs,
            motion_timestamps,
            motion_profile=motion_profile,
        )
        report["checks"]["motion_density"] = motion_density
        if motion_density["status"] != "passed":
            if "insufficient_motion_density" not in report["review_reasons"]:
                report["review_reasons"].append("insufficient_motion_density")
            if report["status"] == "passed":
                report["status"] = "needs_visual_review"
        report.update({
            "schema_version": 1,
            "video": {
                "name": video_path.name,
                "size": video_path.stat().st_size,
                "sha256": _file_sha256(video_path),
            },
            "material_sources": {
                "present": bool(material_sources_path and material_sources_path.is_file()),
                "valid": material_sources_valid,
                "source_count": len(material_hashes),
            },
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        _build_visual_contact_sheet(
            frames,
            timestamps,
            output_dir / "contact-sheet.png",
            str(report["status"]),
        )
    atomic_json(output_dir / "visual-qc.json", report)
    return report


def _bind_visual_qc_to_engine_report(folder: Path, visual_qc: dict[str, Any]) -> None:
    report_path = folder / "engine_report.json"
    if not report_path.is_file():
        return
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("MPT生产引擎报告无效") from exc
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list):
        raise RuntimeError("MPT生产引擎报告产物清单无效")
    for name in ("contact-sheet.png", "visual-qc.json"):
        path = folder / name
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError("正式成片视觉门禁产物缺失")
    control = report.get("control_layer_validation")
    if not isinstance(control, dict):
        control = {}
        report["control_layer_validation"] = control
    control["visual_qc"] = {
        "status": visual_qc.get("status"),
        "blocking_reasons": list(visual_qc.get("blocking_reasons", [])),
        "review_reasons": list(visual_qc.get("review_reasons", [])),
        "sample_count": visual_qc.get("sample_count"),
        "report_sha256": _file_sha256(folder / "visual-qc.json"),
        "contact_sheet_sha256": _file_sha256(folder / "contact-sheet.png"),
    }
    atomic_json(report_path, report)


def load_pattern_cards() -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for line in PATTERN_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cards.append(json.loads(line))
    return cards


def review_script(
    script: str,
    approved_findings: list[dict[str, Any]] | None = None,
    capability_pack: dict[str, Any] | None = None,
    learning_rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    script = str(script or "")
    strict_findings = _strict_findings(approved_findings)
    support_rows = [
        {
            "claim": item.get("claim", ""),
            "allowed_use": item.get("allowed_use", ""),
        }
        if _finding_requires_source_attribution(item)
        else item
        for item in strict_findings
    ]
    evidence_text = re.sub(r"\s+", "", json.dumps(support_rows, ensure_ascii=False))
    evidence_context_supplied = approved_findings is not None
    legacy_trusted_template = script.strip() == LEGACY_DEFAULT_SCRIPT

    def evidence_supports(value: str) -> bool:
        normalized = re.sub(r"\s+", "", value)
        number_tokens = re.findall(
            r"(?:[¥￥$]\s*)?\d+(?:\.\d+)?\s*(?:%|元|块|万元|亿元|美元|美金|m[³3]|立方米|平方米|mg(?:/m[³3])?|毫克(?:每立方米)?|罐|倍|单|客户|用户|粉丝|播放)?|百分之[零一二三四五六七八九十百]+",
            value,
            flags=re.IGNORECASE,
        )
        if number_tokens:
            return bool(strict_findings) and all(re.sub(r"\s+", "", token) in evidence_text for token in number_tokens)
        return bool(strict_findings) and normalized in evidence_text

    hits = [phrase for phrase in BANNED_PHRASES if phrase in script]
    generalizations = [phrase for phrase in UNSUPPORTED_GENERALIZATIONS if phrase in script]
    percentages = list(dict.fromkeys(re.findall(
        r"\d+(?:\.\d+)?\s*%|百分之[零一二三四五六七八九十百]+", script
    )))
    measurements = list(dict.fromkeys(re.findall(
        r"\d+(?:\.\d+)?\s*(?:m[³3]|立方米|平方米|mg(?:/m[³3])?|毫克(?:每立方米)?|罐|倍|小时|分钟|年)",
        script,
        flags=re.IGNORECASE,
    )))
    numeric_claims = list(dict.fromkeys(
        match.group(0)
        for pattern in GENERIC_NUMERIC_CLAIM_PATTERNS
        for match in re.finditer(pattern, script, flags=re.IGNORECASE)
    ))
    conditions = [name for name in ("剂量", "空间", "作用时间", "初始浓度", "检测方法", "报告来源") if name in script]
    medical_claims = [match.group(0) for pattern in MEDICAL_CLAIM_PATTERNS for match in re.finditer(pattern, script)]
    financial_guarantees = [match.group(0) for pattern in FINANCIAL_GUARANTEE_PATTERNS for match in re.finditer(pattern, script)]
    legal_guarantees = [match.group(0) for pattern in LEGAL_GUARANTEE_PATTERNS for match in re.finditer(pattern, script)]
    absolute_guarantees = [match.group(0) for pattern in ABSOLUTE_GUARANTEE_PATTERNS for match in re.finditer(pattern, script)]
    social_proof_claims = [
        match.group(0)
        for pattern in TESTIMONIAL_CERTIFICATION_RANKING_PATTERNS
        for match in re.finditer(pattern, script)
    ]
    strict_statements = [
        re.sub(r"\s+", "", _finding_statement(item, max_chars=1000)).strip("。；;，,")
        for item in strict_findings
    ]
    strict_statements = [value for value in strict_statements if value]

    def qualitative_clause_supported(clause: str) -> bool:
        normalized = re.sub(r"\s+", "", clause).strip("。；;，,")
        return bool(normalized) and any(
            statement in normalized or normalized in statement for statement in strict_statements
        )

    qualitative_claims: list[str] = []
    for clause in re.split(r"[。！？；;\n]+", script):
        clean_clause = clause.strip(" ，,")
        if not clean_clause or not any(re.search(pattern, clean_clause) for pattern in QUALITATIVE_FACT_PATTERNS):
            continue
        if not qualitative_clause_supported(clean_clause) and clean_clause not in qualitative_claims:
            qualitative_claims.append(clean_clause)
    warnings: list[dict[str, Any]] = []

    missing_attribution = _source_page_attribution_violations(script, strict_findings)
    if missing_attribution:
        warnings.append({
            "type": "source_page_claim_missing_attribution",
            "level": "block",
            "message": "来源页限定finding在脚本中缺少同句来源归属。",
            "finding_ids": missing_attribution,
        })

    if percentages:
        warnings.append({
            "type": "numeric_claim_context",
            "level": "review",
            "message": "出现百分比，必须确认它只是证据支持的有限表述，没有被写成效果承诺。",
            "matches": percentages,
        })
    if measurements:
        supported_measurements = [value for value in measurements if evidence_supports(value)]
        unsupported_measurements = [value for value in measurements if value not in supported_measurements]
        if supported_measurements:
            warnings.append({
                "type": "evidence_bound_measurement",
                "level": "review",
                "message": "具体测量数字已在批准证据中找到，仍需人工确认没有扩大适用范围。",
                "matches": supported_measurements,
            })
        if unsupported_measurements and not legacy_trusted_template:
            warnings.append({
                "type": "unsupported_measurement",
                "level": "block",
                "message": "脚本包含没有批准证据的具体测量数字。",
                "matches": unsupported_measurements,
            })

    checked_numbers = list(dict.fromkeys(percentages + measurements + numeric_claims))
    unsupported_numbers = [value for value in checked_numbers if not evidence_supports(value)]
    if unsupported_numbers and (evidence_context_supplied or numeric_claims) and not legacy_trusted_template:
        warnings.append({
            "type": "unsupported_numeric_claim",
            "level": "block",
            "message": "功效、价格或业绩数字没有阶段审查批准且严格通过的finding支持。",
            "matches": unsupported_numbers,
        })

    legacy_numeric_context = _is_legacy_pack(capability_pack) or any(
        term in script for term in ("甲醛", "除醛", "检测报告", "试验舱", "实验舱")
    )
    if legacy_numeric_context and checked_numbers and len(conditions) < 6 and not legacy_trusted_template:
        warnings.append({
            "type": "missing_conditions",
            "level": "block",
            "message": "净界legacy数值功效语境没有覆盖六项内部审核条件。",
            "present": conditions,
        })

    for phrase in hits:
        warnings.append({"type": "banned_phrase", "level": "block", "message": f"命中不可变高风险表达：{phrase}"})
    for phrase in generalizations:
        warnings.append({"type": "unsupported_generalization", "level": "block", "message": f"命中无来源行业泛化：{phrase}"})
    for phrase in medical_claims:
        warnings.append({"type": "unsupported_medical_causality", "level": "block", "message": f"命中医学因果或健康保证：{phrase}"})
    for phrase in financial_guarantees:
        warnings.append({"type": "financial_return_guarantee", "level": "block", "message": f"命中金融收益保证：{phrase}"})
    for phrase in legal_guarantees:
        warnings.append({"type": "legal_outcome_guarantee", "level": "block", "message": f"命中法律结果保证：{phrase}"})
    for phrase in absolute_guarantees:
        warnings.append({"type": "absolute_guarantee", "level": "block", "message": f"命中保证性或绝对化表达：{phrase}"})
    for phrase in social_proof_claims:
        supported = evidence_supports(phrase)
        warnings.append({
            "type": "evidence_bound_social_proof" if supported else "fabricated_testimonial_certification_ranking",
            "level": "review" if supported else "block",
            "message": (
                f"证言、认证或排名表述已绑定批准证据，仍需人工确认：{phrase}"
                if supported else f"证言、认证或排名表述没有批准证据：{phrase}"
            ),
        })
    if qualitative_claims and not legacy_trusted_template:
        warnings.append({
            "type": "unsupported_qualitative_claim",
            "level": "block",
            "message": "脚本包含没有阶段审查批准且严格通过的定性事实、来源或因果表述。",
            "matches": qualitative_claims,
        })

    for phrase, source, rule_id in _additional_banned_phrases(capability_pack, learning_rules):
        if phrase in script:
            warning = {
                "type": "learning_rule_banned_phrase" if source == "learning_rule" else "capability_pack_banned_phrase",
                "level": "block",
                "message": f"命中能力包或纠错记忆新增禁词：{phrase}",
                "phrase": phrase,
            }
            if rule_id:
                warning["rule_id"] = rule_id
            warnings.append(warning)

    snapshot = _pack_snapshot(capability_pack)
    pack_text = json.dumps(snapshot, ensure_ascii=False)
    if isinstance(capability_pack, dict) and not _is_legacy_pack(capability_pack):
        leaked = [term for term in LEGACY_BRAND_TERMS if term in script and term not in pack_text]
        if not _trusted_pack_allows_clean_air_domain_terms(capability_pack):
            leaked.extend(term for term in CLEAN_AIR_DOMAIN_TERMS if term in script)
        leaked = list(dict.fromkeys(leaked))
        if leaked:
            warnings.append({
                "type": "legacy_domain_leak",
                "level": "block",
                "message": "普通项目脚本混入净界legacy行业内容。",
                "matches": leaked,
            })

    blocked = any(item["level"] == "block" for item in warnings)
    status = "blocked" if blocked else ("needs_human" if warnings else "passed")
    return {
        "status": status,
        "blocked": blocked,
        "warnings": warnings,
        "conditions_present": conditions,
        "human_confirmation_required": True,
        "scope": "只允许使用阶段审查批准且严格通过的证据；能力包与记忆只能收紧规则",
        "capability_pack_id": _pack_id(capability_pack),
        "learning_rule_ids": [
            str(item.get("rule_id")) for item in _normalized_learning_rules(learning_rules) if item.get("rule_id")
        ],
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
        production_engine_adapter: Any | None = None,
        production_engine_options: dict[str, Any] | None = None,
        visual_qc_adapter: Any | None = None,
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
        self.production_engine_adapter = production_engine_adapter
        self.production_engine_options = dict(production_engine_options or {})
        self.visual_qc_adapter = visual_qc_adapter or verify_video_visuals

    def run(self, folder: Path, production_input: dict[str, Any] | None = None) -> dict[str, Any]:
        raise RuntimeError("v2生产线必须通过JobStore分阶段运行并完成阶段审查门禁")

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
        capability_pack = config.get("capability_pack") if isinstance(config.get("capability_pack"), dict) else None
        learning_rules = _normalized_learning_rules(config.get("learning_rules"))
        insight = self._build_insight(config, research)
        atomic_json(folder / "insight.json", insight)
        variants, provider_report = self._generate_variants(config, insight, approved_findings)
        provider_report["budget"] = self.budget.snapshot()
        provider_report["tool_calls"] = len(research.get("tool_trace", []))
        atomic_json(folder / "script_variants.json", {"variants": variants, "provider": provider_report})
        approved_path = folder / "approved_script.json"
        safe_candidates = [
            item for item in variants
            if not review_script(
                str(item.get("script", "")), approved_findings, capability_pack, learning_rules
            )["blocked"]
        ]
        paced_candidates = [
            item for item in safe_candidates
            if not review_narration_pacing(str(item.get("script", "")))["blocked"]
        ]
        if not paced_candidates:
            local_candidates = build_local_variants(
                str(config["topic"]), str(config["audience"]), approved_findings,
                capability_pack, learning_rules,
            )
            paced_candidates = [
                item for item in local_candidates
                if not review_script(
                    str(item.get("script", "")), approved_findings, capability_pack, learning_rules
                )["blocked"]
                and not review_narration_pacing(str(item.get("script", "")))["blocked"]
            ]
            provider_report["narration_pacing_fallback_used"] = True
            provider_report["narration_pacing_rejections"] = [
                {
                    "id": str(item.get("id", "")),
                    "review": review_narration_pacing(str(item.get("script", ""))),
                }
                for item in (safe_candidates or variants)
            ]
        if not paced_candidates:
            raise ScriptRevisionRequired("脚本Agent及本地安全兜底均未生成180到195个口播字的自然节奏脚本")
        approved = dict(paced_candidates[0])
        approved.update({"selected_by": "local_compliance_prefilter", "selected_at": datetime.now().astimezone().isoformat(timespec="seconds")})
        atomic_json(approved_path, approved)
        review = review_script(str(approved["script"]), approved_findings, capability_pack, learning_rules)
        if review["blocked"] and self.provider is not None and provider_report.get("source") == "DeepSeek":
            try:
                original_script = str(approved["script"])
                repair = _call_with_optional_policy(
                    self.provider.repair_content_script,
                    original_script,
                    review,
                    insight,
                    capability_pack=capability_pack,
                    production_input=config,
                    learning_rules=learning_rules,
                )
                provider_report["repair_source"] = "DeepSeek"
                repaired_script = str(repair.get("script", "")).strip()
                if repaired_script:
                    approved.update({"script": repaired_script, "selected_by": "DeepSeek_repair_then_local_rules", "original_blocked_script": original_script, "repair_changes": repair.get("changes", [])})
                    atomic_json(approved_path, approved)
                    review = review_script(repaired_script, approved_findings, capability_pack, learning_rules)
                    review["repair"] = {"applied": True, "changes": repair.get("changes", [])}
            except ProviderError as exc:
                review["repair_error"] = str(exc)
        elif self.provider is not None and provider_report.get("source") == "DeepSeek":
            try:
                model_review = _call_with_optional_policy(
                    self.provider.review_content_script,
                    approved["script"],
                    review,
                    capability_pack=capability_pack,
                    production_input=config,
                    learning_rules=learning_rules,
                )
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
            safe_template = build_local_variants(
                str(config["topic"]), str(config["audience"]), approved_findings,
                capability_pack, learning_rules,
            )[0]
            approved.update({
                "script": safe_template["script"],
                "selected_by": "trusted_topic_aware_safety_template",
                "unsafe_candidate_preserved": unsafe_script,
            })
            atomic_json(approved_path, approved)
            previous_warnings = review.get("warnings", [])
            review = review_script(str(approved["script"]), approved_findings, capability_pack, learning_rules)
            review["safety_fallback"] = {
                "applied": True,
                "reason": "候选仍命中本地阻断规则，改用与选题相关的安全模板",
                "previous_warnings": previous_warnings,
            }
        final_pacing = review_narration_pacing(str(approved["script"]))
        if final_pacing["blocked"]:
            local_candidates = build_local_variants(
                str(config["topic"]), str(config["audience"]), approved_findings,
                capability_pack, learning_rules,
            )
            replacement = next(
                (
                    item for item in local_candidates
                    if not review_script(
                        str(item.get("script", "")), approved_findings, capability_pack, learning_rules
                    )["blocked"]
                    and not review_narration_pacing(str(item.get("script", "")))["blocked"]
                ),
                None,
            )
            if replacement is None:
                raise ScriptRevisionRequired("最终脚本未通过自然节奏门禁，且没有合格的本地安全脚本")
            approved.update({
                "script": str(replacement["script"]),
                "selected_by": "trusted_pacing_safe_topic_template",
                "pacing_rejected_script": str(approved.get("script", "")),
            })
            atomic_json(approved_path, approved)
            review = review_script(str(approved["script"]), approved_findings, capability_pack, learning_rules)
            final_pacing = review_narration_pacing(str(approved["script"]))
        review["narration_pacing"] = final_pacing
        if resolve_production_mode(config) == "motion":
            storyboard, storyboard_report = self._generate_motion_storyboard(
                config,
                insight,
                str(approved["script"]),
                # The visual director is an independent Agent stage.  A safe
                # local script fallback must not silently disable DeepSeek's
                # shot and card-layout decisions.
                prefer_provider=True,
            )
            atomic_json(folder / "motion_storyboard.json", storyboard)
            provider_report["motion_storyboard"] = storyboard_report
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
        production_mode = resolve_production_mode(
            production_input,
            legacy_engine_adapter=(
                self.production_engine_adapter is not None
                and not isinstance((production_input or {}).get("production_mode"), str)
            ),
        )
        config.update(production_mode_contract(production_mode))
        if approvals.get("research", {}).get("status") != "approved" or approvals.get("compliance", {}).get("status") != "approved":
            raise RuntimeError("研究与合规阶段审查门禁尚未全部批准")
        if production_mode == "hybrid":
            raise RuntimeError("hybrid生产模式尚未实现，不能降级或伪装为其他引擎")
        approved = json.loads((folder / "approved_script.json").read_text(encoding="utf-8"))
        review = json.loads((folder / "review.json").read_text(encoding="utf-8"))
        if review.get("status") == "blocked" or review.get("blocked"):
            raise RuntimeError("合规审核仍处于阻断状态")
        segments = self._segments(config, str(approved["script"]), folder=folder)
        production_engine_report: dict[str, Any] | None = None
        motion_caption_report: dict[str, Any] | None = None
        program_audio_report: dict[str, Any] | None = None
        if production_mode == "footage":
            if self.production_engine_adapter is None:
                raise RuntimeError("footage生产模式要求已验证的MoneyPrinterTurbo适配器")
            engine_stage = self._run_production_engine(folder, approved, config, segments)
            voice_report = engine_stage["voice"]
            duration = float(engine_stage["duration_seconds"])
            render_report = engine_stage["render"]
            production_engine_report = dict(engine_stage["engine"])
            production_engine_report["selected_mode"] = "footage"
            production_engine_report["codec_strategy"] = H264_CODEC_STRATEGY
            render_report["codec_strategy"] = H264_CODEC_STRATEGY
        else:
            voice_report = (
                self.voice_adapter(folder, approved["script"], config)
                if self.voice_adapter
                else self._synthesize_voice(folder, approved["script"], config, segments=segments)
            )
            if not (folder / "voice.wav").is_file():
                raise RuntimeError("配音适配器没有生成voice.wav")
            if voice_report.get("segment_aligned") is True:
                delivered_duration = self._audio_duration(folder / "voice.wav")
                if not 45.0 <= delivered_duration <= 60.0:
                    raise ScriptRevisionRequired(
                        f"逐镜头配音总时长{delivered_duration:.2f}秒，不在45-60秒；"
                        "脚本Agent必须重分镜或调整逐镜头台词，禁止整轨加速或减速"
                    )
                voice_report.update({
                    "duration_seconds": round(delivered_duration, 3),
                    "tempo_adjusted": False,
                    "duration_source": "scene_voice_segments",
                })
            else:
                voice_report.update(
                    self._normalize_voice_duration(
                        folder, float(config.get("target_duration_seconds", 52))
                    )
                )
            if self.voice_adapter is None:
                voice_report.update(
                    self._validate_delivered_voice_pacing(
                        str(approved["script"]), float(voice_report["duration_seconds"])
                    )
                )
                voice_report.update({
                    "schema_version": 3,
                    "script_sha256": hashlib.sha256(str(approved["script"]).encode("utf-8")).hexdigest().upper(),
                    "voice_sha256": _file_sha256(folder / "voice.wav"),
                    "quality_eligible": (
                        voice_report.get("natural_voice") is True
                        and voice_report.get("quality_blocked") is not True
                        and str(voice_report.get("engine")) in NATURAL_VOICE_ENGINES
                    ),
                })
                atomic_json(folder / "voice_identity.json", voice_report)
            duration = self._audio_duration(folder / "voice.wav")
            if self.voice_adapter is None and self.render_adapter is None:
                program_audio_report = build_default_program_audio(
                    folder / "voice.wav",
                    folder / "bgm.wav",
                    folder / "program_audio.wav",
                    ffmpeg_path=FFMPEG,
                    duration_seconds=duration,
                    script_sha256=hashlib.sha256(str(approved["script"]).encode("utf-8")).hexdigest().upper(),
                )
                duration = self._audio_duration(folder / "program_audio.wav")
            captions = self._write_captions(
                folder,
                segments,
                duration,
                scene_timings=(
                    voice_report.get("scene_segments")
                    if voice_report.get("segment_aligned") is True
                    else None
                ),
            )
            if production_mode == "motion":
                motion_caption_report = self._validate_motion_captions(
                    folder / "captions.srt", duration, str(approved["script"])
                )
        capability_pack = config.get("capability_pack") if isinstance(config.get("capability_pack"), dict) else None
        learning_rules = _normalized_learning_rules(config.get("learning_rules"))
        # Exact caption boundaries are authoritative only when they came from
        # the per-scene voice contract.  Footage runs and legacy/injected voice
        # adapters keep the director's proven duration allocation instead of
        # accidentally treating derived subtitle timestamps as scene locks.
        motion_segments = (
            captions
            if production_mode != "footage" and voice_report.get("segment_aligned") is True
            else segments
        )
        motion_plan = build_motion_plan(
            config["topic"], config["audience"], motion_segments, duration, capability_pack=capability_pack
        )
        atomic_json(folder / "motion_plan.json", motion_plan)
        injected_adapter_identity: dict[str, Any] | None = None
        if production_mode != "footage":
            if self.render_adapter:
                injected_adapter_identity = _injected_render_adapter_identity(self.render_adapter)
                render_report = self.render_adapter(folder, motion_plan, config)
                if not isinstance(render_report, dict):
                    raise RuntimeError("渲染适配器返回了无效诊断报告")
                if not (folder / "final.mp4").is_file():
                    raise RuntimeError("渲染适配器没有生成final.mp4")
                render_report["diagnostic_only"] = True
                render_report["renderer_identity"] = dict(injected_adapter_identity)
            elif production_mode == "motion":
                # Motion is the formal default and is deliberately fail-closed:
                # HyperFrames failure must never become a static or MPT result.
                render_report = self._render_animated_video(folder, motion_plan, config)
            else:
                render_report = self._render_video(folder, segments, captions, duration, config)
                render_report["mode"] = "static_requested"
            if production_mode == "motion":
                render_report["caption_validation"] = motion_caption_report
                if injected_adapter_identity is None:
                    production_engine_report = {
                        "name": MOTION_ENGINE_NAME,
                        "version": HYPERFRAMES_VERSION,
                        "renderer": HYPERFRAMES_RENDERER,
                        "mode": MOTION_ENGINE_MODE,
                        "selected_mode": "motion",
                        "health": "completed",
                        "codec_strategy": H264_CODEC_STRATEGY,
                        "patch_id": HYPERFRAMES_PATCH_ID,
                        "patch_version": HYPERFRAMES_PATCH_VERSION,
                        "patched_cli_sha256": HYPERFRAMES_PATCHED_CLI_SHA256,
                        "browser_strategy": render_report.get("browser_strategy"),
                        "browser_version": render_report.get("browser_version"),
                        "browser_minimum_major": render_report.get("browser_minimum_major"),
                    }
                else:
                    production_engine_report = {
                        "name": "Injected render adapter",
                        "version": None,
                        "mode": "diagnostic_adapter",
                        "selected_mode": "motion",
                        "health": "diagnostic_only",
                        "adapter_identity": dict(injected_adapter_identity),
                    }
            else:
                render_report["diagnostic_only"] = True
                production_engine_report = {
                    "name": "Simple diagnostic renderer",
                    "version": "internal",
                    "mode": "diagnostic",
                    "selected_mode": "simple",
                    "health": "diagnostic_only",
                    "codec_strategy": H264_CODEC_STRATEGY,
                }

        render_report["production_mode"] = production_mode

        render_report["visual_qc"] = self.run_visual_qc_stage(folder)

        elapsed = round(time.monotonic() - started, 2)
        variants_payload = json.loads((folder / "script_variants.json").read_text(encoding="utf-8"))
        variants = [item for item in variants_payload.get("variants", []) if isinstance(item, dict)]
        provider_report = dict(variants_payload.get("provider", {}))
        provider_report["budget"] = self.budget.snapshot()
        research = json.loads((folder / "research.json").read_text(encoding="utf-8"))
        approved_ids = {
            str(item.get("finding_id"))
            for item in approvals.get("research", {}).get("findings", [])
            if item.get("decision") == "approved"
        }
        approved_findings = [
            item for item in research.get("findings", []) if str(item.get("finding_id")) in approved_ids
        ]
        report = {
            "status": "diagnostic_only" if production_mode == "simple" else "complete",
            "topic": config["topic"],
            "production_mode": production_mode,
            "started_at": started_at,
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "wall_clock_seconds": elapsed,
            "provider": provider_report,
            "capability_pack": _pack_report(capability_pack),
            "learning_rule_ids": [str(item.get("rule_id")) for item in learning_rules if item.get("rule_id")],
            "voice": voice_report,
            "program_audio": program_audio_report,
            "render": render_report,
            "production_engine": production_engine_report,
            "compliance": review,
            "adoption_proxy": {
                "candidate_count": len(variants),
                "provisionally_usable_count": sum(
                    not review_script(
                        str(item.get("script", "")), approved_findings, capability_pack, learning_rules
                    )["blocked"]
                    for item in variants
                ),
                "evidence_binding": "approved_research_findings",
                "definition": "匹配已批准证据且未命中阻断项；尚未经过企业运营团队验证",
            },
            "artifacts": [
                "research.json", "insight.json", "script_variants.json", "approved_script.json", "review.json",
                "voice.wav", "captions.srt", "motion_plan.json", "final.mp4", "run_report.json",
                "contact-sheet.png", "visual-qc.json",
            ] + [
                name for name in (
                    "motion_storyboard.json", "bgm.wav", "program_audio.wav",
                    "material_sources.json", "engine_report.json",
                )
                if (folder / name).is_file()
            ],
        }
        atomic_json(folder / "run_report.json", report)
        return report

    def run_visual_qc_stage(self, folder: Path) -> dict[str, Any]:
        """Revalidate the current final video before any run can be published."""
        visual_qc = self.visual_qc_adapter(
            folder / "final.mp4",
            output_dir=folder,
            material_sources_path=(
                folder / "material_sources.json" if (folder / "material_sources.json").is_file() else None
            ),
            ffmpeg_path=FFMPEG,
            ffprobe_path=FFPROBE,
            motion_profile=(
                "semantic_motion_graphics" if (folder / "motion_plan.json").is_file() else "sustained_video"
            ),
        )
        if not isinstance(visual_qc, dict) or visual_qc.get("status") not in {
            "passed", "needs_visual_review", "blocked",
        }:
            raise RuntimeError("正式成片视觉门禁返回无效结果")
        for required_name in ("contact-sheet.png", "visual-qc.json"):
            required_path = folder / required_name
            if not required_path.is_file() or required_path.stat().st_size <= 0:
                raise RuntimeError("正式成片视觉门禁产物缺失")
        _bind_visual_qc_to_engine_report(folder, visual_qc)
        summary = {
            "status": visual_qc["status"],
            "sample_count": visual_qc.get("sample_count"),
            "blocking_reasons": list(visual_qc.get("blocking_reasons", [])),
            "review_reasons": list(visual_qc.get("review_reasons", [])),
            "report_sha256": _file_sha256(folder / "visual-qc.json"),
            "contact_sheet_sha256": _file_sha256(folder / "contact-sheet.png"),
        }
        existing_report_path = folder / "run_report.json"
        if existing_report_path.is_file():
            try:
                existing_report = json.loads(existing_report_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("成功运行报告无效，无法重新执行视觉门禁") from exc
            if not isinstance(existing_report, dict):
                raise RuntimeError("成功运行报告无效，无法重新执行视觉门禁")
            render = existing_report.get("render")
            if not isinstance(render, dict):
                render = {}
                existing_report["render"] = render
            render["visual_qc"] = summary
            atomic_json(existing_report_path, existing_report)
        if visual_qc["status"] == "needs_visual_review":
            reasons = ",".join(str(item) for item in summary["review_reasons"]) or "manual_visual_review_required"
            raise VideoVisualQualityBlocked(f"正式成片等待视觉复核：{reasons}")
        if visual_qc["status"] == "blocked":
            reasons = ",".join(str(item) for item in summary["blocking_reasons"]) or "unsafe_visuals"
            raise VideoVisualQualityBlocked(f"正式成片视觉门禁阻断：{reasons}")
        return summary

    def rebuild_run_report(self, folder: Path, approvals: dict[str, Any]) -> dict[str, Any]:
        """Recalculate report-only fields from an already verified successful artifact set."""
        report_path = folder / "run_report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        research = json.loads((folder / "research.json").read_text(encoding="utf-8"))
        variants_payload = json.loads((folder / "script_variants.json").read_text(encoding="utf-8"))
        variants = [item for item in variants_payload.get("variants", []) if isinstance(item, dict)]
        approved_ids = {
            str(item.get("finding_id"))
            for item in approvals.get("research", {}).get("findings", [])
            if item.get("decision") == "approved"
        }
        approved_findings = [
            item for item in research.get("findings", []) if str(item.get("finding_id")) in approved_ids
        ]
        insight = json.loads((folder / "insight.json").read_text(encoding="utf-8"))
        capability_pack = insight.get("capability_pack") if isinstance(insight.get("capability_pack"), dict) else None
        learning_rules = _normalized_learning_rules(insight.get("learning_rules"))
        report["adoption_proxy"] = {
            "candidate_count": len(variants),
            "provisionally_usable_count": sum(
                not review_script(
                    str(item.get("script", "")), approved_findings, capability_pack, learning_rules
                )["blocked"]
                for item in variants
            ),
            "evidence_binding": "approved_research_findings",
            "definition": "匹配已批准证据且未命中阻断项；尚未经过企业运营团队验证",
        }
        report["report_rebuilt_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        atomic_json(report_path, report)
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
                max_model_turns=int(self.research_config.get("max_model_turns", 4)),
            )
            return _call_with_optional_policy(
                agent.run,
                str(config["topic"]),
                str(config["audience"]),
                source_urls,
                capability_pack=config.get("capability_pack"),
                production_input=config,
                learning_rules=_normalized_learning_rules(config.get("learning_rules")),
            )
        except Exception:
            return {
                "status": "failed",
                "summary": "联网调研失败，未生成可审批证据",
                "findings": [],
                "content_patterns": [],
                "evidence_gaps": ["联网调研未完成；请修复后重试或明确关闭联网调研"],
                "sources": [],
                "tool_trace": [],
                "model_calls": 0,
                "budget": self.budget.snapshot(),
            }

    @staticmethod
    def _build_insight(config: dict[str, Any], research: dict[str, Any] | None = None) -> dict[str, Any]:
        capability_pack = config.get("capability_pack") if isinstance(config.get("capability_pack"), dict) else None
        snapshot = _pack_snapshot(capability_pack)
        legacy = _is_legacy_pack(capability_pack) or (
            capability_pack is None and any(term in str(config.get("topic", "")) for term in ("甲醛", "除醛", "测醛"))
        )
        ids = {str(value) for value in config.get("pattern_card_ids", [])}
        selected = [card for card in load_pattern_cards() if str(card.get("item_id")) in ids] if legacy else []
        research_for_script = dict(research or {})
        if research_for_script:
            research_for_script["findings"] = list(research_for_script.get("script_eligible_findings", []))
            research_for_script.pop("script_eligible_findings", None)
            research_for_script.pop("tool_trace", None)
        learning_rules = [
            {
                key: rule[key]
                for key in ("rule_id", "scope", "instruction", "pack_id", "source_event_ids")
                if key in rule
            }
            for rule in _normalized_learning_rules(config.get("learning_rules"))
        ]
        if legacy:
            pattern = "大字主张→小字条件→条件换算→现实场景反差→行动建议"
            source_boundary = "公开视频仅用于学习内容结构，不作为产品功效证据"
            requirements = ["剂量", "空间体积", "作用时间", "初始浓度", "检测方法", "报告来源"]
            references = [
                {"name": "GB/T 18883-2022 室内空气质量标准", "url": "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=6188E23AE55E8F557043401FC2EDC436"},
                {"name": "中华人民共和国广告法", "url": "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/fgs/art/2023/art_5474cf75173c45d6a0379730fb4e8d97.html"},
            ]
        else:
            content_purpose = str(snapshot.get("content_purpose") or "解释一个真实业务问题").strip()
            pattern = f"问题界定→证据核验→适用边界→{content_purpose}→下一步行动"
            source_boundary = "只把可追溯且通过严格审核的材料写入正文；未知项保持未知"
            requirements = _string_list(snapshot.get("evidence_requirements")) or [
                "来源可追溯", "对象明确", "时间明确", "适用范围明确", "限制条件明确",
            ]
            references = []
        return {
            "topic": config["topic"],
            "audience": config["audience"],
            "selected_pattern_cards": selected,
            "pattern": pattern,
            "source_boundary": source_boundary,
            "evidence_requirements": requirements,
            "tone": snapshot.get("tone") or "清晰、克制、可复核",
            "visual_direction": snapshot.get("visual_direction") or {},
            "official_references": references,
            "capability_pack": capability_pack or {},
            "learning_rules": learning_rules,
            "web_research": research_for_script,
        }

    def _generate_variants(
        self,
        config: dict[str, Any],
        insight: dict[str, Any],
        approved_findings: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        report: dict[str, Any] = {"source": "local_deterministic", "fallback_used": False}
        capability_pack = config.get("capability_pack") if isinstance(config.get("capability_pack"), dict) else None
        learning_rules = _normalized_learning_rules(config.get("learning_rules"))
        if self.provider is None or not getattr(self.provider, "api_key", ""):
            return build_local_variants(
                str(config["topic"]), str(config["audience"]), approved_findings,
                capability_pack, learning_rules,
            ), report
        try:
            variants = self.provider.generate_content_scripts(config, insight)
            if not isinstance(variants, list) or len(variants) != 4:
                raise ProviderError("脚本接口没有返回4个候选")
            report.update({"source": "DeepSeek"})
            return variants, report
        except ProviderError as exc:
            report.update({"fallback_used": True, "fallback_reason": str(exc)})
            return build_local_variants(
                str(config["topic"]), str(config["audience"]), approved_findings,
                capability_pack, learning_rules,
            ), report

    def _generate_motion_storyboard(
        self,
        config: dict[str, Any],
        insight: dict[str, Any],
        script: str,
        *,
        prefer_provider: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        capability_pack = config.get("capability_pack") if isinstance(config.get("capability_pack"), dict) else None
        if (
            prefer_provider
            and self.provider is not None
            and getattr(self.provider, "api_key", "")
            and callable(getattr(self.provider, "generate_motion_storyboard", None))
        ):
            try:
                raw = self.provider.generate_motion_storyboard(script, config, insight)
                retry_used = False
                try:
                    storyboard = validate_storyboard(
                        raw,
                        script,
                        source="DeepSeek",
                        model=str(getattr(self.provider, "model", "")),
                    )
                except StoryboardError as first_error:
                    retry_used = True
                    repaired = self.provider.generate_motion_storyboard(
                        script,
                        config,
                        insight,
                        mechanical_feedback=str(first_error),
                    )
                    storyboard = validate_storyboard(
                        repaired,
                        script,
                        source="DeepSeek",
                        model=str(getattr(self.provider, "model", "")),
                    )
                return storyboard, {
                    "source": "DeepSeek",
                    "model": str(getattr(self.provider, "model", "")),
                    "fallback_used": False,
                    "mechanical_review": "passed",
                    "mechanical_retry_used": retry_used,
                }
            except (ProviderError, StoryboardError, TypeError, ValueError):
                fallback_reason = "provider_output_failed_mechanical_storyboard_review"
        else:
            fallback_reason = "provider_unavailable_or_content_used_local_fallback"
        storyboard = build_local_storyboard(str(config["topic"]), script, capability_pack)
        return storyboard, {
            "source": "local_deterministic_storyboard",
            "model": "",
            "fallback_used": True,
            "fallback_reason": fallback_reason,
            "mechanical_review": "passed",
        }

    @staticmethod
    def _voice_segments_digest(segments: list[dict[str, Any]] | None) -> str:
        captions = [str(item.get("caption") or "").strip() for item in (segments or [])]
        payload = json.dumps(captions, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest().upper()

    @staticmethod
    def _pcm_wave_duration(path: Path) -> float:
        try:
            with wave.open(str(path), "rb") as audio:
                if (
                    audio.getnchannels() != 1
                    or audio.getsampwidth() != 2
                    or audio.getframerate() != 48000
                    or audio.getcomptype() != "NONE"
                ):
                    raise RuntimeError("逐镜头配音必须是48kHz、单声道、16位PCM WAV")
                return audio.getnframes() / audio.getframerate()
        except (OSError, EOFError, wave.Error) as exc:
            raise RuntimeError(f"逐镜头配音WAV无效: {path.name}") from exc

    @classmethod
    def _concatenate_voice_segments(
        cls,
        segment_rows: list[dict[str, Any]],
        output_path: Path,
    ) -> list[dict[str, Any]]:
        if not segment_rows:
            raise RuntimeError("逐镜头配音不能为空")
        output_path.unlink(missing_ok=True)
        cursor_frames = 0
        sample_rate = 48000
        silence_frames = round(VOICE_SCENE_PAUSE_SECONDS * sample_rate)
        timings: list[dict[str, Any]] = []
        with wave.open(str(output_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            for index, row in enumerate(segment_rows, start=1):
                segment_path = Path(row["path"])
                with wave.open(str(segment_path), "rb") as audio:
                    if (
                        audio.getnchannels() != 1
                        or audio.getsampwidth() != 2
                        or audio.getframerate() != sample_rate
                        or audio.getcomptype() != "NONE"
                    ):
                        raise RuntimeError(f"逐镜头配音格式不一致: {segment_path.name}")
                    frame_count = audio.getnframes()
                    frame_bytes = audio.readframes(frame_count)
                start = cursor_frames / sample_rate
                output.writeframesraw(frame_bytes)
                cursor_frames += frame_count
                spoken_end = cursor_frames / sample_rate
                pause_after = VOICE_SCENE_PAUSE_SECONDS if index < len(segment_rows) else 0.0
                if pause_after:
                    output.writeframesraw(b"\0" * silence_frames * 2)
                    cursor_frames += silence_frames
                end = cursor_frames / sample_rate
                timings.append({
                    "id": str(row["id"]),
                    "index": index,
                    "caption": str(row["caption"]),
                    "text_sha256": str(row["text_sha256"]),
                    "voice_file": str(row["voice_file"]),
                    "voice_sha256": str(row["voice_sha256"]),
                    "spoken_characters": int(row["spoken_characters"]),
                    "spoken_characters_per_second": float(row["spoken_characters_per_second"]),
                    "start_seconds": round(start, 3),
                    "spoken_end_seconds": round(spoken_end, 3),
                    "end_seconds": round(end, 3),
                    "spoken_duration_seconds": round(spoken_end - start, 3),
                    "pause_after_seconds": round(pause_after, 3),
                })
            output.writeframes(b"")
        if not output_path.is_file() or output_path.stat().st_size <= 44:
            raise RuntimeError("逐镜头配音拼接失败")
        return timings

    @classmethod
    def _synthesize_voice(
        cls,
        folder: Path,
        script: str,
        config: dict[str, Any],
        *,
        segments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        requested_engine = normalize_voice_engine(config.get("voice_engine", DEFAULT_VOICE_ENGINE))
        chunk_max_chars = normalize_voice_chunk_max_chars(config.get("voice_chunk_max_chars"))
        text_path = folder / "narration.txt"
        previous_text = text_path.read_text(encoding="utf-8") if text_path.exists() else None
        existing_voice = folder / "voice.wav"
        identity_path = folder / "voice_identity.json"
        script_sha256 = hashlib.sha256(script.encode("utf-8")).hexdigest().upper()
        segment_aligned = bool(segments)
        segments_sha256 = cls._voice_segments_digest(segments)
        if previous_text == script and existing_voice.exists() and existing_voice.stat().st_size > 44:
            try:
                identity = json.loads(identity_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                identity = None
            if (
                isinstance(identity, dict)
                and identity.get("schema_version") == 3
                and identity.get("script_sha256") == script_sha256
                and identity.get("voice_sha256") == _file_sha256(existing_voice)
                and identity.get("natural_voice") is True
                and identity.get("quality_eligible") is True
                and identity.get("requested_engine") == requested_engine
                and identity.get("voice") == DEFAULT_EDGE_TTS_VOICE
                and identity.get("voice_rate") == DEFAULT_VOICE_RATE
                and identity.get("voice_selection_exposed") is False
                and identity.get("voice_chunk_max_chars") == chunk_max_chars
                and identity.get("segment_contract_version") == VOICE_SEGMENT_CONTRACT_VERSION
                and identity.get("segment_aligned") is segment_aligned
                and identity.get("segments_sha256") == segments_sha256
                and str(identity.get("engine")) in NATURAL_VOICE_ENGINES
                and identity.get("engine") == requested_engine
            ):
                report = dict(identity)
                report.update({"reused": True, "reason": "脚本、音频哈希和自然配音身份均匹配"})
                return report
        text_path.write_text(script, encoding="utf-8")
        existing_voice.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)
        segment_folder = folder / "voice_segments"
        if segment_folder.exists():
            shutil.rmtree(segment_folder)
        log_path = folder / "voice_generation.log"
        failures: list[str] = []
        configured_voice = str(config.get("edge_tts_voice") or DEFAULT_EDGE_TTS_VOICE).strip()
        if configured_voice != DEFAULT_EDGE_TTS_VOICE:
            raise ValueError("产品只允许固定的普通中文播报声，不提供音色选择")

        if requested_engine == DEFAULT_VOICE_ENGINE:
            edge_voice = DEFAULT_EDGE_TTS_VOICE
            try:
                if not _tool_available(FFMPEG):
                    raise FileNotFoundError("未找到FFmpeg，无法规范化Edge Neural TTS音频")
                if segment_aligned:
                    if not 4 <= len(segments or []) <= 8:
                        raise ScriptRevisionRequired("逐镜头配音必须与4到8幕导演蓝图一一对应")
                    normalized_script = re.sub(r"\s+", "", script)
                    normalized_captions = re.sub(
                        r"\s+", "", "".join(str(item.get("caption") or "") for item in (segments or []))
                    )
                    if normalized_script != normalized_captions:
                        raise ScriptRevisionRequired("逐镜头台词未按原顺序完整覆盖最终脚本")
                    segment_folder.mkdir(parents=True, exist_ok=False)
                    voice_inputs = [
                        {
                            "id": f"scene-{index:02d}",
                            "caption": str(item.get("caption") or "").strip(),
                            "text_path": segment_folder / f"scene-{index:02d}.txt",
                            "edge_media": segment_folder / f"scene-{index:02d}.edge.mp3",
                            "wav_path": segment_folder / f"scene-{index:02d}.wav",
                        }
                        for index, item in enumerate(segments or [], start=1)
                    ]
                else:
                    voice_inputs = [{
                        "id": "scene-01",
                        "caption": script,
                        "text_path": text_path,
                        "edge_media": folder / "voice.edge.mp3",
                        "wav_path": existing_voice,
                    }]

                segment_rows: list[dict[str, Any]] = []
                for voice_input in voice_inputs:
                    caption = str(voice_input["caption"]).strip()
                    if not caption:
                        raise ScriptRevisionRequired(f"{voice_input['id']}没有可配音台词")
                    segment_text_path = Path(voice_input["text_path"])
                    edge_media = Path(voice_input["edge_media"])
                    segment_wav = Path(voice_input["wav_path"])
                    segment_text_path.write_text(caption, encoding="utf-8")
                    edge_media.unlink(missing_ok=True)
                    segment_wav.unlink(missing_ok=True)
                    edge_result = subprocess.run(
                        [
                            sys.executable, "-m", "edge_tts", "--file", str(segment_text_path),
                            "--voice", edge_voice, f"--rate={DEFAULT_VOICE_RATE}",
                            "--write-media", str(edge_media),
                        ],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=300,
                        check=True,
                    )
                    if not edge_media.is_file() or edge_media.stat().st_size <= 0:
                        raise RuntimeError(f"Edge Neural TTS未生成{voice_input['id']}音频")
                    conversion = subprocess.run(
                        [
                            str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                            "-i", str(edge_media), "-vn", "-ac", "1", "-ar", "48000",
                            "-c:a", "pcm_s16le", str(segment_wav),
                        ],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=300,
                        check=True,
                    )
                    if not segment_wav.is_file() or segment_wav.stat().st_size <= 44:
                        raise RuntimeError(f"{voice_input['id']}音频规范化失败")
                    with log_path.open("a", encoding="utf-8") as handle:
                        handle.write((edge_result.stdout or "") + "\n" + (edge_result.stderr or ""))
                        handle.write((conversion.stdout or "") + "\n" + (conversion.stderr or ""))
                    if segment_aligned:
                        spoken_duration = cls._pcm_wave_duration(segment_wav)
                        spoken_characters = int(estimate_narration_duration(caption)["spoken_characters"])
                        delivered_rate = spoken_characters / max(spoken_duration, 0.001)
                        if delivered_rate > NARRATION_MAX_DELIVERED_CHARACTERS_PER_SECOND:
                            raise ScriptRevisionRequired(
                                f"{voice_input['id']}配音密度{delivered_rate:.2f}字/秒，"
                                f"超过{NARRATION_MAX_DELIVERED_CHARACTERS_PER_SECOND:.2f}字/秒；"
                                "脚本Agent必须缩短该幕台词或重新分镜"
                            )
                        segment_rows.append({
                            "id": voice_input["id"],
                            "caption": caption,
                            "path": segment_wav,
                            "voice_file": segment_wav.relative_to(folder).as_posix(),
                            "text_sha256": hashlib.sha256(caption.encode("utf-8")).hexdigest().upper(),
                            "voice_sha256": _file_sha256(segment_wav),
                            "spoken_characters": spoken_characters,
                            "spoken_characters_per_second": round(delivered_rate, 3),
                        })
                    edge_media.unlink(missing_ok=True)

                if segment_aligned:
                    scene_timings = cls._concatenate_voice_segments(segment_rows, existing_voice)
                    atomic_json(folder / "voice_segments.json", {
                        "schema_version": 1,
                        "contract": "scene_aligned_edge_tts_pcm_v1",
                        "segments_sha256": segments_sha256,
                        "pause_seconds": VOICE_SCENE_PAUSE_SECONDS,
                        "scene_segments": scene_timings,
                    })
                else:
                    scene_timings = []
                if not existing_voice.is_file() or existing_voice.stat().st_size <= 44:
                    raise RuntimeError("Edge Neural TTS音频规范化失败")
                report = {
                    "engine": "edge_tts",
                    "voice": edge_voice,
                    "voice_rate": DEFAULT_VOICE_RATE,
                    "voice_label": DEFAULT_VOICE_LABEL,
                    "voice_selection_exposed": False,
                    "requires_network": True,
                    "requires_api_key": False,
                    "requires_local_private_voice_model": False,
                    "fallback": False,
                    "requested_engine": requested_engine,
                    "voice_chunk_max_chars": chunk_max_chars,
                    "segment_count": len(voice_inputs),
                    "segment_aligned": segment_aligned,
                    "segment_contract_version": VOICE_SEGMENT_CONTRACT_VERSION,
                    "segments_sha256": segments_sha256,
                    "scene_pause_seconds": VOICE_SCENE_PAUSE_SECONDS if segment_aligned else 0.0,
                    "scene_segments": scene_timings,
                    "natural_voice": True,
                }
                return report
            except Exception as exc:
                failures.append(f"edge_tts:{type(exc).__name__}:{exc}")
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(f"Edge natural voice failed: {exc}\n")
            finally:
                for media_path in folder.glob("voice*.edge.mp3"):
                    media_path.unlink(missing_ok=True)
                if segment_folder.exists():
                    for media_path in segment_folder.glob("*.edge.mp3"):
                        media_path.unlink(missing_ok=True)

        raise RuntimeError(
            "固定普通中文播报生成失败，已禁止切换其他音色或本机私人模型：" + "; ".join(failures)
        )

    @staticmethod
    def _segments(
        config: dict[str, Any],
        script: str,
        *,
        folder: Path | None = None,
    ) -> list[dict[str, Any]]:
        # The current content-stage Agent storyboard outranks legacy caller
        # overrides.  Old jobs without the artifact retain their narrow
        # motion_scenes compatibility path below.
        storyboard_path = Path(folder) / "motion_storyboard.json" if folder is not None else None
        if storyboard_path is not None and storyboard_path.is_file():
            raw_storyboard = json.loads(storyboard_path.read_text(encoding="utf-8"))
            storyboard = validate_storyboard(
                raw_storyboard,
                script,
                source=str(raw_storyboard.get("source", "stored_storyboard")),
                model=str(raw_storyboard.get("model", "")),
            )
            return storyboard_to_motion_segments(storyboard)
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
        capability_pack = config.get("capability_pack") if isinstance(config.get("capability_pack"), dict) else None
        return derive_motion_segments(
            str(config["topic"]), script, target_count=7, capability_pack=capability_pack
        )

    def _run_production_engine(
        self,
        folder: Path,
        approved: dict[str, Any],
        config: dict[str, Any],
        segments: list[dict[str, str]],
    ) -> dict[str, Any]:
        options = dict(self.production_engine_options)
        topic = str(config.get("topic", "")).strip()
        keywords = [topic]
        for segment in segments:
            value = str(segment.get("title") or segment.get("caption") or "").strip()
            if value and value not in keywords:
                keywords.append(value)
            if len(keywords) >= 12:
                break
        result = self.production_engine_adapter.run(
            approved=True,
            script=str(approved["script"]),
            keywords=keywords,
            aspect="portrait",
            target_duration_seconds=float(config.get("target_duration_seconds", 52)),
            staging_dir=folder,
            material_strategy=str(options.get("material_strategy", "pexels")),
            voice_strategy=str(options.get("voice_strategy", "edge_tts")),
            local_material_paths=options.get("local_material_paths"),
        )
        result_payload = result.as_dict() if callable(getattr(result, "as_dict", None)) else {}
        if not isinstance(result_payload, dict):
            raise RuntimeError("MPT生产引擎返回了无效结果")
        if (
            result_payload.get("engine_name"),
            result_payload.get("engine_version"),
            result_payload.get("engine_commit"),
            result_payload.get("mode"),
        ) != (ENGINE_NAME, ENGINE_VERSION, ENGINE_COMMIT, ENGINE_MODE):
            raise RuntimeError("MPT生产引擎身份与固定版本不一致")

        raw_audio = folder / ".engine-import" / "audio.mp3"
        self._import_engine_audio(raw_audio, folder / "voice.wav")
        voice_report = {
            "engine": result_payload.get("engine_name", "MoneyPrinterTurbo"),
            "source_format": "audio/mpeg",
            "canonical_format": "audio/wav",
            "source_sha256": _file_sha256(raw_audio),
        }
        voice_report.update(
            self._normalize_voice_duration(folder, float(config.get("target_duration_seconds", 52)))
        )
        retiming_report = {"applied": False}
        if voice_report.get("tempo_adjusted") is True:
            retiming_report = self._retime_engine_output(
                folder, float(voice_report["tempo_factor"])
            )
        duration = self._audio_duration(folder / "voice.wav")
        caption_report = self._validate_engine_captions(
            folder / "captions.srt", duration, str(approved["script"])
        )
        render_report = self._probe_engine_video(folder / "final.mp4")
        render_report.update(
            {
                "mode": "moneyprinterturbo_local_http",
                "subtitle_mode": "engine_burned_and_sidecar_srt",
                "caption_validation": caption_report,
                "retiming": retiming_report,
            }
        )

        engine_report_path = folder / "engine_report.json"
        try:
            engine_report = json.loads(engine_report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("MPT生产引擎报告无效") from exc
        expected_script_hash = hashlib.sha256(str(approved["script"]).encode("utf-8")).hexdigest().upper()
        if engine_report.get("script_sha256") != expected_script_hash:
            raise RuntimeError("MPT生产引擎报告与已批准脚本不一致")
        report_artifacts = engine_report.get("artifacts")
        if not isinstance(report_artifacts, list):
            raise RuntimeError("MPT生产引擎报告产物清单无效")
        artifact_index: dict[str, dict[str, Any]] = {}
        for item in report_artifacts:
            if not isinstance(item, dict) or not isinstance(item.get("relative_path"), str):
                raise RuntimeError("MPT生产引擎报告产物清单无效")
            relative_path = str(item["relative_path"])
            if relative_path in artifact_index:
                raise RuntimeError("MPT生产引擎报告包含重复产物")
            artifact_index[relative_path] = dict(item)
        expected_engine_artifacts = {
            "final.mp4",
            ".engine-import/audio.mp3",
            "captions.srt",
            "material_sources.json",
        }
        if set(artifact_index) != expected_engine_artifacts:
            raise RuntimeError("MPT生产引擎报告产物集合不完整")
        imported_audio = artifact_index[".engine-import/audio.mp3"]
        if imported_audio is None or imported_audio.get("sha256") != voice_report["source_sha256"]:
            raise RuntimeError("MPT生产引擎配音哈希不一致")
        imported_audio["disposition"] = "transcoded_to_voice_wav_then_removed"
        engine_report["engine_imports"] = [imported_audio]
        published_engine_artifacts: list[dict[str, Any]] = []
        for item in report_artifacts:
            if not isinstance(item, dict):
                raise RuntimeError("MPT生产引擎报告产物清单无效")
            relative_path = item.get("relative_path")
            if relative_path == ".engine-import/audio.mp3":
                continue
            record = dict(item)
            if relative_path in {"final.mp4", "captions.srt", "material_sources.json"}:
                artifact_path = folder / str(relative_path)
                if not artifact_path.is_file() or artifact_path.stat().st_size <= 0:
                    raise RuntimeError("MPT生产引擎报告产物缺失")
                if retiming_report.get("applied") is True and relative_path in {"final.mp4", "captions.srt"}:
                    record["engine_source_sha256"] = record.get("sha256")
                    record["control_layer_retimed"] = True
                record["size"] = artifact_path.stat().st_size
                record["sha256"] = _file_sha256(artifact_path)
            published_engine_artifacts.append(record)
        engine_report["artifacts"] = published_engine_artifacts
        engine_report["control_layer_validation"] = {
            "status": "passed",
            "canonical_voice_sha256": _file_sha256(folder / "voice.wav"),
            "final_video_sha256": _file_sha256(folder / "final.mp4"),
            "captions_sha256": _file_sha256(folder / "captions.srt"),
            "caption_validation": caption_report,
            "media": render_report,
        }
        atomic_json(engine_report_path, engine_report)

        raw_audio.unlink(missing_ok=True)
        private_dir = raw_audio.parent
        if private_dir.is_dir() and not any(private_dir.iterdir()):
            private_dir.rmdir()
        return {
            "voice": voice_report,
            "duration_seconds": duration,
            "render": render_report,
            "engine": {
                "name": result_payload.get("engine_name"),
                "version": result_payload.get("engine_version"),
                "commit": result_payload.get("engine_commit"),
                "mode": result_payload.get("mode"),
                "task_id": result_payload.get("task_id"),
                "status": "complete",
            },
        }

    @staticmethod
    def _import_engine_audio(source: Path, destination: Path) -> None:
        if not source.is_file() or source.stat().st_size <= 0:
            raise RuntimeError("MPT没有生成可导入的配音")
        if not _tool_available(FFMPEG):
            raise FileNotFoundError("未找到FFmpeg，无法导入MPT配音")
        command = [
            str(FFMPEG), "-y", "-i", str(source), "-vn", "-ac", "2", "-ar", "44100",
            "-c:a", "pcm_s16le", str(destination),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if result.returncode or not destination.is_file() or destination.stat().st_size <= 0:
            destination.unlink(missing_ok=True)
            raise RuntimeError("MPT配音导入失败")

    @staticmethod
    def _parse_srt_seconds(value: str) -> float:
        match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value.strip())
        if not match:
            raise RuntimeError("MPT字幕时间格式无效")
        hours, minutes, seconds, milliseconds = (int(item) for item in match.groups())
        if minutes >= 60 or seconds >= 60:
            raise RuntimeError("MPT字幕时间格式无效")
        return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000

    @classmethod
    def _retime_engine_output(cls, folder: Path, tempo_factor: float) -> dict[str, Any]:
        if not math.isfinite(tempo_factor) or not VOICE_TEMPO_MINIMUM <= tempo_factor <= VOICE_TEMPO_MAXIMUM:
            raise RuntimeError("MPT安全变速系数无效")
        video = folder / "final.mp4"
        voice = folder / "voice.wav"
        captions = folder / "captions.srt"
        if not video.is_file() or not voice.is_file() or not captions.is_file():
            raise RuntimeError("MPT重定时缺少视频、配音或字幕")
        if not _tool_available(FFMPEG):
            raise FileNotFoundError("未找到FFmpeg，无法同步调整MPT音画与字幕")

        adjusted_video = folder / "final.retimed.mp4"
        adjusted_captions = folder / "captions.retimed.srt"
        for temporary in (adjusted_video, adjusted_captions):
            if temporary.is_symlink() or (temporary.exists() and not temporary.is_file()):
                raise RuntimeError("MPT重定时临时产物路径不安全")
            temporary.unlink(missing_ok=True)

        timing_pattern = re.compile(
            r"(?m)^(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})$"
        )
        source_captions = captions.read_text(encoding="utf-8-sig")

        def replace_timing(match: re.Match[str]) -> str:
            start = cls._parse_srt_seconds(match.group(1)) / tempo_factor
            end = cls._parse_srt_seconds(match.group(2)) / tempo_factor
            return f"{cls._srt_time(start)} --> {cls._srt_time(end)}"

        retimed_captions, timing_count = timing_pattern.subn(replace_timing, source_captions)
        if timing_count <= 0:
            raise RuntimeError("MPT字幕时间轴无法安全重定时")

        command = [
            str(FFMPEG),
            "-y",
            "-i",
            str(video),
            "-i",
            str(voice),
            "-filter_complex",
            f"[0:v:0]setpts=PTS/{tempo_factor:.6f}[v]",
            "-map",
            "[v]",
            "-map",
            "1:a:0",
            *h264_mf_video_args(crf_equivalent=20),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(adjusted_video),
        ]
        try:
            try:
                adjusted_captions.write_text(retimed_captions, encoding="utf-8", newline="\n")
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=600,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError("MPT音画与字幕同步重定时失败") from exc
            if (
                result.returncode
                or not adjusted_video.is_file()
                or adjusted_video.stat().st_size <= 0
                or not adjusted_captions.is_file()
                or adjusted_captions.stat().st_size <= 0
            ):
                raise RuntimeError("MPT音画与字幕同步重定时失败")
            adjusted_video.replace(video)
            adjusted_captions.replace(captions)
        finally:
            for temporary in (adjusted_video, adjusted_captions):
                if temporary.is_file() or temporary.is_symlink():
                    temporary.unlink(missing_ok=True)
        return {
            "applied": True,
            "tempo_factor": round(tempo_factor, 6),
            "video_track": "setpts",
            "audio_track": "normalized_voice_wav_to_aac",
            "caption_timing_scaled": True,
            "timing_count": timing_count,
            "codec_strategy": H264_CODEC_STRATEGY,
        }

    @staticmethod
    def _caption_binding_text(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return "".join(
            character
            for character in normalized
            if not character.isspace()
            and unicodedata.category(character)[0] not in {"C", "P", "Z"}
        )

    @classmethod
    def _validate_caption_contract(
        cls,
        path: Path,
        media_duration: float,
        approved_script: str,
        label: str,
    ) -> dict[str, Any]:
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError(f"{label}没有生成字幕文件")
        text = path.read_text(encoding="utf-8-sig")
        blocks = [item for item in re.split(r"\r?\n\s*\r?\n", text.strip()) if item.strip()]
        if not 1 <= len(blocks) <= 500:
            raise RuntimeError(f"{label}字幕条目数量无效")
        previous_end = 0.0
        maximum_gap = 0.0
        caption_fragments: list[str] = []
        for expected_index, block in enumerate(blocks, start=1):
            lines = block.splitlines()
            if len(lines) < 3 or lines[0].strip() != str(expected_index) or " --> " not in lines[1]:
                raise RuntimeError(f"{label}字幕编号或结构不连续")
            start_text, end_text = lines[1].split(" --> ", 1)
            start = cls._parse_srt_seconds(start_text)
            end = cls._parse_srt_seconds(end_text)
            if start + 0.001 < previous_end or end <= start or not "".join(lines[2:]).strip():
                raise RuntimeError(f"{label}字幕存在重叠、倒序或空文本")
            maximum_gap = max(maximum_gap, start - previous_end)
            previous_end = end
            caption_fragments.extend(lines[2:])
        trailing_gap = max(0.0, media_duration - previous_end)
        maximum_gap = max(maximum_gap, trailing_gap)
        if previous_end > media_duration + 0.75 or maximum_gap > 2.0:
            raise RuntimeError(f"{label}字幕时间轴与成片不连续")
        approved_binding = cls._caption_binding_text(approved_script)
        caption_binding = cls._caption_binding_text("".join(caption_fragments))
        if not approved_binding or caption_binding != approved_binding:
            raise RuntimeError(f"{label}字幕正文与已批准脚本不一致")
        return {
            "status": "passed",
            "cue_count": len(blocks),
            "overlap_count": 0,
            "last_end_seconds": round(previous_end, 3),
            "maximum_gap_seconds": round(maximum_gap, 3),
            "text_sha256": hashlib.sha256(caption_binding.encode("utf-8")).hexdigest().upper(),
        }

    @classmethod
    def _validate_engine_captions(
        cls,
        path: Path,
        media_duration: float,
        approved_script: str,
    ) -> dict[str, Any]:
        return cls._validate_caption_contract(path, media_duration, approved_script, "MPT")

    @classmethod
    def _validate_motion_captions(
        cls,
        path: Path,
        media_duration: float,
        approved_script: str,
    ) -> dict[str, Any]:
        return cls._validate_caption_contract(path, media_duration, approved_script, "动画")

    @staticmethod
    def _probe_video_contract(path: Path, label: str) -> dict[str, Any]:
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError(f"{label}没有生成成片")
        if not _tool_available(FFPROBE):
            raise FileNotFoundError(f"未找到FFprobe，无法验证{label}成片")
        command = [str(FFPROBE), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
        try:
            probe = json.loads(subprocess.check_output(command, text=True, encoding="utf-8"))
            video_stream = next(stream for stream in probe["streams"] if stream.get("codec_type") == "video")
            audio_stream = next(stream for stream in probe["streams"] if stream.get("codec_type") == "audio")
            duration = float(probe["format"]["duration"])
        except (OSError, ValueError, KeyError, StopIteration, json.JSONDecodeError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"{label}成片媒体结构无效") from exc
        if video_stream.get("width") != 1080 or video_stream.get("height") != 1920:
            raise RuntimeError(f"{label}成片分辨率不符合1080x1920")
        if video_stream.get("codec_name") != "h264" or audio_stream.get("codec_name") != "aac":
            raise RuntimeError(f"{label}成片编码必须为H.264/AAC")
        if not 45 <= duration <= 60:
            raise RuntimeError(f"{label}成片时长{duration:.2f}秒，不在45-60秒范围内")
        return {
            "ok": True,
            "file": "final.mp4",
            "duration_seconds": round(duration, 3),
            "video_codec": "h264",
            "codec_strategy": H264_CODEC_STRATEGY,
            "audio_codec": "aac",
            "width": 1080,
            "height": 1920,
            "fps": video_stream.get("r_frame_rate"),
        }

    @classmethod
    def _probe_engine_video(cls, path: Path) -> dict[str, Any]:
        return cls._probe_video_contract(path, "MPT")

    @classmethod
    def _probe_motion_video(cls, path: Path) -> dict[str, Any]:
        return cls._probe_video_contract(path, "动画")

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
        target = min(60.0, max(45.0, target_seconds))
        feasible_minimum = max(45.0, duration / VOICE_TEMPO_MAXIMUM)
        feasible_maximum = min(60.0, duration / VOICE_TEMPO_MINIMUM)
        if feasible_minimum > feasible_maximum:
            raise ScriptRevisionRequired(
                f"配音时长{duration:.2f}秒，无法在{VOICE_TEMPO_MINIMUM:.2f}到{VOICE_TEMPO_MAXIMUM:.2f}倍自然语速范围内达到45-60秒"
            )
        desired = min(feasible_maximum, max(feasible_minimum, target))
        tempo = duration / desired
        if not VOICE_TEMPO_MINIMUM <= tempo <= VOICE_TEMPO_MAXIMUM:
            raise ScriptRevisionRequired(
                f"配音时长{duration:.2f}秒，需要{tempo:.2f}倍变速，超出{VOICE_TEMPO_MINIMUM:.2f}到{VOICE_TEMPO_MAXIMUM:.2f}的自然语速范围"
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
            "tempo_factor": round(tempo, 6),
            "original_file": "voice.original.wav",
        }

    @staticmethod
    def _validate_delivered_voice_pacing(script: str, duration_seconds: float) -> dict[str, Any]:
        spoken = int(estimate_narration_duration(script)["spoken_characters"])
        duration = float(duration_seconds)
        rate = spoken / max(duration, 0.001)
        if rate > NARRATION_MAX_DELIVERED_CHARACTERS_PER_SECOND:
            raise ScriptRevisionRequired(
                f"最终配音密度{rate:.2f}字/秒，超过{NARRATION_MAX_DELIVERED_CHARACTERS_PER_SECOND:.2f}字/秒；"
                "脚本Agent必须缩短句子、增加自然停顿后重做"
            )
        return {
            "pacing_status": "passed",
            "spoken_characters": spoken,
            "spoken_characters_per_second": round(rate, 3),
            "maximum_spoken_characters_per_second": NARRATION_MAX_DELIVERED_CHARACTERS_PER_SECOND,
        }

    @staticmethod
    def _srt_time(seconds: float) -> str:
        milliseconds = max(0, round(seconds * 1000))
        hours, milliseconds = divmod(milliseconds, 3_600_000)
        minutes, milliseconds = divmod(milliseconds, 60_000)
        secs, milliseconds = divmod(milliseconds, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"

    def _write_captions(
        self,
        folder: Path,
        segments: list[dict[str, str]],
        duration: float,
        *,
        scene_timings: Any = None,
    ) -> list[dict[str, Any]]:
        exact_timings = scene_timings if isinstance(scene_timings, list) else None
        if exact_timings is not None and len(exact_timings) != len(segments):
            raise RuntimeError("逐镜头音频时间轴与导演蓝图数量不一致")
        weights = [max(1, len(item["caption"])) for item in segments]
        total = sum(weights)
        cursor = 0.0
        output: list[dict[str, Any]] = []
        lines: list[str] = []
        for index, (segment, weight) in enumerate(zip(segments, weights), start=1):
            if exact_timings is not None:
                timing = exact_timings[index - 1]
                if not isinstance(timing, dict) or int(timing.get("index", 0)) != index:
                    raise RuntimeError(f"逐镜头音频时间轴第{index}幕身份无效")
                start = float(timing.get("start_seconds", -1))
                end = float(timing.get("end_seconds", -1))
                if abs(start - cursor) > 0.002 or end <= start:
                    raise RuntimeError(f"逐镜头音频时间轴第{index}幕不连续")
            else:
                start = cursor
                end = duration if index == len(segments) else cursor + duration * weight / total
            item = dict(segment)
            item.update({"start": round(start, 3), "end": round(end, 3)})
            output.append(item)
            lines.extend([str(index), f"{self._srt_time(start)} --> {self._srt_time(end)}", segment["caption"], ""])
            cursor = end
        if abs(cursor - duration) > 0.05:
            raise RuntimeError(
                f"字幕结束时间{cursor:.3f}秒与音轨{duration:.3f}秒不一致"
            )
        (folder / "captions.srt").write_text("\n".join(lines), encoding="utf-8")
        return output

    def _render_video(
        self,
        folder: Path,
        segments: list[dict[str, str]],
        captions: list[dict[str, Any]],
        duration: float,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not _tool_available(FFMPEG) or not _tool_available(FFPROBE):
            raise FileNotFoundError("未找到FFmpeg或FFprobe；请加入PATH或设置FFMPEG_PATH/FFPROBE_PATH")
        cards_dir = folder / "cards"
        cards_dir.mkdir(exist_ok=True)
        for index, segment in enumerate(segments, start=1):
            self._draw_card(
                cards_dir / f"scene_{index:02d}.png", segment, index, len(segments), config or {}
            )

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
            "-i", str(folder / ("program_audio.wav" if (folder / "program_audio.wav").is_file() else "voice.wav")), "-vf", "fps=30,format=yuv420p",
            *h264_mf_video_args(crf_equivalent=20),
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
            "codec_strategy": H264_CODEC_STRATEGY,
        }

    def _render_animated_video(self, folder: Path, motion_plan: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        project_dir = folder / "animation_project"
        capability_pack = config.get("capability_pack") if isinstance(config.get("capability_pack"), dict) else None
        build_report = build_motion_project(
            project_dir,
            motion_plan,
            folder / ("program_audio.wav" if (folder / "program_audio.wav").is_file() else "voice.wav"),
            capability_pack=capability_pack,
        )
        quality = str(config.get("animation_quality", "standard"))
        check_command, render_command, runtime = _hyperframes_commands(quality)
        env = _hyperframes_subprocess_environment()
        if runtime["browser"] is not None:
            env["HYPERFRAMES_BROWSER_PATH"] = str(runtime["browser"])
        if FFMPEG.exists():
            env["PATH"] = str(FFMPEG.parent) + os.pathsep + env.get("PATH", "")

        check = subprocess.run(
            check_command, cwd=project_dir, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        (folder / "animation_check.log").write_text((check.stdout or "") + "\n" + (check.stderr or ""), encoding="utf-8")
        if check.returncode:
            raise RuntimeError(f"动画工程检查失败: {(check.stderr or check.stdout or '')[-1200:]}")
        output = project_dir / "renders" / "final.mp4"
        render = subprocess.run(
            render_command,
            cwd=project_dir, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800,
        )
        (folder / "animation_render.log").write_text((render.stdout or "") + "\n" + (render.stderr or ""), encoding="utf-8")
        if render.returncode:
            raise RuntimeError(f"动画渲染失败: {(render.stderr or render.stdout or '')[-1200:]}")
        if not output.exists():
            raise RuntimeError("HyperFrames命令完成但没有生成final.mp4")
        shutil.copy2(output, folder / "final.mp4")
        media_report = self._probe_motion_video(folder / "final.mp4")
        return {
            **media_report,
            "mode": "animated_hyperframes",
            "project_dir": "animation_project",
            "runtime_source": runtime["runtime_source"],
            "runtime_version": HYPERFRAMES_VERSION,
            "renderer": HYPERFRAMES_RENDERER,
            "codec_strategy": runtime["codec_strategy"],
            "patch_id": runtime["patch_id"],
            "patch_version": runtime["patch_version"],
            "patched_cli_sha256": runtime["patched_cli_sha256"],
            "browser_strategy": runtime["browser_strategy"],
            "browser_version": runtime["browser_version"],
            "browser_minimum_major": runtime["browser_minimum_major"],
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

    def _draw_card(
        self,
        path: Path,
        segment: dict[str, str],
        index: int,
        count: int,
        config: dict[str, Any] | None = None,
    ) -> None:
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

        capability_pack = (config or {}).get("capability_pack")
        snapshot = _pack_snapshot(capability_pack if isinstance(capability_pack, dict) else None)
        visual_direction = snapshot.get("visual_direction") if isinstance(snapshot.get("visual_direction"), dict) else {}
        legacy = _is_legacy_pack(capability_pack if isinstance(capability_pack, dict) else None)
        brand = str(visual_direction.get("brand_name") or snapshot.get("label") or (
            "净界AI内容工厂" if legacy else "Evidence Motion"
        ))[:24]
        footer = "先看条件，再看数字" if legacy else "问题 · 证据 · 边界 · 行动"
        draw.text((78, 1770), brand, font=small_font, fill=mint)
        draw.text((620, 1770), footer, font=small_font, fill=cream)
        progress = 860 * index / count
        draw.rounded_rectangle((80, 1830, 1000, 1844), radius=7, fill="#31584B")
        draw.rounded_rectangle((80, 1830, 80 + progress, 1844), radius=7, fill=lime)
        image.save(path, quality=95)
