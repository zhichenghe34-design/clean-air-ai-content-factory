from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from core.config import ConfigStore
from core.catalog import HardwareProbe, PackageCatalog
from core.discovery import ProjectDiscovery
from core.orchestrator import JobStore, local_fallback_plan
from core.production import DEFAULT_INPUT, ProductionRunner
from core.provider import OpenAICompatibleProvider, ProviderError


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
    server_version = "ShiyiContentFactory/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self.json_response(self._status())
        elif path == "/api/config":
            self.json_response(config_store.public_config())
        elif path == "/api/tools":
            with state_lock:
                self.json_response({"tools": app_state["tools"], "last_scan": app_state["last_scan"], "report": app_state["last_scan_report"]})
        elif path == "/api/jobs":
            self.json_response({"jobs": job_store.list()})
        elif path.startswith("/api/jobs/") and "/artifacts/" in path:
            self.serve_job_artifact(path)
        elif path.startswith("/api/jobs/"):
            parts = path.strip("/").split("/")
            if len(parts) == 3:
                self.json_response(job_store.get(parts[2]))
            else:
                self.json_response({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        elif path == "/api/catalog":
            catalog = package_catalog.load()
            self.json_response(catalog)
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
            config = config_store.public_config()
            self.json_response({"storage": config["storage"]})
        elif path.startswith("/api/"):
            self.json_response({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        else:
            self.serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            if path == "/api/config":
                self.json_response(config_store.save(body))
            elif path == "/api/discover":
                self.json_response(self._discover(body))
            elif path == "/api/provider/test":
                provider = self._provider()
                self.json_response(provider.test_connection())
            elif path == "/api/agent/plan":
                self.json_response(self._plan(body))
            elif path == "/api/jobs":
                plan = body.get("plan")
                if not isinstance(plan, dict):
                    raise ValueError("缺少有效计划")
                production_input = body.get("production_input")
                if production_input is not None and not isinstance(production_input, dict):
                    raise ValueError("production_input必须是JSON对象")
                self.json_response(job_store.create(plan, production_input=production_input), HTTPStatus.CREATED)
            elif path == "/api/demo-job":
                production_input = dict(DEFAULT_INPUT)
                production_input.update(body.get("production_input") or {})
                plan = local_fallback_plan(f"制作样片：{production_input['topic']}", [])
                for step in plan["steps"]:
                    if step.get("capability") != "human_refinement":
                        step["tool_id"] = "trusted-local-production-adapter"
                        step["risk"] = "固定路径本地适配器，仍需人工批准"
                plan["summary"] = "首条除甲醛科普样片：固定范式、合规预审、本地配音、MG信息卡与FFmpeg成片。"
                self.json_response(job_store.create(plan, production_input=production_input), HTTPStatus.CREATED)
            elif path.startswith("/api/jobs/") and path.endswith("/approve"):
                job_id = path.split("/")[3]
                self.json_response(job_store.approve(job_id))
            elif path.startswith("/api/jobs/") and path.endswith("/run"):
                job_id = path.split("/")[3]
                job = job_store.get(job_id)
                if isinstance(job.get("production_input"), dict):
                    app_config = config_store.load()
                    runner = ProductionRunner(provider=self._provider(), research_config=app_config.get("research", {}))
                    self.json_response(job_store.run_production(job_id, runner))
                else:
                    allow = bool(config_store.load()["security"].get("allow_external_commands", False))
                    self.json_response(job_store.run_safe(job_id, allow_external_commands=allow))
            else:
                self.json_response({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except (ValueError, FileNotFoundError) as exc:
            self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except ProviderError as exc:
            self.json_response({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        except Exception as exc:  # keep UI responsive and report concrete error
            self.json_response({"error": f"服务器错误: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            if path.startswith("/api/jobs/") and path.endswith("/script"):
                job_id = path.split("/")[3]
                self.json_response(job_store.update_script(job_id, str(body.get("script", ""))))
            else:
                self.json_response({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except (ValueError, FileNotFoundError) as exc:
            self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.json_response({"error": f"服务器错误: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

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
        capabilities = sorted(discovered_capabilities | bundled_capabilities)
        return {
            "name": "时宜 AIGC 内容工厂",
            "version": "0.1.0",
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "provider": config["provider"]["name"],
            "model": config["provider"]["model"],
            "provider_ready": config["provider"]["has_api_key"],
            "tool_count": len(tools),
            "capabilities": capabilities,
            "job_count": len(job_store.list()),
            "safe_mode": not config["security"].get("allow_external_commands", False),
            "catalog_package_count": len(catalog.get("packages", [])),
            "catalog_install_enabled": catalog["policy"]["auto_install_enabled"],
        }

    def _discover(self, body: dict) -> dict:
        config = config_store.load()
        roots = body.get("roots") or config["discovery"].get("roots", [])
        discovery = ProjectDiscovery(
            max_depth=config["discovery"].get("max_depth", 3),
            max_directories=config["discovery"].get("max_directories", 1500),
        )
        report = discovery.scan(roots)
        with state_lock:
            app_state["tools"] = report["tools"]
            app_state["last_scan"] = datetime.now().astimezone().isoformat(timespec="seconds")
            app_state["last_scan_report"] = {k: v for k, v in report.items() if k != "tools"}
            DISCOVERY_CACHE.write_text(json.dumps(app_state, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def _provider(self) -> OpenAICompatibleProvider:
        config = config_store.load()["provider"]
        return OpenAICompatibleProvider(config, config_store.get_api_key())

    def _plan(self, body: dict) -> dict:
        goal = str(body.get("goal", "")).strip()
        if not goal:
            raise ValueError("请先描述要完成的内容任务")
        with state_lock:
            tools = list(app_state["tools"])
        try:
            plan = self._provider().plan(goal, tools)
            plan["planner"] = "api"
            return {"plan": plan, "fallback": False}
        except ProviderError as exc:
            if config_store.get_api_key():
                raise
            plan = local_fallback_plan(goal, tools)
            return {"plan": plan, "fallback": True, "notice": str(exc)}

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("请求JSON格式错误") from exc
        if not isinstance(data, dict):
            raise ValueError("请求必须是JSON对象")
        return data

    def json_response(self, data: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

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
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") or mime == "application/javascript" else mime)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def serve_job_artifact(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 5 or parts[0:2] != ["api", "jobs"] or parts[3] != "artifacts":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        job_id, name = parts[2], parts[4]
        if not job_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in job_id):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        allowed = {"research.json", "insight.json", "script_variants.json", "approved_script.json", "review.json", "voice.wav", "captions.srt", "motion_plan.json", "final.mp4", "run_report.json"}
        if name not in allowed:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        target = (RUNTIME_DIR / "jobs" / job_id / name).resolve()
        jobs_root = (RUNTIME_DIR / "jobs").resolve()
        try:
            target.relative_to(jobs_root)
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition", f'inline; filename="{name}"')
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args) -> None:
        line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} {self.client_address[0]} {format % args}\n"
        with (RUNTIME_DIR / "server.log").open("a", encoding="utf-8") as handle:
            handle.write(line)


def find_server(host: str, port: int) -> tuple[ThreadingHTTPServer, int]:
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
