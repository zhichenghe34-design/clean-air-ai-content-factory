from __future__ import annotations

import argparse
import json
import mimetypes
import secrets
import threading
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from core.catalog import HardwareProbe, PackageCatalog
from core.config import ConfigStore
from core.discovery import ProjectDiscovery
from core.orchestrator import JobStore, UnprocessableError, WorkflowError, local_fallback_plan
from core.production import DEFAULT_INPUT, ProductionRunner, estimate_narration_duration, review_script
from core.provider import BudgetLedger, OpenAICompatibleProvider, ProviderError


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
RUNTIME_DIR = APP_DIR / "runtime"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
DISCOVERY_CACHE = RUNTIME_DIR / "discovery.json"
CATALOG_FILE = APP_DIR / "catalog" / "package-catalog.json"

config_store = ConfigStore(RUNTIME_DIR)
config_store.ensure_storage_layout()
job_store = JobStore(RUNTIME_DIR)
package_catalog = PackageCatalog(CATALOG_FILE)
state_lock = threading.Lock()
SESSION_COOKIE = "shiyi_session"
SESSION_ID = secrets.token_urlsafe(32)
CSRF_TOKEN = secrets.token_urlsafe(32)


def load_discovery_cache() -> dict:
    if DISCOVERY_CACHE.exists():
        try:
            cached = json.loads(DISCOVERY_CACHE.read_text(encoding="utf-8"))
            if isinstance(cached, dict):
                return {
                    "tools": cached.get("tools", []),
                    "last_scan": cached.get("last_scan"),
                    "last_scan_report": cached.get("last_scan_report"),
                }
        except (OSError, json.JSONDecodeError):
            pass
    return {"tools": [], "last_scan": None, "last_scan_report": None}


app_state = load_discovery_cache()


