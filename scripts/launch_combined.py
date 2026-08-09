from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
import tomllib
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.request import ProxyHandler, Request, build_opener


LOOPBACK_HOST = "127.0.0.1"
EXPECTED_MPT_VERSION = "1.3.3"
EXPECTED_MPT_COMMIT = "254cd028906ee657eab844dc94087cdbea2a7aa8"
HYPERFRAMES_VERSION = "0.7.86"
NODE_MINIMUM_MAJOR = 22
MPT_API_PREFIX = "/api/v1"
MPT_OPENAPI_REQUIRED_PATH = "/api/v1/videos"
MPT_HEALTH_STATE_NAME = "mpt-health.json"
MOTION_OPTIONAL_MPT_HEALTH_TIMEOUT_SECONDS = 8.0
LAUNCHER_STATE_NAME = "launcher-state.json"
LEGACY_RUNTIME_FILES = frozenset({"config.json", "secrets.json", "rules.json", "corrections.jsonl"})
LEGACY_RUNTIME_DIRECTORIES = frozenset({"jobs", "capability-packs", "skills"})
MIGRATABLE_STATE_SUFFIXES = frozenset(
    {
        ".aac", ".ass", ".db", ".flac", ".gif", ".html", ".jpeg", ".jpg",
        ".json", ".jsonl", ".lock", ".log", ".m4a", ".md", ".mp3", ".mp4",
        ".ogg", ".png", ".srt", ".sqlite", ".sqlite3", ".txt", ".vtt", ".wav",
        ".webm", ".webp",
    }
)
ENGINE_ENV_ALLOWLIST = (
    "APPDATA",
    "COMSPEC",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
)
APP_RELEASE_FORBIDDEN_ENV = (
    "SHIYI_ALLOW_TEST_PROVIDER",
    "SHIYI_EXPERIMENTAL_DYNAMIC_TOPICS",
    "SHIYI_AGENT_TEST_REVIEW",
    "SHIYI_APP_EXECUTABLE",
    "SHIYI_APP_PYTHON",
    "SHIYI_MPT_ROOT",
    "SHIYI_MPT_PYTHON",
    "SHIYI_FFMPEG_PATH",
    "SHIYI_FFPROBE_PATH",
    "SHIYI_NOTO_REGULAR",
    "SHIYI_NOTO_BOLD",
    "SHIYI_MPT_LOCAL_MATERIAL_DIR",
    "SHIYI_MPT_HEALTH_FILE",
    "SHIYI_NODE_EXECUTABLE",
    "SHIYI_HYPERFRAMES_CLI",
    "SHIYI_MOTION_HEALTH_VERIFIED",
    "SHIYI_LAUNCH_INSTANCE_TOKEN",
    "HYPERFRAMES_BROWSER_PATH",
    "HYPERFRAMES_FFMPEG_PATH",
    "HYPERFRAMES_FFPROBE_PATH",
    "HYPERFRAMES_NO_UPDATE_CHECK",
    "HYPERFRAMES_NO_AUTO_INSTALL",
    "HYPERFRAMES_NO_TELEMETRY",
    "HYPERFRAMES_SKIP_SKILLS",
    "CHROME_BIN",
    "CHROME_PATH",
    "PUPPETEER_EXECUTABLE_PATH",
    "PLAYWRIGHT_BROWSERS_PATH",
    "KEEP_TEMP",
    "AUTHORIZATION",
    "COOKIE",
)
APP_RELEASE_FORBIDDEN_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "BROWSER_",
    "CHROME_",
    "DEEPSEEK_",
    "GEMINI_",
    "GOOGLE_",
    "HF_",
    "HUGGINGFACE_",
    "HYPERFRAME_",
    "HYPERFRAMES_",
    "MODEL_",
    "OPENAI_",
    "OPENROUTER_",
    "PLAYWRIGHT_",
    "PRODUCER_",
    "PUPPETEER_",
    "VERTEX_",
)
APP_PROVIDER_ENV_ALLOWLIST = ("DEEPSEEK_API_KEY",)
NODE_HOST_ENV_FORBIDDEN_PREFIXES = ("NODE_", "NPM_", "COREPACK_", "PNPM_", "YARN_")
NODE_HOST_ENV_FORBIDDEN_NAMES = frozenset(
    {"ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"}
)


class LauncherError(RuntimeError):
    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": "error",
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True)
class LauncherConfig:
    project_root: Path
    mpt_root: Path
    mpt_python: Path
    ffmpeg: Path
    ffprobe: Path
    font_regular: Path
    font_bold: Path
    material_root: Path
    app_port: int = 8765
    mpt_port: int = 0
    health_timeout_seconds: float = 60.0
    open_browser: bool = True
    preflight_only: bool = False
    app_executable: Path | None = None
    app_python: Path | None = None
    app_script: Path | None = None
    agent_test_review: bool = False
    motion_runtime_required: bool = False
    node_executable: Path | None = None
    hyperframes_cli: Path | None = None
    hyperframes_browser: Path | None = None
    node_version: str | None = None
    hyperframes_version: str | None = None
    browser_version: str | None = None


def _looks_like_packaged_root(project_root: Path) -> bool:
    return bool(
        (project_root / "PACKAGE-MANIFEST.json").is_file()
        or (
            (project_root / "runtime" / "python" / "python.exe").is_file()
            and (project_root / "tools" / "verify_combined_portable.py").is_file()
            and (project_root / "scripts" / "launch_combined.py").is_file()
        )
    )


def _launcher_runtime_dir(project_root: Path) -> Path:
    if _looks_like_packaged_root(project_root):
        return _local_product_root() / "Launcher"
    return project_root / "runtime" / "combined-launcher"


