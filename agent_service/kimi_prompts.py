"""Prompt payloads exchanged with the PPT expert agent."""
from __future__ import annotations

import json


def base_message_payload(
    request,
    template_info: dict | None,
    reference_info: dict | None,
) -> dict:
    attachments = []
    for item in getattr(request, "attachments", []) or []:
        if isinstance(item, dict):
            attachments.append(
                {
                    "name": item.get("name"),
                    "visualAnalysis": item.get("visualAnalysis"),
                }
            )
    references = getattr(request, "references", "") or ""
    reference_info = reference_info or {}
    use_external_reference = bool(reference_info.get("files")) and len(references) > 24000
    return {
        "prompt": getattr(request, "prompt", "") or "",
        "audience": getattr(request, "audience", "") or "通用汇报",
        "pageCount": getattr(request, "pageCount", None),
        "references": "" if use_external_reference else references,
        "referenceCharacters": len(references),
        "referenceFiles": reference_info.get("files", []),
        "referenceOutline": reference_info.get("outline", ""),
        "referenceFile": reference_info.get("referenceFile"),
        "attachments": attachments,
        "template": template_info,
        "_usesExternalReference": use_external_reference,
    }


def planning_message(
    request,
    template_info: dict | None,
    reference_info: dict | None,
    requirements: dict,
) -> str:
    payload = base_message_payload(request, template_info, reference_info)
    use_external_reference = payload.pop("_usesExternalReference")
    payload["planRequirements"] = requirements
    payload["note"] = (
        "你会被后续宿主校验。如果你违背以下约束，结果会被拒绝并重新规划。"
        "当前阶段只做完整内容规划：写出 deck/plan.json 后立即停止。"
        "本阶段禁止创建或修改任何 deck/pages/*.page、deck/deck.pptd 或 deck/media/*；"
        "页面写入会在计划通过后由宿主分批派发。首批最多 5 页是后续写入批次上限，"
        "不是整份 PPT 的计划页数，绝不能因此把完整报告压缩成 5 页。"
        "plan.json 的 pages 必须覆盖整份报告，并与最终 deck.pptd 页码顺序一致；"
        "每个计划项必须包含 path（pages/*.page）、title、readerTask、content、visualPlan 和 sourceSections。"
        "sourceSections 必须逐字复制 sources/reference-outline.md 中的相关一级章节标题；封面、目录、致谢填 []。"
        "长报告中每个一级章节至少分配到 2 个计划页，不能把主要章节压成一张泛化摘要页。"
        "plan.json 是紧凑的页面契约，不是正文草稿：title ≤ 60 字、readerTask ≤ 100 字、"
        "content ≤ 240 字、visualPlan ≤ 180 字。content 只写本页要覆盖的论点和事实线索，"
        "严禁复制原文长段落、表格内容或证据全文；页面作者会按计划再读取对应源章节。"
        "先用一次 Write 写完整合法 JSON，再由 GLOB/READ 核对，不要逐页重写整个计划。"
        "规划阶段只需阅读 reference-outline.md 和少量相关章节，不需要逐张查看模板截图；"
        "模板封面、背景、页脚和公司元素由宿主统一套用。"
        "references 是内容、数据和结构的首要事实来源。参考资料的章节、关键事实、指标、"
        "结论和专有名词必须进入计划，不得只规划前几章。"
        "附件仅提供名称与已有视觉分析；未提供的二进制内容不要假设。按 SKILL.md 判定任务与设计方式。"
    )
    if template_info:
        payload["note"] += (
            f" 已提供公司模板：pptx={template_info['pptx']}，已预转换的 PPTD={template_info['pptd']}"
            f"（入口 {template_info['entry']}），总览截图={template_info['screenshots']}。"
            "按 SKILL.md「Using a template」：分析其配色/字体/版式/可复用组件，然后为本次需求从头生成新内容。"
            "template-pptd/ 与 template-screenshots/ 只是分析素材，严禁把它们当作交付物，也严禁照抄模板的文字、数据或页数。"
            "封面与致谢页会由宿主统一套用模板设计：首页给出标题/副标题/日期即可，末页只写一句收尾语（如「感谢观看」），"
            "正文内容放在中间页。"
        )
    if use_external_reference:
        payload["note"] += (
            " Long reference material is stored inside the workspace. Read the complete "
            "sources/reference-outline.md before planning, then inspect every principal "
            "chapter in sources/reference.md. The written plan must cover the entire "
            "outline, not only its opening chapters."
        )
    return json.dumps(payload, ensure_ascii=False)


def execution_message(
    request,
    template_info: dict | None,
    reference_info: dict | None,
    plan: dict,
    continuation: dict,
) -> str:
    payload = base_message_payload(request, template_info, reference_info)
    use_external_reference = payload.pop("_usesExternalReference")
    pages = {
        page["path"]: page
        for page in plan.get("pages", [])
        if isinstance(page, dict) and isinstance(page.get("path"), str)
    }
    payload["plan"] = plan
    payload["continuation"] = continuation
    payload["batchPagePlans"] = [
        pages[path] for path in continuation.get("batchPages", []) if path in pages
    ]
    batch_paths = set(continuation.get("batchPages", []))
    payload["pageValidationIssues"] = [
        issue
        for issue in continuation.get("invalidPages", [])
        if issue.get("path") in batch_paths
    ]
    payload["note"] = (
        "完整计划已经通过宿主校验，当前只执行分配给你的写页批次。"
        "写入 continuation.batchPages 中列出的 .page 文件；不得修改 deck/plan.json、"
        "不得改变页面数量或顺序、不得删除已有页面、不得把多个计划页合并成一页。"
        "deck/deck.pptd 已由宿主按完整计划创建；不要重写它。"
        "尽量并行写本批全部页面，完成后停止，宿主会启动下一批。"
        "pageValidationIssues 中列出的页面虽然已经存在，但 YAML 无法解析，必须完整重写；"
        "所有数组元素和嵌套字段必须使用合法 YAML 缩进。"
        "页面内容必须对应 batchPagePlans 中的 title、readerTask、content、visualPlan 和 sourceSections。"
        "使用 deck/plan.json、源提纲和最多两个已有代表页维持整套视觉系统。"
    )
    if template_info:
        payload["note"] += (
            " 样式与模板复用规则见系统提示；宿主会在最终阶段统一套用封面、致谢和公司元素。"
        )
    if use_external_reference:
        payload["note"] += (
            " 只读取当前页码对应的源章节；如计划信息不足，再读取 sources/reference.md 的相关位置。"
        )
    return json.dumps(payload, ensure_ascii=False)
