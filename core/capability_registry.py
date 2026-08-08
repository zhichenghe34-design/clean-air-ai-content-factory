from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .capability_pack import validate_capability_pack


_PACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_BYTES = 64 * 1024
_POINTER_FIELDS = {
    "schema_version",
    "id",
    "sha256",
    "artifact_sha256",
    "version",
    "generated_at",
}


class CapabilityPackRegistryError(ValueError):
    """Base error for registry paths, storage, and integrity failures."""

    status = 422
    code = "invalid_capability_pack_registry_request"


class CapabilityPackConflictError(CapabilityPackRegistryError):
    """Raised when immutable content already exists under a different value."""

    status = 409
    code = "capability_pack_conflict"


class CapabilityPackNotFoundError(CapabilityPackRegistryError):
    """Raised when a requested pack or version does not exist."""

    status = 404
    code = "capability_pack_not_found"


class CapabilityPackIntegrityError(CapabilityPackConflictError):
    """Raised when on-disk registry content is malformed or has been changed."""

    code = "capability_pack_integrity_error"


def _canonical_bytes(value: Any) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CapabilityPackRegistryError("registry content must be canonical JSON") from exc
    return payload.encode("utf-8")


def _artifact_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_pack_id(value: object) -> str:
    if not isinstance(value, str) or not _PACK_ID_RE.fullmatch(value):
        raise CapabilityPackRegistryError("pack_id must be a safe lowercase identifier")
    return value


def _validate_sha256(value: object) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise CapabilityPackRegistryError("sha256 must be 64 lowercase hexadecimal characters")
    return value


