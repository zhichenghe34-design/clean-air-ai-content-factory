# HyperFrames third-party boundary

- Upstream: https://github.com/heygen-com/hyperframes
- Fixed release: `v0.7.86`
- Fixed commit: `1a52351f05237433006e6ca92db18feafed16fed`
- npm package: `hyperframes@0.7.86`
- License: Apache-2.0

`LICENSE` is the byte-identical license from the fixed upstream tag. The npm
package does not contain that file, so release builds use this reviewed project
copy instead of inventing a package-local license.

The runtime SBOM records the SPDX declaration and every LICENSE/NOTICE file
actually present in the dependency closure. A dependency without its own text
must have an exact-version reviewed override in
`dependency-license-overrides.json`; otherwise the release build fails closed.

Each override freezes the exact upstream commit, official raw source URL and
source SHA-256, a redistributable local license notice, copyright attribution,
and any required NOTICE files. ONNX Runtime 1.21.1 therefore carries both its
MIT license and the official `ThirdPartyNotices.txt`; shared evidence files are
deduplicated in the portable package but package identities remain separate.

## Modified Windows runtime

The formal Windows motion runtime is an Apache-2.0 modified distribution, not
an unmodified npm payload. `tools/apply_hyperframes_windows_mf_patch.py`
accepts only the frozen `hyperframes@0.7.86` bundle SHA-256, applies the
deterministic `shiyi-hyperframes-windows-mf` patch, verifies the frozen patched
SHA-256, and writes atomically. The modified-file boundary, source and patched
hashes, patcher hash, patch version, purpose, codec policy, and mandatory
staging order are frozen in `windows-mf-patch.json`. The patch also inserts a
prominent Apache-2.0 modified-file notice immediately after the executable
shebang in `dist/cli.js`, so the distributed modified file identifies itself
even when separated from this directory.

Every formal H.264 output-encoding path uses Windows Media Foundation
`h264_mf`; H.264 CRF, preset, x264 parameters, profile forcing, GPU
alternatives, and H.265 fallbacks are fail-closed before output encoding.
Non-output capability probes remain inert and are not an alternate production
encoder. Draft, standard, and high map to quality 60, 72, and 80 based on the
tracked 1080x1920 comparison in
`windows-mf-quality-evidence.json`. Real strict-check, streaming-render, and
disk-render results against the LGPL shared FFmpeg runtime, with an injected
system Edge executable and a rejecting network guard, are recorded in
`windows-mf-runtime-evidence.json`.

The portable runtime does not bundle Chrome, Chrome for Testing, or Edge. The
combined launcher accepts only Microsoft Edge 151+ from the machine-level
`Program Files` locations after Authenticode, Microsoft product/company, real
four-part file version, and path-boundary checks. It then runs a real
HyperFrames strict canary before publishing motion readiness; missing, old,
user-writable, overridden, or untrusted browser identities fail closed.

Release staging must apply the patch after extracting the exact npm artifact
and before regenerating the runtime manifest. A manually edited `node_modules`
tree is never a release input. Runtime and engine reports identify
`codec_strategy=h264_mf`, the patch ID/version, and `patched_cli_sha256`;
FFprobe continues to identify the resulting elementary codec as `h264`.