class AppHandler(BaseHTTPRequestHandler):
    server_version = "ShiyiContentFactory/0.2"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/session":
                self.json_response({"csrf_token": CSRF_TOKEN, "same_origin_required": True}, set_session=True)
            elif path == "/api/status":
                self.json_response(self._status())
            elif path == "/api/config":
                self.json_response(config_store.public_config())
            elif path == "/api/tools":
                with state_lock:
                    self.json_response({"tools": app_state["tools"], "last_scan": app_state["last_scan"], "report": app_state["last_scan_report"]})
            elif path == "/api/jobs":
                self.json_response({"jobs": job_store.list()})
            elif path.startswith("/api/jobs/") and "/review-artifacts/" in path:
                parts = path.strip("/").split("/")
                if len(parts) != 5:
                    raise FileNotFoundError("接口不存在")
                self.serve_file(job_store.resolve_review_artifact(parts[2], parts[4]), parts[4])
            elif path.startswith("/api/jobs/") and "/runs/" in path and "/artifacts/" in path:
                parts = path.strip("/").split("/")
                if len(parts) != 7:
                    raise FileNotFoundError("接口不存在")
                self.serve_file(job_store.resolve_artifact(parts[2], parts[6], run_id=parts[4]), parts[6])
            elif path.startswith("/api/jobs/") and "/artifacts/" in path:
                parts = path.strip("/").split("/")
                if len(parts) != 5:
                    raise FileNotFoundError("接口不存在")
                self.serve_file(job_store.resolve_artifact(parts[2], parts[4]), parts[4])
            elif path.startswith("/api/jobs/"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    raise FileNotFoundError("接口不存在")
                self.json_response(job_store.get(parts[2]))
            elif path == "/api/catalog":
                self.json_response(package_catalog.load())
            elif path == "/api/hardware":
                catalog = package_catalog.load()
                hardware = HardwareProbe.probe()
                profile = package_catalog.select_profile(catalog, hardware["gpu"]["vram_gb"])
                recommendations = package_catalog.recommendations(catalog, profile["id"])
                self.json_response({
                    "hardware": hardware,
                    "profile": profile,
                    "recommended_package_ids": [item["id"] for item in recommendations],
                    "auto_install_enabled": catalog["policy"]["auto_install_enabled"],
                    "storage": config_store.public_config()["storage"],
                })
            elif path == "/api/storage":
                self.json_response({"storage": config_store.public_config()["storage"]})
            elif path.startswith("/api/"):
                self.error_response("not_found", "接口不存在", HTTPStatus.NOT_FOUND)
            else:
                self.serve_static(path)
        except Exception as exc:
            self.handle_exception(exc)

    def do_POST(self) -> None:
        try:
            self.require_mutation_security()
            path = urlparse(self.path).path
            body = self.read_json()
            if path == "/api/config":
                self.json_response(config_store.save(body))
            elif path == "/api/discover":
                self.json_response(self._discover(body))
            elif path == "/api/provider/test":
                self.json_response(self._provider().test_connection())
            elif path == "/api/agent/plan":
                self.json_response(self._plan(body))
            elif path == "/api/jobs":
                plan = body.get("plan")
                if not isinstance(plan, dict):
                    raise UnprocessableError("缺少有效计划")
                self.json_response(job_store.create(plan, production_input=body.get("production_input")), HTTPStatus.CREATED)
            elif path == "/api/demo-job":
                supplied = body.get("production_input") or {}
                if not isinstance(supplied, dict):
                    raise UnprocessableError("production_input必须是JSON对象")
                production_input = dict(DEFAULT_INPUT)
                production_input.update(supplied)
                plan = local_fallback_plan(f"制作样片：{production_input['topic']}", [])
                for step in plan["steps"]:
                    if step.get("capability") != "human_refinement":
                        step["tool_id"] = "trusted-local-production-adapter"
                        step["risk"] = "固定路径本地适配器，仍需人工批准"
                plan["summary"] = "v2分阶段样片：研究、证据人工审定、脚本合规放行、配音与成片。"
                self.json_response(job_store.create(plan, production_input=production_input), HTTPStatus.CREATED)
            elif path.startswith("/api/jobs/") and path.endswith("/approve"):
                self.json_response(job_store.approve(path.split("/")[3]))
            elif path.startswith("/api/jobs/") and path.endswith("/approvals/research"):
                self.json_response(job_store.approve_research(path.split("/")[3], body))
            elif path.startswith("/api/jobs/") and path.endswith("/approvals/compliance"):
                self.json_response(job_store.approve_compliance(path.split("/")[3], body))
            elif path.startswith("/api/jobs/") and path.endswith("/run"):
                job_id = path.split("/")[3]
                job = job_store.get(job_id)
                if not isinstance(job.get("production_input"), dict):
                    allow = bool(config_store.load()["security"].get("allow_external_commands", False))
                    self.json_response(job_store.run_safe(job_id, allow_external_commands=allow))
                else:
                    limit = int(config_store.load().get("research", {}).get("max_provider_calls_per_job", 7))
                    budget = BudgetLedger(limit=limit, snapshot=job.get("budget"))
                    provider = self._provider(budget)
                    runner = ProductionRunner(provider=provider, research_config=config_store.load().get("research", {}), budget=budget)
                    self.json_response(job_store.advance(job_id, runner, self.headers.get("Idempotency-Key", "")))
            else:
                self.error_response("not_found", "接口不存在", HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.handle_exception(exc)

    def do_PATCH(self) -> None:
        try:
            self.require_mutation_security()
            path = urlparse(self.path).path
            body = self.read_json()
            if path.startswith("/api/jobs/") and path.endswith("/script"):
                value = str(body.get("script", "")).strip()
                estimate = estimate_narration_duration(value)
                if not 35 <= float(estimate["estimated_seconds"]) <= 75:
                    raise UnprocessableError("脚本预计口播时长必须在35到75秒之间", details=estimate)
                job_id = path.split("/")[3]
                self.json_response(job_store.update_script(job_id, value, review_script(value, job_store.approved_findings(job_id)), estimate))
            else:
                self.error_response("not_found", "接口不存在", HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.handle_exception(exc)

    def do_OPTIONS(self) -> None:
        self.error_response("method_not_allowed", "不提供跨来源请求", HTTPStatus.METHOD_NOT_ALLOWED)

    def _status(self) -> dict:
        config = config_store.public_config()
        with state_lock:
            tools = list(app_state["tools"])
        catalog = package_catalog.load()
        discovered_capabilities = {cap for tool in tools for cap in tool.get("capabilities", [])}
        bundled_capabilities = {
            cap
            for package in catalog.get("packages", [])
            if package.get("trust_status") == "approved_bundled_skill"
            for cap in package.get("capabilities", [])
        }
        return {
            "name": "时宜 AIGC 内容工厂",
            "version": "0.2.0",
            "schema_version": 2,
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "provider": config["provider"]["name"],
            "model": config["provider"]["model"],
            "provider_ready": config["provider"]["has_api_key"],
            "tool_count": len(tools),
            "capabilities": sorted(discovered_capabilities | bundled_capabilities),
            "job_count": len(job_store.list()),
            "safe_mode": not config["security"].get("allow_external_commands", False),
            "catalog_package_count": len(catalog.get("packages", [])),
            "catalog_install_enabled": catalog["policy"]["auto_install_enabled"],
        }

    def _discover(self, body: dict) -> dict:
        config = config_store.load()
        roots = body.get("roots") or config["discovery"].get("roots", [])
        if not isinstance(roots, list):
            raise UnprocessableError("roots必须是数组")
        discovery = ProjectDiscovery(
            max_depth=config["discovery"].get("max_depth", 3),
            max_directories=config["discovery"].get("max_directories", 1500),
        )
        report = discovery.scan(roots)
        with state_lock:
            app_state["tools"] = report["tools"]
            app_state["last_scan"] = datetime.now().astimezone().isoformat(timespec="seconds")
            app_state["last_scan_report"] = {key: value for key, value in report.items() if key != "tools"}
            DISCOVERY_CACHE.write_text(json.dumps(app_state, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def _provider(self, budget: BudgetLedger | None = None) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(config_store.load()["provider"], config_store.get_api_key(), budget=budget)

    def _plan(self, body: dict) -> dict:
        goal = str(body.get("goal", "")).strip()
        if not goal:
            raise UnprocessableError("请先描述要完成的内容任务")
        with state_lock:
            tools = list(app_state["tools"])
        try:
            plan = self._provider().plan(goal, tools)
            plan["planner"] = "api"
            return {"plan": plan, "fallback": False}
        except ProviderError as exc:
            if config_store.get_api_key():
                raise
            return {"plan": local_fallback_plan(goal, tools), "fallback": True, "notice": str(exc)}

    def require_mutation_security(self) -> None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise UnprocessableError("写接口只接受application/json")
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        session = cookies.get(SESSION_COOKIE)
        if session is None or not secrets.compare_digest(session.value, SESSION_ID):
            error = WorkflowError("本机会话无效，请刷新页面")
            error.status, error.code = 403, "invalid_session"
            raise error
        if not secrets.compare_digest(self.headers.get("X-Shiyi-CSRF", ""), CSRF_TOKEN):
            error = WorkflowError("CSRF校验失败，请刷新页面")
            error.status, error.code = 403, "csrf_failed"
            raise error
        origin = self.headers.get("Origin", "")
        port = int(self.server.server_address[1])
        allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        if origin not in allowed:
            error = WorkflowError("请求来源不受信任")
            error.status, error.code = 403, "origin_rejected"
            raise error

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise UnprocessableError("Content-Length无效") from exc
        if length < 0 or length > 2_000_000:
            raise UnprocessableError("请求内容过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UnprocessableError("请求JSON格式错误") from exc
        if not isinstance(data, dict):
            raise UnprocessableError("请求必须是JSON对象")
        return data

    def json_response(self, data: dict, status: HTTPStatus | int = HTTPStatus.OK, *, set_session: bool = False) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        if set_session:
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}={SESSION_ID}; Path=/; HttpOnly; SameSite=Strict")
        self.end_headers()
        self.wfile.write(payload)

    def error_response(self, code: str, message: str, status: HTTPStatus | int, details: Any | None = None) -> None:
        error = {"code": code, "message": message}
        if details is not None:
            error["details"] = details
        self.json_response({"error": error}, status)

    def handle_exception(self, exc: Exception) -> None:
        if isinstance(exc, WorkflowError):
            self.error_response(exc.code, str(exc), exc.status, exc.details)
        elif isinstance(exc, FileNotFoundError):
            self.error_response("not_found", str(exc), HTTPStatus.NOT_FOUND)
        elif isinstance(exc, ProviderError):
            self.error_response("provider_error", str(exc), HTTPStatus.BAD_GATEWAY)
        elif isinstance(exc, ValueError):
            self.error_response("bad_request", str(exc), HTTPStatus.BAD_REQUEST)
        else:
            self.error_response("internal_error", f"服务器错误: {type(exc).__name__}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def serve_static(self, path: str) -> None:
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.error_response("forbidden", "路径不允许", HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            self.error_response("not_found", "文件不存在", HTTPStatus.NOT_FOUND)
            return
        content = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") or mime == "application/javascript" else mime)
        self.send_header("Content-Length", str(len(content)))
        self.security_headers()
        if relative == "index.html":
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}={SESSION_ID}; Path=/; HttpOnly; SameSite=Strict")
        self.end_headers()
        self.wfile.write(content)

    def serve_file(self, target: Path, name: str) -> None:
        content = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition", f'inline; filename="{name}"')
        self.security_headers()
        self.end_headers()
        self.wfile.write(content)

    def security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'")

    def log_message(self, format: str, *args) -> None:
        line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} {self.client_address[0]} {format % args}\n"
        with (RUNTIME_DIR / "server.log").open("a", encoding="utf-8") as handle:
            handle.write(line)


def find_server(host: str, port: int) -> tuple[ThreadingHTTPServer, int]:
    if host != "127.0.0.1":
        raise ValueError("v2安全模式只允许监听127.0.0.1")
    last_error = None
    for candidate in range(port, port + 20):
        try:
            return ThreadingHTTPServer((host, candidate), AppHandler), candidate
        except OSError as exc:
            last_error = exc
    raise RuntimeError(f"无法找到可用端口: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="时宜 AIGC 内容工厂控制台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    args = parser.parse_args()
    server, port = find_server(args.host, args.port)
    url = f"http://{args.host}:{port}"
    (RUNTIME_DIR / "status.json").write_text(
        json.dumps({"status": "running", "url": url, "pid": __import__("os").getpid()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"时宜 AIGC 内容工厂已启动: {url}")
    if args.open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        (RUNTIME_DIR / "status.json").write_text(json.dumps({"status": "stopped"}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