class CapabilityPackRegistry:
    """Publish and retrieve immutable, declarative capability-pack snapshots.

    Pack content is never imported or executed. Every read is parsed as JSON and
    sent back through ``validate_capability_pack`` before it is returned.
    """

    def __init__(self, runtime_root: str | os.PathLike[str]):
        root = Path(runtime_root).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        self.runtime_root = root.resolve()
        self.registry_root = self.runtime_root / "capability-packs"
        self.registry_root.mkdir(parents=True, exist_ok=True)
        self.registry_root = self.registry_root.resolve()
        self._assert_within(self.registry_root, self.runtime_root)

    def publish(self, pack: object) -> dict[str, Any]:
        """Validate and atomically publish one immutable capability pack."""

        validated = validate_capability_pack(pack)
        pack_id = _validate_pack_id(validated["id"])
        snapshot_sha = _validate_sha256(validated["sha256"])
        payload = _canonical_bytes(validated)
        artifact_sha = _artifact_sha256(payload)
        pack_dir = self._pack_dir(pack_id, create=True)
        target = self._pack_path(pack_dir, snapshot_sha)

        with self._publish_lock(pack_id):
            if target.exists() or target.is_symlink():
                if target.is_symlink():
                    raise CapabilityPackIntegrityError("immutable pack path may not be a symbolic link")
                existing = self._read_raw(target, pack_dir)
                if existing != payload:
                    raise CapabilityPackConflictError(
                        "a different immutable document already exists for this snapshot hash"
                    )
                self._decode_pack(existing, expected_id=pack_id, expected_sha=snapshot_sha)
            else:
                self._atomic_replace(
                    target,
                    payload,
                    validator=lambda staged: self._validate_staged_pack(
                        staged,
                        expected_payload=payload,
                        expected_id=pack_id,
                        expected_sha=snapshot_sha,
                    ),
                )

            pointer = {
                "schema_version": 1,
                "id": pack_id,
                "sha256": snapshot_sha,
                "artifact_sha256": artifact_sha,
                "version": validated["version"],
                "generated_at": validated["generated_at"],
            }
            self._publish_latest(pack_dir, pointer)

        return self.get(pack_id, snapshot_sha)

    def get(self, pack_id: object, sha256: object | None = None) -> dict[str, Any]:
        """Return a validated pack, using the atomically published latest pointer by default."""

        safe_id = _validate_pack_id(pack_id)
        pack_dir = self._pack_dir(safe_id, create=False)
        if not pack_dir.exists():
            raise CapabilityPackNotFoundError(f"capability pack not found: {safe_id}")

        expected_artifact_sha: str | None = None
        if sha256 is None:
            pointer = self._read_pointer(pack_dir, expected_id=safe_id)
            safe_sha = pointer["sha256"]
            expected_artifact_sha = pointer["artifact_sha256"]
        else:
            safe_sha = _validate_sha256(sha256)
            latest_path = pack_dir / "latest.json"
            if latest_path.exists() and not latest_path.is_symlink():
                try:
                    pointer = self._read_pointer(pack_dir, expected_id=safe_id)
                except CapabilityPackRegistryError:
                    pointer = None
                if pointer and pointer["sha256"] == safe_sha:
                    expected_artifact_sha = pointer["artifact_sha256"]

        target = self._pack_path(pack_dir, safe_sha)
        if not target.exists() or target.is_symlink():
            if target.is_symlink():
                raise CapabilityPackIntegrityError("immutable pack path may not be a symbolic link")
            raise CapabilityPackNotFoundError(f"capability pack version not found: {safe_id}@{safe_sha}")
        raw = self._read_raw(target, pack_dir)
        if expected_artifact_sha is not None and _artifact_sha256(raw) != expected_artifact_sha:
            raise CapabilityPackIntegrityError("capability pack artifact hash mismatch")
        return self._decode_pack(raw, expected_id=safe_id, expected_sha=safe_sha)

    def list(self) -> list[dict[str, Any]]:
        """Return non-sensitive summaries without exposing goals, rules, or evidence text."""

        summaries: list[dict[str, Any]] = []
        if not self.registry_root.exists():
            return summaries
        for candidate in sorted(self.registry_root.iterdir(), key=lambda path: path.name):
            if candidate.name.startswith(".") or candidate.is_symlink() or not candidate.is_dir():
                continue
            try:
                pack_id = _validate_pack_id(candidate.name)
                pack_dir = self._pack_dir(pack_id, create=False)
                pack = self.get(pack_id)
                versions = sum(
                    1
                    for path in pack_dir.glob("*.json")
                    if path.name != "latest.json" and _SHA256_RE.fullmatch(path.stem) and not path.is_symlink()
                )
                summaries.append(
                    {
                        "id": pack["id"],
                        "version": pack["version"],
                        "sha256": pack["sha256"],
                        "label": pack["snapshot"]["label"],
                        "industry": pack["snapshot"]["industry"],
                        "risk_level": pack["snapshot"]["risk_level"],
                        "generated_at": pack["generated_at"],
                        "audit_status": pack["audit"]["status"],
                        "version_count": versions,
                        "integrity": "verified",
                    }
                )
            except (CapabilityPackRegistryError, OSError, UnicodeError, json.JSONDecodeError):
                summaries.append({"id": candidate.name, "integrity": "invalid"})
        return summaries

    def _pack_dir(self, pack_id: str, *, create: bool) -> Path:
        safe_id = _validate_pack_id(pack_id)
        candidate = self.registry_root / safe_id
        if create:
            candidate.mkdir(parents=False, exist_ok=True)
        resolved = candidate.resolve(strict=False)
        self._assert_within(resolved, self.registry_root)
        if resolved.parent != self.registry_root:
            raise CapabilityPackRegistryError("pack path escaped the registry root")
        return resolved

    @staticmethod
    def _pack_path(pack_dir: Path, snapshot_sha: str) -> Path:
        safe_sha = _validate_sha256(snapshot_sha)
        target = pack_dir / f"{safe_sha}.json"
        if target.parent != pack_dir:
            raise CapabilityPackRegistryError("invalid capability pack path")
        return target

    @staticmethod
    def _assert_within(path: Path, parent: Path) -> None:
        try:
            path.relative_to(parent)
        except ValueError as exc:
            raise CapabilityPackRegistryError("registry path escaped its configured root") from exc

    @contextmanager
    def _publish_lock(self, pack_id: str) -> Iterator[None]:
        lock_path = self.registry_root / f".{pack_id}.publish.lock"
        token = f"{os.getpid()}:{uuid.uuid4().hex}".encode("ascii")
        deadline = time.monotonic() + 3.0
        while True:
            try:
                descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as exc:
                if time.monotonic() >= deadline:
                    raise CapabilityPackConflictError("capability pack publication is busy") from exc
                time.sleep(0.02)
                continue
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(token)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                raise
            break
        try:
            yield
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def _publish_latest(self, pack_dir: Path, pointer: dict[str, Any]) -> None:
        normalized = self._validate_pointer(pointer, expected_id=pointer["id"])
        payload = _canonical_bytes(normalized)
        target = pack_dir / "latest.json"
        if target.is_symlink():
            raise CapabilityPackIntegrityError("latest pointer may not be a symbolic link")
        if target.exists():
            existing = self._read_raw(target, pack_dir)
            if existing == payload:
                self._decode_pointer(existing, expected_id=pointer["id"])
                return
        self._atomic_replace(
            target,
            payload,
            validator=lambda staged: self._validate_staged_pointer(
                staged,
                expected_payload=payload,
                expected_id=pointer["id"],
            ),
        )

    def _atomic_replace(self, target: Path, payload: bytes, *, validator: Any) -> None:
        staging = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.staging"
        try:
            with staging.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            validator(staging)
            os.replace(staging, target)
        finally:
            try:
                staging.unlink()
            except FileNotFoundError:
                pass

    def _validate_staged_pack(
        self,
        staging: Path,
        *,
        expected_payload: bytes,
        expected_id: str,
        expected_sha: str,
    ) -> None:
        raw = self._read_raw(staging, staging.parent, allow_staging=True)
        if raw != expected_payload:
            raise CapabilityPackIntegrityError("staged capability pack changed before publication")
        self._decode_pack(raw, expected_id=expected_id, expected_sha=expected_sha)

    def _validate_staged_pointer(
        self,
        staging: Path,
        *,
        expected_payload: bytes,
        expected_id: str,
    ) -> None:
        raw = self._read_raw(staging, staging.parent, allow_staging=True)
        if raw != expected_payload:
            raise CapabilityPackIntegrityError("staged latest pointer changed before publication")
        self._decode_pointer(raw, expected_id=expected_id)

    def _read_pointer(self, pack_dir: Path, *, expected_id: str) -> dict[str, Any]:
        pointer_path = pack_dir / "latest.json"
        if not pointer_path.exists() or pointer_path.is_symlink():
            if pointer_path.is_symlink():
                raise CapabilityPackIntegrityError("latest pointer may not be a symbolic link")
            raise CapabilityPackNotFoundError(f"capability pack has no latest version: {expected_id}")
        return self._decode_pointer(self._read_raw(pointer_path, pack_dir), expected_id=expected_id)

    def _read_raw(self, path: Path, parent: Path, *, allow_staging: bool = False) -> bytes:
        if path.is_symlink():
            raise CapabilityPackIntegrityError("registry files may not be symbolic links")
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise CapabilityPackNotFoundError(f"registry file not found: {path.name}") from exc
        if resolved.parent != parent.resolve():
            raise CapabilityPackIntegrityError("registry file escaped its pack directory")
        if not allow_staging and path.name.startswith("."):
            raise CapabilityPackIntegrityError("staging files are not public registry entries")
        with resolved.open("rb") as stream:
            payload = stream.read(_MAX_JSON_BYTES + 1)
        if len(payload) > _MAX_JSON_BYTES:
            raise CapabilityPackIntegrityError("registry JSON exceeds the size limit")
        return payload

    @staticmethod
    def _decode_json(payload: bytes) -> Any:
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CapabilityPackIntegrityError("registry file is not valid UTF-8 JSON") from exc
        if _canonical_bytes(value) != payload:
            raise CapabilityPackIntegrityError("registry JSON is not in canonical form")
        return value

    def _decode_pack(self, payload: bytes, *, expected_id: str, expected_sha: str) -> dict[str, Any]:
        value = self._decode_json(payload)
        try:
            validated = validate_capability_pack(value)
        except (TypeError, ValueError) as exc:
            raise CapabilityPackIntegrityError("stored capability pack failed validation") from exc
        if validated["id"] != expected_id or validated["sha256"] != expected_sha:
            raise CapabilityPackIntegrityError("stored capability pack identity does not match its path")
        return validated

    def _decode_pointer(self, payload: bytes, *, expected_id: str) -> dict[str, Any]:
        value = self._decode_json(payload)
        return self._validate_pointer(value, expected_id=expected_id)

    @staticmethod
    def _validate_pointer(pointer: object, *, expected_id: str) -> dict[str, Any]:
        if not isinstance(pointer, dict) or set(pointer) != _POINTER_FIELDS:
            raise CapabilityPackIntegrityError("latest pointer has an invalid schema")
        if pointer.get("schema_version") != 1:
            raise CapabilityPackIntegrityError("latest pointer has an unsupported schema version")
        if _validate_pack_id(pointer.get("id")) != expected_id:
            raise CapabilityPackIntegrityError("latest pointer refers to a different pack")
        _validate_sha256(pointer.get("sha256"))
        _validate_sha256(pointer.get("artifact_sha256"))
        if not isinstance(pointer.get("version"), str) or not 1 <= len(pointer["version"]) <= 32:
            raise CapabilityPackIntegrityError("latest pointer has an invalid version")
        if not isinstance(pointer.get("generated_at"), str) or not pointer["generated_at"]:
            raise CapabilityPackIntegrityError("latest pointer has an invalid generated_at value")
        return dict(pointer)
