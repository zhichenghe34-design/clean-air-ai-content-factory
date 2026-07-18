from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any


class ProviderError(RuntimeError):
    pass


class OpenAICompatibleProvider:
    def __init__(self, config: dict[str, Any], api_key: str):
        self.config = config
        self.api_key = api_key.strip()
        self.base_url = str(config.get("base_url", "")).rstrip("/")
        self.model = str(config.get("model", "deepseek-v4-flash"))
        self.timeout = int(config.get("timeout_seconds", 90))

    def test_connection(self) -> dict[str, Any]:
        if not self.api_key:
            raise ProviderError("尚未填写 API Key，也未设置对应环境变量")
        request = urllib.request.Request(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
            method="GET",
        )
        data = self._send(request)
        models = [item.get("id") for item in data.get("data", []) if isinstance(item, dict)]
        return {"ok": True, "models": models, "configured_model_available": self.model in models}

    def plan(self, goal: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.api_key:
            raise ProviderError("缺少 API Key")
        tool_context = [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "path": item.get("path"),
                "capabilities": item.get("capabilities", []),
                "enabled": item.get("enabled", False),
            }
            for item in tools[:80]
        ]
        system = (
            "你是AIGC短视频内容工厂的任务规划器。根据用户目标和本地工具清单生成安全、可审计的执行计划。"
            "必须区分内容洞察、脚本生成、事实与广告合规审核、镜头/视频/语音生成、自动合成、人工精修。"
            "工具被发现不代表允许执行；enabled=false时只能建议配置适配器。不要虚构文件、检测结果、API或工具能力。"
            "只输出JSON对象，字段为goal, summary, steps, missing, estimated_cost_level。"
            "steps每项字段为id,name,capability,tool_id,input,output,requires_approval,risk。"
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps({"goal": goal, "tools": tool_context}, ensure_ascii=False)},
            ],
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        if self.base_url.startswith("https://api.deepseek.com"):
            payload["thinking"] = {"type": self.config.get("thinking", "disabled")}
            payload["reasoning_effort"] = self.config.get("reasoning_effort", "high")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        data = self._send(request)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("接口返回中没有可读取的消息内容") from exc
        return self.parse_json_content(content)

    def generate_content_scripts(self, production_input: dict[str, Any], insight: dict[str, Any]) -> list[dict[str, Any]]:
        """Generate exactly four script variants in one paid request."""
        system = (
            "你是除甲醛健康科普短视频脚本编辑。只写通用科普，不得虚构品牌、检测报告、功效或用户证言。"
            "百分比只能作为待解释的广告主张出现，必须说明剂量、空间体积、作用时间、初始浓度、检测方法和报告来源。"
            "禁止绝对安全、彻底去除、零甲醛、立即入住、母婴零风险等无证据保证。"
            "输出JSON对象，唯一字段variants，必须有4项；每项字段为id,hook_type,script,reason。"
            "每条脚本适合45-60秒中文口播，结尾不得推销具体产品。"
        )
        data = self._chat_json(system, {"production_input": production_input, "insight": insight})
        variants = data.get("variants")
        if not isinstance(variants, list):
            raise ProviderError("脚本接口没有返回variants数组")
        return variants

    def review_content_script(self, script: str, local_review: dict[str, Any]) -> dict[str, Any]:
        """Review one approved script in the second and final planned paid request."""
        system = (
            "你是广告与科学表达预审助手，不给出法律结论，只识别风险并提出保守修改。"
            "检查虚构数据、实验舱外推、绝对化承诺、医疗化表达、母婴安全保证、引证缺失。"
            "只输出JSON对象，字段status,risks,suggested_script,human_confirmation_required。"
            "status只能是pass_with_human_review或blocked。"
        )
        return self._chat_json(system, {"script": script, "local_review": local_review})

    def _chat_json(self, system: str, user_data: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise ProviderError("缺少 API Key")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user_data, ensure_ascii=False)},
            ],
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        if self.base_url.startswith("https://api.deepseek.com"):
            payload["thinking"] = {"type": self.config.get("thinking", "disabled")}
            payload["reasoning_effort"] = self.config.get("reasoning_effort", "high")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        data = self._send(request)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("接口返回中没有可读取的消息内容") from exc
        return self.parse_json_content(content)

    def _send(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise ProviderError(f"接口返回 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"无法连接接口: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("接口返回的不是有效 JSON") from exc

    @staticmethod
    def parse_json_content(content: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(content, dict):
            return content
        text = str(content).strip()
        fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            text = fenced.group(1)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("模型没有返回有效的结构化计划") from exc
        if not isinstance(parsed, dict):
            raise ProviderError("模型计划必须是 JSON 对象")
        return parsed
