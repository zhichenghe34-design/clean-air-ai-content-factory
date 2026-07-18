from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


MARKERS = {
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "setup.py",
    "SKILL.md",
    "docker-compose.yml",
    "Cargo.toml",
}

CAPABILITY_RULES = {
    "content_insight": ("whisper", "asr", "ocr", "transcrib", "解析", "字幕提取"),
    "script_generation": ("script", "prompt", "llm", "文案", "脚本"),
    "compliance_review": ("audit", "review", "guard", "合规", "审核", "证据"),
    "video_generation": ("wan", "comfyui", "text2video", "img2video", "video-gen", "视频生成", "图生视频"),
    "voice_generation": ("tts", "sovits", "voice", "vocal", "语音", "配音"),
    "animation": ("hyperframes", "remotion", "manim", "motion", "动画", "mg"),
    "video_editing": ("ffmpeg", "capcut", "openreel", "video-use", "autopilot", "edit", "剪辑", "合成"),
}

ENTRYPOINT_NAMES = (
    "app.py",
    "main.py",
    "server.py",
    "run.py",
    "start.bat",
    "run.bat",
    "启动.bat",
    "一站式音视频解析.bat",
    "voice_workbench.py",
)


@dataclass
class DiscoveredTool:
    id: str
    name: str
    path: str
    capabilities: list[str]
    confidence: float
    reasons: list[str]
    entrypoints: list[str]
    markers: list[str]
    enabled: bool = False


class ProjectDiscovery:
    def __init__(self, max_depth: int = 3, max_directories: int = 1500):
        self.max_depth = max(0, min(int(max_depth), 8))
        self.max_directories = max(20, min(int(max_directories), 10000))

    def scan(self, roots: Iterable[str]) -> dict:
        tools: list[DiscoveredTool] = []
        errors: list[dict[str, str]] = []
        visited = 0
        seen: set[str] = set()

        for raw_root in roots:
            root = Path(raw_root).expanduser()
            if not root.exists() or not root.is_dir():
                errors.append({"path": str(root), "error": "目录不存在或不可访问"})
                continue
            try:
                root = root.resolve()
            except OSError:
                pass

            for current, dirnames, filenames in os.walk(root):
                visited += 1
                if visited > self.max_directories:
                    errors.append({"path": str(root), "error": "达到扫描目录上限，结果已截断"})
                    break
                current_path = Path(current)
                try:
                    relative_depth = len(current_path.relative_to(root).parts)
                except ValueError:
                    relative_depth = 0
                dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "runtime"}]
                if relative_depth >= self.max_depth:
                    dirnames[:] = []

                file_set = set(filenames)
                is_root = current_path == root
                is_project = is_root or bool(file_set & MARKERS) or ".codex-plugin" in dirnames
                if not is_project:
                    continue
                tool = self._classify(current_path, file_set)
                if tool and tool.id not in seen:
                    seen.add(tool.id)
                    tools.append(tool)

        tools.sort(key=lambda item: (-item.confidence, item.name.lower()))
        return {
            "tools": [asdict(tool) for tool in tools],
            "errors": errors,
            "visited_directories": min(visited, self.max_directories),
            "count": len(tools),
        }

    def _classify(self, path: Path, filenames: set[str]) -> DiscoveredTool | None:
        haystack = (str(path) + " " + " ".join(sorted(filenames))).lower()
        capabilities: list[str] = []
        reasons: list[str] = []
        for capability, keywords in CAPABILITY_RULES.items():
            hits = [word for word in keywords if word.lower() in haystack]
            if hits:
                capabilities.append(capability)
                reasons.append(f"{capability}: {', '.join(hits[:3])}")

        markers = sorted(filenames & MARKERS)
        entrypoints = [name for name in ENTRYPOINT_NAMES if name in filenames]
        if "package.json" in filenames:
            entrypoints.extend(self._package_scripts(path / "package.json"))

        if not capabilities and not entrypoints and not markers:
            return None
        if not capabilities:
            capabilities = ["generic_project"]
            reasons.append("发现项目标记，但能力类型需要人工确认")

        score = min(0.98, 0.35 + 0.12 * len(capabilities) + 0.08 * len(entrypoints) + 0.04 * len(markers))
        stable_id = str(path).replace("\\", "/").lower()
        return DiscoveredTool(
            id=f"tool-{abs(hash(stable_id)):x}",
            name=path.name or str(path),
            path=str(path),
            capabilities=capabilities,
            confidence=round(score, 2),
            reasons=reasons,
            entrypoints=entrypoints[:10],
            markers=markers,
        )

    @staticmethod
    def _package_scripts(package_path: Path) -> list[str]:
        try:
            data = json.loads(package_path.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            return [f"npm:{name}" for name in scripts.keys() if name in {"start", "dev", "serve", "build"}]
        except (OSError, json.JSONDecodeError, AttributeError):
            return []