def _launcher_state_path(project_root: Path) -> Path:
    return _launcher_runtime_dir(project_root) / LAUNCHER_STATE_NAME


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _create_json_exclusive(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _local_product_root(base: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if base is None else base
    local_app_data = str(environment.get("LOCALAPPDATA") or "").strip()
    root = Path(local_app_data).expanduser() if local_app_data else Path()
    if not local_app_data or not root.is_absolute():
        raise LauncherError("LOCAL_APP_DATA_MISSING", "无法定位当前 Windows 用户的数据目录。")
    return (root / "ShiyiContentFactory").resolve()


def _is_reparse_path(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return True
    junction = getattr(path, "is_junction", None)
    return bool(
        path.is_symlink()
        or (callable(junction) and junction())
        or getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _collect_migratable_state(runtime_root: Path) -> list[tuple[str, Path]]:
    root = runtime_root.resolve(strict=True)
    if not root.is_dir() or _is_reparse_path(runtime_root):
        raise LauncherError("MIGRATION_SOURCE_UNSAFE", "旧版 runtime 不是可安全读取的普通目录。")
    collected: list[tuple[str, Path]] = []
    stack: list[Path] = []
    for name in sorted(LEGACY_RUNTIME_FILES | LEGACY_RUNTIME_DIRECTORIES, key=str.casefold):
        candidate = root / name
        if not candidate.exists():
            continue
        if _is_reparse_path(candidate):
            raise LauncherError("MIGRATION_SOURCE_UNSAFE", "旧版数据含链接或重解析点，已拒绝迁移。")
        if name in LEGACY_RUNTIME_FILES:
            if not candidate.is_file():
                raise LauncherError("MIGRATION_SOURCE_INVALID", f"旧版 {name} 不是普通文件。")
            stack.append(candidate)
        else:
            if not candidate.is_dir():
                raise LauncherError("MIGRATION_SOURCE_INVALID", f"旧版 {name} 不是普通目录。")
            stack.append(candidate)
    while stack:
        current = stack.pop()
        try:
            resolved = current.resolve(strict=True)
            relative = resolved.relative_to(root).as_posix()
        except (OSError, ValueError) as exc:
            raise LauncherError("MIGRATION_SOURCE_UNSAFE", "旧版数据越出了指定 runtime 边界。") from exc
        if _is_reparse_path(current):
            raise LauncherError("MIGRATION_SOURCE_UNSAFE", "旧版数据含链接或重解析点，已拒绝迁移。")
        if current.is_dir():
            try:
                children = list(current.iterdir())
            except OSError as exc:
                raise LauncherError("MIGRATION_SOURCE_INVALID", "旧版数据目录无法完整读取。") from exc
            stack.extend(children)
            continue
        if not current.is_file() or current.suffix.casefold() not in MIGRATABLE_STATE_SUFFIXES:
            raise LauncherError(
                "MIGRATION_FILE_TYPE_REJECTED",
                "旧版数据含未声明类型或可执行文件，已拒绝迁移。",
                relative_path=relative,
            )
        collected.append((relative, current))
    return sorted(collected, key=lambda item: item[0].casefold())


def _migratable_state_manifest(runtime_root: Path) -> list[tuple[str, int, str]]:
    manifest: list[tuple[str, int, str]] = []
    for relative, path in _collect_migratable_state(runtime_root):
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise LauncherError("MIGRATION_SOURCE_INVALID", "旧版数据文件无法完整读取。") from exc
        manifest.append((relative, path.stat().st_size, digest.hexdigest().upper()))
    return manifest


def import_legacy_runtime(
    source_runtime: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    if not source_runtime.expanduser().is_absolute():
        raise LauncherError("MIGRATION_SOURCE_INVALID", "旧版 runtime 必须使用完整绝对路径。")
    try:
        source = source_runtime.expanduser().resolve(strict=True)
    except OSError as exc:
        raise LauncherError("MIGRATION_SOURCE_INVALID", "指定的旧版 runtime 不存在。") from exc
    destination = (_local_product_root(environment) / "UserData").resolve()
    if source == destination:
        return {"status": "already_current", "message": "该目录已经是当前用户数据目录。", "file_count": 0}
    if source in destination.parents or destination in source.parents:
        raise LauncherError("MIGRATION_PATH_OVERLAP", "旧版数据与当前用户数据目录不得互相嵌套。")
    source_manifest = _migratable_state_manifest(source)
    if not source_manifest:
        raise LauncherError("MIGRATION_SOURCE_EMPTY", "旧版 runtime 中没有可迁移的任务、配置或 Key。")
    if destination.exists():
        if _is_reparse_path(destination) or not destination.is_dir():
            raise LauncherError("MIGRATION_DESTINATION_UNSAFE", "当前用户数据目录不是安全的普通目录。")
        destination_manifest = _migratable_state_manifest(destination)
        if destination_manifest == source_manifest:
            return {
                "status": "already_migrated",
                "message": "旧版任务、配置和 Key 已经迁移，无需重复操作。",
                "file_count": len(source_manifest),
            }
        if any(destination.iterdir()):
            raise LauncherError(
                "MIGRATION_DESTINATION_CONFLICT",
                "当前版本已经产生数据且与旧版不同；为避免覆盖，已拒绝自动合并。",
            )
        destination.rmdir()
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".UserData-migration-{os.getpid()}-{time.time_ns()}"
    try:
        staging.mkdir(parents=False, exist_ok=False)
        for relative, source_path in _collect_migratable_state(source):
            target = staging / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, target)
        if _migratable_state_manifest(staging) != source_manifest:
            raise LauncherError("MIGRATION_COPY_MISMATCH", "旧版数据复制后的哈希不一致，未切换用户数据。")
        os.replace(staging, destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "status": "migrated",
        "message": "旧版任务、配置和本机加密 Key 已复制到稳定用户数据目录；原目录保持不变。",
        "file_count": len(source_manifest),
        "secrets_migrated": any(relative == "secrets.json" for relative, _, _ in source_manifest),
    }


def _process_entry(
    process: subprocess.Popen[bytes] | None,
    executable: Path | None,
    started_at: str | None,
) -> dict[str, object] | None:
    if process is None or executable is None:
        return None
    if not started_at:
        raise LauncherError("PROCESS_IDENTITY_MISSING", "无法记录本包子进程的启动时刻。")
    return {
        "pid": int(process.pid),
        "executable": str(executable.resolve()),
        "started_at": started_at,
    }


def _write_launcher_state(
    config: LauncherConfig,
    *,
    started_at: str,
    app_port: int,
    mpt_port: int,
    app_process: subprocess.Popen[bytes],
    engine_process: subprocess.Popen[bytes] | None,
    app_started_at: str,
    engine_started_at: str | None,
) -> Path:
    app_executable = config.app_executable or config.app_python
    if app_executable is None:
        raise LauncherError("APP_EXECUTABLE_MISSING", "无法记录工作台进程身份。")
    path = _launcher_state_path(config.project_root)
    try:
        claimed_state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LauncherError("LAUNCHER_STATE_INVALID", "无法保留启动器的真实进程身份。") from exc
    claimed_launcher = claimed_state.get("launcher") if isinstance(claimed_state, dict) else None
    if (
        not isinstance(claimed_launcher, dict)
        or set(claimed_launcher) != {"pid", "executable", "started_at"}
        or claimed_launcher.get("pid") != os.getpid()
        or not isinstance(claimed_launcher.get("executable"), str)
        or not _same_windows_path(
            str(claimed_launcher.get("executable")), str(Path(sys.executable).resolve())
        )
        or not isinstance(claimed_launcher.get("started_at"), str)
    ):
        raise LauncherError("LAUNCHER_STATE_INVALID", "启动器的真实进程身份已变化，拒绝发布就绪状态。")
    state = {
        "schema_version": 2,
        "phase": "ready",
        "project_root": str(config.project_root.resolve()),
        "started_at": started_at,
        "app_port": int(app_port),
        "mpt_port": int(mpt_port),
        "launcher": dict(claimed_launcher),
        "app": _process_entry(app_process, app_executable, app_started_at),
        "mpt": _process_entry(engine_process, config.mpt_python, engine_started_at),
    }
    _atomic_json(path, state)
    return path


def _remove_own_launcher_state(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    launcher = payload.get("launcher") if isinstance(payload, dict) else None
    if isinstance(launcher, dict) and launcher.get("pid") == os.getpid():
        path.unlink(missing_ok=True)


def _windows_powershell(system_root: str | None = None) -> Path:
    root = Path(system_root or os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or "")
    candidate = root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not root.is_absolute() or not candidate.is_file():
        raise LauncherError("WINDOWS_POWERSHELL_MISSING", "未找到 Windows PowerShell，无法管理本包进程。")
    return candidate.resolve()


def _query_windows_process(pid: int, *, runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run) -> dict[str, object] | None:
    if os.name != "nt":
        return None
    powershell = _windows_powershell()
    command = (
        f"$p=Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue;"
        "if ($null -eq $p) { exit 4 };"
        "[ordered]@{pid=$p.Id;path=$p.Path;started_at=$p.StartTime.ToUniversalTime().ToString('o')}"
        "|ConvertTo-Json -Compress"
    )
    completed = runner(
        [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    if completed.returncode == 4:
        return None
    if completed.returncode != 0:
        raise LauncherError("PROCESS_IDENTITY_QUERY_FAILED", "无法核对待关闭进程的身份。")
    try:
        payload = json.loads(completed.stdout.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LauncherError("PROCESS_IDENTITY_QUERY_FAILED", "待关闭进程身份返回无效。") from exc
    if not isinstance(payload, dict):
        raise LauncherError("PROCESS_IDENTITY_QUERY_FAILED", "待关闭进程身份返回无效。")
    return payload


def _same_windows_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _claim_launcher_state(
    project_root: Path,
    *,
    started_at: str,
    query_process: Callable[[int], dict[str, object] | None] = _query_windows_process,
) -> Path:
    root = project_root.resolve()
    path = _launcher_state_path(root)
    launcher_started_at = started_at
    launcher_actual = query_process(os.getpid())
    if isinstance(launcher_actual, dict):
        actual_path = launcher_actual.get("path")
        actual_started = launcher_actual.get("started_at")
        if (
            launcher_actual.get("pid") == os.getpid()
            and isinstance(actual_path, str)
            and _same_windows_path(actual_path, str(Path(sys.executable).resolve()))
            and isinstance(actual_started, str)
        ):
            launcher_started_at = actual_started
    payload = {
        "schema_version": 2,
        "phase": "starting",
        "project_root": str(root),
        "started_at": started_at,
        "app_port": None,
        "mpt_port": None,
        "launcher": {
            "pid": os.getpid(),
            "executable": str(Path(sys.executable).resolve()),
            "started_at": launcher_started_at,
        },
        "app": None,
        "mpt": None,
    }
    for attempt in range(2):
        try:
            _create_json_exclusive(path, payload)
            return path
        except FileExistsError:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LauncherError("LAUNCHER_STATE_INVALID", "已有启动状态无法安全读取，请先使用关闭入口。") from exc
            if (
                not isinstance(existing, dict)
                or existing.get("schema_version") != 2
            ):
                raise LauncherError("LAUNCHER_STATE_INVALID", "已有启动状态结构无效。")
            active = False
            for role in ("launcher", "app", "mpt"):
                item = existing.get(role)
                if item is None:
                    continue
                if not isinstance(item, dict) or set(item) != {"pid", "executable", "started_at"}:
                    raise LauncherError("LAUNCHER_STATE_INVALID", "已有启动状态的进程身份无效。")
                pid = item.get("pid")
                executable = item.get("executable")
                recorded_started = item.get("started_at")
                if (
                    isinstance(pid, bool)
                    or not isinstance(pid, int)
                    or pid <= 0
                    or not isinstance(executable, str)
                    or not isinstance(recorded_started, str)
                ):
                    raise LauncherError("LAUNCHER_STATE_INVALID", "已有启动状态的进程身份无效。")
                actual = query_process(pid)
                if actual is None:
                    continue
                actual_path = actual.get("path") if isinstance(actual, dict) else None
                actual_started_value = actual.get("started_at") if isinstance(actual, dict) else None
                try:
                    recorded_time = dt.datetime.fromisoformat(recorded_started.replace("Z", "+00:00"))
                    actual_time = dt.datetime.fromisoformat(str(actual_started_value).replace("Z", "+00:00"))
                except ValueError:
                    raise LauncherError("LAUNCHER_STATE_INVALID", "已有启动状态的进程时刻无效。")
                if recorded_time.tzinfo is None:
                    recorded_time = recorded_time.replace(tzinfo=dt.timezone.utc)
                if actual_time.tzinfo is None:
                    actual_time = actual_time.replace(tzinfo=dt.timezone.utc)
                if (
                    isinstance(actual_path, str)
                    and _same_windows_path(actual_path, executable)
                    and abs((actual_time - recorded_time).total_seconds()) <= 5
                ):
                    if (
                        role == "launcher"
                        and pid == os.getpid()
                        and _same_windows_path(executable, str(Path(sys.executable).resolve()))
                        and _same_windows_path(str(existing.get("project_root", "")), str(root))
                    ):
                        return path
                    active = True
                    break
            if active:
                raise LauncherError("WORKBENCH_ALREADY_RUNNING", "当前便携包已经在运行，请使用已有浏览器页面或先执行关闭入口。")
            path.unlink(missing_ok=True)
            if attempt:
                break
    raise LauncherError("LAUNCHER_STATE_BUSY", "无法取得本包的单实例启动锁。")


def stop_recorded_processes(
    project_root: Path,
    *,
    query_process: Callable[[int], dict[str, object] | None] = _query_windows_process,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, object]:
    root = project_root.resolve()
    state_path = _launcher_state_path(root)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "stopped", "message": "没有发现正在运行的本包服务。", "stopped_pids": []}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LauncherError("LAUNCHER_STATE_INVALID", "停止状态文件无法安全读取。") from exc
    if os.name != "nt" and query_process is _query_windows_process:
        raise LauncherError("STOP_UNSUPPORTED_PLATFORM", "本便携包停止入口仅支持 Windows。")
    if not isinstance(state, dict) or state.get("schema_version") != 2:
        raise LauncherError("LAUNCHER_STATE_INVALID", "停止状态文件结构无效。")
    recorded_root = Path(str(state.get("project_root", ""))).expanduser()
    if not recorded_root.is_absolute():
        raise LauncherError("LAUNCHER_STATE_ROOT_MISMATCH", "停止状态缺少有效的原始包路径。")
    if not (root / "PACKAGE-MANIFEST.json").is_file() and not _same_windows_path(str(recorded_root), str(root)):
        raise LauncherError("LAUNCHER_STATE_ROOT_MISMATCH", "停止状态不属于当前便携包。")
    try:
        baseline = dt.datetime.fromisoformat(str(state["started_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise LauncherError("LAUNCHER_STATE_INVALID", "停止状态缺少有效启动时间。") from exc
    if baseline.tzinfo is None:
        baseline = baseline.replace(tzinfo=dt.timezone.utc)
    verified: list[int] = []
    for role in ("app", "mpt", "launcher"):
        item = state.get(role)
        if item is None:
            continue
        if not isinstance(item, dict) or set(item) != {"pid", "executable", "started_at"}:
            raise LauncherError("LAUNCHER_STATE_INVALID", f"停止状态中的 {role} 身份无效。")
        pid = item.get("pid")
        expected = item.get("executable")
        recorded_started_value = item.get("started_at")
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or not isinstance(expected, str)
            or not isinstance(recorded_started_value, str)
        ):
            raise LauncherError("LAUNCHER_STATE_INVALID", f"停止状态中的 {role} 身份无效。")
        actual = query_process(pid)
        if actual is None:
            continue
        try:
            actual_pid = int(actual["pid"])
            actual_path = str(actual["path"])
            actual_started = dt.datetime.fromisoformat(str(actual["started_at"]).replace("Z", "+00:00"))
            recorded_started = dt.datetime.fromisoformat(recorded_started_value.replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError) as exc:
            raise LauncherError("PROCESS_IDENTITY_MISMATCH", f"无法验证 {role} 进程身份。") from exc
        if actual_started.tzinfo is None:
            actual_started = actual_started.replace(tzinfo=dt.timezone.utc)
        if recorded_started.tzinfo is None:
            recorded_started = recorded_started.replace(tzinfo=dt.timezone.utc)
        if (
            actual_pid != pid
            or not _same_windows_path(actual_path, expected)
            or abs((actual_started - recorded_started).total_seconds()) > 5
        ):
            raise LauncherError("PROCESS_IDENTITY_MISMATCH", f"{role} PID 已被其他进程占用，拒绝终止。")
        verified.append(pid)
    stopped: list[int] = []
    for pid in verified:
        completed = runner(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        if completed.returncode not in {0, 128, 255}:
            raise LauncherError("PROCESS_STOP_FAILED", "本包进程树未能完整停止。", pid=pid)
        stopped.append(pid)
    state_path.unlink(missing_ok=True)
    return {"status": "stopped", "message": "本包工作台与生产引擎已停止。", "stopped_pids": stopped}


def _require_file(path: Path, code: str, label: str) -> None:
    if not path.is_file():
        raise LauncherError(code, f"未找到预置{label}。", path=str(path))


def _require_directory(path: Path, code: str, label: str) -> None:
    if not path.is_dir():
        raise LauncherError(code, f"未找到预置{label}目录。", path=str(path))


def _read_toml(path: Path, code: str) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LauncherError(code, "MPT 配置文件无法安全解析。", path=str(path)) from exc
    if not isinstance(value, dict):
        raise LauncherError(code, "MPT 配置文件结构无效。", path=str(path))
    return value


def verify_packaged_integrity(project_root: Path) -> None:
    """Recheck a packaged immutable payload even when the root BAT is bypassed."""
    manifest = project_root / "PACKAGE-MANIFEST.json"
    if not manifest.is_file():
        # A source checkout has no release manifest. Its Git/source checks are
        # handled by the build pipeline; packaged releases must always have one.
        return
    verifier = project_root / "tools" / "verify_combined_portable.py"
    _require_file(verifier, "PACKAGE_VERIFIER_MISSING", "组合包完整性验证器")
    try:
        spec = importlib.util.spec_from_file_location("_shiyi_packaged_verifier", verifier)
        if spec is None or spec.loader is None:
            raise ImportError("missing verifier loader")
        module = importlib.util.module_from_spec(spec)
        previous_dont_write_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = previous_dont_write_bytecode
        verify_folder = getattr(module, "verify_folder")
        errors = verify_folder(project_root, allow_runtime_state=True)
    except LauncherError:
        raise
    except Exception as exc:
        raise LauncherError(
            "PACKAGE_INTEGRITY_CHECK_FAILED",
            "组合包完整性检查无法安全完成，已拒绝启动。",
        ) from exc
    if errors:
        raise LauncherError(
            "PACKAGE_INTEGRITY_MISMATCH",
            "组合包源码、运行时或清单已发生变化，已拒绝启动。",
            issue_count=len(errors),
        )


def validate_mpt_network_config(mpt_root: Path, mpt_port: int) -> None:
    """Fail closed when the packaged MPT config advertises a non-loopback service."""
    config_path = mpt_root / "config.toml"
    _require_file(config_path, "MPT_SAFE_CONFIG_MISSING", "MPT 安全配置")
    config = _read_toml(config_path, "MPT_SAFE_CONFIG_INVALID")

    listen_host = config.get("listen_host", "")
    if listen_host not in (LOOPBACK_HOST, "localhost"):
        raise LauncherError(
            "MPT_CONFIG_HOST_NOT_LOOPBACK",
            "MPT 配置的监听地址不是本机回环地址，已拒绝启动。",
            field="listen_host",
        )

    app_section = config.get("app", {})
    if not isinstance(app_section, dict):
        raise LauncherError("MPT_SAFE_CONFIG_INVALID", "MPT app 配置结构无效。")
    endpoint = app_section.get("endpoint", "")
    allowed_endpoints = {
        "",
        f"http://{LOOPBACK_HOST}:{mpt_port}",
        f"http://localhost:{mpt_port}",
    }
    if not isinstance(endpoint, str) or endpoint.rstrip("/") not in allowed_endpoints:
        raise LauncherError(
            "MPT_CONFIG_ENDPOINT_NOT_LOOPBACK",
            "MPT 配置的产物地址不是当前本机引擎，已拒绝启动。",
            field="app.endpoint",
        )


def validate_preinstalled_layout(config: LauncherConfig) -> None:
    verify_packaged_integrity(config.project_root)
    _require_directory(config.project_root, "PROJECT_ROOT_MISSING", "项目")
    if config.motion_runtime_required:
        if config.node_executable is None or config.hyperframes_cli is None or config.hyperframes_browser is None:
            raise LauncherError("MOTION_RUNTIME_MISSING", "正式纯动画包未配置完整的离线动画运行时。")
        if not all(
            isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", value)
            for value in (config.node_version, config.hyperframes_version, config.browser_version)
        ):
            raise LauncherError("MOTION_VERSION_LOCK_MISSING", "正式纯动画包缺少固定运行时版本锁。")
        if config.hyperframes_version != HYPERFRAMES_VERSION:
            raise LauncherError(
                "HYPERFRAMES_VERSION_MISMATCH",
                f"正式纯动画包只允许 HyperFrames {HYPERFRAMES_VERSION}。",
            )
        if int(str(config.node_version).split(".", 1)[0]) < NODE_MINIMUM_MAJOR:
            raise LauncherError(
                "MOTION_NODE_VERSION_UNSUPPORTED",
                f"正式纯动画包要求 Node {NODE_MINIMUM_MAJOR} 或更高版本。",
            )
        _require_file(config.node_executable, "MOTION_NODE_MISSING", "Node 动画运行时")
        _require_file(config.hyperframes_cli, "HYPERFRAMES_CLI_MISSING", "HyperFrames CLI")
        _require_file(config.hyperframes_browser, "HYPERFRAMES_BROWSER_MISSING", "无头浏览器")
        _require_file(config.ffmpeg, "FFMPEG_MISSING", "FFmpeg")
        _require_file(config.ffprobe, "FFPROBE_MISSING", "FFprobe")
        _require_file(config.font_regular, "NOTO_FONT_MISSING", "Noto Sans SC Regular 字体")
        _require_file(config.font_bold, "NOTO_FONT_MISSING", "Noto Sans SC Bold 字体")
        _require_file(config.project_root / "core" / "sapi_tts.ps1", "SAPI_SCRIPT_MISSING", "中文离线配音脚本")
        _windows_powershell()
        if config.app_executable is not None:
            _require_file(config.app_executable, "APP_EXECUTABLE_MISSING", "工作台可执行文件")
        else:
            if config.app_python is None or config.app_script is None:
                raise LauncherError("APP_RUNTIME_MISSING", "未配置预置工作台 Python 环境。")
            _require_file(config.app_python, "APP_PYTHON_MISSING", "工作台 Python 环境")
            _require_file(config.app_script, "APP_SCRIPT_MISSING", "工作台入口")
        # Footage is a secondary route. Isolated/development motion launcher
        # configurations may omit MPT; the current generated combined package
        # still includes it and integrity verification remains fail-closed.
        if not (config.mpt_root / "app" / "asgi.py").is_file() or not config.mpt_python.is_file():
            return
    _require_directory(config.mpt_root, "MPT_ROOT_MISSING", "MPT 引擎")
    _require_file(config.mpt_root / "app" / "asgi.py", "MPT_SOURCE_INCOMPLETE", "MPT API 入口")
    pyproject = config.mpt_root / "pyproject.toml"
    _require_file(pyproject, "MPT_SOURCE_INCOMPLETE", "MPT 版本清单")
    metadata = _read_toml(pyproject, "MPT_VERSION_INVALID")
    project = metadata.get("project", {})
    version = project.get("version") if isinstance(project, dict) else None
    if version != EXPECTED_MPT_VERSION:
        raise LauncherError(
            "MPT_VERSION_MISMATCH",
            "MPT 版本与发布锁定值不一致。",
            expected=EXPECTED_MPT_VERSION,
            actual=str(version or "unknown"),
        )
    lock_path = config.project_root / "third_party" / "moneyprinterturbo" / "upstream-lock.json"
    _require_file(lock_path, "MPT_LOCK_MISSING", "MPT 版本锁")
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LauncherError("MPT_LOCK_INVALID", "MPT 版本锁无法安全解析。") from exc
    if not isinstance(lock, dict) or (
        lock.get("upstream_version") != EXPECTED_MPT_VERSION
        or lock.get("upstream_commit") != EXPECTED_MPT_COMMIT
        or lock.get("license") != "MIT"
    ):
        raise LauncherError("MPT_LOCK_MISMATCH", "MPT 版本、提交或许可证与发布锁不一致。")
    commit_marker = config.mpt_root / "UPSTREAM_COMMIT"
    _require_file(commit_marker, "MPT_COMMIT_MARKER_MISSING", "MPT 提交锁")
    try:
        engine_commit = commit_marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise LauncherError("MPT_COMMIT_MARKER_INVALID", "MPT 提交锁无法读取。") from exc
    if engine_commit != EXPECTED_MPT_COMMIT:
        raise LauncherError("MPT_COMMIT_MISMATCH", "MPT 引擎源码与发布锁定提交不一致。")
    _require_file(config.mpt_python, "MPT_PYTHON_MISSING", "MPT Python 环境")
    _require_file(config.ffmpeg, "FFMPEG_MISSING", "FFmpeg")
    _require_file(config.ffprobe, "FFPROBE_MISSING", "FFprobe")
    _require_file(config.font_regular, "NOTO_FONT_MISSING", "Noto Sans SC Regular 字体")
    _require_file(config.font_bold, "NOTO_FONT_MISSING", "Noto Sans SC Bold 字体")
    mpt_font = config.mpt_root / "resource" / "fonts" / "NotoSansSC-Regular.ttf"
    _require_file(mpt_font, "MPT_NOTO_FONT_MISSING", "MPT Noto Sans SC 字体")
    if hashlib.sha256(mpt_font.read_bytes()).digest() != hashlib.sha256(config.font_regular.read_bytes()).digest():
        raise LauncherError("MPT_NOTO_FONT_MISMATCH", "MPT 字体与发布锁定字体不一致。")
    # MPT's local-video service deliberately accepts files only from its own
    # storage/local_videos directory.  Requiring the same root here prevents a
    # launcher configuration that passes our preflight but is later rejected by
    # the upstream engine (and keeps arbitrary host paths out of the API).
    expected_material_root = (config.mpt_root / "storage" / "local_videos").resolve()
    if config.material_root.resolve() != expected_material_root:
        raise LauncherError(
            "MATERIAL_ROOT_NOT_MPT_ALLOWLIST",
            "本地素材目录必须是 MPT 的专用白名单目录。",
        )
    _require_directory(config.material_root, "MATERIAL_ROOT_MISSING", "已核验本地素材")
    local_materials = [path for path in config.material_root.glob("*.mp4") if path.is_file()]
    if not 1 <= len(local_materials) <= 24:
        raise LauncherError("MATERIAL_SET_INVALID", "本地素材必须包含 1 到 24 个 MP4 文件。")

    if config.app_executable is not None:
        _require_file(config.app_executable, "APP_EXECUTABLE_MISSING", "工作台可执行文件")
    else:
        if config.app_python is None or config.app_script is None:
            raise LauncherError("APP_RUNTIME_MISSING", "未配置预置工作台 Python 环境。")
        _require_file(config.app_python, "APP_PYTHON_MISSING", "工作台 Python 环境")
        _require_file(config.app_script, "APP_SCRIPT_MISSING", "工作台入口")


def probe_preinstalled_runtimes(
    config: LauncherConfig,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> bool:
    probes: list[tuple[str, list[str], Path, Mapping[str, str], str | None]] = []
    if (
        not config.motion_runtime_required
        and (config.mpt_root / "app" / "asgi.py").is_file()
        and config.mpt_python.is_file()
    ):
        probes.append(
            (
                "MPT_RUNTIME_INVALID",
                [str(config.mpt_python), "-I", "-B", "-c", "import fastapi, uvicorn"],
                config.mpt_root,
                build_engine_environment(os.environ, config),
                None,
            )
        )
    if config.motion_runtime_required:
        assert config.node_executable is not None and config.hyperframes_cli is not None
        assert config.hyperframes_browser is not None
        motion_environment = build_app_environment(
            os.environ, config, app_port=8765, mpt_port=0, motion_health_verified=False
        )
        sapi_script = config.project_root / "core" / "sapi_tts.ps1"
        probes.extend(
            [
                (
                    "MOTION_NODE_INVALID",
                    [str(config.node_executable), "--version"],
                    config.project_root,
                    motion_environment,
                    config.node_version,
                ),
                (
                    "HYPERFRAMES_RUNTIME_INVALID",
                    [str(config.node_executable), str(config.hyperframes_cli), "--version"],
                    config.hyperframes_cli.parents[3],
                    motion_environment,
                    config.hyperframes_version,
                ),
                (
                    "HYPERFRAMES_BROWSER_INVALID",
                    [str(config.hyperframes_browser), "--version"],
                    config.project_root,
                    motion_environment,
                    config.browser_version,
                ),
                (
                    "SAPI_ZH_CN_VOICE_MISSING",
                    [
                        str(_windows_powershell()),
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(sapi_script),
                        "-ProbeOnly",
                        "-Language",
                        "zh-CN",
                    ],
                    config.project_root,
                    motion_environment,
                    "CULTURE=zh-CN",
                ),
            ]
        )
    if config.app_executable is None and config.app_python is not None:
        probes.append(
            (
                "APP_RUNTIME_INVALID",
                [str(config.app_python), "-I", "-B", "-c", "import PIL, ddgs, langgraph"],
                config.project_root,
                {**os.environ, "PYTHONUTF8": "1"},
                None,
            )
        )

    for code, command, cwd, environment, expected_version in probes:
        try:
            result = runner(
                command,
                cwd=str(cwd),
                env=dict(environment),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LauncherError(code, "预置运行环境自检无法完成。") from exc
        if result.returncode != 0:
            if code == "SAPI_ZH_CN_VOICE_MISSING":
                raise LauncherError(code, "本机缺少 zh-CN 中文离线语音，纯动画配音暂不可用。")
            raise LauncherError(code, "预置运行环境不完整，运行时不会自动下载依赖。")
        if expected_version is not None:
            stdout = result.stdout.decode("utf-8", "replace") if isinstance(result.stdout, bytes) else str(result.stdout or "")
            stderr = result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes) else str(result.stderr or "")
            if not re.search(
                rf"(?<![0-9.]){re.escape(expected_version)}(?![0-9.])",
                f"{stdout}\n{stderr}",
            ):
                if code == "SAPI_ZH_CN_VOICE_MISSING":
                    raise LauncherError(code, "中文离线语音探针未返回 zh-CN，纯动画配音暂不可用。")
                raise LauncherError(code, "预置运行环境版本探针与发布锁不一致。")
    return config.motion_runtime_required


def _port_is_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind((LOOPBACK_HOST, port))
        except OSError:
            return False
    return True


def select_loopback_port(preferred: int = 0, *, exclude: set[int] | None = None) -> int:
    excluded = exclude or set()
    if preferred:
        if not 1024 <= preferred <= 65535:
            raise LauncherError("PORT_INVALID", "端口必须介于 1024 与 65535 之间。", port=preferred)
        candidates = range(preferred, min(preferred + 20, 65536))
        for candidate in candidates:
            if candidate not in excluded and _port_is_available(candidate):
                return candidate
        raise LauncherError("PORT_UNAVAILABLE", "未找到可用的本机回环端口。", start=preferred)

    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((LOOPBACK_HOST, 0))
            candidate = int(probe.getsockname()[1])
        if candidate not in excluded:
            return candidate
    raise LauncherError("PORT_UNAVAILABLE", "未找到可用的 MPT 本机回环端口。")


def build_engine_command(config: LauncherConfig, mpt_port: int) -> list[str]:
    return [
        str(config.mpt_python),
        "-E",
        "-s",
        "-B",
        "-X",
        "utf8",
        "-m",
        "uvicorn",
        "app.asgi:app",
        "--host",
        LOOPBACK_HOST,
        "--port",
        str(mpt_port),
        "--log-level",
        "warning",
    ]


def build_app_command(config: LauncherConfig, app_port: int) -> list[str]:
    if config.app_executable is not None:
        command = [str(config.app_executable)]
    else:
        assert config.app_python is not None and config.app_script is not None
        command = [
            str(config.app_python),
            "-E",
            "-s",
            "-B",
            "-X",
            "utf8",
            str(config.app_script),
        ]
    command.extend(["--host", LOOPBACK_HOST, "--port", str(app_port), "--strict-port"])
    return command


def build_engine_environment(base: Mapping[str, str], config: LauncherConfig) -> dict[str, str]:
    """Build a deliberately small environment that cannot inherit Provider secrets."""
    environment = {key: base[key] for key in ENGINE_ENV_ALLOWLIST if base.get(key)}
    ffmpeg_dir = str(config.ffmpeg.parent)
    current_path = environment.get("PATH", "")
    environment["PATH"] = ffmpeg_dir if not current_path else f"{ffmpeg_dir}{os.pathsep}{current_path}"
    environment.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "IMAGEIO_FFMPEG_EXE": str(config.ffmpeg),
        }
    )
    return environment


def build_app_environment(
    base: Mapping[str, str],
    config: LauncherConfig,
    *,
    app_port: int,
    mpt_port: int,
    mpt_ready: bool | None = None,
    motion_health_verified: bool = False,
    mpt_health_file: Path | None = None,
    launch_instance_token: str | None = None,
) -> dict[str, str]:
    environment = {
        key: value
        for key, value in base.items()
        if not key.upper().startswith("PYTHON")
        and not key.upper().startswith(NODE_HOST_ENV_FORBIDDEN_PREFIXES)
        and not key.upper().startswith(APP_RELEASE_FORBIDDEN_PREFIXES)
        and key.upper() not in NODE_HOST_ENV_FORBIDDEN_NAMES
    }
    for name in APP_RELEASE_FORBIDDEN_ENV:
        environment.pop(name, None)
    # The workbench owns the Provider boundary.  Restore only the one supported
    # credential after broad prefix filtering; endpoints, models, and every
    # other Provider variable remain blocked, and MPT still receives none.
    for name in APP_PROVIDER_ENV_ALLOWLIST:
        value = base.get(name)
        if value:
            environment[name] = value
    if mpt_ready is None:
        mpt_ready = (config.mpt_root / "app" / "asgi.py").is_file() and config.mpt_python.is_file()
    environment.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SHIYI_MPT_ENABLED": "1" if mpt_ready else "0",
            "SHIYI_MPT_BASE_URL": f"http://{LOOPBACK_HOST}:{mpt_port}{MPT_API_PREFIX}",
            "SHIYI_MPT_VERSION": EXPECTED_MPT_VERSION,
            "SHIYI_MPT_HEALTH_VERIFIED": "1" if mpt_ready else "0",
            "SHIYI_MPT_MATERIAL_STRATEGY": "local",
            "SHIYI_MPT_LOCAL_MATERIAL_DIR": str(config.material_root),
            "FFMPEG_PATH": str(config.ffmpeg),
            "FFPROBE_PATH": str(config.ffprobe),
            "SHIYI_NOTO_REGULAR": str(config.font_regular),
            "SHIYI_NOTO_BOLD": str(config.font_bold),
        }
    )
    if (config.project_root / "PACKAGE-MANIFEST.json").is_file():
        environment["SHIYI_RUNTIME_DIR"] = str(_local_product_root(base) / "UserData")
    if config.motion_runtime_required:
        assert config.node_executable is not None
        assert config.hyperframes_cli is not None
        assert config.hyperframes_browser is not None
        system_root = base.get("SYSTEMROOT") or base.get("WINDIR")
        fixed_path = [str(config.node_executable.parent), str(config.ffmpeg.parent)]
        if system_root:
            # The packaged voice fallback invokes Windows PowerShell after the
            # offline Voice Workbench probe fails.  System32 alone does not
            # contain powershell.exe, so keep the exact in-box Windows path in
            # the otherwise isolated PATH instead of inheriting host entries.
            fixed_path.append(str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0"))
            fixed_path.append(str(Path(system_root) / "System32"))
        environment["PATH"] = os.pathsep.join(fixed_path)
        environment.update(
            {
                "SHIYI_NODE_EXECUTABLE": str(config.node_executable),
                "SHIYI_HYPERFRAMES_CLI": str(config.hyperframes_cli),
                "HYPERFRAMES_BROWSER_PATH": str(config.hyperframes_browser),
                "HYPERFRAMES_FFMPEG_PATH": str(config.ffmpeg),
                "HYPERFRAMES_FFPROBE_PATH": str(config.ffprobe),
                "HYPERFRAMES_NO_UPDATE_CHECK": "1",
                "HYPERFRAMES_NO_AUTO_INSTALL": "1",
                "HYPERFRAMES_NO_TELEMETRY": "1",
                "HYPERFRAMES_SKIP_SKILLS": "1",
                "DO_NOT_TRACK": "1",
                "NO_UPDATE_NOTIFIER": "1",
                "NPM_CONFIG_UPDATE_NOTIFIER": "false",
                "NPM_CONFIG_AUDIT": "false",
                "NPM_CONFIG_FUND": "false",
                "NPM_CONFIG_OFFLINE": "true",
                "NPM_CONFIG_PREFER_OFFLINE": "true",
                "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
                "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
                "PUPPETEER_SKIP_DOWNLOAD": "true",
            }
        )
        if motion_health_verified:
            environment["SHIYI_MOTION_HEALTH_VERIFIED"] = "1"
        if mpt_health_file is not None:
            environment["SHIYI_MPT_HEALTH_FILE"] = str(mpt_health_file)
    # This value is sent only to the workbench. MPT receives the same exact
    # origin through its separate, secret-free environment below.
    environment["SHIYI_WORKBENCH_ORIGIN"] = f"http://{LOOPBACK_HOST}:{app_port}"
    if launch_instance_token:
        environment["SHIYI_LAUNCH_INSTANCE_TOKEN"] = launch_instance_token
    if config.agent_test_review:
        environment["SHIYI_AGENT_TEST_REVIEW"] = "1"
    return environment


def _write_mpt_health_state(path: Path, healthy: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps({"schema_version": 1, "healthy": bool(healthy)}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def wait_for_mpt_health(
    url: str,
    *,
    timeout_seconds: float,
    requester: Callable[[str, float], bytes] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    process: subprocess.Popen[bytes] | None = None,
) -> None:
    if requester is None:
        opener = build_opener(ProxyHandler({}))

        def requester(target: str, timeout: float) -> bytes:
            request = Request(target, headers={"Accept": "application/json"}, method="GET")
            with opener.open(request, timeout=timeout) as response:
                return response.read(2_000_000)

    deadline = clock() + timeout_seconds
    while clock() < deadline:
        if process is not None and process.poll() is not None:
            raise LauncherError("MPT_EARLY_EXIT", "MPT API 在就绪前意外退出。")
        try:
            payload = json.loads(requester(url, min(2.0, timeout_seconds)).decode("utf-8"))
            paths = payload.get("paths", {}) if isinstance(payload, dict) else {}
            if isinstance(paths, dict) and MPT_OPENAPI_REQUIRED_PATH in paths:
                return
        except (OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        sleeper(0.25)
    raise LauncherError("MPT_HEALTH_TIMEOUT", "MPT API 未在限定时间内就绪。")


def wait_for_workbench_health(
    url: str,
    *,
    timeout_seconds: float,
    requester: Callable[[str, float], bytes] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    process: subprocess.Popen[bytes] | None = None,
    expected_instance_sha256: str | None = None,
) -> None:
    if requester is None:
        opener = build_opener(ProxyHandler({}))

        def requester(target: str, timeout: float) -> bytes:
            request = Request(target, headers={"Accept": "application/json"}, method="GET")
            with opener.open(request, timeout=timeout) as response:
                return response.read(2_000_000)

    deadline = clock() + timeout_seconds
    while clock() < deadline:
        if process is not None and process.poll() is not None:
            raise LauncherError("WORKBENCH_EARLY_EXIT", "工作台在就绪前意外退出。")
        try:
            payload = json.loads(requester(url, min(2.0, timeout_seconds)).decode("utf-8"))
            launch_digest = payload.get("launch_instance_sha256") if isinstance(payload, dict) else None
            product_matches = bool(
                isinstance(payload, dict)
                and payload.get("name") == "时宜 Agent 内容工厂"
                and payload.get("version") == "0.3.0"
                and payload.get("schema_version") == 2
            )
            instance_matches = expected_instance_sha256 is None or bool(
                isinstance(launch_digest, str)
                and secrets.compare_digest(launch_digest, expected_instance_sha256)
            )
            if product_matches and instance_matches:
                if process is not None and process.poll() is not None:
                    raise LauncherError("WORKBENCH_EARLY_EXIT", "工作台在就绪响应后意外退出。")
                return
        except (OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        sleeper(0.25)
    raise LauncherError("WORKBENCH_HEALTH_TIMEOUT", "工作台未在限定时间内就绪。")


def _new_launch_instance_identity() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode("utf-8")).hexdigest()


def _windows_hidden_process_options() -> dict[str, object]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    }


def terminate_process_tree(
    process: subprocess.Popen[bytes] | None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        try:
            runner(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass


def run_combined(
    config: LauncherConfig,
    *,
    popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    health_waiter: Callable[..., None] = wait_for_mpt_health,
    workbench_health_waiter: Callable[..., None] = wait_for_workbench_health,
    process_terminator: Callable[[subprocess.Popen[bytes] | None], None] = terminate_process_tree,
    sleeper: Callable[[float], None] = time.sleep,
    motion_health_verified: bool = False,
) -> int:
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    if config.motion_runtime_required:
        if not motion_health_verified:
            raise LauncherError(
                "MOTION_HEALTH_NOT_VERIFIED",
                "离线动画运行时尚未完成固定版本探针，拒绝显示为可用。",
            )
        return _run_motion_primary(
            config,
            popen_factory=popen_factory,
            health_waiter=health_waiter,
            workbench_health_waiter=workbench_health_waiter,
            process_terminator=process_terminator,
            sleeper=sleeper,
            motion_health_verified=motion_health_verified,
            started_at=started_at,
        )
    app_port = select_loopback_port(config.app_port)
    mpt_port = select_loopback_port(config.mpt_port, exclude={app_port})
    validate_mpt_network_config(config.mpt_root, mpt_port)

    runtime_dir = _launcher_runtime_dir(config.project_root)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    engine_log_path = runtime_dir / "mpt-api.log"
    engine_process: subprocess.Popen[bytes] | None = None
    app_process: subprocess.Popen[bytes] | None = None
    engine_started_at: str | None = None
    app_started_at: str | None = None
    launcher_state_path: Path | None = _claim_launcher_state(config.project_root, started_at=started_at)

    with engine_log_path.open("ab", buffering=0) as engine_log:
        try:
            engine_environment = build_engine_environment(os.environ, config)
            engine_environment["CORS_ALLOWED_ORIGINS"] = f"http://{LOOPBACK_HOST}:{app_port}"
            try:
                engine_started_at = dt.datetime.now(dt.timezone.utc).isoformat()
                engine_process = popen_factory(
                    build_engine_command(config, mpt_port),
                    cwd=str(config.mpt_root),
                    env=engine_environment,
                    stdin=subprocess.DEVNULL,
                    stdout=engine_log,
                    stderr=subprocess.STDOUT,
                    **_windows_hidden_process_options(),
                )
            except OSError as exc:
                raise LauncherError("MPT_START_FAILED", "MPT API 进程无法启动。") from exc
            health_waiter(
                f"http://{LOOPBACK_HOST}:{mpt_port}/openapi.json",
                timeout_seconds=config.health_timeout_seconds,
                process=engine_process,
            )
            if engine_process.poll() is not None:
                raise LauncherError("MPT_EARLY_EXIT", "MPT API 在就绪后意外退出。")

            launch_instance_token, expected_instance_sha256 = _new_launch_instance_identity()
            app_environment = build_app_environment(
                os.environ,
                config,
                app_port=app_port,
                mpt_port=mpt_port,
                launch_instance_token=launch_instance_token,
            )
            try:
                app_started_at = dt.datetime.now(dt.timezone.utc).isoformat()
                app_process = popen_factory(
                    build_app_command(config, app_port),
                    cwd=str(config.project_root),
                    env=app_environment,
                    stdin=None,
                    stdout=None,
                    stderr=None,
                )
            except OSError as exc:
                raise LauncherError("WORKBENCH_START_FAILED", "工作台进程无法启动。") from exc
            workbench_url = f"http://{LOOPBACK_HOST}:{app_port}"
            workbench_health_waiter(
                f"{workbench_url}/api/status",
                timeout_seconds=config.health_timeout_seconds,
                process=app_process,
                expected_instance_sha256=expected_instance_sha256,
            )
            if app_process.poll() is not None:
                raise LauncherError("WORKBENCH_EARLY_EXIT", "工作台在就绪后意外退出。")
            launcher_state_path = _write_launcher_state(
                config,
                started_at=started_at,
                app_port=app_port,
                mpt_port=mpt_port,
                app_process=app_process,
                engine_process=engine_process,
                app_started_at=app_started_at,
                engine_started_at=engine_started_at,
            )
            if config.open_browser:
                webbrowser.open(workbench_url)
            _emit(
                {
                    "status": "running",
                    "message": "时宜组合版已启动。",
                    "workbench_url": workbench_url,
                    "production_engine": {
                        "name": "MoneyPrinterTurbo",
                        "version": EXPECTED_MPT_VERSION,
                        "mode": "local_http",
                        "health": "ready",
                    },
                }
            )
            while True:
                app_result = app_process.poll()
                if app_result is not None:
                    return int(app_result)
                if engine_process.poll() is not None:
                    raise LauncherError("MPT_EARLY_EXIT", "MPT API 在工作台运行期间意外退出。")
                sleeper(0.5)
        except KeyboardInterrupt:
            return 130
        finally:
            process_terminator(app_process)
            process_terminator(engine_process)
            if launcher_state_path is not None:
                _remove_own_launcher_state(launcher_state_path)


def _run_motion_primary(
    config: LauncherConfig,
    *,
    popen_factory: Callable[..., subprocess.Popen[bytes]],
    health_waiter: Callable[..., None],
    workbench_health_waiter: Callable[..., None],
    process_terminator: Callable[[subprocess.Popen[bytes] | None], None],
    sleeper: Callable[[float], None],
    motion_health_verified: bool,
    started_at: str,
) -> int:
    """Start the offline motion workbench; MPT footage is best-effort only."""
    app_port = select_loopback_port(config.app_port)
    mpt_port = select_loopback_port(config.mpt_port, exclude={app_port})
    runtime_dir = _launcher_runtime_dir(config.project_root)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    mpt_health_file = runtime_dir / MPT_HEALTH_STATE_NAME
    engine_process: subprocess.Popen[bytes] | None = None
    app_process: subprocess.Popen[bytes] | None = None
    engine_started_at: str | None = None
    app_started_at: str | None = None
    launcher_state_path: Path | None = _claim_launcher_state(config.project_root, started_at=started_at)
    mpt_ready = False
    mpt_available = (config.mpt_root / "app" / "asgi.py").is_file() and config.mpt_python.is_file()
    with (runtime_dir / "mpt-api.log").open("ab", buffering=0) as engine_log:
        try:
            if mpt_available:
                try:
                    validate_mpt_network_config(config.mpt_root, mpt_port)
                    engine_environment = build_engine_environment(os.environ, config)
                    engine_environment["CORS_ALLOWED_ORIGINS"] = f"http://{LOOPBACK_HOST}:{app_port}"
                    engine_started_at = dt.datetime.now(dt.timezone.utc).isoformat()
                    engine_process = popen_factory(
                        build_engine_command(config, mpt_port),
                        cwd=str(config.mpt_root),
                        env=engine_environment,
                        stdin=subprocess.DEVNULL,
                        stdout=engine_log,
                        stderr=subprocess.STDOUT,
                        **_windows_hidden_process_options(),
                    )
                    health_waiter(
                        f"http://{LOOPBACK_HOST}:{mpt_port}/openapi.json",
                        timeout_seconds=min(
                            config.health_timeout_seconds,
                            MOTION_OPTIONAL_MPT_HEALTH_TIMEOUT_SECONDS,
                        ),
                        process=engine_process,
                    )
                    mpt_ready = engine_process.poll() is None
                except (LauncherError, OSError):
                    process_terminator(engine_process)
                    engine_process = None
                    engine_started_at = None

            _write_mpt_health_state(mpt_health_file, mpt_ready)

            launch_instance_token, expected_instance_sha256 = _new_launch_instance_identity()
            app_environment = build_app_environment(
                os.environ,
                config,
                app_port=app_port,
                mpt_port=mpt_port,
                mpt_ready=mpt_ready,
                motion_health_verified=motion_health_verified,
                mpt_health_file=mpt_health_file,
                launch_instance_token=launch_instance_token,
            )
            app_started_at = dt.datetime.now(dt.timezone.utc).isoformat()
            try:
                app_process = popen_factory(
                    build_app_command(config, app_port),
                    cwd=str(config.project_root),
                    env=app_environment,
                    stdin=None,
                    stdout=None,
                    stderr=None,
                )
            except OSError as exc:
                raise LauncherError("WORKBENCH_START_FAILED", "工作台进程无法启动。") from exc
            workbench_url = f"http://{LOOPBACK_HOST}:{app_port}"
            workbench_health_waiter(
                f"{workbench_url}/api/status",
                timeout_seconds=config.health_timeout_seconds,
                process=app_process,
                expected_instance_sha256=expected_instance_sha256,
            )
            if app_process.poll() is not None:
                raise LauncherError("WORKBENCH_EARLY_EXIT", "工作台在就绪后意外退出。")
            launcher_state_path = _write_launcher_state(
                config,
                started_at=started_at,
                app_port=app_port,
                mpt_port=mpt_port,
                app_process=app_process,
                engine_process=engine_process,
                app_started_at=app_started_at,
                engine_started_at=engine_started_at,
            )
            if config.open_browser:
                webbrowser.open(workbench_url)
            _emit(
                {
                    "status": "running",
                    "workbench_url": workbench_url,
                    "motion_engine": {"name": "HyperFrames", "mode": "offline_bundled", "health": "ready"},
                    "production_engine": {
                        "name": "MoneyPrinterTurbo",
                        "version": EXPECTED_MPT_VERSION,
                        "mode": "local_http",
                        "health": "ready" if mpt_ready else ("degraded" if mpt_available else "disabled"),
                    },
                }
            )
            while True:
                app_result = app_process.poll()
                if app_result is not None:
                    return int(app_result)
                # A footage-engine crash cannot take down the motion-primary UI.
                if engine_process is not None and engine_process.poll() is not None:
                    process_terminator(engine_process)
                    engine_process = None
                    mpt_ready = False
                    _write_mpt_health_state(mpt_health_file, False)
                sleeper(0.5)
        except KeyboardInterrupt:
            return 130
        finally:
            process_terminator(app_process)
            process_terminator(engine_process)
            if launcher_state_path is not None:
                _remove_own_launcher_state(launcher_state_path)


def _default_path(explicit: str | None, environment_name: str, candidates: Sequence[Path]) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    environment_value = os.getenv(environment_name, "").strip()
    if environment_value:
        return Path(environment_value).expanduser().resolve()
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve()


def _packaged_path(project_root: Path, explicit: str | None, relative: str, label: str) -> Path:
    expected = (project_root / relative).resolve()
    if explicit and Path(explicit).expanduser().resolve() != expected:
        raise LauncherError(
            "PACKAGE_PATH_OVERRIDE_REJECTED",
            f"正式便携包不允许把{label}改到包外路径。",
        )
    return expected


def config_from_args(args: argparse.Namespace) -> LauncherConfig:
    project_root = Path(args.project_root).expanduser().resolve()
    if _looks_like_packaged_root(project_root) and not (project_root / "PACKAGE-MANIFEST.json").is_file():
        raise LauncherError("PACKAGE_MANIFEST_MISSING", "便携包缺少完整性清单，已拒绝启动。")
    if (project_root / "PACKAGE-MANIFEST.json").is_file():
        if args.app_executable:
            raise LauncherError(
                "PACKAGE_PATH_OVERRIDE_REJECTED",
                "正式便携包不允许改用包外工作台程序。",
            )
        mpt_root = _packaged_path(
            project_root, args.mpt_root, "engine/MoneyPrinterTurbo", "MPT 引擎"
        )
        shared_python = _packaged_path(
            project_root, args.app_python, "runtime/python/python.exe", "工作台 Python"
        )
        mpt_python = _packaged_path(
            project_root, args.mpt_python, "runtime/python/python.exe", "MPT Python"
        )
        ffmpeg = _packaged_path(
            project_root, args.ffmpeg, "runtime/ffmpeg/ffmpeg.exe", "FFmpeg"
        )
        ffprobe = _packaged_path(
            project_root, args.ffprobe, "runtime/ffmpeg/ffprobe.exe", "FFprobe"
        )
        font_regular = _packaged_path(
            project_root, args.font_regular, "docs/fonts/NotoSansSC-Regular.ttf", "正文字体"
        )
        font_bold = _packaged_path(
            project_root, args.font_bold, "docs/fonts/NotoSansSC-Bold.ttf", "粗体字体"
        )
        material_root = _packaged_path(
            project_root,
            args.material_root,
            "engine/MoneyPrinterTurbo/storage/local_videos",
            "本地素材目录",
        )
        try:
            package_manifest = json.loads((project_root / "PACKAGE-MANIFEST.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LauncherError("PACKAGE_MANIFEST_INVALID", "便携包清单无法安全解析。") from exc
        motion_required = isinstance(package_manifest, dict) and package_manifest.get("package_profile") == "motion_primary"
        motion_manifest = package_manifest.get("motion_runtime", {}) if isinstance(package_manifest, dict) else {}
        if motion_required and not isinstance(motion_manifest, dict):
            raise LauncherError("PACKAGE_MANIFEST_INVALID", "便携包动画运行时清单无效。")
        node_executable = _packaged_path(project_root, args.node_executable, "runtime/node/node.exe", "Node") if motion_required else None
        hyperframes_cli = _packaged_path(
            project_root,
            args.hyperframes_cli,
            "runtime/hyperframes/node_modules/hyperframes/bin/hyperframes.mjs",
            "HyperFrames CLI",
        ) if motion_required else None
        hyperframes_browser = _packaged_path(
            project_root, args.hyperframes_browser, "runtime/browser/chrome-headless-shell.exe", "无头浏览器"
        ) if motion_required else None
        return LauncherConfig(
            project_root=project_root,
            mpt_root=mpt_root,
            mpt_python=mpt_python,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            font_regular=font_regular,
            font_bold=font_bold,
            material_root=material_root,
            app_port=args.app_port,
            mpt_port=args.mpt_port,
            health_timeout_seconds=args.health_timeout,
            open_browser=not args.no_open,
            preflight_only=args.preflight_only,
            app_executable=None,
            app_python=shared_python,
            app_script=(project_root / "app.py").resolve(),
            agent_test_review=bool(args.agent_test_review),
            motion_runtime_required=motion_required,
            node_executable=node_executable,
            hyperframes_cli=hyperframes_cli,
            hyperframes_browser=hyperframes_browser,
            node_version=str(motion_manifest.get("node_version", "")) if motion_required else None,
            hyperframes_version=str(motion_manifest.get("hyperframes_version", "")) if motion_required else None,
            browser_version=str(motion_manifest.get("browser_version", "")) if motion_required else None,
        )
    mpt_root = _default_path(
        args.mpt_root,
        "SHIYI_MPT_ROOT",
        [project_root / "engine" / "MoneyPrinterTurbo", project_root / "third_party" / "MoneyPrinterTurbo"],
    )
    app_executable = None
    app_python = None
    app_script = None
    explicit_app_executable = args.app_executable or os.getenv("SHIYI_APP_EXECUTABLE", "").strip()
    packaged_executable = project_root / "ShiyiContentFactory.exe"
    if explicit_app_executable or packaged_executable.is_file():
        app_executable = Path(explicit_app_executable or packaged_executable).expanduser().resolve()
    else:
        app_python = _default_path(
            args.app_python,
            "SHIYI_APP_PYTHON",
            [project_root / ".venv" / "Scripts" / "python.exe"],
        )
        app_script = project_root / "app.py"

    mpt_python = _default_path(
        args.mpt_python,
        "SHIYI_MPT_PYTHON",
        [mpt_root / ".venv" / "Scripts" / "python.exe"],
    )
    ffmpeg = _default_path(
        args.ffmpeg,
        "SHIYI_FFMPEG_PATH",
        [project_root / "tools" / "ffmpeg" / "ffmpeg.exe", project_root / "ffmpeg" / "bin" / "ffmpeg.exe"],
    )
    ffprobe = _default_path(
        args.ffprobe,
        "SHIYI_FFPROBE_PATH",
        [project_root / "tools" / "ffmpeg" / "ffprobe.exe", project_root / "ffmpeg" / "bin" / "ffprobe.exe"],
    )
    font_regular = _default_path(
        args.font_regular,
        "SHIYI_NOTO_REGULAR",
        [project_root / "docs" / "fonts" / "NotoSansSC-Regular.ttf"],
    )
    font_bold = _default_path(
        args.font_bold,
        "SHIYI_NOTO_BOLD",
        [project_root / "docs" / "fonts" / "NotoSansSC-Bold.ttf"],
    )
    material_root = _default_path(
        args.material_root,
        "SHIYI_MPT_LOCAL_MATERIAL_DIR",
        [mpt_root / "storage" / "local_videos"],
    )
    motion_required = bool(args.require_motion_runtime)
    node_executable = Path(args.node_executable).expanduser().resolve() if args.node_executable else None
    hyperframes_cli = Path(args.hyperframes_cli).expanduser().resolve() if args.hyperframes_cli else None
    hyperframes_browser = Path(args.hyperframes_browser).expanduser().resolve() if args.hyperframes_browser else None
    return LauncherConfig(
        project_root=project_root,
        mpt_root=mpt_root,
        mpt_python=mpt_python,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        font_regular=font_regular,
        font_bold=font_bold,
        material_root=material_root,
        app_port=args.app_port,
        mpt_port=args.mpt_port,
        health_timeout_seconds=args.health_timeout,
        open_browser=not args.no_open,
        preflight_only=args.preflight_only,
        app_executable=app_executable,
        app_python=app_python,
        app_script=app_script,
        agent_test_review=bool(args.agent_test_review),
        motion_runtime_required=motion_required,
        node_executable=node_executable,
        hyperframes_cli=hyperframes_cli,
        hyperframes_browser=hyperframes_browser,
        node_version=args.node_version,
        hyperframes_version=args.hyperframes_version,
        browser_version=args.browser_version,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="时宜 Agent 内容工厂 Windows 本地组合版启动器")
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--mpt-root")
    parser.add_argument("--mpt-python")
    parser.add_argument("--app-executable")
    parser.add_argument("--app-python")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    parser.add_argument("--material-root")
    parser.add_argument("--font-regular")
    parser.add_argument("--font-bold")
    parser.add_argument("--node-executable")
    parser.add_argument("--hyperframes-cli")
    parser.add_argument("--hyperframes-browser")
    parser.add_argument("--node-version")
    parser.add_argument("--hyperframes-version")
    parser.add_argument("--browser-version")
    parser.add_argument("--require-motion-runtime", action="store_true")
    parser.add_argument("--app-port", type=int, default=8765)
    parser.add_argument("--mpt-port", type=int, default=0)
    parser.add_argument("--health-timeout", type=float, default=60.0)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    maintenance = parser.add_mutually_exclusive_group()
    maintenance.add_argument("--stop", action="store_true", help="只停止由当前便携包记录的进程树")
    maintenance.add_argument("--import-runtime", help="从旧版包内 runtime 复制任务、配置和本机加密 Key")
    parser.add_argument(
        "--agent-test-review",
        action="store_true",
        help="仅用于受控测试：两道阶段门禁由Codex浏览器操作，记录不冒充人审",
    )
    return parser


def _emit(payload: Mapping[str, object]) -> None:
    print(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True), flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    early_launcher_state: Path | None = None
    try:
        args = build_parser().parse_args(argv)
        if args.stop:
            project_root = Path(args.project_root).expanduser().resolve()
            result = stop_recorded_processes(project_root)
            _emit(result)
            return 0
        if args.import_runtime:
            project_root = Path(args.project_root).expanduser().resolve()
            result = import_legacy_runtime(Path(args.import_runtime))
            _emit(result)
            return 0
        project_root = Path(args.project_root).expanduser().resolve()
        if (project_root / "PACKAGE-MANIFEST.json").is_file() and not args.preflight_only:
            early_launcher_state = _claim_launcher_state(
                project_root,
                started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
        config = config_from_args(args)
        validate_preinstalled_layout(config)
        motion_health_verified = probe_preinstalled_runtimes(config)
        if config.preflight_only:
            app_port = select_loopback_port(config.app_port)
            mpt_port = select_loopback_port(config.mpt_port, exclude={app_port})
            if (config.mpt_root / "app" / "asgi.py").is_file() and config.mpt_python.is_file():
                validate_mpt_network_config(config.mpt_root, mpt_port)
            _emit(
                {
                    "status": "ready",
                    "message": "预置依赖、字体、FFmpeg 与本机网络边界已通过检查。",
                    "downloads_started": False,
                    "production_engine": {
                        "name": "MoneyPrinterTurbo",
                        "version": EXPECTED_MPT_VERSION,
                        "mode": "local_http",
                        "health": "not_started",
                    },
                }
            )
            return 0
        return run_combined(config, motion_health_verified=motion_health_verified)
    except LauncherError as exc:
        _emit(exc.as_dict())
        return 2
    except Exception:
        _emit(
            {
                "status": "error",
                "code": "UNEXPECTED_LAUNCHER_ERROR",
                "message": "组合版启动器遇到未分类错误，未继续启动。",
            }
        )
        return 3
    finally:
        if early_launcher_state is not None:
            _remove_own_launcher_state(early_launcher_state)


if __name__ == "__main__":
    raise SystemExit(main())
