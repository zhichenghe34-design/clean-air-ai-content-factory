from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


PATCH_ID = "shiyi-hyperframes-windows-mf"
PATCH_VERSION = "1.2.0"
HYPERFRAMES_VERSION = "0.7.86"
UPSTREAM_CLI_SHA256 = "B89672986C4487A133B241261AC610EA9F9CCDE467F206E18A60BEFFACAB6CB8"
PATCHED_CLI_SHA256 = "86DA751BA397FF551355BA0C90370D732A297C3DC4652C981E9A8146D8EAC108"


class PatchContractError(RuntimeError):
    pass


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def _replace_exact(text: str, old: str, new: str, *, label: str, count: int = 1) -> str:
    actual = text.count(old)
    if actual != count:
        raise PatchContractError(f"{label}: expected {count} exact match(es), found {actual}")
    return text.replace(old, new)


def _replace_region(
    text: str,
    *,
    scope: str,
    start_marker: str,
    end_marker: str,
    replacement: str,
    label: str,
) -> str:
    scope_start = text.find(scope)
    if scope_start < 0:
        raise PatchContractError(f"{label}: scope marker missing")
    start = text.find(start_marker, scope_start)
    if start < 0:
        raise PatchContractError(f"{label}: start marker missing")
    end = text.find(end_marker, start + len(start_marker))
    if end < 0:
        raise PatchContractError(f"{label}: end marker missing")
    return text[:start] + replacement + text[end:]


