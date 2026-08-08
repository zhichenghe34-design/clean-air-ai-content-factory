"""Fail-closed adapter for the pinned MoneyPrinterTurbo production engine."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ENGINE_NAME = "MoneyPrinterTurbo"
ENGINE_VERSION = "1.3.3"
ENGINE_COMMIT = "254cd028906ee657eab844dc94087cdbea2a7aa8"
ENGINE_MODE = "local_http"
ENGINE_REQUIRED_ENV = (("PYTHONUTF8", "1"),)

_TASK_STATES = {-1, 1, 4}
_SAFE_FAILURE_STAGES = {
    "preflight",
    "script",
    "terms",
    "audio",
    "subtitle",
    "materials",
    "video",
}
_MATERIAL_STRATEGIES = {"pexels", "pixabay", "coverr", "local"}
_VOICE_STRATEGIES = {"edge_tts": "zh-CN-YunxiNeural-Male"}
_LOOPBACK_NAMES = {"localhost"}
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_VIDEO_BYTES = 1024 * 1024 * 1024
_MAX_AUDIO_BYTES = 128 * 1024 * 1024
_MAX_SUBTITLE_BYTES = 4 * 1024 * 1024
_TASK_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|authorization|bearer|cookie|credential|password|passwd|secret|token)",
    re.IGNORECASE,
)


class ProductionEngineError(RuntimeError):
    """A stable, sanitized production-engine failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.retryable = retryable


@dataclass(frozen=True)
class JsonTransportResponse:
    payload: Mapping[str, Any]
    final_url: str


@dataclass(frozen=True)
class DownloadTransportResponse:
    final_url: str
    size: int


class ProductionEngineTransport(Protocol):
    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, Any] | None,
        timeout_seconds: float,
    ) -> JsonTransportResponse: ...

    def download(
        self,
        url: str,
        destination: Path,
        *,
        timeout_seconds: float,
        max_bytes: int,
    ) -> DownloadTransportResponse: ...


@dataclass(frozen=True)
class ProductionEngineArtifact:
    name: str
    relative_path: str
    mime: str
    size: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "relative_path": self.relative_path,
            "mime": self.mime,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ProductionEngineResult:
    engine_name: str
    engine_version: str
    engine_commit: str
    mode: str
    task_id: str
    artifacts: tuple[ProductionEngineArtifact, ...]
    report_path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "engine_name": self.engine_name,
            "engine_version": self.engine_version,
            "engine_commit": self.engine_commit,
            "mode": self.mode,
            "task_id": self.task_id,
            "artifacts": [item.as_dict() for item in self.artifacts],
            "report_path": self.report_path,
        }


def _effective_port(parsed: Any) -> int:
    try:
        return parsed.port or 80
    except ValueError as exc:
        raise ProductionEngineError(
            "invalid_engine_url", "MPT local URL is invalid.", stage="configuration"
        ) from exc


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _decoded_path(path: str) -> str:
    value = path
    for _ in range(4):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return value.replace("\\", "/")


def _reject_path_traversal(path: str) -> None:
    decoded = _decoded_path(path)
    if _CONTROL_RE.search(decoded):
        raise ProductionEngineError(
            "unsafe_artifact_path", "MPT artifact path is unsafe.", stage="collect"
        )
    if any(part in {".", ".."} for part in decoded.split("/")):
        raise ProductionEngineError(
            "unsafe_artifact_path", "MPT artifact path is unsafe.", stage="collect"
        )


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), _effective_port(parsed)


