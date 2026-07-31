from __future__ import annotations

import json
import os
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import BaseDocTemplate, Frame, Image, NextPageTemplate, PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "competition-proposal.pdf"
ASSETS = ROOT / "docs" / "assets"

GREEN = colors.HexColor("#153F34")
GREEN_2 = colors.HexColor("#2E765C")
LIME = colors.HexColor("#D7EF68")
CREAM = colors.HexColor("#F6F3E8")
MUTED = colors.HexColor("#64736D")
LINE = colors.HexColor("#D9DED8")
RED = colors.HexColor("#9A4937")


def register_fonts() -> None:
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    regular = fonts / "msyh.ttc"
    bold = fonts / "msyhbd.ttc"
    if not regular.is_file() or not bold.is_file():
        raise FileNotFoundError("Microsoft YaHei fonts are required to build the proposal PDF")
    pdfmetrics.registerFont(TTFont("MSYH", str(regular), subfontIndex=0))
    pdfmetrics.registerFont(TTFont("MSYH-Bold", str(bold), subfontIndex=0))


def style(name: str, **overrides) -> ParagraphStyle:
    values = {"fontName": "MSYH", "fontSize": 9.8, "leading": 16, "textColor": GREEN, "spaceAfter": 7, "wordWrap": "CJK"}
    values.update(overrides)
    return ParagraphStyle(name, **values)


def paragraph(text: str, selected: ParagraphStyle) -> Paragraph:
    return Paragraph(text, selected)


def bullet(text: str, body: ParagraphStyle) -> Paragraph:
    return paragraph(f"• {text}", body)


def fit_image(path: Path, max_width: float, max_height: float) -> Image:
    image = Image(str(path))
    scale = min(max_width / image.imageWidth, max_height / image.imageHeight)
    image.drawWidth = image.imageWidth * scale
    image.drawHeight = image.imageHeight * scale
    return image


