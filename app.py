from __future__ import annotations

import argparse
import hashlib
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
from core.orchestrator import (
    IDEMPOTENCY_RE,
    JobStore,
    UnprocessableError,
    WorkflowError,
    local_fallback_plan,
    topic_in_scope,
    validate_topic_input,
)
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
provider_session_state = {"verified_signature": None, "verified_at": None, "revision": 0}
pretask_provider_budget = BudgetLedger(limit=3)
agent_create_replays: dict[str, dict[str, str]] = {}

SAFE_TOPIC_POOL = [
    {
        "title": "通风后没有气味，室内空气就安全了吗？",
        "reason": "从常见误区切入，适合新房家庭。",
        "audience": "新房家庭",
    },
    {
        "title": "99%除醛率，到底应该看哪些检测条件？",
        "reason": "数字有冲突感，也能自然引出证据核验。",
        "audience": "关注除醛产品的家庭",
    },
    {
        "title": "入住前看检测报告，最容易漏掉哪三项？",
        "reason": "实用清单型，便于收藏和转发。",
        "audience": "准备入住的新房家庭",
    },
    {
        "title": "测醛前为什么要先确认封闭时间和检测方法？",
        "reason": "从检测流程入手，避免只看一个数字。",
        "audience": "准备做室内检测的家庭",
    },
    {
        "title": "新房通风多久才够，为什么不能只凭气味判断？",
        "reason": "把高频疑问拆成可以公开核验的问题。",
        "audience": "装修后的新房家庭",
    },
    {
        "title": "同一份除醛数据，为什么不能直接套到真实房间？",
        "reason": "解释实验条件与真实使用场景的差异。",
        "audience": "正在比较除醛方案的家庭",
    },
    {
        "title": "室内空气检测报告里，哪些信息决定结论能不能用？",
        "reason": "用报告阅读框架代替简单下结论。",
        "audience": "看不懂检测报告的家庭",
    },
    {
        "title": "治理完成就入住的说法，还缺哪些公开证据？",
        "reason": "以反向举证检查高风险承诺。",
        "audience": "急于入住的新房家庭",
    },
    {
        "title": "装修污染只测甲醛够不够，检测范围应该怎么看？",
        "reason": "从检测范围切入，避免把单项结果当成全部。",
        "audience": "关注室内空气的新房家庭",
    },
    {
        "title": "便携测醛仪的数字，为什么不能直接当检测结论？",
        "reason": "区分日常观察工具与正式检测结论。",
        "audience": "正在自行测醛的家庭",
    },
    {
        "title": "开窗测和关窗测差很多，应该先核对什么？",
        "reason": "用场景差异解释检测条件的重要性。",
        "audience": "准备复测室内空气的家庭",
    },
    {
        "title": "除醛产品写着高去除率，报告里还要找哪些前提？",
        "reason": "帮助用户把宣传数字放回实验条件中理解。",
        "audience": "正在选购除醛产品的家庭",
    },
    {
        "title": "家具进场前后，为什么室内空气结果可能不同？",
        "reason": "从污染源变化切入，避免一次检测代表全部阶段。",
        "audience": "正在装修或添置家具的家庭",
    },
    {
        "title": "检测报告有合格结论，就能忽略采样过程吗？",
        "reason": "引导用户同时核对结论和采样条件。",
        "audience": "收到检测报告的新房家庭",
    },
    {
        "title": "闻不到装修味之后，为什么还要看检测依据？",
        "reason": "把嗅觉判断与可核验检测分开。",
        "audience": "准备入住的新房家庭",
    },
    {
        "title": "治理前后对比数据，怎样判断比较条件是否一致？",
        "reason": "聚焦前后对比最容易忽略的变量控制。",
        "audience": "正在验收治理效果的家庭",
    },
    {
        "title": "一张实验室报告，能不能证明整套房的实际效果？",
        "reason": "解释样品测试与真实空间之间的证据边界。",
        "audience": "正在比较治理服务的家庭",
    },
    {
        "title": "甲醛检测数值接近限值时，应该怎样读结果？",
        "reason": "从临界数值切入，提醒关注方法与不确定性。",
        "audience": "拿到临界检测结果的家庭",
    },
    {
        "title": "夏天和冬天测出的室内空气数据为什么会变化？",
        "reason": "用环境条件说明单次结果的适用范围。",
        "audience": "计划跨季节复测的家庭",
    },
    {
        "title": "儿童房检测时，哪些采样信息值得单独记录？",
        "reason": "给关注儿童房的家庭一份可核验的信息清单。",
        "audience": "关注儿童房空气的家庭",
    },
    {
        "title": "通风、净化和治理数据，为什么不能混成一个结论？",
        "reason": "拆分不同措施的证据，避免把相关性当成功效。",
        "audience": "正在组合改善方案的家庭",
    },
    {
        "title": "检测机构和产品商家给出不同结果时先看什么？",
        "reason": "提供核对方法、时间和条件的比较框架。",
        "audience": "遇到检测争议的家庭",
    },
    {
        "title": "新房复测为什么要保留时间、温度和通风记录？",
        "reason": "把复测变成条件可比的过程，而不是只比数字。",
        "audience": "准备多次检测的新房家庭",
    },
    {
        "title": "检测报告里的检出限和单位，为什么不能跳过？",
        "reason": "从基础字段入手，降低误读具体数值的风险。",
        "audience": "第一次阅读检测报告的家庭",
    },
    {
        "title": "网传除醛小妙招，怎样用公开证据逐条核对？",
        "reason": "用反向举证框架审视高传播但来源不明的方法。",
        "audience": "正在搜索除醛方法的家庭",
    },
    {
        "title": "治理服务承诺多久见效，哪些条件必须先问清？",
        "reason": "把时间承诺拆成可验证的服务条件。",
        "audience": "准备购买治理服务的家庭",
    },
    {
        "title": "入住计划很赶时，室内空气判断应该保留哪些底线？",
        "reason": "用审慎决策框架替代未经证实的入住保证。",
        "audience": "临近入住的新房家庭",
    },
]
UNSAFE_TOPIC_PHRASES = ("绝对安全", "完全去除", "彻底去除", "零甲醛", "立即入住", "母婴零风险")
BLOCKED_GOAL_PHRASES = (
    "忽略前面", "忽略以上", "忽略之前", "忽略所有指令", "系统提示词", "开发者指令", "越狱提示",
    "勒索软件", "恶意软件", "木马程序", "窃取密码", "钓鱼网站", "攻击服务器", "绕过安全限制",
)


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


