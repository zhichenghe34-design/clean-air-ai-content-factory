from __future__ import annotations

import copy
import json
import os
import uuid
from pathlib import Path
from typing import Any

from core.provider import validate_provider_base_url
from core.secrets import SecretStorageError, protect_secret, unprotect_secret


DEFAULT_STORAGE_ROOT = str(Path.home() / "ShiyiAIGC")


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
    "research": {
        "enabled": True,
        "search_provider": "ddgs",
        "max_search_calls": 3,
        "max_results_per_search": 5,
        "max_pages": 5,
        "max_model_turns": 2,
        "max_provider_calls_per_job": 7,
        "max_chars_per_page": 6000,
        "media_parser_root": "",
    },
    "discovery": {
        "roots": [],
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
        "root": DEFAULT_STORAGE_ROOT,
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
        self.secret_warning = ""
        self._migrate_legacy_secret()

    def load(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        return _deep_merge(DEFAULT_CONFIG, data)

    def save(self, incoming: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(incoming, dict):
            raise ValueError("配置必须是JSON对象")
        current = self.load()
        safe = copy.deepcopy({k: v for k, v in incoming.items() if k in {"provider", "research", "discovery", "security", "storage"}})
        provider = safe.get("provider", {})
        if not isinstance(provider, dict):
            raise ValueError("provider配置必须是JSON对象")
        api_key = str(provider.pop("api_key", "") or "").strip()
        persist = bool(provider.pop("persist_api_key", False))
        clear_api_key = bool(provider.pop("clear_api_key", False))
        provider["name"] = "DeepSeek"
        provider["kind"] = "openai_compatible"
        provider["api_key_env"] = "DEEPSEEK_API_KEY"
        provider["base_url"] = validate_provider_base_url(provider.get("base_url", current["provider"]["base_url"]))

        merged = _deep_merge(current, safe)
        merged["provider"]["name"] = "DeepSeek"
        merged["provider"]["kind"] = "openai_compatible"
        merged["provider"]["api_key_env"] = "DEEPSEEK_API_KEY"
        merged["provider"]["base_url"] = validate_provider_base_url(merged["provider"]["base_url"])
        merged["research"]["max_provider_calls_per_job"] = min(
            7, max(0, int(merged.get("research", {}).get("max_provider_calls_per_job", 7)))
        )
        roots = merged.get("discovery", {}).get("roots", [])
        merged["discovery"]["roots"] = [str(Path(p).expanduser()) for p in roots if str(p).strip()]
        merged["storage"]["root"] = str(self._validated_storage_root(merged["storage"].get("root", "")))
        self.ensure_storage_layout(merged)
        self._atomic_json(self.config_path, merged)

        if api_key:
            self._session_api_key = api_key
            if persist:
                if os.name != "nt":
                    self.secret_warning = "当前系统不支持DPAPI，Key仅保留在本次进程会话中"
                else:
                    encrypted = protect_secret(api_key)
                    if unprotect_secret(encrypted) != api_key:
                        raise SecretStorageError("DPAPI回读校验失败")
                    self._atomic_json(self.secrets_path, encrypted)
                    self.secret_warning = ""
        elif clear_api_key:
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
        env_value = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if env_value:
            return env_value
        if self.secrets_path.exists():
            try:
                payload = json.loads(self.secrets_path.read_text(encoding="utf-8"))
                return unprotect_secret(payload).strip()
            except (OSError, json.JSONDecodeError, SecretStorageError):
                return ""
        return ""

    def public_config(self) -> dict[str, Any]:
        config = self.load()
        persisted = self._has_valid_persisted_key()
        config["provider"]["has_api_key"] = bool(self.get_api_key())
        config["provider"]["api_key"] = ""
        config["provider"]["persisted_api_key"] = persisted
        config["provider"]["secret_storage"] = "dpapi-current-user" if persisted else "session_or_environment"
        config["provider"]["secret_warning"] = self.secret_warning
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
        temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def _migrate_legacy_secret(self) -> None:
        if not self.secrets_path.exists():
            return
        try:
            payload = json.loads(self.secrets_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.secret_warning = "持久化密钥文件损坏，已停止使用"
            return
        plaintext = payload.get("api_key") if isinstance(payload, dict) else None
        if not plaintext:
            return
        try:
            encrypted = protect_secret(str(plaintext))
            if unprotect_secret(encrypted) != str(plaintext):
                raise SecretStorageError("DPAPI回读校验失败")
            self._atomic_json(self.secrets_path, encrypted)
        except (OSError, SecretStorageError) as exc:
            self.secret_warning = f"旧明文密钥迁移失败，已停止使用：{type(exc).__name__}"

    def _has_valid_persisted_key(self) -> bool:
        if not self.secrets_path.exists():
            return False
        try:
            payload = json.loads(self.secrets_path.read_text(encoding="utf-8"))
            return bool(unprotect_secret(payload).strip())
        except (OSError, json.JSONDecodeError, SecretStorageError):
            return False
