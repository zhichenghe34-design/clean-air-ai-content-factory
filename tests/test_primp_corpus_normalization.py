from __future__ import annotations

import base64
import gzip
import unittest
from pathlib import Path

from tools.normalize_primp_license_corpus import (
    NEW_DECODED_SHA256,
    NEW_ENCODED_SHA256,
    decode,
    normalize,
    sha256,
    validate_portable_decoded,
)


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "third_party" / "python_runtime" / "dependency-licenses" / "primp-1.3.1-third-party-licenses.txt.gz.b64"


class PrimpCorpusNormalizationTests(unittest.TestCase):
    def test_tracked_corpus_is_portable_and_idempotent(self) -> None:
        raw = CORPUS.read_bytes()
        self.assertEqual(sha256(raw), NEW_ENCODED_SHA256)
        self.assertEqual(sha256(decode(raw)), NEW_DECODED_SHA256)
        self.assertEqual(normalize(raw), raw)

    def test_local_path_is_rejected_even_with_reencoded_bytes(self) -> None:
        raw = CORPUS.read_bytes()
        decoded = decode(raw).replace(
            b"git+https://github.com/deedy5/primp.git@",
            b"path+file:///D:/a/primp/primp/",
            1,
        )
        compressed = gzip.compress(decoded, compresslevel=9, mtime=0)
        tampered = base64.b64encode(compressed)
        with self.assertRaises(ValueError):
            validate_portable_decoded(gzip.decompress(base64.b64decode(tampered)))


if __name__ == "__main__":
    unittest.main()
