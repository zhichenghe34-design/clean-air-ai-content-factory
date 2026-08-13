from __future__ import annotations

import html
import json
import mimetypes
from pathlib import Path
from typing import Any


HUMAN_REPORT_NAME = "00-验收报告.html"
HUMAN_REPORT_CONTRACT = "shiyi-human-evidence-report-v1"
HUMAN_REPORT_STYLE = """
    :root{--ink:#082f2a;--muted:#5c6964;--paper:#f7f5ee;--card:#fffefa;--line:#d9ddd4;--accent:#b9e84a;--warn:#fff0e7}
    *{box-sizing:border-box} body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif}
    main{width:min(980px,calc(100% - 32px));margin:32px auto 72px} .eyebrow{font-size:12px;letter-spacing:.14em;font-weight:800;color:#34645a}
    h1{font-size:clamp(28px,5vw,48px);line-height:1.15;margin:.25rem 0 1rem} h2{font-size:21px;margin:0 0 12px} p{margin:.4rem 0}
    .hero,.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:24px;box-shadow:0 12px 35px rgba(15,48,42,.06)}
    .hero{border-top:7px solid var(--accent)} .status{display:inline-block;margin-top:12px;padding:8px 12px;border-radius:999px;background:#eaf7ce;font-weight:800}
    .warning{margin-top:16px;padding:14px 16px;border-radius:12px;background:var(--warn);border:1px solid #f3c4ae}
    .grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:18px 0} .metric{background:#eef1e8;border-radius:12px;padding:13px}
    .metric span{display:block;color:var(--muted);font-size:13px} .metric strong{display:block;margin-top:4px;overflow-wrap:anywhere}
    .card{margin-top:16px} video{display:block;width:min(100%,420px);aspect-ratio:9/16;background:#071713;border-radius:14px;margin:16px auto}
    .actions{display:flex;flex-wrap:wrap;gap:10px} a.primary,a.secondary{display:inline-flex;align-items:center;min-height:44px;padding:10px 15px;border-radius:10px;text-decoration:none;font-weight:800}
    a.primary{background:var(--accent);color:var(--ink)} a.secondary{border:1px solid #9daaa3;color:var(--ink);background:white}
    .script{white-space:pre-wrap;background:#f1f1e9;border-radius:12px;padding:18px;max-height:420px;overflow:auto}
    details{margin-top:14px} summary{cursor:pointer;font-weight:800} ul{padding-left:1.2rem} li span{display:block;color:var(--muted);font-size:12px}
    footer{margin-top:24px;color:var(--muted);font-size:13px}
    @media(max-width:680px){main{width:min(100% - 20px,980px);margin-top:12px}.hero,.card{padding:18px;border-radius:14px}.grid{grid-template-columns:1fr 1fr}}
  """.strip("\n")


