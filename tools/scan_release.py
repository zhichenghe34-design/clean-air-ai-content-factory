from __future__ import annotations

import argparse
import re
from pathlib import Path


TEXT_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".md", ".json", ".yml", ".yaml", ".html",
    ".css", ".txt", ".ps1", ".bat", ".toml", ".srt", ".env", ".example",
}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "runtime", "tmp", "__pycache__"}
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "live-looking API key": re.compile(r"\b(?:sk|ds)-[A-Za-z0-9_-]{20,}\b"),
    "Bearer token": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}={0,2}"),
    "user profile path": re.compile(r"(?i)(?:C:\\Users\\[^\\\s\"']+|/Users/[^/\s\"']+|/home/[^/\s\"']+)"),
    "workspace drive path": re.compile(r"(?i)(?:^|[\s\"'])(?:D|F):\\"),
    "mainland phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan release text for secrets and machine-specific paths.")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    repo = args.repo.resolve()
    errors: list[str] = []
    scanned = 0
    for path in repo.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(repo).parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != ".env.example":
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in PATTERNS.items():
            if path.name == "scan_release.py" or (path.name == "verify_public_evidence.py" and label == "user profile path"):
                continue
            if pattern.search(text):
                errors.append(f"{path.relative_to(repo)}: {label}")
        if "tests" not in path.relative_to(repo).parts and re.search(r'(?i)"(?:api_?key|token|secret|password|cookie|authorization)"\s*:\s*"(?!\s*")[^"]{8,}"', text):
            errors.append(f"{path.relative_to(repo)}: non-empty sensitive JSON field")
    if errors:
        for error in sorted(set(errors)):
            print(f"ERROR: {error}")
        return 1
    print(f"RELEASE_SCAN_OK files={scanned}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
