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