def _patched_text(upstream: str) -> str:
    text = upstream

    modification_notice = (
        "#!/usr/bin/env node\n"
        f"// Apache-2.0 modification notice: Shanghai Shiyi Brand Management Co., Ltd. modified this file via {PATCH_ID} v{PATCH_VERSION} for the Windows Media Foundation H.264 and offline-runtime policies; this is not the unmodified upstream file.\n"
    )
    text = _replace_exact(
        text,
        "#!/usr/bin/env node\n",
        modification_notice,
        label="embed Apache-2.0 modified-file notice after the shebang",
    )

    helper_anchor = (
        '  return { ...base2, pixelFormat: "yuv420p" };\n'
        '}\n'
        'function buildEncoderArgs(options, inputArgs, outputPath, gpuEncoder = null) {\n'
    )
    helper_replacement = (
        '  return { ...base2, pixelFormat: "yuv420p" };\n'
        '}\n'
        'function shiyiH264MfQualityFromCrf(crf) {\n'
        '  const numeric = Number(crf);\n'
        '  if (!Number.isFinite(numeric) || numeric < 0 || numeric > 51) {\n'
        '    throw new Error(`[shiyi-mf] H.264 quality must be a CRF-equivalent number from 0 to 51 (received ${String(crf)}).`);\n'
        '  }\n'
        '  const anchors = [[0, 100], [15, 80], [18, 72], [28, 60], [51, 1]];\n'
        '  for (let index = 1; index < anchors.length; index += 1) {\n'
        '    const [rightCrf, rightQuality] = anchors[index];\n'
        '    if (numeric <= rightCrf) {\n'
        '      const [leftCrf, leftQuality] = anchors[index - 1];\n'
        '      const ratio = (numeric - leftCrf) / (rightCrf - leftCrf);\n'
        '      return Math.max(1, Math.min(100, Math.round(leftQuality + ratio * (rightQuality - leftQuality))));\n'
        '    }\n'
        '  }\n'
        '  return 1;\n'
        '}\n'
        'function appendShiyiH264MfArgs(args, options = {}) {\n'
        '  if (process.platform !== "win32") {\n'
        '    throw new Error("[shiyi-mf] This patched portable runtime requires Windows Media Foundation.");\n'
        '  }\n'
        '  args.push("-c:v", "h264_mf");\n'
        '  if (options.bitrate) {\n'
        '    args.push("-rate_control", "cbr", "-b:v", String(options.bitrate));\n'
        '  } else {\n'
        '    args.push("-rate_control", "quality", "-quality", String(shiyiH264MfQualityFromCrf(options.quality ?? 23)));\n'
        '  }\n'
        '  args.push("-scenario", "archive", "-hw_encoding", "0");\n'
        '}\n'
        'function buildEncoderArgs(options, inputArgs, outputPath, gpuEncoder = null) {\n'
    )
    text = _replace_exact(
        text,
        helper_anchor,
        helper_replacement,
        label="insert Media Foundation argument policy",
    )

    chunk_branch = (
        '  const shouldUseGpu = useGpu && gpuEncoder !== null;\n'
        '  if (codec === "h264" || codec === "h265") {\n'
        '    if (codec !== "h264") {\n'
        '      throw new Error(`[shiyi-mf] The Windows portable runtime supports only H.264 via h264_mf (received ${codec}).`);\n'
        '    }\n'
        '    if (useGpu) {\n'
        '      throw new Error("[shiyi-mf] --gpu is disabled; h264_mf software mode is the fixed portable encoder policy.");\n'
        '    }\n'
        '    appendShiyiH264MfArgs(args, { quality, bitrate });\n'
        '    const lockGop = options.lockGopForChunkConcat === true;\n'
        '    if (lockGop) {\n'
        '      if (typeof options.gopSize !== "number" || !Number.isFinite(options.gopSize) || options.gopSize <= 0) {\n'
        '        throw new Error(\n'
        '          `[chunkEncoder] lockGopForChunkConcat=true requires a positive integer gopSize (received ${String(options.gopSize)})`\n'
        '        );\n'
        '      }\n'
        '      const gop = Math.floor(options.gopSize);\n'
        '      args.push(\n'
        '        "-g",\n'
        '        String(gop),\n'
        '        "-keyint_min",\n'
        '        String(gop),\n'
        '        "-force_key_frames",\n'
        '        `expr:eq(mod(n,${gop}),0)`,\n'
        '        "-flags",\n'
        '        "+cgop"\n'
        '      );\n'
        '    }\n'
        '    args.push("-bf", "0");\n'
    )
    text = _replace_region(
        text,
        scope="function buildEncoderArgs(options, inputArgs, outputPath, gpuEncoder = null) {",
        start_marker=(
            '  const shouldUseGpu = useGpu && gpuEncoder !== null;\n'
            '  if (codec === "h264" || codec === "h265") {\n'
        ),
        end_marker='  } else if (codec === "vp9") {',
        replacement=chunk_branch,
        label="replace normal and chunk H.264 encoder branch",
    )

    streaming_branch = (
        '  const shouldUseGpu = useGpu && gpuEncoder !== null;\n'
        '  if (codec === "h264" || codec === "h265") {\n'
        '    if (codec !== "h264") {\n'
        '      throw new Error(`[shiyi-mf] The Windows portable runtime supports only H.264 via h264_mf (received ${codec}).`);\n'
        '    }\n'
        '    if (useGpu) {\n'
        '      throw new Error("[shiyi-mf] --gpu is disabled; h264_mf software mode is the fixed portable encoder policy.");\n'
        '    }\n'
        '    appendShiyiH264MfArgs(args, { quality, bitrate });\n'
        '    args.push("-bf", "0");\n'
    )
    text = _replace_region(
        text,
        scope="function buildStreamingArgs(options, outputPath, gpuEncoder = null) {",
        start_marker=(
            '  const shouldUseGpu = useGpu && gpuEncoder !== null;\n'
            '  if (codec === "h264" || codec === "h265") {\n'
        ),
        end_marker='  } else if (codec === "vp9") {',
        replacement=streaming_branch,
        label="replace streaming H.264 encoder branch",
    )

    text = _replace_region(
        text,
        scope="function resolveH264EncoderMode(ffmpegEncodersOutput, gpuRequested) {",
        start_marker="function resolveH264EncoderMode(ffmpegEncodersOutput, gpuRequested) {\n",
        end_marker="function detectH264EncoderMode(ffmpegPath, gpuRequested) {",
        replacement=(
            'function resolveH264EncoderMode(ffmpegEncodersOutput, gpuRequested) {\n'
            '  if (gpuRequested) {\n'
            '    throw new Error("[shiyi-mf] --gpu is disabled for the fixed Windows Media Foundation encoder policy.");\n'
            '  }\n'
            '  if (/\\bh264_mf\\b/.test(ffmpegEncodersOutput)) return "software";\n'
            '  throw new Error("This portable FFmpeg build does not expose the required h264_mf encoder.");\n'
            '}\n'
        ),
        label="require h264_mf during encoder probe",
    )

    text = _replace_region(
        text,
        scope="async function runFfmpeg2(sourcePath, outputPath, variant) {",
        start_marker="    const h264Args = [\n",
        end_marker="    const vp8Args = [",
        replacement=(
            '    const h264Args = [\n'
            '      "-c:v",\n'
            '      "h264_mf",\n'
            '      "-rate_control",\n'
            '      "quality",\n'
            '      "-quality",\n'
            '      "72",\n'
            '      "-scenario",\n'
            '      "archive",\n'
            '      "-hw_encoding",\n'
            '      "0",\n'
            '      "-bf",\n'
            '      "0",\n'
            '      "-pix_fmt",\n'
            '      "yuv420p",\n'
            '      "-colorspace",\n'
            '      "bt709",\n'
            '      "-color_primaries",\n'
            '      "bt709",\n'
            '      "-color_trc",\n'
            '      "bt709",\n'
            '      "-c:a",\n'
            '      "aac",\n'
            '      "-movflags",\n'
            '      "+faststart"\n'
            '    ];\n'
        ),
        label="replace media proxy H.264 transcode",
    )

    text = _replace_exact(
        text,
        'const reencode = `ffmpeg -i "${video.src}" -c:v libx264 -r 30 -g 30 -keyint_min 30 -movflags +faststart -c:a copy output.mp4`;',
        'const reencode = `ffmpeg -i "${video.src}" -c:v h264_mf -rate_control quality -quality 72 -scenario archive -hw_encoding 0 -r 30 -g 30 -keyint_min 30 -force_key_frames "expr:eq(mod(n,30),0)" -flags +cgop -bf 0 -pix_fmt yuv420p -movflags +faststart -c:a copy output.mp4`;',
        label="replace compiler pre-encode recommendation",
    )

    text = _replace_region(
        text,
        scope="function resolveMp4EncoderTriple(codec) {",
        start_marker="function resolveMp4EncoderTriple(codec) {\n",
        end_marker="function resolveNonMp4EncoderTriple(format, quality) {",
        replacement=(
            'function resolveMp4EncoderTriple(codec) {\n'
            '  const c3 = codec ?? "h264";\n'
            '  if (c3 !== "h264") {\n'
            '    throw new Error(\n'
            '      `[plan] This Windows portable runtime supports only codec="h264" through h264_mf; received ${JSON.stringify(c3)}.`\n'
            '    );\n'
            '  }\n'
            '  return { encoder: "h264-mf-windows", pixelFormat: "yuv420p", preset: "quality" };\n'
            '}\n'
        ),
        label="lock distributed MP4 encoder identity",
    )

    text = _replace_region(
        text,
        scope="async function assemble(planDir, chunkPaths, audioPath, outputPath, options) {",
        start_marker="    if (cfr) {\n",
        end_marker="    let normalizedAudio = null;",
        replacement=(
            '    if (cfr) {\n'
            '      if (plan2.dimensions.format !== "mp4") {\n'
            '        throw new Error(\n'
            '          `[assemble] cfr=true is only supported for format="mp4" (got "${plan2.dimensions.format}"). Stream-copy paths for webm and mov already produce exact avg_frame_rate; cfr re-encode is not needed.`\n'
            '        );\n'
            '      }\n'
            '      const encoderJsonPath = join69(planDir, "meta", "encoder.json");\n'
            '      if (!existsSync60(encoderJsonPath)) {\n'
            '        throw new Error(`[assemble] planDir missing meta/encoder.json: ${encoderJsonPath}`);\n'
            '      }\n'
            '      const encoderJson = JSON.parse(readFileSync35(encoderJsonPath, "utf-8"));\n'
            '      if (encoderJson.encoder !== "h264-mf-windows") {\n'
            '        throw new Error(\n'
            '          `[assemble] cfr=true requires the fixed h264-mf-windows plan encoder (got ${JSON.stringify(encoderJson.encoder)}).`\n'
            '        );\n'
            '      }\n'
            '      const cfrOutputPath = join69(workDir, `cfr.${plan2.dimensions.format}`);\n'
            '      const cfrGopSize = Math.max(1, Math.round(plan2.dimensions.fpsNum / plan2.dimensions.fpsDen));\n'
            '      const cfrArgs = [\n'
            '        "-i",\n'
            '        concatOutputPath,\n'
            '        "-c:v",\n'
            '        "h264_mf",\n'
            '        "-rate_control",\n'
            '        "quality",\n'
            '        "-quality",\n'
            '        "72",\n'
            '        "-scenario",\n'
            '        "archive",\n'
            '        "-hw_encoding",\n'
            '        "0",\n'
            '        "-g",\n'
            '        String(cfrGopSize),\n'
            '        "-keyint_min",\n'
            '        String(cfrGopSize),\n'
            '        "-force_key_frames",\n'
            '        `expr:eq(mod(n,${cfrGopSize}),0)`,\n'
            '        "-flags",\n'
            '        "+cgop",\n'
            '        "-bf",\n'
            '        "0",\n'
            '        "-pix_fmt",\n'
            '        "yuv420p",\n'
            '        "-fps_mode",\n'
            '        "cfr",\n'
            '        "-r",\n'
            '        fpsArg,\n'
            '        "-y",\n'
            '        cfrOutputPath\n'
            '      ];\n'
            '      const cfrResult = await runFfmpeg(cfrArgs, { signal: abortSignal });\n'
            '      if (!cfrResult.success) {\n'
            '        throw new Error(\n'
            '          `[assemble] ffmpeg cfr re-encode failed (exit ${cfrResult.exitCode}): ${cfrResult.stderr.slice(-400)}`\n'
            '        );\n'
            '      }\n'
            '      postConcatPath = cfrOutputPath;\n'
            '      log2.info("[assemble] cfr re-encode applied", {\n'
            '        format: plan2.dimensions.format,\n'
            '        fpsNum: plan2.dimensions.fpsNum,\n'
            '        fpsDen: plan2.dimensions.fpsDen,\n'
            '        codecStrategy: "h264_mf"\n'
            '      });\n'
            '    }\n'
        ),
        label="replace CFR H.264 re-encode",
    )

    text = _replace_region(
        text,
        scope="function transcodeToMp4(inputPath, outputPath) {",
        start_marker="    const child = spawn13(\n",
        end_marker="    child.on(\"close\"",
        replacement=(
            '    const child = spawn13(\n'
            '      ffmpegPath,\n'
            '      [\n'
            '        "-i",\n'
            '        inputPath,\n'
            '        "-c:v",\n'
            '        "h264_mf",\n'
            '        "-rate_control",\n'
            '        "quality",\n'
            '        "-quality",\n'
            '        "72",\n'
            '        "-scenario",\n'
            '        "archive",\n'
            '        "-hw_encoding",\n'
            '        "0",\n'
            '        "-bf",\n'
            '        "0",\n'
            '        "-pix_fmt",\n'
            '        "yuv420p",\n'
            '        "-c:a",\n'
            '        "aac",\n'
            '        "-b:a",\n'
            '        "192k",\n'
            '        "-movflags",\n'
            '        "+faststart",\n'
            '        "-y",\n'
            '        outputPath\n'
            '      ],\n'
            '      { stdio: "pipe" }\n'
            '    );\n'
        ),
        label="replace scaffold media transcode",
    )

    text = _replace_region(
        text,
        scope="async function renderLocal(projectDir, outputPath, options) {",
        start_marker='  if (!options.gpu && options.format === "mp4" && preflight.ffmpegPath) {\n',
        end_marker="  const producer = await loadProducer();",
        replacement=(
            '  if (options.format === "mp4" && preflight.ffmpegPath) {\n'
            '    if (options.gpu) {\n'
            '      throw new Error("[shiyi-mf] --gpu is disabled for the fixed h264_mf portable render policy.");\n'
            '    }\n'
            '    detectH264EncoderMode(preflight.ffmpegPath, false);\n'
            '  }\n'
        ),
        label="fail closed during local render encoder preflight",
    )

    text = _replace_region(
        text,
        scope="async function fetchGoogleFont(familyName, options, fontText) {",
        start_marker="async function fetchGoogleFont(familyName, options, fontText) {\n",
        end_marker="function extractGoogleFontsText(html) {",
        replacement=(
            "async function fetchGoogleFont(familyName, options, fontText) {\n"
            "  return [];\n"
            "}\n"
        ),
        label="disable Google Fonts network resolution in the offline runtime",
    )
    text = _replace_region(
        text,
        scope="async function fetchExternalStylesheetCss(href) {",
        start_marker="async function fetchExternalStylesheetCss(href) {\n",
        end_marker="function extractFontFaceBlocks(css) {",
        replacement=(
            "async function fetchExternalStylesheetCss(href) {\n"
            "  return null;\n"
            "}\n"
        ),
        label="disable external stylesheet fetching in the offline runtime",
    )

    text = _replace_region(
        text,
        scope="async function checkForUpdate(force) {",
        start_marker="async function checkForUpdate(force) {\n",
        end_marker="function fallbackResult(cachedLatest) {",
        replacement=(
            'async function checkForUpdate(force) {\n'
            '  return { current: VERSION, latest: VERSION, updateAvailable: false, offline: true };\n'
            '}\n'
        ),
        label="disable npm update requester",
    )
    text = _replace_region(
        text,
        scope="function getUpdateMeta() {",
        start_marker="function getUpdateMeta() {\n",
        end_marker="function withMeta(data2, options) {",
        replacement=(
            'function getUpdateMeta() {\n'
            '  return { version: VERSION, updateAvailable: false, offline: true };\n'
            '}\n'
        ),
        label="remove cached latest version from command metadata",
    )
    text = _replace_exact(
        text,
        'NPM_REGISTRY_URL = "https://registry.npmjs.org/hyperframes/latest";',
        'NPM_REGISTRY_URL = "urn:shiyi:offline-update-disabled";',
        label="make accidental update URL non-networkable",
    )

    cli_bootstrap_anchor = "var cliCommandArg = process.argv[2];\n"
    text = _replace_exact(
        text,
        cli_bootstrap_anchor,
        (
            'process.env.HYPERFRAMES_NO_UPDATE_CHECK = "1";\n'
            'process.env.HYPERFRAMES_NO_AUTO_INSTALL = "1";\n'
            'process.env.HYPERFRAMES_NO_TELEMETRY = "1";\n'
            'process.env.HYPERFRAMES_SKIP_SKILLS = "1";\n'
            + cli_bootstrap_anchor
        ),
        label="force offline CLI policy before startup side effects",
    )
    text = _replace_region(
        text,
        scope='if (!isHelp && !hasJsonFlag && command !== "upgrade"',
        start_marker='if (!isHelp && !hasJsonFlag && command !== "upgrade"',
        end_marker="var commandStart = Date.now();",
        replacement=(
            '// Shiyi offline runtime: background package, skill and auto-install checks are disabled.\n'
        ),
        label="remove background update and skill network checks",
    )

    remaining_libx264 = text.count("libx264")
    if remaining_libx264 != 4:
        raise PatchContractError(
            f"remaining libx264 audit: expected 4 non-argument references, found {remaining_libx264}"
        )
    text = text.replace("libx264", "h264_mf")

    for forbidden in ("-x264-params", "libx264", "openh264", "rav1e"):
        if forbidden in text:
            raise PatchContractError(f"patched runtime still contains prohibited encoder path: {forbidden}")
    if text.count('"-preset"') != 0:
        raise PatchContractError("patched runtime still contains an executable preset argument")
    crf_offsets: list[int] = []
    cursor = 0
    while True:
        offset = text.find('"-crf"', cursor)
        if offset < 0:
            break
        crf_offsets.append(offset)
        cursor = offset + 1
    if len(crf_offsets) != 5 or any(
        "libvpx" not in text[max(0, offset - 320) : offset + 120]
        for offset in crf_offsets
    ):
        raise PatchContractError("CRF audit found an H.264 or unknown encoder path")
    profile_offsets: list[int] = []
    cursor = 0
    while True:
        offset = text.find('"-profile:v"', cursor)
        if offset < 0:
            break
        profile_offsets.append(offset)
        cursor = offset + 1
    if len(profile_offsets) != 3 or any(
        "prores" not in text[max(0, offset - 320) : offset + 120]
        for offset in profile_offsets
    ):
        raise PatchContractError("profile audit found a non-ProRes encoder path")
    for sentinel in (
        f"Apache-2.0 modification notice: Shanghai Shiyi Brand Management Co., Ltd. modified this file via {PATCH_ID} v{PATCH_VERSION}",
        'function appendShiyiH264MfArgs(args, options = {})',
        'return { encoder: "h264-mf-windows"',
        'NPM_REGISTRY_URL = "urn:shiyi:offline-update-disabled";',
        'process.env.HYPERFRAMES_NO_TELEMETRY = "1";',
        "async function fetchGoogleFont(familyName, options, fontText) {\n  return [];\n}",
        "async function fetchExternalStylesheetCss(href) {\n  return null;\n}",
    ):
        if sentinel not in text:
            raise PatchContractError(f"patched runtime is missing sentinel: {sentinel}")
    return text


