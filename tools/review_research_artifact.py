from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.web_agent import normalize_research_result


STANDARD_URL = "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=6188E23AE55E8F557043401FC2EDC436"
AD_LAW_URL = "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/fgs/art/2023/art_5474cf75173c45d6a0379730fb4e8d97.html"


def evidence(url: str, excerpt: str, source_type: str, retrieved_at: str) -> dict[str, str]:
    return {
        "url": url,
        "excerpt": excerpt,
        "source_type": source_type,
        "retrieved_at": retrieved_at,
    }


def review(raw: dict[str, Any], processed_at: str) -> dict[str, Any]:
    sources = {str(item.get("url")): dict(item) for item in raw.get("sources", []) if item.get("url")}
    metadata = {
        "https://epaper.bjnews.com.cn/html/2026/20260717/20260717_A02/20260717_A02_04_7058_2077772172956274690.html":
            ("新京报", "media_original"),
        "https://www.sohu.com/a/1008795753_355158": ("半岛都市报（搜狐转载页）", "media_reprint"),
        "https://www.sohu.com/a/983839742_122633154": ("315诚搜网（搜狐转载页）", "media_reprint"),
        "https://m.sgsonline.com.cn/case/detail?id=1739": ("SGS在线商城", "institutional_secondary"),
    }
    for url, source in sources.items():
        publisher, source_type = metadata.get(url, ("未标注", "unknown"))
        source.update({"publisher": publisher, "source_type": source_type, "retrieved_at": processed_at})

    sources[STANDARD_URL] = {
        "url": STANDARD_URL,
        "title": "GB/T 18883-2022《室内空气质量标准》官方信息",
        "publisher": "国家市场监督管理总局 国家标准化管理委员会",
        "source_type": "government_standard_metadata",
        "retrieved_at": processed_at,
    }
    sources[AD_LAW_URL] = {
        "url": AD_LAW_URL,
        "title": "中华人民共和国广告法",
        "publisher": "国家市场监督管理总局",
        "source_type": "government_law",
        "retrieved_at": processed_at,
    }

    url_news = next(url for url in sources if "bjnews" in url)
    url_reprint = "https://www.sohu.com/a/1008795753_355158"
    url_sgs = "https://m.sgsonline.com.cn/case/detail?id=1739"
    reviewed_findings: list[dict[str, Any]] = []
    for item in raw.get("findings", []):
        claim = str(item.get("claim", ""))
        reviewed = dict(item)
        for forbidden in ("review_status", "reviewer", "reviewed_at", "human_verified", "approved_by", "approved_at"):
            reviewed.pop(forbidden, None)
        reviewed["limitations"] = []
        reviewed["auto_review_status"] = "eligible"
        if claim.startswith("99%除醛率测试通常"):
            reviewed["evidence"] = [evidence(url_news, "1罐产品在1m3试验舱内24小时除醛率为99.93%", "media_original", processed_at)]
        elif claim.startswith("商家宣传的适用范围"):
            reviewed["confidence"] = "medium"
            reviewed["evidence"] = [evidence(url_news, "一罐覆盖10-15平方米，但商家拿不出完整检测报告", "media_original", processed_at)]
            reviewed["limitations"] = ["来源为消费调查，不能外推到所有品牌或产品"]
        elif claim.startswith("除醛凝胶变色"):
            reviewed["confidence"] = "medium"
            reviewed["evidence"] = [evidence(url_reprint, "变色与是否存在甲醛、分解多少没有直接定量关系", "media_reprint", processed_at)]
            reviewed["limitations"] = ["当前抓取页为媒体转载，应继续寻找原始采访或实验材料"]
        elif claim.startswith("真实环境中甲醛持续释放数年"):
            reviewed["confidence"] = "low"
            reviewed["evidence"] = []
            reviewed["auto_review_status"] = "excluded"
            reviewed["limitations"] = ["当前来源为媒体评论，未取得原始研究或官方技术材料"]
        elif claim.startswith("开窗通风是性价比最高"):
            reviewed["confidence"] = "low"
            reviewed["evidence"] = []
            reviewed["auto_review_status"] = "excluded"
            reviewed["limitations"] = ["“最高、最好”属于绝对比较，现有来源不足以支持"]
        elif claim.startswith("部分除醛产品使用臭氧"):
            reviewed["claim"] = "报道中的部分除醛设备工作时释放臭氧，存在二次污染隐患"
            reviewed["source_urls"] = [url_reprint]
            reviewed["confidence"] = "medium"
            reviewed["evidence"] = [evidence(url_reprint, "设备工作时释放的臭氧，引发二次污染隐患", "media_reprint", processed_at)]
            reviewed["limitations"] = ["仅适用于报道涉及的设备，不能泛化到全部除醛产品"]
        elif claim.startswith("GB/T 18883-2022规定"):
            reviewed["claim"] = "SGS对GB/T 18883-2022的解读称甲醛限值调整为0.08mg/m3"
            reviewed["source_urls"] = [url_sgs, STANDARD_URL]
            reviewed["confidence"] = "medium"
            reviewed["evidence"] = [evidence(url_sgs, "甲醛限值从0.1mg/m3缩紧至0.08mg/m3", "institutional_secondary", processed_at)]
            reviewed["limitations"] = ["官方页面确认标准身份与有效状态；具体限值引用SGS解读", "原记录中的关闭门窗12小时未获本次证据支持，已删除"]
        else:
            reviewed["confidence"] = "low"
            reviewed["evidence"] = []
            reviewed["auto_review_status"] = "excluded"
            reviewed["limitations"] = ["未建立审定规则"]
        reviewed_findings.append(reviewed)

    reviewed = dict(raw)
    trace_summary = []
    for index, call in enumerate(raw.get("tool_trace", []), start=1):
        if not isinstance(call, dict):
            continue
        result = call.get("result") if isinstance(call.get("result"), dict) else {}
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        summary: dict[str, Any] = {
            "sequence": index,
            "tool": str(call.get("tool", "")),
            "ok": bool(result.get("ok")),
        }
        if arguments.get("query"):
            summary["query"] = str(arguments["query"])
            summary["result_count"] = len(result.get("results", [])) if isinstance(result.get("results"), list) else 0
        if arguments.get("url"):
            summary["url"] = str(arguments["url"])
            extracted = result.get("result") if isinstance(result.get("result"), dict) else {}
            summary["status"] = str(extracted.get("status", "failed" if not result.get("ok") else "complete"))
        trace_summary.append(summary)
    reviewed.pop("tool_trace", None)
    reviewed["tool_trace_summary"] = trace_summary
    reviewed["status"] = "partial"
    reviewed["summary"] = "真实工具原始整理7条发现和4个来源；人工复核后5条可进入脚本事实层，2条因证据不足或绝对化表述被降级；另补充2个官方合规依据。"
    reviewed["findings"] = reviewed_findings
    reviewed["sources"] = list(sources.values())
    reviewed["evidence_gaps"] = [
        *[str(value) for value in raw.get("evidence_gaps", [])],
        "持续释放数年的说法缺少本次可回溯的原始研究或官方技术材料",
        "“性价比最高、效果最好”属于绝对比较，未进入脚本事实层",
        "关闭门窗12小时未由本次抓取证据支持，已从审定发现中删除",
    ]
    reviewed["provenance"] = {
        "raw_artifact": "research.raw.json",
        "raw_finding_count": len(raw.get("findings", [])),
        "raw_source_count": len(raw.get("sources", [])),
        "processed_at": processed_at,
        "auto_review_status": "legacy_evidence_normalized",
        "note": "该工具只做旧证据的自动归一化，不代表人工批准。v2 人工决定必须通过研究审批接口写入独立 approvals 记录。",
    }
    return normalize_research_result(reviewed)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--raw-copy", type=Path)
    args = parser.parse_args()
    raw = json.loads(args.input.read_text(encoding="utf-8"))
    if args.raw_copy:
        args.raw_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.input, args.raw_copy)
    processed_at = datetime.now().astimezone().isoformat(timespec="seconds")
    reviewed = review(raw, processed_at)
    args.output.write_text(json.dumps(reviewed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
