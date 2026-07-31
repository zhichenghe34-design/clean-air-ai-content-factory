from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MIN_USEFUL_TEXT_CHARS = 200
PLATFORM_HOSTS = {
    "douyin": ("douyin.com", "iesdouyin.com"),
    "bilibili": ("bilibili.com", "b23.tv"),
    "youtube": ("youtube.com", "youtu.be"),
    "x": ("x.com", "twitter.com"),
    "tiktok": ("tiktok.com",),
}
PARSER_CONFIG_ALLOWLIST = {
    "ffmpeg_path", "ffprobe_path", "frame_sample_fps", "enable_ocr", "enable_asr",
    "ocr_engine", "asr_engine", "whisper_model", "whisper_device", "allow_cpu_fallback",
    "cuda_dll_dirs", "audio_language", "save_frames", "delete_temp_audio",
    "dedupe_similarity", "min_text_length", "video_extensions", "audio_extensions", "image_extensions",
}
SENSITIVE_CONFIG_MARKERS = ("key", "token", "secret", "password", "cookie", "authorization")


class ExtractionError(RuntimeError):
    pass


class BodyTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.title_depth = 0
        self.text: list[str] = []
        self.title: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "template"}:
            self.skip_depth += 1
        if tag.lower() == "title":
            self.title_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "template"} and self.skip_depth:
            self.skip_depth -= 1
        if tag.lower() == "title" and self.title_depth:
            self.title_depth -= 1

    def handle_data(self, data: str) -> None:
        value = re.sub(r"\s+", " ", data).strip()
        if not value:
            return
        if self.title_depth:
            self.title.append(value)
        if not self.skip_depth:
            self.text.append(value)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def detect_platform(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    for platform, suffixes in PLATFORM_HOSTS.items():
        if any(host == suffix or host.endswith("." + suffix) for suffix in suffixes):
            return platform
    return "web"


def planned_routes(url: str) -> list[str]:
    if detect_platform(url) == "web":
        return ["direct_http", "playwright", "manual_auth"]
    return ["one_stop_media_parser", "direct_http", "playwright", "manual_auth"]


def validate_public_url(url: str, *, resolve_dns: bool = True) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ExtractionError("only public HTTP(S) URLs are allowed")
    if parsed.username or parsed.password:
        raise ExtractionError("credentials in URLs are blocked")
    host = parsed.hostname.strip("[]")
    addresses: set[str] = set()
    host_is_literal = False
    try:
        addresses.add(str(ipaddress.ip_address(host)))
        host_is_literal = True
    except ValueError:
        if resolve_dns:
            try:
                default_port = 443 if parsed.scheme.lower() == "https" else 80
                addresses.update(item[4][0] for item in socket.getaddrinfo(host, parsed.port or default_port))
            except OSError as exc:
                raise ExtractionError(f"DNS resolution failed: {exc}") from exc
    proxy_fake_networks = (
        ipaddress.ip_network("198.18.0.0/15"),
        ipaddress.ip_network("fdfe:dcba:9876::/48"),
    )
    proxy_is_configured = bool(urllib.request.getproxies())
    for value in addresses:
        ip = ipaddress.ip_address(value)
        if (
            not host_is_literal
            and proxy_is_configured
            and any(ip.version == network.version and ip in network for network in proxy_fake_networks)
        ):
            continue
        if not ip.is_global:
            raise ExtractionError(f"private or non-global target is blocked: {ip}")
    return parsed


class PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def extract_html(html: str) -> tuple[str, str]:
    parser = BodyTextParser()
    parser.feed(html)
    text = "\n".join(dict.fromkeys(parser.text))
    return " ".join(parser.title).strip(), text.strip()


def direct_http(url: str) -> dict[str, Any]:
    validate_public_url(url)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "CleanAirContentFactory/0.2 (+public research; read-only)"},
    )
    opener = urllib.request.build_opener(PublicRedirectHandler())
    with opener.open(request, timeout=30) as response:
        final_url = response.geturl()
        validate_public_url(final_url)
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ExtractionError("response exceeds 5 MiB limit")
        content_type = response.headers.get_content_type()
        charset = response.headers.get_content_charset() or "utf-8"
    decoded = raw.decode(charset, errors="replace")
    if content_type in {"text/html", "application/xhtml+xml"}:
        title, text = extract_html(decoded)
    else:
        title, text = "", decoded.strip()
    return {
        "final_url": final_url,
        "title": title,
        "text": text,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content_type": content_type,
    }