def build() -> Path:
    register_fonts()
    title = style("Title", fontName="MSYH-Bold", fontSize=28, leading=38, textColor=CREAM, spaceAfter=10)
    subtitle = style("Subtitle", fontSize=15, leading=23, textColor=LIME)
    h1 = style("H1", fontName="MSYH-Bold", fontSize=20, leading=29, spaceAfter=10)
    h2 = style("H2", fontName="MSYH-Bold", fontSize=12.5, leading=19, textColor=GREEN_2, spaceBefore=4)
    body = style("Body")
    small = style("Small", fontSize=8.1, leading=12.5, textColor=MUTED)
    white = style("White", fontSize=10.5, leading=18, textColor=CREAM)
    metric = style("Metric", fontName="MSYH-Bold", fontSize=19, leading=24, alignment=TA_CENTER)
    metric_label = style("MetricLabel", fontSize=7.8, leading=11, textColor=MUTED, alignment=TA_CENTER)
    tag_style = style("Tag", fontName="MSYH-Bold", fontSize=8.3, leading=11, alignment=TA_CENTER)

    def section_tag(text: str) -> Table:
        table = Table([[paragraph(text, tag_style)]], colWidths=[48 * mm])
        table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIME), ("BOX", (0, 0), (-1, -1), 0, LIME), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        return table

    def metric_table(items: list[tuple[str, str]]) -> Table:
        table = Table([[paragraph(value, metric) for value, _ in items], [paragraph(label, metric_label) for _, label in items]], colWidths=[174 * mm / len(items)] * len(items))
        table.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.7, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE), ("TOPPADDING", (0, 0), (-1, 0), 10), ("BOTTOMPADDING", (0, 1), (-1, 1), 8)]))
        return table

    def footer(canvas, doc) -> None:
        canvas.saveState()
        width, _ = A4
        canvas.setStrokeColor(LINE)
        canvas.line(18 * mm, 14 * mm, width - 18 * mm, 14 * mm)
        canvas.setFillColor(MUTED)
        canvas.setFont("MSYH", 7.2)
        canvas.drawString(18 * mm, 9 * mm, "净界 AI 内容工厂 v2 · 2026 AI 先锋未来人才大赛")
        canvas.drawRightString(width - 18 * mm, 9 * mm, str(doc.page))
        canvas.restoreState()

    def cover(canvas, _doc) -> None:
        width, height = A4
        canvas.saveState()
        canvas.setFillColor(GREEN)
        canvas.rect(0, 0, width, height, stroke=0, fill=1)
        canvas.setFillColor(colors.HexColor("#1E5546"))
        canvas.circle(30 * mm, height - 30 * mm, 55 * mm, stroke=0, fill=1)
        canvas.setFillColor(LIME)
        canvas.roundRect(18 * mm, height - 35 * mm, 58 * mm, 12 * mm, 6 * mm, stroke=0, fill=1)
        canvas.setFont("MSYH-Bold", 9)
        canvas.setFillColor(GREEN)
        canvas.drawCentredString(47 * mm, height - 31 * mm, "v2 加固版补充材料")
        canvas.restoreState()

    doc = BaseDocTemplate(str(OUTPUT), pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm, topMargin=17 * mm, bottomMargin=19 * mm, title="净界 AI 内容工厂 v2 初赛补充材料", author="净界 AI 内容工厂")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="normal")
    doc.addPageTemplates([PageTemplate(id="cover", frames=[frame], onPage=cover), PageTemplate(id="content", frames=[frame], onPage=footer)])
    story = [
        Spacer(1, 74 * mm), paragraph("净界 AI 内容工厂 v2", title),
        paragraph("面向除甲醛品类的可信、可重复 AIGC 短视频生产线", subtitle), Spacer(1, 14 * mm),
        paragraph("研究证据人工审定 → 脚本生成 → 合规人工放行 → 隔离渲染 → 哈希清单", white), Spacer(1, 33 * mm),
        paragraph("企业命题：时宜品牌　｜　赛区：东部赛区（上海）　｜　默认端口：127.0.0.1:8765", style("CoverMeta", fontSize=8.7, leading=15, textColor=colors.HexColor("#B8CFC5"))),
        NextPageTemplate("content"), PageBreak(),

        section_tag("01 赛题与风险"), Spacer(1, 4 * mm), paragraph("快生产不能牺牲证据边界", h1),
        paragraph("除甲醛内容常出现百分比、检测条件、健康风险和入住建议。单纯让模型写稿，容易把实验条件外推为家庭效果，或把自动检查误写成人工审定。v2 的目标不是把内容做得更激进，而是让每句话可回查、每次运行可复现。", body),
        metric_table([("3 段", "研究/内容/渲染"), ("2 道", "独立人工门禁"), ("7 次", "单任务硬预算"), ("13 项", "最终公开产物")]), Spacer(1, 7 * mm),
        paragraph("阻断规则", h2), bullet("无已批准证据支持的功效数字、具体测量和健康保证不能进入渲染。", body),
        bullet("医疗因果、绝对安全、彻底去除、零风险等表达由本地规则阻断。", body),
        bullet("自动系统只写 auto_review_status，不能生成 human_verified 或冒用人工姓名。", body),
        paragraph("人工责任", h2), paragraph("研究 finding 由用户逐条批准或拒绝；脚本自动通过后，仍必须由用户亲自完成最终合规放行。", body), PageBreak(),

        section_tag("02 v2 状态机"), Spacer(1, 4 * mm), paragraph("一次运行，只推进到下一道人工作业门禁", h1),
        Table([[paragraph(cell, h2 if row % 2 == 0 else small) for cell in values] for row, values in enumerate([
            ["01 执行授权", "02 研究运行", "03 研究审定"], ["planned → authorized", "独立 run 与预算", "逐 finding + 文件哈希"],
            ["04 内容生成", "05 合规放行", "06 渲染发布"], ["四稿与本地阻断", "脚本/审核双哈希", "成功后原子发布"],
        ])], colWidths=[58 * mm] * 3, style=TableStyle([("BACKGROUND", (0, 0), (-1, 0), CREAM), ("BACKGROUND", (0, 2), (-1, 2), CREAM), ("BOX", (0, 0), (-1, -1), 0.7, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)])),
        Spacer(1, 7 * mm), paragraph("返工规则", h2),
        bullet("研究文件变化，研究审批立即失效；仅人工改稿时保留仍匹配的研究审批。", body),
        bullet("人工改稿先校验 35-75 秒预计朗读时长，并撤销旧合规审批。", body),
        bullet("真实配音若需超出 0.75-1.5 倍变速才能进入 45-60 秒，则退回改稿。", body),
        paragraph("旧任务", h2), paragraph("旧 job 不改写，统一显示 legacy_read_only；旧报告不通过 v2 正式产物接口公开。", body), PageBreak(),

        section_tag("03 可视化控制台"), Spacer(1, 4 * mm), paragraph("非技术用户也能看懂当前卡在哪一关", h1),
        paragraph("控制台显示实际端口、预算、当前成功 run、失败尝试和上一成功成片。研究审定与合规放行是两个不同面板，危险、待审、运行和完成使用不同颜色。", body),
        fit_image(ASSETS / "console.png", 174 * mm, 112 * mm), Spacer(1, 5 * mm),
        metric_table([("13", "当前能力包"), ("7", "生产节点"), ("72", "Python 测试"), ("0", "浏览器错误")]), PageBreak(),

        section_tag("04 现有作品"), Spacer(1, 4 * mm), paragraph("保留三支现有视频；v2 联调另留审计成片", h1),
        Table([[fit_image(ASSETS / "sample-frame.png", 64 * mm, 96 * mm), paragraph("现有样片用于证明媒体链路和视觉方案，不自动成为 v2 合规证据。2026-08-01 的 v2 真实联调实际使用 7/7 次硬预算；严格反证审核通过 3 条 finding，用户亲自完成研究逐项审定和最终合规放行。新旧媒体与 legacy 证据分开保存。", body)]], colWidths=[72 * mm, 100 * mm], style=TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5)])),
        Spacer(1, 4 * mm), metric_table([("52.00 秒", "v2 审计成片"), ("1080x1920", "竖屏"), ("H.264", "视频编码"), ("AAC", "音频编码")]),
        paragraph("v2 真实运行：13 项脱敏证据包已逐项复算 SHA-256、审批哈希、字幕连续性和媒体规格；自动流程没有代签。", style("Success", fontName="MSYH-Bold", fontSize=9.2, leading=14, textColor=GREEN_2)), PageBreak(),

        section_tag("05 运行隔离"), Spacer(1, 4 * mm), paragraph("失败尝试不会覆盖上一份成功成片", h1),
        paragraph("每次 /run 创建不可变 run_id 和 staging。阶段验证成功后才原子发布；失败目录保留错误与实际阶段，但不能通过正式产物接口读取。", body),
        metric_table([("run_id", "不可变尝试"), ("PID lock", "崩溃恢复"), ("409", "并发冲突"), ("SHA-256", "逐文件清单")]), Spacer(1, 7 * mm),
        paragraph("manifest.json", h2), bullet("记录 job/run ID、输入哈希、两道审批哈希、开始结束时间和预算。", body),
        bullet("记录每个产物的名称、阶段、MIME、大小与 SHA-256。", body),
        bullet("普通 URL 只解析 current_run_id；历史成功产物必须显式指定 run_id。", body),
        paragraph("幂等", h2), paragraph("相同 Idempotency-Key 重放原结果；不同 Key 在同任务运行中返回 409。失效 PID 锁将最后一次尝试标记 interrupted。", body), PageBreak(),

        section_tag("06 安全与预算"), Spacer(1, 4 * mm), paragraph("本机可用，不等于可以放宽边界", h1),
        Table([[paragraph(cell, small if row else h2) for cell in values] for row, values in enumerate([
            ["边界", "v2 控制", "验证"],
            ["本地 HTTP", "127.0.0.1 + Cookie + CSRF + Origin", "API 安全回归"],
            ["Provider", "DeepSeek 官方 HTTPS 白名单", "路径/端口/重定向测试"],
            ["Key", "Windows DPAPI；非 Windows 不落盘", "回读与明文迁移测试"],
            ["预算", "请求前计数，成功失败超时均占用", "7 次硬停止"],
            ["解析器", "字段白名单 + 一次性配置", "敏感字段扫描"],
        ])], colWidths=[38 * mm, 82 * mm, 54 * mm], style=TableStyle([("BACKGROUND", (0, 0), (-1, 0), GREEN), ("TEXTCOLOR", (0, 0), (-1, 0), CREAM), ("BOX", (0, 0), (-1, -1), 0.7, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)])),
        Spacer(1, 7 * mm), paragraph("依赖", h2), paragraph("Node 使用 package-lock.json 固定 HyperFrames 0.7.86 与 Playwright 1.62.1；npm audit 当前 0 个漏洞。运行时禁止 npx --yes 自动下载，缺失适配器会明确报错。", body), PageBreak(),

        section_tag("07 验证与交付"), Spacer(1, 4 * mm), paragraph("测试证明机制，人工证明内容", h1),
        metric_table([("3.12 / 3.14", "Python CI"), ("72", "Python 测试"), ("13", "能力包"), ("0", "npm 漏洞")]), Spacer(1, 6 * mm),
        paragraph("CI", h2), bullet("单元、API、安全、预算、并发、确定性无 Key E2E、JS 与 Playwright 烟雾测试。", body),
        bullet("可注入假配音/渲染适配器用于快速 CI；本地终验使用真实 FFmpeg/FFprobe。", body),
        bullet("已提交视频执行 FFprobe；PDF 与证据包复算哈希并扫描敏感内容。", body),
        paragraph("公开入口", h2),
        paragraph('<link href="https://github.com/zhichenghe34-design/clean-air-ai-content-factory" color="#2E765C"><u>GitHub：clean-air-ai-content-factory</u></link>', body),
        paragraph('<link href="https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=6188E23AE55E8F557043401FC2EDC436" color="#2E765C"><u>GB/T 18883-2022《室内空气质量标准》</u></link>', body),
        paragraph('<link href="https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/fgs/art/2023/art_5474cf75173c45d6a0379730fb4e8d97.html" color="#2E765C"><u>《中华人民共和国广告法》</u></link>', body),
        Spacer(1, 8 * mm), Table([[paragraph("v2 DeepSeek 证据包已在用户亲自完成两次审批后生成。自动测试证明机制，用户审批证明人工事实；旧运行快照继续只作为 legacy 对照。", style("Callout", fontName="MSYH-Bold", fontSize=11.5, leading=19))]], colWidths=[174 * mm], style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), CREAM), ("BOX", (0, 0), (-1, -1), 0.8, LINE), ("TOPPADDING", (0, 0), (-1, -1), 12), ("BOTTOMPADDING", (0, 0), (-1, -1), 12)])),
    ]
    doc.build(story)
    return OUTPUT


if __name__ == "__main__":
    print(build())