def _read_json(folder: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads((folder / name).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _escape(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _asset(prefix: str, name: str) -> str:
    return _escape(f"{prefix}{name}")


def _media_data_uri(folder: Path, name: str) -> str | None:
    path = folder / name
    if not path.is_file():
        return None
    import base64

    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def build_human_evidence_report(
    folder: Path,
    *,
    asset_prefix: str = "",
    package_download_url: str | None = None,
    job_id: str | None = None,
    run_id: str | None = None,
    embed_media: bool = False,
) -> str:
    """Render a script-free, human-readable report from one immutable run.

    Every dynamic value is escaped.  The same renderer is used by the local UI
    and by the offline public-evidence ZIP so the ordinary-language view cannot
    drift away from the technical evidence contract.
    """

    folder = Path(folder)
    manifest = _read_json(folder, "manifest.json")
    report = _read_json(folder, "run_report.json")
    approvals = _read_json(folder, "approvals.json")
    approved = _read_json(folder, "approved_script.json")
    research = _read_json(folder, "research.json")
    review = _read_json(folder, "review.json")
    visual = _read_json(folder, "visual-qc.json")
    motion = _read_json(folder, "motion_plan.json")

    topic = report.get("topic") or motion.get("topic") or "未命名成片"
    script = str(approved.get("script", "")).strip() or "未找到可展示的文案。"
    render = report.get("render") if isinstance(report.get("render"), dict) else {}
    voice = report.get("voice") if isinstance(report.get("voice"), dict) else {}
    provider = report.get("provider") if isinstance(report.get("provider"), dict) else {}
    program_audio = report.get("program_audio") if isinstance(report.get("program_audio"), dict) else {}
    findings = research.get("findings") if isinstance(research.get("findings"), list) else []
    eligible_findings = sum(
        1 for item in findings
        if isinstance(item, dict) and item.get("auto_review_status") == "eligible"
    )
    research_status = str(research.get("status", "unknown"))
    offline_research = research_status in {"offline", "disabled"} or not findings
    review_passed = review.get("status") == "passed" and not review.get("blocked")
    visual_passed = visual.get("status") == "passed"
    final_human_required = bool(
        (manifest.get("review_policy") or {}).get("final_human_acceptance_required", True)
    )
    scene_count = len(motion.get("scenes", [])) if isinstance(motion.get("scenes"), list) else voice.get("segment_count", 0)
    duration = render.get("duration_seconds") or voice.get("duration_seconds") or motion.get("duration_seconds") or "未知"
    width = render.get("width") or (motion.get("format") or {}).get("width") or "?"
    height = render.get("height") or (motion.get("format") or {}).get("height") or "?"
    voice_label = voice.get("voice_label") or "普通中文播报"
    voice_rate = voice.get("voice_rate") or "固定档"
    bgm = "已加入内置背景音乐" if program_audio.get("background_music") else "未记录背景音乐"
    source_label = {
        "local_deterministic": "本地安全 Agent",
        "deepseek": "DeepSeek",
    }.get(str(provider.get("source", "")), str(provider.get("source") or "未记录"))
    provider_calls = (provider.get("budget") or {}).get("attempted", 0)
    warnings = review.get("warnings") if isinstance(review.get("warnings"), list) else []
    warning_text = "；".join(
        str(item.get("message", "")).strip()
        for item in warnings
        if isinstance(item, dict) and str(item.get("message", "")).strip()
    )
    editor_identity = approved.get("editor_identity") if isinstance(approved.get("editor_identity"), dict) else {}
    local_browser_edit = (
        approved.get("selected_by") == "browser_editor"
        and editor_identity.get("editor") == "本地浏览器用户"
        and editor_identity.get("actor_type") == "human"
        and editor_identity.get("interaction_mode") == "browser_operated"
        and editor_identity.get("human_edit_claimed") is True
    )
    stage_records = [
        item for item in (
            approvals.get("research") if isinstance(approvals.get("research"), dict) else {},
            approvals.get("compliance") if isinstance(approvals.get("compliance"), dict) else {},
        )
    ]
    mechanical_checks = bool(stage_records) and all(
        item.get("reviewer") == "反向机械审核器"
        and item.get("actor_type") == "mechanical_reviewer"
        and item.get("review_mode") == "mechanical"
        and item.get("human_approval_claimed") is False
        for item in stage_records
    )
    edit_summary = "本地浏览器用户直接编辑" if local_browser_edit else "由生成流程产出"
    check_summary = (
        "反向机械审核器自动通过，human_approval_claimed=false，最终仍待负责人验收"
        if mechanical_checks else "按当前任务阶段检查记录执行，最终仍待负责人验收"
    )
    technical_files = [
        ("manifest.json", "文件与哈希清单"),
        ("run_report.json", "运行报告"),
        ("research.json", "资料与研究记录"),
        ("review.json", "合规检查记录"),
        ("motion_plan.json", "镜头与动画计划"),
        ("visual-qc.json", "视觉抽检记录"),
        ("approved_script.json", "机器可读文案记录"),
    ]
    technical_links = "".join(
        f'<li><a href="{_asset(asset_prefix, name)}">{_escape(label)}</a><span>{_escape(name)}</span></li>'
        for name, label in technical_files
        if (folder / name).is_file()
    )
    package_link = (
        f'<a class="secondary" href="{_escape(package_download_url)}">下载交付材料（ZIP）</a>'
        if package_download_url else ""
    )
    video_source = (
        _media_data_uri(folder, "final.mp4") if embed_media else None
    ) or f"{asset_prefix}final.mp4"
    contact_source = (
        _media_data_uri(folder, "contact-sheet.png") if embed_media else None
    ) or f"{asset_prefix}contact-sheet.png"
    contact_link = (
        f'<a class="secondary" href="{_escape(contact_source)}">查看画面抽检图</a>'
        if (folder / "contact-sheet.png").is_file() else ""
    )
    research_notice = (
        "本次未联网取得可采信的外部资料，使用本地安全文案。它不能作为外部事实证明，公开发布前仍需负责人核对。"
        if offline_research
        else f"本次记录 {len(findings)} 条资料，其中 {eligible_findings} 条通过自动资格检查；发布前仍需负责人核对原始来源。"
    )
    conclusion = (
        "机器检查未发现阻断项，等待负责人最终验收。"
        if review_passed and visual_passed
        else "机器检查仍有未通过或未完成项目，请先查看下方说明。"
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="shiyi-report-contract" content="{HUMAN_REPORT_CONTRACT}">
  <title>{_escape(topic)}｜验收报告</title>
  <style>{HUMAN_REPORT_STYLE}</style>
</head>
<body><main>
  <section class="hero">
    <div class="eyebrow">给运营与负责人看的成片说明 · 不是动画代码</div>
    <h1>{_escape(topic)}</h1>
    <p>{_escape(conclusion)}</p>
    <span class="status">{_escape("等待负责人最终验收" if final_human_required else "已完成")}</span>
    <div class="warning"><strong>资料边界：</strong>{_escape(research_notice)}</div>
  </section>
  <section class="card">
    <h2>先看成片</h2>
    <p>如果这份报告来自 ZIP，请先把 ZIP 完整解压，再打开“00-验收报告.html”；不要直接在压缩包预览窗口里使用。</p>
    <video controls preload="metadata" src="{_escape(video_source)}">浏览器不支持直接播放，请下载成片。</video>
    <div class="actions"><a class="primary" href="{_escape(video_source)}" download="final.mp4">下载成片</a>{package_link}{contact_link}</div>
  </section>
  <section class="card">
    <h2>这次生成了什么</h2>
    <div class="grid">
      <div class="metric"><span>时长</span><strong>{_escape(duration)} 秒</strong></div>
      <div class="metric"><span>画面</span><strong>{_escape(width)}×{_escape(height)}</strong></div>
      <div class="metric"><span>镜头</span><strong>{_escape(scene_count)} 幕</strong></div>
      <div class="metric"><span>声音</span><strong>{_escape(voice_label)} {_escape(voice_rate)}</strong></div>
    </div>
    <p>生成来源：{_escape(source_label)}；模型请求 {int(provider_calls or 0)} 次；{_escape(bgm)}。</p>
    <p>脚本检查：{_escape("通过" if review_passed else "未通过或未完成")}。画面抽检：{_escape("通过" if visual_passed else "未通过或未完成")}。</p>
    <p><strong>文案修改：</strong>{_escape(edit_summary)}。</p>
    <p><strong>阶段检查：</strong>{_escape(check_summary)}。</p>
    {f'<div class="warning"><strong>检查提示：</strong>{_escape(warning_text)}</div>' if warning_text else ''}
  </section>
  <section class="card">
    <h2>完整文案</h2>
    <div class="script">{_escape(script)}</div>
  </section>
  <section class="card">
    <h2>怎么使用这份报告</h2>
    <p>先播放成片，再核对文案、资料边界、品牌表达和广告合规。自动检查只负责发现明显问题，不能代替负责人最终确认。</p>
    <details><summary>技术附件（开发或复核人员使用）</summary><p>下面是机器可读的 JSON 记录，不是动画代码，也不是普通运营人员必须阅读的内容。</p><ul>{technical_links}</ul></details>
  </section>
  <footer>Job ID：{_escape(job_id or manifest.get("job_id", ""))} · Run ID：{_escape(run_id or manifest.get("run_id", ""))} · 报告合同：{HUMAN_REPORT_CONTRACT}</footer>
</main></body></html>"""
