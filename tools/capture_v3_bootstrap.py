from __future__ import annotations

import argparse
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any


class BootstrapError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


class LocalApi:
    def __init__(self, base_url: str):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password:
            raise BootstrapError("base-url必须是127.0.0.1上的HTTP地址")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise BootstrapError("base-url不能包含路径、查询或片段")
        self.base_url = base_url.rstrip("/")
        self.cookie = ""
        self.csrf = ""

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers.update({"Content-Type": "application/json", "Origin": self.base_url, "X-Shiyi-CSRF": self.csrf})
        if self.cookie:
            headers["Cookie"] = self.cookie
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                if not self.cookie:
                    raw_cookie = response.headers.get("Set-Cookie", "")
                    parsed_cookie = SimpleCookie()
                    parsed_cookie.load(raw_cookie)
                    if parsed_cookie:
                        morsel = next(iter(parsed_cookie.values()))
                        self.cookie = f"{morsel.key}={morsel.value}"
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read().decode("utf-8"))
                message = str((error.get("error") or {}).get("message", "HTTP请求失败"))
            except Exception:
                message = "HTTP请求失败"
            raise BootstrapError(f"{path}返回HTTP {exc.code}: {message[:500]}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise BootstrapError(f"{path}请求失败：{type(exc).__name__}") from exc
        if not isinstance(value, dict):
            raise BootstrapError(f"{path}没有返回JSON对象")
        return value

    def establish_session(self) -> None:
        session = self.request("GET", "/api/session")
        self.csrf = str(session.get("csrf_token", ""))
        if not self.cookie or not self.csrf:
            raise BootstrapError("本地会话没有返回Cookie/CSRF")


def capture(base_url: str, output_dir: Path, goal: str, formal_secret: Path, isolated_secret: Path) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise BootstrapError("output-dir必须不存在或为空")
    if not formal_secret.is_file() or not isolated_secret.is_file():
        raise BootstrapError("正式或隔离secrets.json不存在")
    output_dir.mkdir(parents=True, exist_ok=True)
    formal_before = sha256(formal_secret)
    isolated_before = sha256(isolated_secret)
    if formal_before != isolated_before:
        raise BootstrapError("隔离Key副本与正式加密文件哈希不同")
    api = LocalApi(base_url)
    api.establish_session()
    connection = api.request("POST", "/api/provider/test", {})
    if connection.get("ok") is not True or connection.get("connection_verified") is not True:
        raise BootstrapError("Provider连接测试未成功")
    topics = api.request("POST", "/api/agent/topics", {"goal": goal, "excluded_topics": []})
    budget = topics.get("pretask_provider_budget", topics.get("topic_provider_budget", {}))
    pack = topics.get("capability_pack")
    review = topics.get("capability_review")
    candidates = topics.get("candidates")
    formal_after = sha256(formal_secret)
    if formal_after != formal_before:
        raise BootstrapError("正式secrets.json在验证期间发生变化")
    provider_validation = {
        "connection_succeeded": True,
        "connection_verified": True,
        "source": topics["source"],
        "model": connection.get("model"),
        "pretask_provider_budget": budget,
        "formal_secret_sha256_before": formal_before,
        "formal_secret_sha256_after": formal_after,
        "isolated_secret_sha256_before": isolated_before,
        "isolated_secret_removed": False,
        "restart_provider_state": "pending",
    }
    gate_errors = []
    if topics.get("source") != "deepseek_bootstrap":
        gate_errors.append(f"选题来源不是deepseek_bootstrap，而是{topics.get('source')}")
    if not isinstance(pack, dict) or (pack.get("audit") or {}).get("status") != "passed":
        gate_errors.append("动态能力包没有通过反证审核")
    if not isinstance(review, dict) or review.get("status") != "passed":
        gate_errors.append("选题响应没有passed反证审核")
    if not isinstance(candidates, list) or len(candidates) != 3:
        gate_errors.append("安全候选不是恰好三个")
    if int(budget.get("attempted", 99)) > 3:
        gate_errors.append("预任务Provider预算超过3")
    if gate_errors:
        provider_validation["gate_passed"] = False
        diagnostic = {
            "status": "stopped_without_evidence_claim",
            "gate_errors": gate_errors,
            "source": topics.get("source"),
            "notice": str(topics.get("notice", ""))[:1000],
            "candidate_count": len(candidates) if isinstance(candidates, list) else None,
            "pack_audit_status": (pack.get("audit") or {}).get("status") if isinstance(pack, dict) else None,
            "capability_review_status": review.get("status") if isinstance(review, dict) else None,
            "pretask_provider_budget": budget,
            "automatic_paid_retry_started": False,
            "tasks_created": 0,
        }
        _atomic_json(output_dir / "provider-validation.json", provider_validation)
        _atomic_json(output_dir / "bootstrap-diagnostic.json", diagnostic)
        raise BootstrapError("；".join(gate_errors))
    provider_validation["gate_passed"] = True
    _atomic_json(output_dir / "provider-validation.json", provider_validation)
    _atomic_json(output_dir / "topics-response.json", topics)
    _atomic_json(output_dir / "capability-pack.json", pack)
    _atomic_json(output_dir / "capability-review.json", review)
    return {
        "status": "V3_BOOTSTRAP_OK",
        "source": topics["source"],
        "candidate_ids": [str(item.get("id", "")) for item in candidates],
        "selection_bundle_id": topics.get("selection_bundle_id"),
        "pack_id": pack.get("id"),
        "pack_sha256": pack.get("sha256"),
        "pretask_attempted": budget.get("attempted"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="捕获一次真实DeepSeek连接与v0.3项目bootstrap证据")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--formal-secret", required=True, type=Path)
    parser.add_argument("--isolated-secret", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = capture(args.base_url, args.output_dir, args.goal, args.formal_secret.resolve(), args.isolated_secret.resolve())
    except BootstrapError as exc:
        print(json.dumps({"status": "V3_BOOTSTRAP_STOPPED", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