def provider_snapshot_signature(provider: dict, api_key: str) -> str | None:
    """Fingerprint an immutable provider snapshot without retaining its Key."""
    if not api_key:
        return None
    payload = json.dumps(
        {
            "base_url": provider.get("base_url", ""),
            "model": provider.get("model", ""),
            "api_key_sha256": hashlib.sha256(api_key.encode("utf-8")).hexdigest(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def provider_configuration_signature() -> str | None:
    return provider_snapshot_signature(config_store.load()["provider"], config_store.get_api_key())


def provider_test_snapshot() -> tuple[OpenAICompatibleProvider, str | None, int]:
    with state_lock:
        revision = int(provider_session_state["revision"])
        provider_config = dict(config_store.load()["provider"])
        api_key = config_store.get_api_key()
    return (
        OpenAICompatibleProvider(provider_config, api_key),
        provider_snapshot_signature(provider_config, api_key),
        revision,
    )


def mark_provider_connection_verified(signature: str | None, revision: int) -> str | None:
    if not signature:
        raise ProviderError("尚未填写 API Key，也未设置对应环境变量")
    verified_at = datetime.now().astimezone().isoformat(timespec="seconds")
    with state_lock:
        if (
            int(provider_session_state["revision"]) != int(revision)
            or provider_configuration_signature() != signature
        ):
            return None
        provider_session_state["verified_signature"] = signature
        provider_session_state["verified_at"] = verified_at
    return verified_at


def clear_provider_connection_verified() -> None:
    with state_lock:
        provider_session_state["verified_signature"] = None
        provider_session_state["verified_at"] = None


def invalidate_provider_configuration() -> None:
    """Clear verification and advance the generation before any Provider save."""
    with state_lock:
        provider_session_state["revision"] = int(provider_session_state["revision"]) + 1
        provider_session_state["verified_signature"] = None
        provider_session_state["verified_at"] = None


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
                if "provider" in body:
                    invalidate_provider_configuration()
                self.json_response(config_store.save(body))
            elif path == "/api/discover":
                self.json_response(self._discover(body))
            elif path == "/api/provider/test":
                clear_provider_connection_verified()
                provider, tested_signature, tested_revision = provider_test_snapshot()
                result = provider.test_connection()
                if result.get("ok") is not True:
                    raise ProviderError("Provider连通性测试没有返回成功状态")
                verified_at = mark_provider_connection_verified(tested_signature, tested_revision)
                if verified_at is None:
                    error = WorkflowError("Provider配置在连接测试期间发生变化，请重新测试")
                    error.status, error.code = 409, "provider_config_changed"
                    raise error
                result["connection_verified"] = True
                result["verified_at"] = verified_at
                self.json_response(result)
            elif path == "/api/agent/topics":
                self.json_response(self._suggest_topics(body))
            elif path == "/api/agent/plan":
                self.json_response(self._plan(body))
            elif path == "/api/jobs":
                plan = body.get("plan")
                if not isinstance(plan, dict):
                    raise UnprocessableError("缺少有效计划")
                self.json_response(job_store.create(plan, production_input=body.get("production_input")), HTTPStatus.CREATED)
            elif path == "/api/demo-job":
                job, replayed = self._create_demo_job(body)
                self.json_response(job, HTTPStatus.OK if replayed else HTTPStatus.CREATED)
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
        provider_configured = bool(config["provider"]["has_api_key"])
        provider_signature = provider_configuration_signature()
        with state_lock:
            tools = list(app_state["tools"])
            provider_connection_verified = bool(
                provider_signature
                and provider_session_state["verified_signature"] == provider_signature
            )
            provider_verified_at = provider_session_state["verified_at"] if provider_connection_verified else None
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
            # provider_ready is retained for older clients and means that a Key
            # is available, not that a network connection has been verified.
            "provider_ready": provider_configured,
            "provider_configured": provider_configured,
            "provider_connection_verified": provider_connection_verified,
            "provider_connection_verified_at": provider_verified_at,
            "provider_state": (
                "verified" if provider_connection_verified else "configured" if provider_configured else "unconfigured"
            ),
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

    def _create_demo_job(self, body: dict) -> tuple[dict, bool]:
        supplied = {} if "production_input" not in body else body["production_input"]
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

        request_key = self.headers.get("Idempotency-Key", "").strip()
        if request_key and not IDEMPOTENCY_RE.fullmatch(request_key):
            error = WorkflowError("Idempotency-Key格式无效")
            error.status, error.code = 400, "invalid_idempotency_key"
            raise error
        fingerprint = hashlib.sha256(
            json.dumps(production_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with state_lock:
            replay = agent_create_replays.get(request_key) if request_key else None
            if replay:
                if replay["fingerprint"] != fingerprint:
                    error = WorkflowError("同一Idempotency-Key不能用于不同创建请求")
                    error.status, error.code = 409, "idempotency_conflict"
                    raise error
                return job_store.get(replay["job_id"]), True
            job = job_store.create(plan, production_input=production_input)
            if request_key:
                agent_create_replays[request_key] = {"fingerprint": fingerprint, "job_id": job["id"]}
                while len(agent_create_replays) > 128:
                    agent_create_replays.pop(next(iter(agent_create_replays)))
            return job, False

    @staticmethod
    def _safe_topic_candidates(excluded: list[str], goal: str) -> list[dict]:
        excluded_set = {item.strip() for item in excluded if isinstance(item, str) and item.strip()}
        pool = [item for item in SAFE_TOPIC_POOL if item["title"] not in excluded_set]
        # Keep the fallback deterministic for tests while changing the visible
        # batch as the client accumulates exclusions.
        offset = len(excluded_set) % len(pool)
        ordered = pool[offset:] + pool[:offset]
        return [dict(item) for item in ordered[:3]]

    @staticmethod
    def _validate_topic_goal(value: object) -> str:
        if not isinstance(value, str):
            raise UnprocessableError("goal必须是字符串")
        goal = value.strip()
        if not 4 <= len(goal) <= 200:
            raise UnprocessableError("请用4到200字告诉Agent你想做什么")
        if any(phrase in goal for phrase in BLOCKED_GOAL_PHRASES):
            raise UnprocessableError("目标包含越域或指令注入内容，请只描述室内空气赛题需求")
        if not topic_in_scope(goal):
            raise UnprocessableError("当前Agent只处理甲醛、除醛与室内空气赛题")
        return goal

    @staticmethod
    def _normalize_topic_candidates(raw: list[dict], excluded: list[str], goal: str) -> tuple[list[dict], bool]:
        excluded_set = {item.strip() for item in excluded if isinstance(item, str) and item.strip()}
        result: list[dict] = []
        seen = set(excluded_set)
        for item in raw:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            reason = str(item.get("reason", "")).strip()
            audience = str(item.get("audience", "新房家庭")).strip() or "新房家庭"
            if title in seen or any(phrase in title for phrase in UNSAFE_TOPIC_PHRASES):
                continue
            try:
                validate_topic_input({"topic": title, "audience": audience, "target_duration_seconds": 52})
            except UnprocessableError:
                continue
            if not 4 <= len(reason) <= 100:
                continue
            seen.add(title)
            result.append({"title": title, "reason": reason, "audience": audience})
            if len(result) == 3:
                break
        used_local_fallback = len(result) < 3
        if used_local_fallback:
            fallback = AppHandler._safe_topic_candidates(list(seen), goal)
            for item in fallback:
                if item["title"] in seen:
                    continue
                result.append(item)
                seen.add(item["title"])
                if len(result) == 3:
                    break
        return [dict(item, id=f"topic-{index + 1}") for index, item in enumerate(result)], used_local_fallback

    def _suggest_topics(self, body: dict) -> dict:
        goal = self._validate_topic_goal(body.get("goal"))
        excluded = [] if "excluded_topics" not in body else body["excluded_topics"]
        if not isinstance(excluded, list) or len(excluded) > 24:
            raise UnprocessableError("excluded_topics必须是不超过24项的数组")
        if any(not isinstance(item, str) for item in excluded):
            raise UnprocessableError("excluded_topics每一项都必须是字符串")
        excluded = [item.strip() for item in excluded if item.strip()]
        if any(len(item) > 80 for item in excluded):
            raise UnprocessableError("excluded_topics每一项最多80字")
        raw: list[dict]
        source = "deepseek"
        notice = ""
        if pretask_provider_budget.snapshot()["remaining"] <= 0:
            raw = self._safe_topic_candidates(excluded, goal)
            source = "local_safe_agent"
            notice = "本机会话的预任务 Agent Provider 预算已耗尽，已只使用本地安全选题。"
        else:
            try:
                raw = self._provider(pretask_provider_budget).suggest_topics(goal, excluded)
            except ProviderError as exc:
                raw = self._safe_topic_candidates(excluded, goal)
                source = "local_safe_agent"
                notice = f"DeepSeek本次未返回可用结果，已切换到本地安全选题：{exc}"
        candidates, used_local_fallback = self._normalize_topic_candidates(raw, excluded, goal)
        if source == "deepseek" and used_local_fallback:
            source = "deepseek_filtered_with_local_fallback"
            notice = "部分模型候选未通过安全校验，已用本地安全选题补足三个角度。"
        if len(candidates) != 3:
            raise WorkflowError("Agent暂时没有找到三个互不重复的安全角度，请稍后再试")
        budget = pretask_provider_budget.snapshot()
        source_label = {
            "deepseek": "DeepSeek",
            "deepseek_filtered_with_local_fallback": "DeepSeek与本地安全候选混合",
            "local_safe_agent": "本地安全候选",
        }[source]
        budget_note = (
            f"预任务 Agent Provider 请求 {budget['attempted']}/{budget['limit']}，"
            f"剩余 {budget['remaining']}；来源：{source_label}"
        )
        notice = f"{notice.rstrip('。')}；{budget_note}。" if notice else f"{budget_note}。"
        return {
            "goal": goal,
            "source": source,
            "notice": notice,
            "candidates": candidates,
            "screening": f"已排除越域、夸大承诺和重复选题；公开依据将在研究阶段逐条核验；{budget_note}。",
            # Keep the topic-specific public field used by the released UI and
            # expose the shared name used by /api/agent/plan as well.
            "topic_provider_budget": budget,
            "pretask_provider_budget": budget,
        }

    def _plan(self, body: dict) -> dict:
        goal = str(body.get("goal", "")).strip()
        if not goal:
            raise UnprocessableError("请先描述要完成的内容任务")
        with state_lock:
            tools = list(app_state["tools"])
        source = "deepseek"
        fallback = False
        notice = ""
        if pretask_provider_budget.snapshot()["remaining"] <= 0:
            plan = local_fallback_plan(goal, tools)
            source = "local_safe_agent"
            fallback = True
            notice = "本机会话的预任务 Agent Provider 预算已耗尽，已只使用本地安全计划。"
        else:
            try:
                plan = self._provider(pretask_provider_budget).plan(goal, tools)
                plan["planner"] = "api"
            except ProviderError as exc:
                plan = local_fallback_plan(goal, tools)
                source = "local_safe_agent"
                fallback = True
                notice = f"DeepSeek本次未返回可用计划，已切换到本地安全计划：{exc}"
        budget = pretask_provider_budget.snapshot()
        source_label = "DeepSeek" if source == "deepseek" else "本地安全计划"
        budget_note = (
            f"预任务 Agent Provider 请求 {budget['attempted']}/{budget['limit']}，"
            f"剩余 {budget['remaining']}；来源：{source_label}"
        )
        notice = f"{notice.rstrip('。')}；{budget_note}。" if notice else f"{budget_note}。"
        return {
            "plan": plan,
            "fallback": fallback,
            "source": source,
            "notice": notice,
            "pretask_provider_budget": budget,
        }

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