def _load_package(package_root: Path) -> tuple[Path, bytes]:
    package_json_path = package_root / "package.json"
    cli_path = package_root / "dist" / "cli.js"
    try:
        package = json.loads(package_json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PatchContractError(f"HyperFrames package metadata is unreadable: {package_json_path}") from exc
    if package.get("name") != "hyperframes" or package.get("version") != HYPERFRAMES_VERSION:
        raise PatchContractError(
            f"expected hyperframes@{HYPERFRAMES_VERSION}, got {package.get('name')}@{package.get('version')}"
        )
    try:
        payload = cli_path.read_bytes()
    except OSError as exc:
        raise PatchContractError(f"HyperFrames executable bundle is unreadable: {cli_path}") from exc
    return cli_path, payload


def _result(status: str, package_root: Path, cli_sha256: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": status,
        "patch_id": PATCH_ID,
        "patch_version": PATCH_VERSION,
        "package": f"hyperframes@{HYPERFRAMES_VERSION}",
        "package_root": str(package_root.resolve()),
        "modified_file": "dist/cli.js",
        "cli_sha256": cli_sha256,
        "codec_strategy": "h264_mf",
        "offline_update_policy": "disabled",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply the exact HyperFrames 0.7.86 Windows Media Foundation/offline patch."
    )
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="Require the patched hash without writing.")
    parser.add_argument(
        "--calculate",
        action="store_true",
        help="Print the deterministic patched hash for the frozen upstream input without writing.",
    )
    args = parser.parse_args(argv)
    package_root = args.package_root.resolve()
    cli_path, payload = _load_package(package_root)
    current_sha256 = _sha256_bytes(payload)

    if PATCHED_CLI_SHA256 and current_sha256 == PATCHED_CLI_SHA256:
        print(json.dumps(_result("already_patched", package_root, current_sha256), ensure_ascii=False))
        return 0
    if current_sha256 != UPSTREAM_CLI_SHA256:
        raise PatchContractError(
            f"dist/cli.js hash is neither frozen upstream nor patched: {current_sha256}"
        )
    patched = _patched_text(payload.decode("utf-8"))
    patched_payload = patched.encode("utf-8")
    patched_sha256 = _sha256_bytes(patched_payload)
    if args.calculate:
        print(json.dumps(_result("calculated", package_root, patched_sha256), ensure_ascii=False))
        return 0
    if args.check:
        raise PatchContractError("frozen upstream bundle is not patched")
    if not PATCHED_CLI_SHA256:
        raise PatchContractError(
            f"PATCHED_CLI_SHA256 is not frozen; calculated value is {patched_sha256}"
        )
    if patched_sha256 != PATCHED_CLI_SHA256:
        raise PatchContractError(
            f"patched output hash mismatch: expected {PATCHED_CLI_SHA256}, got {patched_sha256}"
        )
    temporary = cli_path.with_name(f".{cli_path.name}.{PATCH_ID}.tmp")
    temporary.write_bytes(patched_payload)
    temporary.replace(cli_path)
    print(json.dumps(_result("patched", package_root, patched_sha256), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PatchContractError as exc:
        print(f"HYPERFRAMES_WINDOWS_MF_PATCH_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
