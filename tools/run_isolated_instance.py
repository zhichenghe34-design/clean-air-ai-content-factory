from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
FORMAL_RUNTIME = (REPO_ROOT / "runtime").resolve()
APP_PATH = REPO_ROOT / "app.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class IsolationError(ValueError):
    pass


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _resolve_safe_directory(value: str | Path, *, label: str) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise IsolationError(f"{label}必须是绝对路径")
    resolved = raw.resolve(strict=False)
    anchor = Path(resolved.anchor).resolve(strict=False)
    if resolved == anchor:
        raise IsolationError(f"{label}不能是磁盘根目录")
    if resolved == REPO_ROOT or _is_relative_to(resolved, REPO_ROOT):
        raise IsolationError(f"{label}必须位于仓库外")
    if resolved == FORMAL_RUNTIME or _is_relative_to(FORMAL_RUNTIME, resolved):
        raise IsolationError(f"{label}不能是正式runtime或其父目录")
    if _is_relative_to(REPO_ROOT, resolved):
        raise IsolationError(f"{label}不能包含仓库目录")
    if resolved.exists() and not resolved.is_dir():
        raise IsolationError(f"{label}指向文件而不是目录")
    return resolved


def validate_isolated_paths(runtime_dir: str | Path, storage_root: str | Path) -> tuple[Path, Path]:
    runtime = _resolve_safe_directory(runtime_dir, label="runtime-dir")
    storage = _resolve_safe_directory(storage_root, label="storage-root")
    if runtime == storage or _is_relative_to(runtime, storage) or _is_relative_to(storage, runtime):
        raise IsolationError("runtime-dir与storage-root必须是互不包含的独立目录")
    return runtime, storage


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def prepare_isolated_config(runtime_dir: Path, storage_root: Path, model: str) -> Path:
    from core.config import DEFAULT_CONFIG

    if not model.strip() or len(model.strip()) > 80:
        raise IsolationError("model必须是1到80位的非空名称")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    storage_root.mkdir(parents=True, exist_ok=True)
    config_path = runtime_dir / "config.json"
    if config_path.exists():
        try:
            current = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IsolationError("隔离config.json损坏，拒绝覆盖") from exc
        if not isinstance(current, dict):
            raise IsolationError("隔离config.json必须是对象")
        config = copy.deepcopy(DEFAULT_CONFIG)
        for key in ("provider", "research", "discovery", "security", "storage"):
            value = current.get(key)
            if isinstance(value, dict):
                config[key].update(value)
    else:
        config = copy.deepcopy(DEFAULT_CONFIG)
    config["provider"]["model"] = model.strip()
    config["provider"]["api_key_env"] = "DEEPSEEK_API_KEY"
    config["research"]["max_provider_calls_per_job"] = 7
    config["storage"]["root"] = str(storage_root)
    _atomic_json(config_path, config)
    return config_path


def build_child_environment(runtime_dir: Path) -> dict[str, str]:
    child = dict(os.environ)
    for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        child.pop(name, None)
    child["SHIYI_RUNTIME_DIR"] = str(runtime_dir)
    child["PYTHONUTF8"] = "1"
    child["PYTHONDONTWRITEBYTECODE"] = "1"
    return child


def main() -> int:
    parser = argparse.ArgumentParser(description="启动完全隔离的时宜 Agent v0.3 验证实例")
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--storage-root", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit("port必须在1024到65535之间")
    try:
        runtime_dir, storage_root = validate_isolated_paths(args.runtime_dir, args.storage_root)
        prepare_isolated_config(runtime_dir, storage_root, args.model)
    except IsolationError as exc:
        raise SystemExit(str(exc)) from exc
    command = [sys.executable, "-B", str(APP_PATH), "--host", "127.0.0.1", "--port", str(args.port)]
    print(json.dumps({
        "status": "starting_isolated_instance",
        "host": "127.0.0.1",
        "port": args.port,
        "runtime_dir": str(runtime_dir),
        "storage_root": str(storage_root),
        "model": args.model,
    }, ensure_ascii=False), flush=True)
    completed = subprocess.run(command, cwd=REPO_ROOT, env=build_child_environment(runtime_dir), check=False)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
