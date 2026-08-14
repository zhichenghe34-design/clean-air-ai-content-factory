from __future__ import annotations

import argparse
import hashlib
import json
import stat
import zipfile
from pathlib import Path, PurePosixPath


ROOT_NAME = "ShiyiContentFactory-v0.3.0-FFmpeg-LGPL-source-d3ad8a7"
MANIFEST_NAME = "MANIFEST.json"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the deterministic FFmpeg source companion")
    parser.add_argument("--staging-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest-output", type=Path)
    args = parser.parse_args()

    staging = args.staging_dir.resolve()
    output = args.output.resolve()
    if not staging.is_dir():
        raise SystemExit(f"staging directory is missing: {staging}")
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing archive: {output}")

    files: list[tuple[str, Path]] = []
    for path in sorted(staging.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise SystemExit(f"symlinks are forbidden in the source companion: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(staging).as_posix()
        parts = PurePosixPath(relative).parts
        if not parts or relative.startswith("/") or ".." in parts:
            raise SystemExit(f"unsafe companion member: {relative}")
        if relative == MANIFEST_NAME:
            raise SystemExit(f"{MANIFEST_NAME} is generated; remove it from the staging directory")
        files.append((relative, path))
    if not files:
        raise SystemExit("source companion staging directory is empty")

    manifest = {
        "schema_version": 1,
        "component": "FFmpeg LGPL shared Windows x64 corresponding source and build evidence",
        "ffmpeg_commit": "d3ad8a7fee6a647c6362e4a105d949282d50a98f",
        "zlib_version": "1.3.2",
        "files": [
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for relative, path in files
        ],
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(zip_info(f"{ROOT_NAME}/{MANIFEST_NAME}"), manifest_bytes)
        for relative, path in files:
            archive.writestr(zip_info(f"{ROOT_NAME}/{relative}"), path.read_bytes())

    if args.manifest_output is not None:
        manifest_output = args.manifest_output.resolve()
        manifest_output.parent.mkdir(parents=True, exist_ok=True)
        manifest_output.write_bytes(manifest_bytes)
    print(
        json.dumps(
            {
                "archive": str(output),
                "bytes": output.stat().st_size,
                "sha256": sha256_file(output),
                "manifest_bytes": len(manifest_bytes),
                "manifest_sha256": sha256_bytes(manifest_bytes),
                "files": len(files),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