def validate_engine_base_url(value: str) -> str:
    """Return a canonical MPT API base, accepting HTTP loopback only."""
    if not isinstance(value, str) or not value.strip():
        raise ProductionEngineError(
            "invalid_engine_url", "MPT local URL is required.", stage="configuration"
        )
    try:
        parsed = urlsplit(value.strip())
    except ValueError as exc:
        raise ProductionEngineError(
            "invalid_engine_url", "MPT local URL is invalid.", stage="configuration"
        ) from exc
    if (
        parsed.scheme.lower() != "http"
        or not _is_loopback_host(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProductionEngineError(
            "invalid_engine_url",
            "MPT must use an HTTP loopback address without credentials.",
            stage="configuration",
        )
    _reject_path_traversal(parsed.path)
    normalized_path = parsed.path.rstrip("/") or "/api/v1"
    if normalized_path != "/api/v1":
        raise ProductionEngineError(
            "invalid_engine_url", "MPT API base must end with /api/v1.", stage="configuration"
        )
    host = parsed.hostname or ""
    netloc_host = f"[{host}]" if ":" in host else host
    netloc = f"{netloc_host}:{_effective_port(parsed)}"
    return urlunsplit(("http", netloc, normalized_path, "", ""))


def _validate_engine_url(url: str, expected_origin: tuple[str, str, int]) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ProductionEngineError(
            "unsafe_engine_response_url",
            "MPT returned an unsafe URL.",
            stage="transport",
        ) from exc
    if (
        _origin(url) != expected_origin
        or not _is_loopback_host(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProductionEngineError(
            "unsafe_engine_response_url",
            "MPT returned a URL outside the local engine.",
            stage="transport",
        )
    _reject_path_traversal(parsed.path)
    return url


class _LoopbackRedirectHandler(HTTPRedirectHandler):
    def __init__(self, expected_origin: tuple[str, str, int]) -> None:
        super().__init__()
        self.expected_origin = expected_origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _validate_engine_url(newurl, self.expected_origin)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibProductionEngineTransport:
    """Small HTTP transport that bypasses proxies and blocks off-origin redirects."""

    def __init__(self, base_url: str) -> None:
        self.base_url = validate_engine_base_url(base_url)
        self.expected_origin = _origin(self.base_url)
        self.opener = build_opener(
            ProxyHandler({}), _LoopbackRedirectHandler(self.expected_origin)
        )

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, Any] | None,
        timeout_seconds: float,
    ) -> JsonTransportResponse:
        _validate_engine_url(url, self.expected_origin)
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method.upper())
        try:
            with self.opener.open(request, timeout=timeout_seconds) as response:
                final_url = response.geturl()
                _validate_engine_url(final_url, self.expected_origin)
                raw = response.read(_MAX_JSON_BYTES + 1)
        except ProductionEngineError:
            raise
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise ProductionEngineError(
                "engine_unavailable",
                "MPT local service is unavailable.",
                stage="transport",
                retryable=True,
            ) from exc
        if len(raw) > _MAX_JSON_BYTES:
            raise ProductionEngineError(
                "invalid_engine_response", "MPT JSON response is too large.", stage="transport"
            )
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProductionEngineError(
                "invalid_engine_response", "MPT returned invalid JSON.", stage="transport"
            ) from exc
        if not isinstance(decoded, dict):
            raise ProductionEngineError(
                "invalid_engine_response", "MPT returned an invalid response.", stage="transport"
            )
        return JsonTransportResponse(decoded, final_url)

    def download(
        self,
        url: str,
        destination: Path,
        *,
        timeout_seconds: float,
        max_bytes: int,
    ) -> DownloadTransportResponse:
        _validate_engine_url(url, self.expected_origin)
        request = Request(url, headers={"Accept": "application/octet-stream"}, method="GET")
        size = 0
        try:
            with self.opener.open(request, timeout=timeout_seconds) as response:
                final_url = response.geturl()
                _validate_engine_url(final_url, self.expected_origin)
                with destination.open("wb") as target:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_bytes:
                            raise ProductionEngineError(
                                "artifact_too_large",
                                "MPT artifact exceeded its size limit.",
                                stage="collect",
                            )
                        target.write(chunk)
        except ProductionEngineError:
            destination.unlink(missing_ok=True)
            raise
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            destination.unlink(missing_ok=True)
            raise ProductionEngineError(
                "artifact_download_failed",
                "MPT artifact download failed.",
                stage="collect",
                retryable=True,
            ) from exc
        return DownloadTransportResponse(final_url, size)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as target:
            json.dump(payload, target, ensure_ascii=False, indent=2, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > limit or _CONTROL_RE.search(text):
        return None
    return text


def _safe_public_url(value: Any) -> str | None:
    text = _safe_text(value, 2048)
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _sanitize_material_sources(raw: Any, expected_provider: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw or len(raw) > 200:
        raise ProductionEngineError(
            "invalid_material_sources",
            "MPT material source records are unavailable.",
            stage="collect",
        )
    sanitized: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ProductionEngineError(
                "invalid_material_sources",
                "MPT material source records are invalid.",
                stage="collect",
            )
        provider = _safe_text(item.get("provider"), 32)
        local_file = _safe_text(item.get("local_file"), 255)
        if provider != expected_provider or not local_file:
            raise ProductionEngineError(
                "invalid_material_sources",
                "MPT material source records do not match the request.",
                stage="collect",
            )
        _reject_path_traversal(local_file)
        if "/" in local_file or "\\" in local_file:
            raise ProductionEngineError(
                "invalid_material_sources",
                "MPT material source records contain an unsafe filename.",
                stage="collect",
            )
        record: dict[str, Any] = {"provider": provider, "local_file": local_file}
        duration = item.get("duration")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and 0 <= duration <= 3600:
            record["duration"] = duration
        for field, limit in (("search_term", 200), ("asset_id", 128)):
            value = _safe_text(item.get(field), limit)
            if value:
                record[field] = value
        source_page = _safe_public_url(item.get("source_page"))
        if source_page:
            record["source_page"] = source_page
        creator = item.get("creator")
        if isinstance(creator, dict):
            safe_creator: dict[str, Any] = {}
            for field in ("id", "name"):
                value = _safe_text(creator.get(field), 200)
                if value:
                    safe_creator[field] = value
            profile_page = _safe_public_url(creator.get("profile_page"))
            if profile_page:
                safe_creator["profile_page"] = profile_page
            if safe_creator:
                record["creator"] = safe_creator
        rendition = item.get("rendition")
        if isinstance(rendition, dict):
            safe_rendition: dict[str, Any] = {}
            rendition_id = _safe_text(rendition.get("id"), 128)
            if rendition_id:
                safe_rendition["id"] = rendition_id
            for field in ("width", "height"):
                value = rendition.get(field)
                if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 16384:
                    safe_rendition[field] = value
            if safe_rendition:
                record["rendition"] = safe_rendition
        sanitized.append(record)
    return sanitized


def _artifact_metadata(root: Path, path: Path, name: str, mime: str) -> ProductionEngineArtifact:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ProductionEngineError(
            "artifact_missing", "MPT did not produce a required artifact.", stage="collect"
        )
    relative = path.relative_to(root).as_posix()
    return ProductionEngineArtifact(
        name=name,
        relative_path=relative,
        mime=mime,
        size=path.stat().st_size,
        sha256=_file_sha256(path),
    )


def _is_reparse_point(path: Path) -> bool:
    info = os.lstat(path)
    junction = getattr(path, "is_junction", None)
    return bool(
        stat.S_ISLNK(info.st_mode)
        or (callable(junction) and junction())
        or getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _samefile_root_anchor(root: Path, candidate: Path) -> tuple[Path, tuple[str, ...]] | None:
    """Find candidate's root ancestor by file identity, not its path spelling."""
    for ancestor in (candidate, *candidate.parents):
        try:
            if os.path.samefile(ancestor, root):
                return ancestor, candidate.relative_to(ancestor).parts
        except (OSError, ValueError):
            continue
    return None


class ProductionEngineAdapter:
    """Submit a pre-approved script to a pinned, loopback-only MPT service."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: ProductionEngineTransport | None = None,
        timeout_seconds: float = 900,
        poll_interval_seconds: float = 1,
        local_material_root: Path | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.base_url = validate_engine_base_url(base_url)
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if isinstance(poll_interval_seconds, bool) or not isinstance(poll_interval_seconds, (int, float)) or poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.expected_origin = _origin(self.base_url)
        self.transport = transport or UrllibProductionEngineTransport(self.base_url)
        self.timeout_seconds = float(timeout_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.local_material_root = (
            Path(local_material_root).absolute() if local_material_root is not None else None
        )
        self.clock = clock or time.monotonic
        self.sleeper = sleeper or time.sleep

    def run(
        self,
        *,
        approved: bool,
        script: str,
        keywords: list[str],
        aspect: str,
        target_duration_seconds: float,
        staging_dir: Path,
        material_strategy: str,
        voice_strategy: str,
        local_material_paths: list[Path] | None = None,
        cancel_event: Any | None = None,
    ) -> ProductionEngineResult:
        self._validate_input(
            approved=approved,
            script=script,
            keywords=keywords,
            aspect=aspect,
            target_duration_seconds=target_duration_seconds,
            material_strategy=material_strategy,
            voice_strategy=voice_strategy,
        )
        local_materials = self._resolve_local_materials(
            material_strategy, local_material_paths
        )
        root = Path(staging_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        private_dir = root / ".engine-import"
        if private_dir.exists() and private_dir.is_symlink():
            raise ProductionEngineError(
                "unsafe_staging", "Engine staging directory is unsafe.", stage="configuration"
            )
        private_dir.mkdir(parents=True, exist_ok=True)

        deadline = self.clock() + self.timeout_seconds
        payload = self._payload(
            script, keywords, material_strategy, voice_strategy, local_materials
        )
        response = self._request_json("POST", self._url("videos"), payload, deadline)
        data = self._response_data(response, "submit")
        task_id = self._task_id(data.get("task_id"))

        final_status = self._poll(task_id, deadline, cancel_event)
        artifact_paths = self._collect(
            root,
            private_dir,
            task_id,
            final_status,
            deadline,
            material_strategy,
            local_materials,
        )
        artifacts = (
            _artifact_metadata(root, artifact_paths["final.mp4"], "final.mp4", "video/mp4"),
            _artifact_metadata(root, artifact_paths["audio.mp3"], "audio.mp3", "audio/mpeg"),
            _artifact_metadata(root, artifact_paths["captions.srt"], "captions.srt", "application/x-subrip"),
            _artifact_metadata(
                root,
                artifact_paths["material_sources.json"],
                "material_sources.json",
                "application/json",
            ),
        )
        report_path = root / "engine_report.json"
        audio_duration = final_status.get("audio_duration")
        report = {
            "schema_version": 1,
            "status": "complete",
            "engine": {
                "name": ENGINE_NAME,
                "version": ENGINE_VERSION,
                "commit": ENGINE_COMMIT,
                "mode": ENGINE_MODE,
            },
            "task_id": task_id,
            "aspect": aspect,
            "target_duration_seconds": target_duration_seconds,
            "actual_audio_duration_seconds": audio_duration,
            "material_strategy": material_strategy,
            "voice_strategy": voice_strategy,
            "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest().upper(),
            "keyword_count": len(keywords),
            "runtime_requirements": {key: value for key, value in ENGINE_REQUIRED_ENV},
            "artifacts": [item.as_dict() for item in artifacts],
        }
        if _contains_secret_key(report):
            raise ProductionEngineError(
                "unsafe_engine_report", "Engine report failed its safety check.", stage="collect"
            )
        _atomic_json(report_path, report)
        report_artifact = _artifact_metadata(
            root, report_path, "engine_report.json", "application/json"
        )
        all_artifacts = (*artifacts, report_artifact)
        return ProductionEngineResult(
            engine_name=ENGINE_NAME,
            engine_version=ENGINE_VERSION,
            engine_commit=ENGINE_COMMIT,
            mode=ENGINE_MODE,
            task_id=task_id,
            artifacts=all_artifacts,
            report_path=report_path.relative_to(root).as_posix(),
        )

    @staticmethod
    def _validate_input(**values: Any) -> None:
        if values["approved"] is not True:
            raise ProductionEngineError(
                "approval_required",
                "A current human compliance approval is required.",
                stage="authorization",
            )
        script = values["script"]
        if not isinstance(script, str) or not script.strip() or len(script) > 8000 or _CONTROL_RE.search(script):
            raise ProductionEngineError(
                "invalid_script", "Approved script is invalid.", stage="validation"
            )
        keywords = values["keywords"]
        if type(keywords) is not list or not 1 <= len(keywords) <= 24:
            raise ProductionEngineError(
                "invalid_keywords", "Keywords must be a non-empty string list.", stage="validation"
            )
        if any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > 200
            or _CONTROL_RE.search(item)
            for item in keywords
        ):
            raise ProductionEngineError(
                "invalid_keywords", "Keywords contain an invalid item.", stage="validation"
            )
        if values["aspect"] != "portrait":
            raise ProductionEngineError(
                "invalid_aspect", "Only portrait output is allowed.", stage="validation"
            )
        duration = values["target_duration_seconds"]
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 45 <= duration <= 60:
            raise ProductionEngineError(
                "invalid_duration", "Target duration must be between 45 and 60 seconds.", stage="validation"
            )
        if values["material_strategy"] not in _MATERIAL_STRATEGIES:
            raise ProductionEngineError(
                "invalid_material_strategy", "Material strategy is not allowed.", stage="validation"
            )
        if values["voice_strategy"] not in _VOICE_STRATEGIES:
            raise ProductionEngineError(
                "invalid_voice_strategy", "Voice strategy is not allowed.", stage="validation"
            )

    @staticmethod
    def _payload(
        script: str,
        keywords: list[str],
        material_strategy: str,
        voice_strategy: str,
        local_materials: tuple[Path, ...],
    ) -> dict[str, Any]:
        payload = {
            "video_subject": keywords[0],
            "video_script": script,
            "video_terms": list(keywords),
            "video_aspect": "9:16",
            "video_concat_mode": "sequential",
            "video_transition_mode": None,
            "video_clip_duration": 5,
            "match_materials_to_script": True,
            "video_count": 1,
            "video_source": material_strategy,
            "voice_name": _VOICE_STRATEGIES[voice_strategy],
            "voice_rate": 1.0,
            "bgm_type": "none",
            "bgm_file": "",
            "bgm_volume": 0,
            "subtitle_enabled": True,
            "font_name": "NotoSansSC-Regular.ttf",
        }
        if material_strategy == "local":
            payload["video_materials"] = [
                {"provider": "local", "url": str(path), "duration": 0}
                for path in local_materials
            ]
        return payload

    def _resolve_local_materials(
        self,
        material_strategy: str,
        local_material_paths: list[Path] | None,
    ) -> tuple[Path, ...]:
        if material_strategy != "local":
            if local_material_paths is not None:
                raise ProductionEngineError(
                    "unexpected_local_materials",
                    "Online material strategies cannot carry local file paths.",
                    stage="validation",
                )
            return ()
        if self.local_material_root is None:
            raise ProductionEngineError(
                "local_material_root_required",
                "Local material strategy requires a configured material root.",
                stage="validation",
            )
        raw_root = Path(os.path.abspath(self.local_material_root))
        try:
            unsafe_root = _is_reparse_point(raw_root)
        except OSError as exc:
            raise ProductionEngineError(
                "invalid_local_material_root",
                "Local material root does not exist.",
                stage="validation",
            ) from exc
        if unsafe_root:
            raise ProductionEngineError(
                "unsafe_local_material_root",
                "Local material root cannot be a symbolic link or junction.",
                stage="validation",
            )
        try:
            root = raw_root.resolve(strict=True)
        except OSError as exc:
            raise ProductionEngineError(
                "invalid_local_material_root",
                "Local material root does not exist.",
                stage="validation",
            ) from exc
        if not root.is_dir():
            raise ProductionEngineError(
                "invalid_local_material_root",
                "Local material root must be a directory.",
                stage="validation",
            )
        if type(local_material_paths) is not list or not 1 <= len(local_material_paths) <= 24:
            raise ProductionEngineError(
                "invalid_local_materials",
                "Local material strategy requires 1 to 24 MP4 files.",
                stage="validation",
            )
        resolved_paths: list[Path] = []
        seen: list[Path] = []
        for raw_path in local_material_paths:
            if not isinstance(raw_path, (str, os.PathLike)):
                raise ProductionEngineError(
                    "invalid_local_materials",
                    "Local material path is invalid.",
                    stage="validation",
                )
            supplied = Path(raw_path)
            candidate = supplied if supplied.is_absolute() else raw_root / supplied
            lexical = Path(os.path.abspath(candidate))
            anchored = _samefile_root_anchor(root, lexical)
            if anchored is None:
                raise ProductionEngineError(
                    "local_material_outside_root",
                    "Local material must stay within the configured root.",
                    stage="validation",
                )
            anchor, relative_parts = anchored
            cursor = anchor
            try:
                path_chain = [cursor]
                for part in relative_parts:
                    cursor = cursor / part
                    path_chain.append(cursor)
                if any(_is_reparse_point(path) for path in path_chain):
                    raise ProductionEngineError(
                        "local_material_symlink",
                        "Symbolic links and junctions are not allowed as local materials.",
                        stage="validation",
                    )
            except ProductionEngineError:
                raise
            except OSError as exc:
                raise ProductionEngineError(
                    "local_material_outside_root",
                    "Local material is missing or outside the configured root.",
                    stage="validation",
                ) from exc
            try:
                resolved = lexical.resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ProductionEngineError(
                    "local_material_outside_root",
                    "Local material is missing or outside the configured root.",
                    stage="validation",
                ) from exc
            try:
                resolved.relative_to(root)
            except ValueError:
                if _samefile_root_anchor(root, resolved) is None:
                    raise ProductionEngineError(
                        "local_material_outside_root",
                        "Local material is missing or outside the configured root.",
                        stage="validation",
                    )
            if not resolved.is_file() or resolved.suffix.lower() != ".mp4":
                raise ProductionEngineError(
                    "invalid_local_material",
                    "Local material must be an existing MP4 file.",
                    stage="validation",
                )
            if any(os.path.samefile(resolved, previous) for previous in seen):
                raise ProductionEngineError(
                    "duplicate_local_material",
                    "Duplicate local material is not allowed.",
                    stage="validation",
                )
            seen.append(resolved)
            resolved_paths.append(resolved)
        return tuple(resolved_paths)

    def _url(self, suffix: str) -> str:
        return f"{self.base_url}/{suffix.lstrip('/')}"

    def _remaining(self, deadline: float) -> float:
        return max(0.001, min(30.0, deadline - self.clock()))

    def _request_json(
        self,
        method: str,
        url: str,
        payload: Mapping[str, Any] | None,
        deadline: float,
    ) -> Mapping[str, Any]:
        if self.clock() >= deadline:
            raise ProductionEngineError(
                "engine_timeout", "MPT task timed out.", stage="transport", retryable=True
            )
        try:
            response = self.transport.request_json(
                method,
                _validate_engine_url(url, self.expected_origin),
                payload=payload,
                timeout_seconds=self._remaining(deadline),
            )
        except ProductionEngineError:
            raise
        except Exception as exc:
            raise ProductionEngineError(
                "engine_unavailable",
                "MPT local service is unavailable.",
                stage="transport",
                retryable=True,
            ) from exc
        if not isinstance(response, JsonTransportResponse):
            raise ProductionEngineError(
                "invalid_engine_response", "MPT transport returned an invalid response.", stage="transport"
            )
        _validate_engine_url(response.final_url, self.expected_origin)
        if not isinstance(response.payload, Mapping):
            raise ProductionEngineError(
                "invalid_engine_response", "MPT returned an invalid response.", stage="transport"
            )
        return response.payload

    @staticmethod
    def _response_data(response: Mapping[str, Any], stage: str) -> Mapping[str, Any]:
        status = response.get("status")
        data = response.get("data")
        if isinstance(status, bool) or not isinstance(status, int) or status != 200 or not isinstance(data, Mapping):
            raise ProductionEngineError(
                "invalid_engine_response", "MPT returned an invalid response.", stage=stage
            )
        return data

    @staticmethod
    def _task_id(value: Any) -> str:
        if not isinstance(value, str) or not _TASK_ID_RE.fullmatch(value):
            raise ProductionEngineError(
                "invalid_task_id", "MPT returned an invalid task identifier.", stage="submit"
            )
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ProductionEngineError(
                "invalid_task_id", "MPT returned an invalid task identifier.", stage="submit"
            ) from exc
        if str(parsed) != value:
            raise ProductionEngineError(
                "invalid_task_id", "MPT returned an invalid task identifier.", stage="submit"
            )
        return value

    def _poll(self, task_id: str, deadline: float, cancel_event: Any | None) -> Mapping[str, Any]:
        task_url = self._url(f"tasks/{task_id}")
        while True:
            if cancel_event is not None and bool(cancel_event.is_set()):
                self._best_effort_cancel(task_url, deadline)
                raise ProductionEngineError(
                    "engine_cancelled", "MPT task was cancelled.", stage="poll"
                )
            if self.clock() >= deadline:
                self._best_effort_cancel(task_url, deadline)
                raise ProductionEngineError(
                    "engine_timeout", "MPT task timed out.", stage="poll", retryable=True
                )
            response = self._request_json("GET", task_url, None, deadline)
            data = self._response_data(response, "poll")
            if data.get("task_id") != task_id:
                raise ProductionEngineError(
                    "task_identity_mismatch", "MPT task identity changed.", stage="poll"
                )
            state = data.get("state")
            progress = data.get("progress")
            if isinstance(state, bool) or not isinstance(state, int) or state not in _TASK_STATES:
                raise ProductionEngineError(
                    "invalid_engine_response", "MPT returned an invalid task state.", stage="poll"
                )
            if isinstance(progress, bool) or not isinstance(progress, int) or not 0 <= progress <= 100:
                raise ProductionEngineError(
                    "invalid_engine_response", "MPT returned invalid task progress.", stage="poll"
                )
            if state == -1:
                raw_stage = data.get("failed_stage")
                safe_stage = raw_stage if raw_stage in _SAFE_FAILURE_STAGES else "unknown"
                raise ProductionEngineError(
                    "engine_task_failed", "MPT task failed.", stage=safe_stage
                )
            if state == 1:
                if progress != 100:
                    raise ProductionEngineError(
                        "invalid_engine_response", "MPT completed with invalid progress.", stage="poll"
                    )
                audio_duration = data.get("audio_duration")
                if (
                    isinstance(audio_duration, bool)
                    or not isinstance(audio_duration, (int, float))
                    or not 35 <= audio_duration <= 75
                ):
                    raise ProductionEngineError(
                        "unsafe_media_duration",
                        "MPT narration is outside the 35 to 75 second safe adjustment range.",
                        stage="poll",
                    )
                return data
            self.sleeper(min(self.poll_interval_seconds, max(0, deadline - self.clock())))

    def _best_effort_cancel(self, task_url: str, deadline: float) -> None:
        try:
            self.transport.request_json(
                "DELETE",
                task_url,
                payload=None,
                timeout_seconds=max(0.001, min(2.0, deadline - self.clock())),
            )
        except Exception:
            pass

    def _collect(
        self,
        root: Path,
        private_dir: Path,
        task_id: str,
        status: Mapping[str, Any],
        deadline: float,
        material_strategy: str,
        local_materials: tuple[Path, ...],
    ) -> dict[str, Path]:
        videos = status.get("videos")
        if type(videos) is not list or len(videos) != 1 or not isinstance(videos[0], str):
            raise ProductionEngineError(
                "invalid_engine_artifacts", "MPT returned an invalid video list.", stage="collect"
            )
        final_url = self._artifact_download_url(videos[0], task_id, "final-1.mp4")
        audio_url = self._artifact_download_url(status.get("audio_file"), task_id, "audio.mp3")
        subtitle_url = self._artifact_download_url(status.get("subtitle_path"), task_id, "subtitle.srt")

        final_path = root / "final.mp4"
        audio_path = private_dir / "audio.mp3"
        subtitle_path = root / "captions.srt"
        source_path = root / "material_sources.json"
        self._download(final_url, final_path, _MAX_VIDEO_BYTES, deadline)
        self._download(audio_url, audio_path, _MAX_AUDIO_BYTES, deadline)
        self._download(subtitle_url, subtitle_path, _MAX_SUBTITLE_BYTES, deadline)
        if material_strategy == "local":
            sources = [
                {
                    "basename": path.name,
                    "size": path.stat().st_size,
                    "sha256": _file_sha256(path),
                    "source_type": "local_user_supplied",
                }
                for path in local_materials
            ]
            _atomic_json(
                source_path,
                {"schema_version": 1, "task_id": task_id, "sources": sources},
            )
        else:
            script_url = self._url(f"download/{task_id}/script.json")
            raw_script_path = private_dir / ".script.json.part"
            try:
                self._download(script_url, raw_script_path, _MAX_JSON_BYTES, deadline)
                try:
                    raw_script = json.loads(raw_script_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProductionEngineError(
                        "invalid_material_sources",
                        "MPT material source manifest is invalid.",
                        stage="collect",
                    ) from exc
                if not isinstance(raw_script, dict):
                    raise ProductionEngineError(
                        "invalid_material_sources",
                        "MPT material source manifest is invalid.",
                        stage="collect",
                    )
                sources = _sanitize_material_sources(
                    raw_script.get("material_sources"), material_strategy
                )
                _atomic_json(
                    source_path,
                    {"schema_version": 1, "task_id": task_id, "sources": sources},
                )
            finally:
                raw_script_path.unlink(missing_ok=True)
        return {
            "final.mp4": final_path,
            "audio.mp3": audio_path,
            "captions.srt": subtitle_path,
            "material_sources.json": source_path,
        }

    def _artifact_download_url(self, reference: Any, task_id: str, filename: str) -> str:
        if not isinstance(reference, str) or not reference.strip():
            raise ProductionEngineError(
                "invalid_engine_artifacts", "MPT artifact reference is invalid.", stage="collect"
            )
        text = reference.strip()
        _reject_path_traversal(text)
        if text.startswith(("http://", "https://", "/")):
            candidate = text if text.startswith(("http://", "https://")) else urljoin(self.base_url, text)
            _validate_engine_url(candidate, self.expected_origin)
            parts = [part for part in _decoded_path(urlsplit(candidate).path).split("/") if part]
        else:
            parts = [part for part in _decoded_path(text).split("/") if part]
        if not parts or parts[-1] != filename:
            raise ProductionEngineError(
                "invalid_engine_artifacts", "MPT artifact filename is invalid.", stage="collect"
            )
        if len(parts) > 1 and task_id not in parts:
            raise ProductionEngineError(
                "task_identity_mismatch", "MPT artifact belongs to another task.", stage="collect"
            )
        return self._url(f"download/{task_id}/{quote(filename)}")

    def _download(self, url: str, destination: Path, max_bytes: int, deadline: float) -> None:
        if destination.exists() and destination.is_symlink():
            raise ProductionEngineError(
                "unsafe_staging", "Engine artifact destination is unsafe.", stage="collect"
            )
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
        try:
            response = self.transport.download(
                _validate_engine_url(url, self.expected_origin),
                temporary,
                timeout_seconds=self._remaining(deadline),
                max_bytes=max_bytes,
            )
            if not isinstance(response, DownloadTransportResponse):
                raise ProductionEngineError(
                    "invalid_engine_response",
                    "MPT transport returned invalid download metadata.",
                    stage="collect",
                )
            _validate_engine_url(response.final_url, self.expected_origin)
            actual_size = temporary.stat().st_size if temporary.is_file() else -1
            if response.size != actual_size or not 0 < actual_size <= max_bytes:
                raise ProductionEngineError(
                    "invalid_engine_artifact", "MPT artifact size is invalid.", stage="collect"
                )
            os.replace(temporary, destination)
        except ProductionEngineError:
            raise
        except Exception as exc:
            raise ProductionEngineError(
                "artifact_download_failed",
                "MPT artifact download failed.",
                stage="collect",
                retryable=True,
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            _SECRET_KEY_RE.search(str(key)) is not None or _contains_secret_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_key(item) for item in value)
    return False


__all__ = [
    "ENGINE_COMMIT",
    "ENGINE_MODE",
    "ENGINE_NAME",
    "ENGINE_REQUIRED_ENV",
    "ENGINE_VERSION",
    "DownloadTransportResponse",
    "JsonTransportResponse",
    "ProductionEngineAdapter",
    "ProductionEngineArtifact",
    "ProductionEngineError",
    "ProductionEngineResult",
    "ProductionEngineTransport",
    "UrllibProductionEngineTransport",
    "validate_engine_base_url",
]
