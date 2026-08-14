# Real portable Python runtime audit

Audit date: 2026-08-10  
Candidate identity: `20260810-motion-primary-candidate-94311a8`  
Method: same-volume temporary hard-link staging; no release ZIP or durable package was built.

## Exact pruning result

- Source runtime: 138 distributions, 19,669 copied-eligible files,
  763,546,631 bytes.
- Exact name/version/RECORD pruning: 49 distributions, 11,590 files,
  486,393,258 bytes.
- Redundant `imageio_ffmpeg/binaries/*.exe`: 1 file, 87,638,016 bytes.
- Retained runtime: 89 distributions, 8,078 files, 189,515,357 bytes.
- Total reduction: 574,031,274 bytes (75.18%).

The complete exact 49-distribution identity list and all 51 import namespaces
are machine-readable in `pruned-import-boundary.json`. The seven approved roots
are `ctranslate2`, `faster-whisper`, `litellm`, `onnxruntime`, `streamlit`,
`streamlit-tour`, and `tokenizers`; every other removed distribution is an
exact metadata-graph orphan. Builder and verifier reject version, dependency
graph, RECORD ownership, import boundary, and residual-file drift.

## SBOM and license closure

- Generated SBOM schema: 2 (`python_distribution_closure`).
- Retained site-packages files: 5,784; RECORD-owned files: 5,784.
- Project-verified exact-version overrides: 4 (`langchain-core 1.5.3`,
  `langsmith 0.10.17`, `loguru 0.7.3`, `primp 1.3.1`).
- Runtime closure SHA-256:
  `AF0383A88F2975BD35ED549CDF8CC2A72F1AC234E3D92BB84132C176EBDB7296`.
- Independent package-verifier result: no errors.
- `primp 1.3.1`: embedded CycloneDX SHA-256
  `726216799D98ABF36AA3D623F3E8A3FC63F70F39B43813DE27C13F89BD797897`;
  236 component identities, 237 dependency entries, and 190 unique original
  license/notice texts were matched bidirectionally to the decoded corpus.
- Decoded `primp` corpus: 794,244 bytes, SHA-256
  `71FA3F86F1A39BD02890FAE1E03BC11DDBCB7253E5C50A6619B2D413299610C4`.

The audit also explicitly observed Azure Speech SDK's proprietary LICENSE,
REDIST, and ThirdPartyNotices; Edge TTS's LGPL-3.0 text; and the complete
multi-license evidence sets for `cryptography`, `ormsgpack`, and `sniffio`.

## Static imports and executable probes

The retained-runtime AST scan covered every retained `.py` file against all 51
removed import namespaces. It found only the 23 exact optional references
recorded in `pruned-import-boundary.json`. The formal payload scan found only
the one reviewed, ImportError-guarded `faster_whisper` reference in MPT's
subtitle service.

The real pruned portable Python then passed one isolated process probe covering:

- MPT ASGI import and the video-only route set;
- fixed `subtitle_provider=edge` and `WhisperModel is None`;
- disabled MPT LLM and social-upload adapters;
- absence of every removed import namespace;
- `primp` and `ddgs` imports;
- retained `multipart`, `openai`, `toml`, and `uvicorn` imports;
- Uvicorn auto protocol resolving to retained `h11`;
- root `app.py` import and `scripts.launch_combined.main` import.

Separate executable version probes passed for Node `22.13.1`, HyperFrames
`0.7.86`, and Chrome Headless Shell `152.0.7928.2`.

The reproducible function chain was:

1. `_python_pruned_runtime_paths` and
   `_validate_retained_python_import_boundary`;
2. `_build_python_runtime_sbom`;
3. verifier `_verify_python_runtime_sbom` over actual staged bytes;
4. `_copy_mpt_payload` and `_verify_mpt_offline_subset_executable`;
5. `_verify_motion_executables`.

All temporary staging directories were removed after the run. These probes
started only bounded child processes and no persistent service or listener.

## Tests

- `python -m unittest tests.test_combined_portable -v`: 34 passed.
- Full suite with the fixed portable Python: 354 discovered, 329 passed,
  24 errors from an existing shared `app.py`/`tests/test_api_v2.py` symbol drift,
  and 1 failure from an existing MPT integration-status expectation drift.
  Those files are outside this audit's authorized write boundary and were not
  changed here.
