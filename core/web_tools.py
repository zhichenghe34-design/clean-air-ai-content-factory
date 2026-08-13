from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit, urlunsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGED_EXTRACT_SCRIPT = REPO_ROOT / "product-tools" / "extract_url.pyc"
SOURCE_EXTRACT_SCRIPT = (
    REPO_ROOT
    / "agent-skills"
    / "extract-web-platform-content"
    / "scripts"
    / "extract_url.py"
)
EXTRACT_SCRIPT = PACKAGED_EXTRACT_SCRIPT if PACKAGED_EXTRACT_SCRIPT.is_file() else SOURCE_EXTRACT_SCRIPT


class SearchProvider(Protocol):
    def search(self, query: str, max_results: int) -> list[dict[str, str]]: ...


class DDGSSearchProvider:
    """Free, keyless search adapter. DDGS is replaceable through SearchProvider."""

    def search(self, query: str, max_results: int) -> list[dict[str, str]]:
        from ddgs import DDGS

        rows = DDGS().text(query, max_results=max_results)
        return [
            {
                "title": str(row.get("title", "")),
                "url": str(row.get("href") or row.get("url") or ""),
                "snippet": str(row.get("body") or row.get("snippet") or ""),
            }
            for row in rows
            if row.get("href") or row.get("url")
        ]


def canonical_url(value: str) -> str:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("仅允许公开 HTTP(S) URL")
    host = parsed.hostname.lower()
    port = parsed.port
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


class TrustedWebToolRegistry:
    """Executes only named, bounded tools and records an auditable trace."""

    def __init__(
        self,
        output_dir: Path,
        config: dict[str, Any] | None = None,
        *,
        search_provider: SearchProvider | None = None,
        extractor: Callable[[str, Path], dict[str, Any]] | None = None,
        seed_urls: list[str] | None = None,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.config = config or {}
        self.search_provider = search_provider or DDGSSearchProvider()
        self.extractor = extractor or self._run_packaged_extractor
        self.max_search_calls = int(self.config.get("max_search_calls", 3))
        self.max_pages = int(self.config.get("max_pages", 5))
        self.max_results = int(self.config.get("max_results_per_search", 5))
        self.max_chars = int(self.config.get("max_chars_per_page", 6000))
        self.search_calls = 0
        self.page_calls = 0
        self.trace: list[dict[str, Any]] = []
        self.allowed_urls: set[str] = set()
        self.topic = ""
        self.authorize_seed_urls(seed_urls or [])

    def authorize_seed_urls(self, urls: list[str] | tuple[str, ...]) -> list[str]:
        """Authorize bounded, server-selected seed pages before tool execution."""

        normalized: list[str] = []
        for url in urls:
            value = canonical_url(url)
            if value not in self.allowed_urls:
                self.allowed_urls.add(value)
            if value not in normalized:
                normalized.append(value)
        return normalized

    def set_topic(self, topic: str) -> None:
        self.topic = str(topic).strip()[:120]

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "搜索公开网页，返回可供后续提取的来源候选。搜索由工具执行，不是模型记忆。",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string", "minLength": 2, "maxLength": 200}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "extract_url",
                    "description": "读取用户提供或搜索工具返回的公开URL；支持普通网页并尝试动态/视频平台降级路线。",
                    "parameters": {
                        "type": "object",
                        "properties": {"url": {"type": "string", "minLength": 8, "maxLength": 2048}},
                        "required": ["url"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "web_search":
                result = self._search(str(arguments.get("query", "")))
            elif name == "extract_url":
                result = self._extract(str(arguments.get("url", "")))
            else:
                result = {"ok": False, "error": f"未注册工具: {name}"}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        self.trace.append({"tool": name, "arguments": arguments, "result": result})
        return result

    def _search(self, query: str) -> dict[str, Any]:
        query = query.strip()
        if len(query) < 2:
            raise ValueError("搜索词过短")
        if self.search_calls >= self.max_search_calls:
            raise RuntimeError("已达到搜索次数上限")
        original_query = query
        if self.topic and self.topic not in query:
            query = f"{self.topic} {query}"
        self.search_calls += 1
        rows = self.search_provider.search(query, self.max_results)
        results: list[dict[str, str]] = []
        for row in rows[: self.max_results]:
            try:
                url = canonical_url(row.get("url", ""))
            except (ValueError, TypeError):
                continue
            self.allowed_urls.add(url)
            results.append({"title": row.get("title", ""), "url": url, "snippet": row.get("snippet", "")})
        return {"ok": True, "query": query, "original_query": original_query, "query_reanchored": query != original_query, "results": results}

    def _extract(self, url: str) -> dict[str, Any]:
        normalized = canonical_url(url)
        if normalized not in self.allowed_urls:
            raise PermissionError("该URL不是用户输入或搜索工具返回的地址，拒绝访问")
        if self.page_calls >= self.max_pages:
            raise RuntimeError("已达到页面提取上限")
        self.page_calls += 1
        page_dir = self.output_dir / f"page-{self.page_calls:02d}"
        result = self.extractor(normalized, page_dir)
        content = result.get("content") if isinstance(result.get("content"), dict) else {}
        source = result.get("source") if isinstance(result.get("source"), dict) else {}
        text = str(content.get("text", ""))[: self.max_chars]
        return {
            "ok": result.get("status") in {"complete", "partial"},
            "status": result.get("status", "unknown"),
            "error": str(result.get("error", "")),
            "url": normalized,
            "final_url": source.get("final_url") or normalized,
            "title": source.get("title", ""),
            "text": text,
            "text_truncated": int(content.get("text_chars", len(text))) > len(text),
            "attempts": result.get("attempts", []),
            "warnings": result.get("warnings", []),
        }

    def _run_packaged_extractor(self, url: str, output_dir: Path) -> dict[str, Any]:
        command = [sys.executable, str(EXTRACT_SCRIPT), "--url", url, "--output", str(output_dir)]
        parser_root = str(self.config.get("media_parser_root", "")).strip()
        if parser_root:
            command.extend(["--media-parser-root", parser_root])
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=660,
        )
        result_path = output_dir / "extraction.json"
        if not result_path.is_file():
            detail = (process.stderr or process.stdout or "提取工具未生成结果")[-1200:]
            raise RuntimeError(detail)
        return json.loads(result_path.read_text(encoding="utf-8"))
