from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path


PATCH_ID = "shiyi-moviepy-windows-mf"
PATCH_VERSION = "1.0.0"
DISTRIBUTION_NAME = "moviepy"
DISTRIBUTION_VERSION = "2.2.1"
MODULE_REPORTED_VERSION = "2.1.2"

WRITER_RELATIVE_PATH = Path("moviepy/video/io/ffmpeg_writer.py")
DIST_INFO_RELATIVE_PATH = Path("moviepy-2.2.1.dist-info")
RECORD_RELATIVE_PATH = DIST_INFO_RELATIVE_PATH / "RECORD"
METADATA_RELATIVE_PATH = DIST_INFO_RELATIVE_PATH / "METADATA"
LICENSE_RELATIVE_PATH = DIST_INFO_RELATIVE_PATH / "licenses/LICENCE.txt"
MODULE_VERSION_RELATIVE_PATH = Path("moviepy/version.py")

UPSTREAM_WRITER_SHA256 = "347E9EE5403A0CBFFDDF6205D7DE9A8B38708BDC9853F22383CFE4987AFA62D3"
PATCHED_WRITER_SHA256 = "DFE76CD8AED151B99881DD01FA2BC1E040D0788EC364A8C6EF14020F2009D8B9"
UPSTREAM_RECORD_SHA256 = "F8D61AAAE58D557D0F67AF5016B5AD15D791A9D79E3B7121BC3CD9C296D78ED8"
PATCHED_RECORD_SHA256 = "4D836329F0D7804F389AED64FB102A48A614821793F5D7C7E419D11EB7A574C5"
METADATA_SHA256 = "BA08814030A33C196589CB232D87518B2BE874B342E8E5F6DFEFB464E52E5569"
LICENSE_SHA256 = "05ECC6144AF83D1B87B64F3E36F4367349AC4486AFBF29E57341B7C4745DD38A"
MODULE_VERSION_SHA256 = "522B817115CFB57C4F5010DDA74643BD697453D0DBD6430D7D3DE801F871A8B8"

UPSTREAM_WRITER_BYTES = 11407
UPSTREAM_RECORD_BYTES = 7281
PATCHED_WRITER_BYTES = 12362
PATCHED_RECORD_BYTES = 7281

MF_ARGS = (
    "-c:v",
    "h264_mf",
    "-rate_control",
    "quality",
    "-quality",
    "72",
    "-scenario",
    "archive",
    "-hw_encoding",
    "0",
    "-bf",
    "0",
    "-pix_fmt",
    "yuv420p",
)