def playwright_extract(url: str) -> dict[str, Any]:
    validate_public_url(url)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ExtractionError("Playwright is not installed") from exc
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(1200)
            final_url = page.url
            validate_public_url(final_url)
            text = (page.locator("body").inner_text(timeout=10_000) or "").strip()
            html = page.content()
            return {
                "final_url": final_url,
                "title": page.title(),
                "text": text,
                "sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
                "content_type": "text/html; rendered=playwright",
            }
        finally:
            browser.close()


def resolve_parser_root(value: str | None) -> Path | None:
    candidate = value or os.environ.get("ONE_STOP_MEDIA_PARSER_ROOT", "")
    if not candidate:
        return None
    root = Path(candidate).expanduser().resolve()
    if (root / "音视频解析器").is_dir():
        root = root / "音视频解析器"
    if not (root / "src" / "download_core.py").is_file():
        raise ExtractionError("configured media parser root is invalid")
    return root


def parser_python(root: Path) -> Path:
    candidate = root / ".venv" / "Scripts" / "python.exe"
    return candidate if candidate.is_file() else Path(sys.executable)


def build_isolated_parser_config(source: dict[str, Any], input_dir: Path, output_dir: Path) -> dict[str, Any]:
    if not isinstance(source, dict):
        raise ExtractionError("media parser config must be a JSON object")
    safe: dict[str, Any] = {}
    for key in PARSER_CONFIG_ALLOWLIST:
        if key in source and not any(marker in key.lower() for marker in SENSITIVE_CONFIG_MARKERS):
            safe[key] = source[key]
    safe.update({"input_dir": str(input_dir), "output_dir": str(output_dir)})
    return safe


def run_media_parser(url: str, output: Path, root: Path, analyze_media: bool) -> dict[str, Any]:
    media_dir = output / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    command = [str(parser_python(root)), "-m", "download_core", str(media_dir), url]
    process = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=600)
    if process.returncode:
        raise ExtractionError((process.stderr or process.stdout or "media parser failed")[-1200:])

    records = []
    for path in media_dir.glob("*.source.json"):
        records.append(json.loads(path.read_text(encoding="utf-8")))
    artifacts = sorted(str(path) for path in media_dir.iterdir() if path.is_file())
    transcript_path = None
    analysis_error = None
    integrated_text = "\n\n".join(str(item.get("text") or item.get("description") or "") for item in records).strip()

    if analyze_media and any(Path(path).suffix.lower() in {".mp4", ".mkv", ".mov", ".webm", ".mp3", ".wav", ".m4a"} for path in artifacts):
        base_config_path = root / "config.json"
        if not base_config_path.is_file():
            base_config_path = root / "config.example.json"
        source_config = json.loads(base_config_path.read_text(encoding="utf-8"))
        analysis_root = output / "analysis"
        config = build_isolated_parser_config(source_config, media_dir, analysis_root)
        handle, isolated_name = tempfile.mkstemp(prefix="shiyi-parser-", suffix=".json")
        os.close(handle)
        isolated_config = Path(isolated_name)
        try:
            isolated_config.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            analysis = subprocess.run(
                [str(parser_python(root)), str(root / "src" / "main.py"), "--config", str(isolated_config)],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=3600,
            )
        finally:
            isolated_config.unlink(missing_ok=True)
        if analysis.returncode:
            analysis_error = (analysis.stderr or analysis.stdout or "media analysis failed")[-1200:]
        else:
            sessions = sorted((analysis_root).glob("*"), key=lambda path: path.stat().st_mtime, reverse=True)
            if sessions:
                integrated = sessions[0] / "综合资料.md"
                if integrated.is_file():
                    transcript_path = str(integrated)
                    integrated_text = integrated.read_text(encoding="utf-8")
                    artifacts.append(str(integrated))

    return {
        "title": str(records[0].get("title", "")) if records else "",
        "text": integrated_text,
        "records": records,
        "artifacts": artifacts,
        "transcript_path": transcript_path,
        "analysis_error": analysis_error,
    }


