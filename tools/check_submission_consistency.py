from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def require(text: str, markers: list[str], label: str, errors: list[str]) -> None:
    for marker in markers:
        if marker not in text:
            errors.append(f"{label} 缺少口径：{marker}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, required=True)
    args = parser.parse_args()
    errors: list[str] = []

    readme = (args.repo / "README.md").read_text(encoding="utf-8")
    competition = (args.repo / "docs" / "COMPETITION.md").read_text(encoding="utf-8")
    validation = (args.repo / "docs" / "REAL_E2E_VALIDATION.md").read_text(encoding="utf-8")
    submission_text = (args.repo / "docs" / "SUBMISSION_TEXT.md").read_text(encoding="utf-8")
    local_submission = (args.submission / "01_报名表可直接粘贴文本.md").read_text(encoding="utf-8")

    common = ["208.36", "0/4", "2/4"]
    require(readme, [*common, "52.01", "10次", "32项", "5条可进入脚本事实层", "2条降级"], "README", errors)
    require(competition, [*common, "52.01", "10次", "32项", "5条可用", "2条降级"], "COMPETITION", errors)
    require(validation, ["208.36", "52.011", "10次", "7次", "0/4", "1.4771", "5条可进入脚本事实层", "2条降级"], "REAL_E2E_VALIDATION", errors)
    require(submission_text, [*common, "52.011", "199字", "519字"], "SUBMISSION_TEXT", errors)
    require(local_submission, [*common, "52.011", "199字", "519字"], "本地报名文本", errors)

    forbidden = ["传播较好的内容", "大家真正担心", "温和加速", "复杂页面会继续尝试Playwright"]
    for label, text in {
        "README": readme,
        "COMPETITION": competition,
        "REAL_E2E_VALIDATION": validation,
        "SUBMISSION_TEXT": submission_text,
        "本地报名文本": local_submission,
    }.items():
        for phrase in forbidden:
            if phrase in text:
                errors.append(f"{label} 仍含越界或旧口径：{phrase}")

    real = json.loads((args.repo / "examples" / "real-e2e" / "run_report.json").read_text(encoding="utf-8"))
    research = json.loads((args.repo / "examples" / "real-e2e" / "research.json").read_text(encoding="utf-8"))
    if real["wall_clock_seconds"] != 208.36 or real["render"]["duration_seconds"] != 52.011:
        errors.append("真实运行报告耗时或成片时长不等于冻结基线")
    if real["provider"]["tool_calls"] != 10 or real["provider"]["api_calls"] != 7:
        errors.append("真实运行报告工具或模型请求数不等于冻结基线")
    if real["adoption_proxy"]["provisionally_usable_count"] != 0:
        errors.append("真实DeepSeek候选口径不是0/4")
    review = research.get("evidence_review", {})
    if review.get("finding_count") != 7 or review.get("script_eligible_count") != 5 or review.get("excluded_count") != 2:
        errors.append("审定研究记录不是7条原始发现、5条可用、2条降级")
    if research.get("provenance", {}).get("raw_source_count") != 4 or len(research.get("sources", [])) != 6:
        errors.append("审定研究记录来源口径不是原始4个、审定版6个")
    if re.search(r"[A-Z]:\\", json.dumps(research, ensure_ascii=False)):
        errors.append("公开研究记录包含本机绝对路径")

    try:
        from pypdf import PdfReader
    except ImportError as exc:
        errors.append(f"无法检查PDF文字和链接：{exc}")
    else:
        reader = PdfReader(str(args.pdf))
        if len(reader.pages) != 8:
            errors.append(f"PDF页数为{len(reader.pages)}，应为8")
        pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        require(pdf_text, ["208.36", "52.01", "0/4", "2/4", "32", "5条可进入脚本事实层", "2条降级"], "PDF", errors)
        link_count = 0
        for page in reader.pages:
            for annotation in page.get("/Annots", []) or []:
                obj = annotation.get_object()
                if obj.get("/Subtype") == "/Link":
                    link_count += 1
        if link_count < 4:
            errors.append(f"PDF可点击链接只有{link_count}个，应至少为4个")

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("CONSISTENCY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
