# FFmpeg LGPL Windows x64 distribution contract

The formal portable runtime is the self-built, shared, LGPL 2.1-or-later set in
`runtime/win-x64`. It replaces the former BtbN GPL package. The formal lock is
`upstream-lock.json`; `historical-gpl-btbn-lock.json` is retained only as an
audit record and must never feed the package builder or release gate.

## Exact runtime

The runtime directory is an exact set. Import libraries, headers, examples,
PDBs, `ffplay`, `avdevice`, an ImageIO FFmpeg copy, and any x264/rav1e DLL are
forbidden.

| File | Bytes | SHA-256 |
|---|---:|---|
| `avcodec-63.dll` | 14,030,848 | `6c4c5a12a84c940bcf12ca729f26f1629a97ba94b8159e6349ecbf176e7a2987` |
| `avfilter-12.dll` | 4,350,976 | `797cfb6fb27da31ca88f9a59508440cd98cb47290bbbde54f7c1089077751132` |
| `avformat-63.dll` | 2,473,472 | `244505aafb3b37ae192b15b03fe871965fbe981082379e0c33bc43f3a7611758` |
| `avutil-61.dll` | 1,143,808 | `37d6fec1955060f1fe867a86bdca968dcfddae76e0456c79dd63633e7befa2ae` |
| `ffmpeg.exe` | 592,384 | `53cc924afeffbe48bd94e569d0081b2eef64ebaea18b7966f7686947b0ebc1dc` |
| `ffprobe.exe` | 335,872 | `12324dccee8985b2d7f83f59c6be0617046df44dd8f25723bb1eecc20abe7a4a` |
| `swresample-7.dll` | 231,424 | `f7c0df579f8464534020f4dca1d870b5ba359c260bbc2450a272c159b16d8772` |
| `swscale-10.dll` | 1,282,560 | `c8ad8c480404c68bb7e82a9eeef35d139fbd65ab632aeb4ce1adefeee8f3281a` |
| `zlib1.dll` | 226,816 | `ab4485b6302f4debaba78d70552445cd0a931610c664e7ff1c9639001d9062e3` |

The PE verifier parses normal and delay-import tables for all nine files. Every
import must resolve to another locked runtime DLL or the exact Windows system
allowlist in the lock. zlib is built with `/MT`; `zlib1.dll` imports only
`kernel32.dll`, so no separate Visual C++ redistributable DLL is required.
`mfplat.dll` and `d3d11.dll` are dynamically loaded Windows system interfaces.

## Build and license boundary

The build is fixed to FFmpeg commit
`d3ad8a7fee6a647c6362e4a105d949282d50a98f` and zlib 1.3.2. Its configuration
is shared, MSVC x64, network-disabled, and explicitly enables zlib, native AAC,
Media Foundation, D3D11VA, and `h264_mf`. `CONFIG_GPL`, `CONFIG_GPLV3`, and
`CONFIG_VERSION3` are all zero. There is no libx264, rav1e, nonfree, or other
external codec library.

The only FFmpeg source change is
`patches/0001-localized-msvc-detection.patch`. It handles a localized `cl.exe`
banner only when `--toolchain=msvc` was explicitly selected. The build script
requires caller-supplied private roots and verifies every downloaded input hash:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File third_party/ffmpeg/build-lgpl-msvc.ps1 `
  -BuildRoot <BUILD_ROOT> `
  -VisualStudioRoot <VISUAL_STUDIO_ROOT>
```

Keep the license files under `licenses/` with every binary distribution. The
application remains Apache/MIT as applicable; using an LGPL shared runtime does
not relicense the application. Users must be able to replace the FFmpeg DLLs,
and the exact corresponding-source companion must remain available. This is an
engineering release policy, not legal advice.

## Real capability evidence

`probe-lgpl-runtime.py` executed 21 real commands against the final bytes. It
proved:

- PNG sequence and PNG `image2pipe` input to `h264_mf`;
- a 1080x1920, 30 fps, H.264 `yuv420p` + native AAC MP4 canary;
- `setpts`, `fps`, `format`, `scale`, `aresample`, `atempo`, and `amix`;
- PNG, JPEG, WAV, MP3, AAC, and H.264 decoding;
- MP4/MOV mux and demux, concat demux, and ffprobe JSON;
- SRT demux to a `mov_text` subtitle stream.

These results are split into `capabilities.hyperframes` and
`capabilities.moneyprinterturbo` in the lock. The normalized report and canary
are inside the source companion. Public evidence is scanned for developer
usernames, host names, home directories, and drive-qualified local paths.

## Source companion and same-Release gate

The exact source companion is frozen locally as a release-candidate asset; it
has not yet been uploaded to a GitHub Release:

- name: `ShiyiContentFactory-v0.3.0-FFmpeg-LGPL-source-d3ad8a7.zip`
- bytes: `19,314,160`
- SHA-256: `a09a28824f6c5ebbfc8cf724136701fa6adfe7f35ba58670a85f48a9ca856c08`

It contains the exact FFmpeg and zlib archives, the patch, build/probe/verifier
scripts, normalized configure/build/install logs, toolchain identities and
hashes, license texts, PE-import evidence, capability report, and canary. Its
internal `MANIFEST.json` is itself byte/hash locked. The verifier checks every
member path, byte size, SHA-256, manifest membership, sensitive-path scan, and
capability report contract.

Local freezing proves reproducibility and identity only; it is not public
source availability. Before publication, the exact source asset and an object ZIP named
`ShiyiContentFactory-v0.3.0-motion-primary-<commit>-Windows-x64.zip` must be on
the same `v0.3.0` GitHub Release. A local file, future-download promise, or
source link alone does not pass.

```powershell
python tools/verify_ffmpeg_distribution.py `
  --runtime-dir third_party/ffmpeg/runtime/win-x64 `
  --source-dir <SOURCE_ASSET_DIRECTORY> `
  --release-manifest <GITHUB_RELEASE_JSON> `
  --require-release-ready
```

## Portable-builder wiring contract

The package builder must implement this exact sequence:

1. Treat `third_party/ffmpeg/runtime/win-x64` as the sole FFmpeg input and run
   `verify_runtime_dir` before copying.
2. Copy the exact nine files to the portable package's `runtime/ffmpeg`
   directory, then run the verifier on that staged directory. Reject a missing,
   changed, or extra entry, including `.lib`, `avdevice`, `ffplay`, old BtbN
   files, and any second ImageIO FFmpeg binary.
3. Copy all four tracked license/notice files into the package's third-party
   notices area. Record the FFmpeg commit, normalized configure flags, all nine
   hashes, and source-companion name/bytes/SHA in portable build-info.
4. Expose the locked `capabilities.hyperframes` and
   `capabilities.moneyprinterturbo` fields to the builder's capability record;
   do not infer them from executable presence.
5. Bind the generated object ZIP and exact source companion to the same Release
   manifest and run `--require-release-ready` before publication.

The package builder/verifier files are maintained by their owning integration
task; this directory provides the exact immutable inputs and fail-closed
contract they must consume.
