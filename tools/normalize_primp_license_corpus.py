from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import re
from pathlib import Path


OLD_ENCODED_SHA256 = "C8EBB3A4B359B7C030DA32605CA77F1654592C7840240FD843D8FCDA6E086599"
NEW_ENCODED_SHA256 = "915C908035FCD8029B692298907C6D7AC0FEBFBE965BE357140F11F86A12A665"
NEW_DECODED_SHA256 = "3D390B586A4AB7C3A6BBD2478A881DDA9D1FBB57A3EEABD9865A4323CDD55B03"
NEW_DECODED_SIZE = 794_639
NEW_GZIP_SHA256 = "D1EB45C9320EA94CC17B5C11892FC5D34AAB16078A482077D1C2A55C4D8F49B7"
NEW_COMPONENT_IDENTITY_SHA256 = "0FA06D3F634E86CDE5BB03E97DD66306DBBC336A6BAC33B3A99CD553C134A47F"
PRIMP_COMMIT = "f662999ad2a44bfad4ee433f8d37dd4a231f3154"
PRIMP_PATHS = {
    ("primp", "1.3.1"): "crates/primp",
    ("primp-h2", "0.4.15"): "crates/primp-h2",
    ("primp-hyper", "1.9.1"): "crates/primp-hyper",
    ("primp-hyper-rustls", "0.27.9"): "crates/primp-hyper-rustls",
    ("primp-hyper-util", "0.1.22"): "crates/primp-hyper-util",
    ("primp-reqwest", "0.13.4"): "crates/primp-reqwest",
    ("primp-rustls", "0.23.40"): "crates/primp-rustls/rustls",
    ("primp-tokio-rustls", "0.26.5"): "crates/primp-tokio-rustls",
}
MAP_HEADER = "COMPONENT TO EVIDENCE MAP\n=========================\n\n"
TEXT_HEADER = "LICENSE AND NOTICE TEXTS\n========================\n"


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def decode(encoded: bytes) -> bytes:
    return gzip.decompress(base64.b64decode(encoded))


def _portable_ref(path: str, version: str) -> str:
    return f"git+https://github.com/deedy5/primp.git@{PRIMP_COMMIT}#{path}@{version}"


def validate_portable_decoded(decoded: bytes) -> None:
    if len(decoded) != NEW_DECODED_SIZE or sha256(decoded) != NEW_DECODED_SHA256:
        raise ValueError("PRIMP_PORTABLE_DECODED_IDENTITY_MISMATCH")
    text = decoded.decode("utf-8")
    if "path+file:" in text.casefold() or re.search(r"(?i)(?:^|[^a-z0-9])[a-z]:[\\/]", text):
        raise ValueError("PRIMP_PORTABLE_CORPUS_CONTAINS_LOCAL_PATH")
    for (name, version), path in PRIMP_PATHS.items():
        expected = f'"bom_ref":"{_portable_ref(path, version)}"'
        if text.count(expected) != 1:
            raise ValueError(f"PRIMP_PORTABLE_COMPONENT_IDENTITY_MISSING:{name}")


def normalize(encoded: bytes) -> bytes:
    current_hash = sha256(encoded)
    if current_hash == NEW_ENCODED_SHA256:
        validate_portable_decoded(decode(encoded))
        return encoded
    if current_hash != OLD_ENCODED_SHA256:
        raise ValueError("PRIMP_CORPUS_INPUT_IDENTITY_MISMATCH")
    text = decode(encoded).decode("utf-8")
    for (name, version), path in PRIMP_PATHS.items():
        pattern = re.compile(
            r'("bom_ref":")path\+file:[^"]+("[^\n]+"name":"'
            + re.escape(name)
            + r'"[^\n]+"version":"'
            + re.escape(version)
            + r'")'
        )
        replacement = _portable_ref(path, version)
        text, count = pattern.subn(lambda match: match.group(1) + replacement + match.group(2), text)
        if count != 1:
            raise ValueError(f"PRIMP_LOCAL_COMPONENT_IDENTITY_COUNT:{name}:{count}")
    text, count = re.subn(
        r"Component identity SHA-256: [0-9A-F]{64}",
        f"Component identity SHA-256: {NEW_COMPONENT_IDENTITY_SHA256}",
        text,
    )
    if count != 1:
        raise ValueError("PRIMP_COMPONENT_IDENTITY_HEADER_COUNT")
    head_map, license_texts = text.split(TEXT_HEADER, 1)
    prefix, component_text = head_map.split(MAP_HEADER, 1)
    blocks = component_text.rstrip("\n").split("\n\n")
    if len(blocks) != 236:
        raise ValueError("PRIMP_COMPONENT_BLOCK_COUNT_MISMATCH")

    def block_key(block: str) -> str:
        first = block.splitlines()[0]
        if not first.startswith("Component: "):
            raise ValueError("PRIMP_COMPONENT_BLOCK_INVALID")
        return str(json.loads(first[len("Component: ") :])["bom_ref"])

    blocks.sort(key=block_key)
    decoded = (prefix + MAP_HEADER + "\n\n".join(blocks) + "\n\n" + TEXT_HEADER + license_texts).encode("utf-8")
    validate_portable_decoded(decoded)
    compressed = gzip.compress(decoded, compresslevel=9, mtime=0)
    if sha256(compressed) != NEW_GZIP_SHA256:
        raise ValueError("PRIMP_PORTABLE_GZIP_IDENTITY_MISMATCH")
    base64_bytes = base64.b64encode(compressed)
    wrapped = b"\n".join(base64_bytes[index : index + 6000] for index in range(0, len(base64_bytes), 6000)) + b"\n"
    if sha256(wrapped) != NEW_ENCODED_SHA256:
        raise ValueError("PRIMP_PORTABLE_ENCODED_IDENTITY_MISMATCH")
    return wrapped


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize primp SBOM-derived license identities")
    parser.add_argument("path", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    raw = args.path.read_bytes()
    normalized = normalize(raw)
    if args.apply and normalized != raw:
        args.path.write_bytes(normalized)
    print(
        json.dumps(
            {
                "status": "PRIMP_LICENSE_CORPUS_OK",
                "changed": normalized != raw,
                "applied": bool(args.apply),
                "encoded_bytes": len(normalized),
                "encoded_sha256": sha256(normalized),
                "decoded_bytes": len(decode(normalized)),
                "decoded_sha256": sha256(decode(normalized)),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
