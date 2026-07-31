from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify staged public-evidence blobs against its manifest.")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    repo = args.repo.resolve()
    folder = args.folder.resolve()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    mismatches: list[str] = []
    for item in manifest.get("artifacts", []):
        name = str(item.get("name", ""))
        target = (folder / name).resolve()
        relative = target.relative_to(repo).as_posix()
        staged = subprocess.check_output(["git", "show", f":{relative}"], cwd=repo)
        if hashlib.sha256(staged).hexdigest() != item.get("sha256") or len(staged) != item.get("size"):
            mismatches.append(name)
    payload = {"status": "STAGED_EVIDENCE_OK" if not mismatches else "STAGED_EVIDENCE_MISMATCH", "checked": len(manifest.get("artifacts", [])), "mismatches": mismatches}
    print(json.dumps(payload, ensure_ascii=False))
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
