"""Compatibility runner for legacy slide projects."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from . import agents, images as image_service, telemetry
from .contracts import ArtifactRecord


async def run_legacy_job(
    identifier,
    request,
    *,
    root: Path,
    prefix: str,
    llm_limit,
    update_job,
    read_job,
    finish_job,
    save_json,
    remove_task,
    safe_id,
    create_deck,
    render_pdf,
    audit_rendered_pdf,
):
    def progress(stage, message):
        update_job(identifier, status="running", stage=stage, message=message)

    try:
        progress(0, "准备生成任务")
        if request.kind == "render":
            if not request.project.slides:
                raise ValueError("请先创建至少一页幻灯片。")
            template_id = request.templateId or request.project.templateId
            template_path = (
                root / "templates" / safe_id(template_id) / "template.pptx"
                if template_id
                else None
            )
            if template_path and not template_path.is_file():
                raise ValueError("公司模板不存在，请重新上传或选择模板。")
            folder = root / "artifacts" / identifier
            folder.mkdir(exist_ok=True)
            illustrated, pictures, image_warnings = await image_service.illustrate_project(
                root,
                request.project,
                progress,
            )
            progress(1, "正在套用公司模板" if template_path else "正在排版幻灯片")
            warnings = await asyncio.to_thread(
                create_deck,
                illustrated,
                folder / "presentation.pptx",
                template_path,
                pictures,
            )
            warnings = list(dict.fromkeys(image_warnings + warnings))
            progress(2, "LibreOffice 正在生成真实预览与 PDF")
            images = await render_pdf(folder / "presentation.pptx")
            warnings.extend(
                await asyncio.to_thread(
                    audit_rendered_pdf,
                    illustrated,
                    folder / "presentation.pdf",
                )
            )
            warnings = list(dict.fromkeys(warnings))
            base = f"{prefix}/artifacts/{identifier}"
            artifact = ArtifactRecord(
                id=identifier,
                pptxUrl=base + "/presentation.pptx",
                pdfUrl=base + "/presentation.pdf",
                previewUrls=[base + "/" + image for image in images],
                warnings=warnings,
            ).model_dump()
            save_json(folder / "metadata.json", artifact)
            result = {
                "slides": [slide.model_dump() for slide in illustrated.slides],
                "artifact": artifact,
                "reply": f"演示文件已生成，共 {len(illustrated.slides)} 页，可预览、播放或下载。",
            }
        else:
            async with llm_limit:
                try:
                    with telemetry.generation(request, identifier) as span:
                        context = span.get_span_context()
                        if context.is_valid:
                            update_job(
                                identifier,
                                traceId=f"{context.trace_id:032x}",
                            )
                        result = await asyncio.wait_for(
                            agents.generate(request, progress),
                            timeout=600,
                        )
                        if result.get("generationMeta"):
                            span.set_attribute(
                                "ppt.prompt.version",
                                result["generationMeta"]["promptVersion"],
                            )
                            span.set_attribute(
                                "ppt.scenario",
                                result["generationMeta"]["scenario"],
                            )
                finally:
                    await telemetry.flush()
        if read_job(identifier)["status"] == "cancelled":
            return
        completed_project = finish_job(identifier, result)
        if completed_project:
            result["project"] = completed_project
        update_job(
            identifier,
            status="completed",
            stage=3,
            message="生成完成",
            result=result,
        )
    except asyncio.CancelledError:
        update_job(identifier, status="cancelled", message="任务已停止")
        finish_job(identifier)
        raise
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            else "生成服务调用失败，请检查模型配置或渲染器后重试。"
        )
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "OPENAI_API_KEY",
            "PPT_VISION_API_KEY",
            "WEKNORA_API_KEY",
            "TAVILY_API_KEY",
            "SERPER_API_KEY",
            "PPT_WEB_MCP_HEADERS",
        ):
            secret = os.getenv(name)
            if secret:
                message = message.replace(secret, "[redacted]")
        update_job(
            identifier,
            status="failed",
            message=message[:500],
            error=message[:500],
        )
        finish_job(identifier)
    finally:
        remove_task(identifier)
