from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "provider": {
        "name": "DeepSeek",
        "kind": "openai_compatible",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "api_key_env": "DEEPSEEK_API_KEY",
        "thinking": "disabled",
        "reasoning_effort": "high",
        "timeout_seconds": 90,
    },
    "discovery": {
        "roots": [
            "D:\\wen zi you xi\\一站式音视频解析",
            "F:\\AI",
        ],
        "max_depth": 3,
        "max_directories": 1500,
    },
    "security": {
        "dry_run_default": True,
        "require_approval": True,
        "allow_external_commands": False,
        "allow_media_upload": False,
    },
    "storage": {
        "root": "D:\\时宜AIGC内容工厂",
        "subdirectories": {
            "tools": "tools",
            "models": "models",
            "downloads": "downloads",
            "cache": "cache",
            "temp": "temp",
            "logs": "logs",
            "projects": "projects",
        },
    },
}


def _deep_merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class ConfigStore:
    def __init__(self, runtime_dir: Path):
        self.runtime_dir = Path(runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.runtime_dir / "config.json"
        self.secrets_path = self.runtime_dir / "secrets.json"
        self._session_api_key = ""

    def load(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        return _deep_merge(DEFAULT_CONFIG, data)

    def save(self, incoming: dict[str, Any]) -> dict[str, Any]:
        current = self.load()
        safe = {k: v for k, v in incoming.items() if k in {"provider", "discovery", "security", "storage"}}
        provider = safe.get("provider", {})
        api_key = str(provider.pop("api_key", "") or "").strip()
        persist = bool(provider.pop("persist_api_key", False))

        merged = _deep_merge(current, safe)
        roots = merged.get("discovery", {}).get("roots", [])
        merged["discovery"]["roots"] = [str(Path(p).expanduser()) for p in roots if str(p).strip()]
        merged["storage"]["root"] = str(self._validated_storage_root(merged["storage"].get("root", "")))
        self.ensure_storage_layout(merged)
        self._atomic_json(self.config_path, merged)

        if api_key:
            self._session_api_key = api_key
            if persist:
                self._atomic_json(self.secrets_path, {"api_key": api_key})
        elif provider.get("clear_api_key"):
            self._session_api_key = ""
            if self.secrets_path.exists():
                self.secrets_path.unlink()
        return self.public_config()

    def ensure_storage_layout(self, config: dict[str, Any] | None = None) -> dict[str, str]:
        config = config or self.load()
        storage = config.get("storage", {})
        root = self._validated_storage_root(storage.get("root", ""))
        root.mkdir(parents=True, exist_ok=True)
        directories = {"root": str(root)}
        for key, relative in storage.get("subdirectories", {}).items():
            name = str(relative).strip()
            if not name or Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError(f"资源库子目录无效: {key}")
            target = root / name
            target.mkdir(parents=True, exist_ok=True)
            directories[str(key)] = str(target)
        return directories

    @staticmethod
    def _validated_storage_root(value: Any) -> Path:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("安装根目录不能为空")
        root = Path(raw).expanduser()
        if not root.is_absolute():
            raise ValueError("安装根目录必须是绝对路径")
        resolved = root.resolve(strict=False)
        if str(resolved).rstrip("\\/") == str(Path(resolved.anchor)).rstrip("\\/"):
            raise ValueError("不能直接使用磁盘根目录，请选择其下的专用文件夹")
        if resolved.exists() and not resolved.is_dir():
            raise ValueError("安装根目录指向了一个文件")
        return resolved

    def get_api_key(self) -> str:
        if self._session_api_key:
            return self._session_api_key
        config = self.load()
        env_name = config["provider"].get("api_key_env", "DEEPSEEK_API_KEY")
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            return env_value
        if self.secrets_path.exists():
            try:
                return str(json.loads(self.secrets_path.read_text(encoding="utf-8")).get("api_key", ""))
            except (OSError, json.JSONDecodeError):
                return ""
        return ""

    def public_config(self) -> dict[str, Any]:
        config = self.load()
        config["provider"]["has_api_key"] = bool(self.get_api_key())
        config["provider"]["api_key"] = ""
        config["provider"]["persisted_api_key"] = self.secrets_path.exists()
        config["storage"]["directories"] = self.storage_layout(config)
        return config

    def storage_layout(self, config: dict[str, Any] | None = None) -> dict[str, str]:
        config = config or self.load()
        root = self._validated_storage_root(config.get("storage", {}).get("root", ""))
        directories = {"root": str(root)}
        for key, relative in config.get("storage", {}).get("subdirectories", {}).items():
            directories[str(key)] = str(root / str(relative))
        return directories

    @staticmethod
    def _atomic_json(path: Path, data: dict[str, Any]) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
