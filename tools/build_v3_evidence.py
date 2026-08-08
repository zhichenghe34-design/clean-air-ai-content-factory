from __future__ import annotations

import argparse
import json
import mimetypes
import shutil
import tempfile
import zipfile
from pathlib import Path

from verify_v3_evidence import EXPECTED_DIRS, EXPECTED_PATHS, PAYLOAD_PATHS, sha256, verify


def build(source: Path, output: Path, zip_path: Path) -> dict[str, object]:
    source = source.resolve()
    output = output.resolve()
    zip_path = zip_path.resolve()
    if output.exists() or zip_path.exists():
        raise ValueError("输出目录或ZIP已存在，拒绝覆盖证据")
    actual_source = {path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file()}
    if actual_source != PAYLOAD_PATHS:
        missing, extra = sorted(PAYLOAD_PATHS - actual_source), sorted(actual_source - PAYLOAD_PATHS)
        raise ValueError(f"证据暂存白名单不一致；missing={missing}; extra={extra}")
    actual_directories = {path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_dir()}
    if actual_directories != EXPECTED_DIRS:
        raise ValueError("证据暂存目录白名单不一致")
    output.parent.mkdir(parents=True, exist_ok=True)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="v3-evidence-", dir=output.parent))
    try:
        for name in sorted(PAYLOAD_PATHS):
            target = temporary / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, target)
        sums = "".join(f"{sha256(temporary / name)}  {name}\n" for name in sorted(PAYLOAD_PATHS))
        (temporary / "SHA256SUMS.txt").write_text(sums, encoding="utf-8", newline="\n")
        manifest_files = []
        for name in sorted(EXPECTED_PATHS - {"evidence-manifest.json"}):
            path = temporary / name
            manifest_files.append({
                "name": name,
                "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
                "size": path.stat().st_size,
                "sha256": sha256(path),
            })
        manifest = {
            "schema_version": 1,
            "evidence_type": "v3-general-local-cafe",
            "file_count": 50,
            "files": manifest_files,
        }
        (temporary / "evidence-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        errors, _ = verify(temporary)
        if errors:
            raise ValueError("证据终验失败：" + "; ".join(errors))
        temporary.replace(output)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in sorted(EXPECTED_PATHS):
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, (output / name).read_bytes())
        return {"status": "V3_EVIDENCE_BUILT", "folder": output.name, "zip": zip_path.name, "files": 50, "zip_sha256": sha256(zip_path)}
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="构建确定性的v0.3通用餐饮证据包")
    parser.add_argument("source", type=Path, help="含48个白名单载荷文件的暂存目录")
    parser.add_argument("output", type=Path, help="不存在的证据输出目录")
    parser.add_argument("--zip", dest="zip_path", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    result = build(args.source, output, (args.zip_path or output.with_suffix(".zip")).resolve())
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