class PatchContractError(RuntimeError):
    pass


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def _record_digest(payload: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode("ascii")


def _replace_exact(payload: bytes, old: bytes, new: bytes, *, label: str) -> bytes:
    count = payload.count(old)
    if count != 1:
        raise PatchContractError(f"{label}: expected exactly one byte-for-byte match, found {count}")
    return payload.replace(old, new, 1)


def _patched_writer_payload(upstream: bytes) -> bytes:
    if _sha256_bytes(upstream) != UPSTREAM_WRITER_SHA256 or len(upstream) != UPSTREAM_WRITER_BYTES:
        raise PatchContractError("ffmpeg_writer.py is not the frozen upstream payload")

    notice_anchor = b'"""\n\nimport subprocess as sp\n'
    notice = (
        b'"""\n\n'
        b"# MIT modification notice: Shanghai Shiyi Brand Management Co., Ltd. modified this file\n"
        + f"# via {PATCH_ID} v{PATCH_VERSION} for the fixed Windows Media Foundation H.264 policy.\n".encode(
            "ascii"
        )
        + b"# This is not the unmodified upstream MoviePy file.\n"
        + b"import subprocess as sp\n"
    )
    payload = _replace_exact(
        upstream,
        notice_anchor,
        notice,
        label="embed the MIT modified-file notice",
    )

    encoder_anchor = (
        b'        if codec == "h264_nvenc":\n'
        b'            cmd.extend(["-c:v", codec])\n'
        b"        else:\n"
        b'            cmd.extend(["-vcodec", codec])\n'
        b"\n"
        b'        cmd.extend(["-preset", preset])\n'
    )
    encoder_replacement = (
        b'        if codec == "h264_mf":\n'
        b"            # The complete encoder policy is appended after caller-supplied options.\n"
        b"            pass\n"
        b'        elif codec == "h264_nvenc":\n'
        b'            cmd.extend(["-c:v", codec])\n'
        b"        else:\n"
        b'            cmd.extend(["-vcodec", codec])\n'
        b"\n"
        b'        if codec != "h264_mf":\n'
        b'            cmd.extend(["-preset", preset])\n'
    )
    payload = _replace_exact(
        payload,
        encoder_anchor,
        encoder_replacement,
        label="suppress the unsupported preset for h264_mf",
    )

    bitrate_anchor = b"        if bitrate is not None:\n            cmd.extend([\"-b\", bitrate])\n"
    payload = _replace_exact(
        payload,
        bitrate_anchor,
        b'        if bitrate is not None and codec != "h264_mf":\n'
        b'            cmd.extend(["-b", bitrate])\n',
        label="keep h264_mf on its frozen quality policy",
    )

    threads_anchor = (
        b"        if threads is not None:\n"
        b'            cmd.extend(["-threads", str(threads)])\n'
        b"\n"
        b"        # Disable auto alt ref for transparent webm and set pix format yo yuva420p\n"
    )
    mf_policy = (
        b"        if threads is not None:\n"
        b'            cmd.extend(["-threads", str(threads)])\n'
        b"\n"
        b'        if codec == "h264_mf":\n'
        b"            cmd.extend(\n"
        b"                [\n"
        b'                    "-c:v",\n'
        b'                    "h264_mf",\n'
        b'                    "-rate_control",\n'
        b'                    "quality",\n'
        b'                    "-quality",\n'
        b'                    "72",\n'
        b'                    "-scenario",\n'
        b'                    "archive",\n'
        b'                    "-hw_encoding",\n'
        b'                    "0",\n'
        b'                    "-bf",\n'
        b'                    "0",\n'
        b'                    "-pix_fmt",\n'
        b'                    "yuv420p",\n'
        b"                ]\n"
        b"            )\n"
        b"\n"
        b"        # Disable auto alt ref for transparent webm and set pix format yo yuva420p\n"
    )
    payload = _replace_exact(
        payload,
        threads_anchor,
        mf_policy,
        label="append the fixed h264_mf encoder policy",
    )

    decoded = payload.decode("utf-8")
    for sentinel in (
        f"MIT modification notice: Shanghai Shiyi Brand Management Co., Ltd. modified this file",
        'if codec != "h264_mf":\n            cmd.extend(["-preset", preset])',
        '"-rate_control",\n                    "quality"',
        '"-scenario",\n                    "archive"',
        '"-hw_encoding",\n                    "0"',
        '"-pix_fmt",\n                    "yuv420p"',
    ):
        if sentinel not in decoded:
            raise PatchContractError(f"patched writer is missing sentinel: {sentinel}")
    return payload


def _record_line(writer_payload: bytes) -> bytes:
    return (
        f"moviepy/video/io/ffmpeg_writer.py,sha256={_record_digest(writer_payload)},{len(writer_payload)}\n"
    ).encode("ascii")


def _patched_record_payload(upstream_record: bytes, patched_writer: bytes) -> bytes:
    if _sha256_bytes(upstream_record) != UPSTREAM_RECORD_SHA256 or len(upstream_record) != UPSTREAM_RECORD_BYTES:
        raise PatchContractError("RECORD is not the frozen upstream payload")
    upstream_line = (
        b"moviepy/video/io/ffmpeg_writer.py,"
        b"sha256=NH6e5UA6DL_932IF196aizhwi9yYU_Ijg8_kmHr6YtM,11407\n"
    )
    return _replace_exact(
        upstream_record,
        upstream_line,
        _record_line(patched_writer),
        label="update RECORD for the patched ffmpeg_writer.py",
    )


def _read_exact(path: Path, expected_sha256: str, *, label: str) -> bytes:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise PatchContractError(f"{label} is unreadable: {path}") from exc
    actual = _sha256_bytes(payload)
    if actual != expected_sha256:
        raise PatchContractError(f"{label} hash mismatch: expected {expected_sha256}, got {actual}")
    return payload


def _load_runtime(python_runtime: Path) -> dict[str, object]:
    site_packages = python_runtime / "Lib" / "site-packages"
    paths = {
        "writer": site_packages / WRITER_RELATIVE_PATH,
        "record": site_packages / RECORD_RELATIVE_PATH,
        "metadata": site_packages / METADATA_RELATIVE_PATH,
        "license": site_packages / LICENSE_RELATIVE_PATH,
        "module_version": site_packages / MODULE_VERSION_RELATIVE_PATH,
    }
    for label, path in paths.items():
        if not path.is_file():
            raise PatchContractError(f"required MoviePy {label} file is missing: {path}")

    metadata = _read_exact(paths["metadata"], METADATA_SHA256, label="MoviePy METADATA")
    for field in (b"Name: moviepy\n", b"Version: 2.2.1\n", b"License: MIT License\n"):
        if field not in metadata:
            raise PatchContractError(f"MoviePy METADATA is missing frozen field: {field!r}")
    _read_exact(paths["license"], LICENSE_SHA256, label="MoviePy MIT license")
    module_version = _read_exact(
        paths["module_version"], MODULE_VERSION_SHA256, label="MoviePy module version"
    )
    if module_version != b'__version__ = "2.1.2"\n':
        raise PatchContractError("MoviePy module-reported version is not the frozen 2.1.2 payload")

    return {
        "paths": paths,
        "writer": paths["writer"].read_bytes(),
        "record": paths["record"].read_bytes(),
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.{PATCH_ID}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _result(status: str, writer_sha256: str, record_sha256: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": status,
        "patch_id": PATCH_ID,
        "patch_version": PATCH_VERSION,
        "distribution": f"{DISTRIBUTION_NAME}@{DISTRIBUTION_VERSION}",
        "module_reported_version": MODULE_REPORTED_VERSION,
        "license": "MIT",
        "codec_strategy": "h264_mf",
        "modified_files": {
            WRITER_RELATIVE_PATH.as_posix(): writer_sha256,
            RECORD_RELATIVE_PATH.as_posix(): record_sha256,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply the exact MoviePy 2.2.1 Windows Media Foundation H.264 patch."
    )
    parser.add_argument("--python-runtime", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true", help="Require exact patched writer and RECORD hashes."
    )
    mode.add_argument(
        "--calculate",
        action="store_true",
        help="Calculate deterministic patched hashes from the frozen upstream files without writing.",
    )
    args = parser.parse_args(argv)
    runtime = _load_runtime(args.python_runtime.resolve())
    paths = runtime["paths"]
    assert isinstance(paths, dict)
    writer = runtime["writer"]
    record = runtime["record"]
    assert isinstance(writer, bytes) and isinstance(record, bytes)
    writer_sha256 = _sha256_bytes(writer)
    record_sha256 = _sha256_bytes(record)

    if PATCHED_WRITER_SHA256 and PATCHED_RECORD_SHA256:
        if writer_sha256 == PATCHED_WRITER_SHA256 and record_sha256 == PATCHED_RECORD_SHA256:
            print(json.dumps(_result("already_patched", writer_sha256, record_sha256), ensure_ascii=False))
            return 0
    if writer_sha256 != UPSTREAM_WRITER_SHA256 or record_sha256 != UPSTREAM_RECORD_SHA256:
        raise PatchContractError(
            "writer/RECORD state is mixed or unknown; both files must match the frozen upstream or patched pair"
        )

    patched_writer = _patched_writer_payload(writer)
    patched_record = _patched_record_payload(record, patched_writer)
    patched_writer_sha256 = _sha256_bytes(patched_writer)
    patched_record_sha256 = _sha256_bytes(patched_record)
    if args.calculate:
        print(
            json.dumps(
                _result("calculated", patched_writer_sha256, patched_record_sha256),
                ensure_ascii=False,
            )
        )
        return 0
    if args.check:
        raise PatchContractError("frozen upstream MoviePy files are not patched")
    if not PATCHED_WRITER_SHA256 or not PATCHED_RECORD_SHA256:
        raise PatchContractError(
            "patched identities are not frozen; run --calculate and record both deterministic hashes"
        )
    if patched_writer_sha256 != PATCHED_WRITER_SHA256 or len(patched_writer) != PATCHED_WRITER_BYTES:
        raise PatchContractError(
            f"patched writer identity mismatch: {patched_writer_sha256}/{len(patched_writer)}"
        )
    if patched_record_sha256 != PATCHED_RECORD_SHA256 or len(patched_record) != PATCHED_RECORD_BYTES:
        raise PatchContractError(
            f"patched RECORD identity mismatch: {patched_record_sha256}/{len(patched_record)}"
        )

    writer_path = paths["writer"]
    record_path = paths["record"]
    assert isinstance(writer_path, Path) and isinstance(record_path, Path)
    try:
        _atomic_write(writer_path, patched_writer)
        _atomic_write(record_path, patched_record)
        if _sha256_bytes(writer_path.read_bytes()) != PATCHED_WRITER_SHA256:
            raise PatchContractError("patched writer failed its post-write identity check")
        if _sha256_bytes(record_path.read_bytes()) != PATCHED_RECORD_SHA256:
            raise PatchContractError("patched RECORD failed its post-write identity check")
    except Exception as exc:
        rollback_errors: list[str] = []
        for path, payload in ((writer_path, writer), (record_path, record)):
            try:
                _atomic_write(path, payload)
            except Exception as rollback_exc:
                rollback_errors.append(f"{path.name}: {rollback_exc}")
        if rollback_errors:
            raise PatchContractError(
                "patch transaction failed and rollback was incomplete: " + "; ".join(rollback_errors)
            ) from exc
        raise

    print(
        json.dumps(
            _result("patched", patched_writer_sha256, patched_record_sha256),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PatchContractError as exc:
        print(f"MOVIEPY_WINDOWS_MF_PATCH_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
