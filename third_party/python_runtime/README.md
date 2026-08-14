# Python runtime license boundary

`dependency-license-overrides.json` is the fail-closed allowlist for an exact
Python distribution version whose installed wheel omits a license text. Each
entry resolves the release tag to an immutable upstream commit, records the raw
license URL and SHA-256, and points at the byte-identical local evidence copy.

The `primp` native wheel is additionally bound to its embedded CycloneDX SBOM.
Its compressed corpus contains the original license/notice files for all 236
SBOM components (including the eight exact-commit local forks and every
crates.io archive verified by its SBOM checksum). The builder decodes this
corpus into the release and independently compares every component identity;
the package verifier repeats the SBOM/corpus comparison.

The eight workspace crates use portable identities of the form
`git+https://github.com/deedy5/primp.git@<commit>#crates/<path>@<version>`.
Build-machine `path+file:` references, drive letters, and backslashes are
rejected. `tools/normalize_primp_license_corpus.py` records the one reviewed,
deterministic migration from the upstream wheel's CI paths to these identities.

The portable build may use an override only for the exact normalized name and
version in this file.  A new or changed distribution without an in-wheel
LICENSE/LICENCE/COPYING/NOTICE (or an applicable proprietary REDIST file) must
be reviewed and frozen here before another formal package can be produced.

The formal small-package profile also removes an exact, metadata-derived
orphan closure rooted at the excluded Whisper subtitle, Streamlit WebUI, and
LiteLLM stacks. Every removed distribution version and RECORD-owned file is
locked by the builder and declared in the generated Python runtime SBOM;
version, dependency-graph, or residual-file drift fails closed.

`pruned-import-boundary.json` is the companion static-import contract. It maps
every removed distribution to its real import namespaces and records the exact
retained wheel files that mention one only in a reviewed optional, CLI, test,
build, or disabled integration path. Any new reference, owner/version drift,
or formal-product import fails closed. The sole formal-source exception is
MoneyPrinterTurbo's guarded `faster_whisper` import: the fixed package config
selects Edge subtitles and the real staging probe requires both
`WhisperModel is None` and Uvicorn's retained h11 fallback.
