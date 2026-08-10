from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


TEXT_LOGS = (
    "build.log",
    "config.h",
    "config_components.h",
    "configure.log",
    "ffmpeg-buildconf.txt",
    "install.log",
    "zlib-build.log",
)
DRIVE_PATH_RE = re.compile(r"(?i)[a-z]:[\\/][^\r\n\"']+")
MSYS_LOCAL_PATH_RE = re.compile(r"(?i)/[a-z]/(?:syi_ffmpeg[^\s\"']*|devtools[^\s\"']*)")
_USER_HOME_SEGMENT = "users"
FORBIDDEN_RE = re.compile(
    r"(?i)(?:(?<![a-z0-9_])[a-z]:[\\/]|/home/|/"
    + _USER_HOME_SEGMENT
    + r"/|\\"
    + _USER_HOME_SEGMENT
    + r"\\|\b(?:LAPTOP|DESKTOP)-[a-z0-9]{4,})"
)


def path_variants(path: Path) -> set[str]:
    resolved = str(path.resolve())
    forward = resolved.replace("\\", "/")
    variants = {resolved, forward}
    match = re.fullmatch(r"([A-Za-z]):/(.*)", forward)
    if match:
        variants.add(f"/{match.group(1).lower()}/{match.group(2)}")
    return variants


def replace_known(value: str, replacements: list[tuple[str, str]]) -> str:
    for source, target in replacements:
        value = value.replace(source, target)
    value = DRIVE_PATH_RE.sub("<LOCAL_PATH>", value)
    value = MSYS_LOCAL_PATH_RE.sub("<LOCAL_PATH>", value)
    return value


def sanitize_json(value: Any, replacements: list[tuple[str, str]]) -> Any:
    if isinstance(value, str):
        normalized = replace_known(value, replacements)
        if re.match(r"(?i)^[a-z]:[\\/]", normalized):
            return "<LOCAL_PATH>"
        return normalized
    if isinstance(value, list):
        return [sanitize_json(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_json(item, replacements) for key, item in value.items()}
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize FFmpeg public build/probe evidence")
    parser.add_argument("--logs-dir", type=Path, required=True)
    parser.add_argument("--probe-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--probe-work-dir", type=Path, required=True)
    parser.add_argument("--visual-studio-root", type=Path, required=True)
    parser.add_argument("--windows-sdk-root", type=Path, required=True)
    args = parser.parse_args()

    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    replacements: list[tuple[str, str]] = []
    for path, placeholder in (
        (args.probe_work_dir, "<PROBE_WORK_DIR>"),
        (args.runtime_dir, "<RUNTIME_DIR>"),
        (args.visual_studio_root, "<VISUAL_STUDIO_ROOT>"),
        (args.windows_sdk_root, "<WINDOWS_SDK_ROOT>"),
        (args.build_root, "<BUILD_ROOT>"),
    ):
        replacements.extend((variant, placeholder) for variant in path_variants(path))
    replacements.sort(key=lambda item: len(item[0]), reverse=True)

    logs_output = output / "build"
    logs_output.mkdir()
    for name in TEXT_LOGS:
        source = args.logs_dir / name
        if not source.is_file():
            raise SystemExit(f"required build log is missing: {source}")
        text = source.read_text(encoding="utf-8", errors="replace")
        normalized = replace_known(text, replacements)
        (logs_output / name).write_text(normalized, encoding="utf-8", newline="\n")

    report = json.loads(args.probe_report.read_text(encoding="utf-8"))
    sanitized_report = sanitize_json(report, replacements)
    evidence_output = output / "evidence"
    evidence_output.mkdir()
    report_path = evidence_output / "probe-report.json"
    report_path.write_text(
        json.dumps(sanitized_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    scanned: list[dict[str, Any]] = []
    for path in sorted(output.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        matches = sorted(set(match.group(0) for match in FORBIDDEN_RE.finditer(text)))
        scanned.append({"path": path.relative_to(output).as_posix(), "matches": matches})
        if matches:
            raise SystemExit(f"sensitive/local path remains in {path}: {matches[:3]}")
    scan_report = {
        "schema_version": 1,
        "status": "passed",
        "rules": [
            "no drive-qualified absolute path",
            "no user-home path",
            "no local username or host prefix",
        ],
        "files": scanned,
    }
    (evidence_output / "sensitive-scan.json").write_text(
        json.dumps(scan_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({"status": "passed", "output": str(output), "files": len(scanned)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