def attempt_record(route: str, status: str, detail: str, started: float) -> dict[str, Any]:
    return {"route": route, "status": status, "detail": detail, "duration_ms": round((time.monotonic() - started) * 1000)}


def extract(
    url: str,
    output: Path,
    *,
    media_parser_root: str | None = None,
    analyze_media: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    validate_public_url(url, resolve_dns=not dry_run)
    platform = detect_platform(url)
    routes = planned_routes(url)
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "planned" if dry_run else "failed",
        "source": {"url": url, "final_url": None, "platform": platform, "title": "", "fetched_at": now_iso(), "sha256": None},
        "content": {"text": "", "text_chars": 0, "transcript_path": None},
        "artifacts": [],
        "attempts": [],
        "warnings": [],
        "next_action": None,
    }
    if dry_run:
        base["attempts"] = [{"route": route, "status": "planned", "detail": "not executed"} for route in routes]
        return base

    best: dict[str, Any] | None = None
    parser_root: Path | None = None
    if platform != "web":
        started = time.monotonic()
        try:
            parser_root = resolve_parser_root(media_parser_root)
            if parser_root is None:
                raise ExtractionError("approved media parser is not configured")
            result = run_media_parser(url, output, parser_root, analyze_media)
            base["attempts"].append(attempt_record("one_stop_media_parser", "complete" if result["text"] else "partial", f"{len(result['artifacts'])} artifacts", started))
            base["artifacts"] = result["artifacts"]
            base["content"].update({"text": result["text"], "text_chars": len(result["text"]), "transcript_path": result["transcript_path"]})
            base["source"]["title"] = result["title"]
            if result.get("analysis_error"):
                base["warnings"].append({"route": "media_analysis", "message": result["analysis_error"]})
            if result["text"] and (result["transcript_path"] or not analyze_media):
                base["status"] = "complete" if result["transcript_path"] else "partial"
                base["next_action"] = None if result["transcript_path"] else "run again with --analyze-media for ASR/OCR"
                return base
        except Exception as exc:
            base["attempts"].append(attempt_record("one_stop_media_parser", "failed", str(exc), started))

    for route, runner in (("direct_http", direct_http), ("playwright", playwright_extract)):
        started = time.monotonic()
        try:
            result = runner(url)
            useful = len(result["text"]) >= MIN_USEFUL_TEXT_CHARS
            base["attempts"].append(attempt_record(route, "complete" if useful else "partial", f"{len(result['text'])} text characters", started))
            if best is None or len(result["text"]) > len(best["text"]):
                best = result
            if useful and platform == "web":
                break
        except Exception as exc:
            base["attempts"].append(attempt_record(route, "failed", str(exc), started))

    if best:
        text = best["text"]
        base["source"].update({"final_url": best["final_url"], "title": best["title"], "sha256": best["sha256"]})
        base["content"].update({"text": text, "text_chars": len(text)})
        base["status"] = "complete" if len(text) >= MIN_USEFUL_TEXT_CHARS and platform == "web" else "partial"
        base["next_action"] = None if base["status"] == "complete" else "authorize a read-only browser or configure the approved media parser"
    else:
        missing_adapter = any(
            item.get("status") == "failed"
            and any(marker in str(item.get("detail", "")).lower() for marker in ("not installed", "not configured"))
            for item in base["attempts"]
        )
        if missing_adapter:
            base["status"] = "adapter_missing"
        base["next_action"] = "authorize a read-only browser or install/configure an approved extraction adapter"
    return base


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely extract public web and platform content through fallback routes")
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--media-parser-root")
    parser.add_argument("--analyze-media", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    try:
        result = extract(
            args.url,
            output,
            media_parser_root=args.media_parser_root,
            analyze_media=args.analyze_media,
            dry_run=args.dry_run,
        )
        destination = output / "extraction.json"
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(destination)
        return 0 if result["status"] in {"complete", "partial", "planned", "adapter_missing"} else 2
    except Exception as exc:
        output.mkdir(parents=True, exist_ok=True)
        failure = {"schema_version": SCHEMA_VERSION, "status": "blocked", "error": str(exc), "source": {"url": args.url}, "attempts": []}
        destination = output / "extraction.json"
        destination.write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
        print(destination)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
