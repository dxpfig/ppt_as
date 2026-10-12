import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
from uuid import uuid4
import time
import logging

from fastapi import FastAPI, File, HTTPException, UploadFile, Request, Body
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse, JSONResponse

from . import agents, images as image_service, surgical, telemetry, template_reference
from .models import ImageSearchRequest, JobRequest, Project
from .pptd_store import DocumentStore, RevisionConflict
from . import pptd_exports, pptd_generation, visual_pipeline
from .job_execution import initial_execution, checkpoint_execution, record_event, terminal_execution, diagnose_error
from pptd_engine import workbench
from .rendering import MAX_UPLOAD, create_deck, render_pdf, renderer_available, template_info, validate_package, audit_rendered_pdf

ROOT = Path(os.getenv("STUDIO_DATA_DIR", Path(__file__).resolve().parents[1] / "runtime" / "studio")).resolve()
for directory in ("projects", "templates", "jobs", "artifacts", "trash", "images", "requests", "checkpoints"):
    (ROOT / directory).mkdir(parents=True, exist_ok=True)
TASKS: dict[str, asyncio.Task] = {}
LLM_LIMIT = asyncio.Semaphore(2)


def key(value: str):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,120}", value):
        raise HTTPException(400, "无效的资源 ID。")
    return value


def save(path, data):
    temp = path.with_suffix(f".{uuid4().hex}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def read(path):
    if not path.is_file():
        raise HTTPException(404, "未找到资源。")
    return json.loads(path.read_text(encoding="utf-8"))


def job_path(identifier):
    return ROOT / "jobs" / f"{key(identifier)}.json"


def document_store():
    return DocumentStore(ROOT)


def project_path(identifier):
    return ROOT / "projects" / f"{key(identifier)}.json"


def hydrate_project(project):
    store = document_store()
    if store.exists(project['id']):
        state = store.read(project['id'])
        base = f"{PREFIX}/projects/{project['id']}/pptd"
        project = {**project, 'workflow': 'pptd-v2', 'slides': [], 'versions': [],
                   'title': state['document'].get('title', project['title']), 'contentGenerated': True,
                   'pptd': {'entry': 'document.pptd', 'revision': state['revision'],
                            'pageCount': len(state['pages']), 'viewUrl': base + '/view?embed=true',
                            'sourceUrl': base + '/source.zip', 'versions': state['versions']}}
        if project.get('artifact') and project['artifact'].get('revision') != state['revision']:
            project['artifact'] = None
    else:
        project.pop('pptd', None)
    return project


def project_busy(project):
    active = project.get('activeJobId')
    return active and job_path(active).is_file() and read(job_path(active))['status'] in ('queued', 'running')


def metadata_from_client(project, previous=None):
    data = project.model_dump()
    if document_store().exists(project.id):
        # Clients may update preferences and conversation, never source or results.
        data = dict(previous or {})
        for name in ('messages', 'audience', 'references', 'pageCount', 'imageSource', 'templateId', 'theme'):
            data[name] = getattr(project, name)
        data.update(id=project.id, workflow='pptd-v2', slides=[], versions=[])
    else:
        for name in ('pptd', 'generationMeta'):
            data.pop(name, None)
        if previous:
            for name in ('artifact', 'activeJobId', 'phase'):
                data[name] = previous.get(name)
        else:
            data.update(artifact=None, activeJobId=None, phase='outline')
    data['updatedAt'] = time.time()
    return data


def update_job(identifier, **changes):
    job = read(job_path(identifier))
    if job["status"] == "cancelled":
        return job
    job.update(changes, updatedAt=time.time())
    if 'message' in changes or 'stage' in changes or 'status' in changes:
        job['events'] = [*job.get('events', []), {'timestamp': time.time(), 'stage': job.get('stage', 0), 'message': job.get('message', ''), 'status': job['status']}][-200:]
    save(job_path(identifier), job)
    return job


@asynccontextmanager
async def lifespan(app):
    telemetry.setup()
    for path in (ROOT / "jobs").glob("*.json"):
        job = read(path)
        if job["status"] in ("queued", "running"):
            job.update(status="interrupted", message="服务重启，任务已保留，可从检查点继续。", error="任务因服务重启中断。", recoverable=(ROOT / "requests" / path.name).is_file())
            if job.get('execution'):
                job['execution'] = terminal_execution(job['execution'], 'interrupted', job['message'])
            save(path, job)
            finish_project_job(job["id"])
    yield
    for task in list(TASKS.values()):
        task.cancel()
    await asyncio.gather(*list(TASKS.values()), return_exceptions=True)
    await telemetry.shutdown()


app = FastAPI(title="PPT Studio Agent Service", lifespan=lifespan)
PREFIX = "/api/studio"


@app.get("/health")
@app.get(PREFIX + "/health")
async def health():
    return {"status": "ok", **agents.health(), "rendererAvailable": await asyncio.to_thread(pptd_exports.renderer_available), "legacyRendererAvailable": await asyncio.to_thread(renderer_available), "workflow": "pptd-v2", "pptdAvailable": True, "images": image_service.capabilities(), "tracing": telemetry.health()}


@app.get(PREFIX + "/images/status")
async def image_status():
    state = image_service.capabilities()
    if state["weknoraConfigured"]:
        try:
            state["knowledgeBases"] = await image_service.knowledge_bases()
            state["weknoraConnected"] = True
        except Exception as exc:
            state["weknoraConnected"] = False
            state["warning"] = str(exc) if isinstance(exc, image_service.ImageSearchError) else "WeKnora 暂时无法连接。"
    return state


@app.post(PREFIX + "/images/search")
async def search_slide_images(request: ImageSearchRequest):
    try:
        found, warnings = await asyncio.wait_for(image_service.search_images(ROOT, request.query, request.source, request.limit), timeout=90)
    except asyncio.TimeoutError:
        return {"images": [], "warnings": ["配图检索超时，请缩短关键词或更换来源后重试。"]}
    return {"images": [image.model_dump() for image in found], "warnings": warnings}


@app.get(PREFIX + "/images/{identifier}")
async def image_file(identifier: str):
    try:
        path = image_service.asset_path(ROOT, identifier)
    except ValueError as exc:
        raise HTTPException(404, "未找到图片。") from exc
    if not path.is_file():
        raise HTTPException(404, "未找到图片。")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=31536000, immutable"})


def ensure_not_deleted(identifier):
    if (ROOT / "trash" / f"{key(identifier)}.json").is_file():
        raise HTTPException(410, "该工作记录已删除，请新建演示或撤销删除。")


def project_summary(project):
    return {"id": project["id"], "title": project["title"], "slideCount": len(document_store().read(project["id"])["pages"]) if document_store().exists(project["id"]) else len(project.get("slides", [])),
            "updatedAt": project.get("updatedAt"), "templateId": project.get("templateId"),
            "phase": project.get("phase", "outline")}


@app.delete(PREFIX + "/projects/{identifier}")
async def delete_project(identifier: str):
    path = ROOT / "projects" / f"{key(identifier)}.json"
    if not path.is_file() and (ROOT / "trash" / path.name).is_file():
        return {"id": identifier, "deleted": True}
    project = read(path)
    # Move first, so late autosaves and task completions cannot revive this record.
    path.replace(ROOT / "trash" / path.name)
    for job_file in (ROOT / "jobs").glob("*.json"):
        job = read(job_file)
        if job.get("projectId") == identifier and job["status"] in ("queued", "running"):
            await cancel_job(job["id"])
    return {"id": project["id"], "deleted": True}


@app.post(PREFIX + "/projects/{identifier}/restore")
async def restore_project(identifier: str):
    path = ROOT / "trash" / f"{key(identifier)}.json"
    project = read(path)
    project.update(activeJobId=None, phase="ready" if project.get("artifact") else "outline", updatedAt=time.time())
    save(ROOT / "projects" / path.name, project)
    path.unlink()
    return project_summary(project)


@app.put(PREFIX + "/projects/{identifier}")
async def put_project(identifier: str, project: Project):
    if key(identifier) != project.id:
        raise HTTPException(400, "项目 ID 不一致。")
    ensure_not_deleted(identifier)
    previous_path = project_path(identifier)
    previous = read(previous_path) if previous_path.is_file() else None
    if previous and project_busy(previous):
        raise HTTPException(409, "该工作记录正在生成，请等待完成或先停止任务。")
    data = metadata_from_client(project, previous)
    save(previous_path, data)
    return hydrate_project(data)


@app.get(PREFIX + "/projects/{identifier}")
async def get_project(identifier: str):
    return hydrate_project(read(project_path(identifier)))


@app.get(PREFIX + "/projects")
async def list_projects():
    projects = [read(path) for path in (ROOT / "projects").glob("*.json")]
    return [project_summary(p) for p in sorted(projects, key=lambda p: p.get("updatedAt", 0), reverse=True)]


@app.post(PREFIX + "/templates")
async def upload_template(file: UploadFile = File(...)):
    if Path(file.filename or "").suffix.lower() not in (".pptx", ".potx"):
        raise HTTPException(400, "仅支持 .pptx 和 .potx 公司模板。")
    data = await file.read(MAX_UPLOAD + 1)
    await file.close()
    identifier = uuid4().hex
    folder = ROOT / "templates" / identifier
    folder.mkdir()
    try:
        normalized = await asyncio.to_thread(validate_package, data)
        path = folder / "template.pptx"
        path.write_bytes(normalized)
        info = await asyncio.to_thread(template_info, path)
    except Exception as exc:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        message = str(exc) if isinstance(exc, ValueError) else "无法读取模板结构，请用 PowerPoint 另存为普通 PPTX 后重试。"
        raise HTTPException(400, message) from exc
    template = {"id": identifier, "name": Path(file.filename or "公司模板").name[:200], **info, "previewUrls": []}
    save(folder / "metadata.json", template)
    return template


@app.get(PREFIX + "/templates")
async def list_templates():
    return [read(p) for p in (ROOT / "templates").glob("*/metadata.json")]


@app.get(PREFIX + "/templates/{identifier}")
async def get_template(identifier: str):
    return read(ROOT / "templates" / key(identifier) / "metadata.json")


def finish_project_job(identifier, result=None):
    job = read(job_path(identifier))
    project_id = job.get("projectId")
    if not project_id:
        return None
    path = ROOT / "projects" / f"{key(project_id)}.json"
    if not path.is_file() or (ROOT / "trash" / path.name).is_file():
        return None
    project = read(path)
    if project.get("activeJobId") != identifier:
        return None
    if result:
        if "generationMeta" in result:
            project["generationMeta"] = result["generationMeta"]
        if "slides" in result:
            project["slides"] = result["slides"]
        project["title"] = result.get("title", project["title"])
        project["artifact"] = result.get("artifact")
        project["contentGenerated"] = bool(result.get("slides") or result.get("artifact") or project.get("contentGenerated"))
        if result.get("reply"):
            project["messages"] = [*project.get("messages", []), {"id": uuid4().hex, "role": "assistant", "text": result["reply"]}][-1000:]
        if result.get("artifact"):
            version = {"id": uuid4().hex, "title": project["title"], "label": "生成版本 · " + time.strftime("%Y-%m-%d %H:%M"),
                       "slides": project["slides"], "theme": project["theme"], "imageSource": project.get("imageSource", "auto"), "templateId": project.get("templateId"), "artifact": result["artifact"]}
            project["versions"] = [version, *project.get("versions", [])][:12]
    ready = bool(project.get("artifact") or job.get("previousPhase") == "ready")
    if document_store().exists(project_id):
        revision = document_store().read(project_id)['revision']
        ready = bool(project.get('artifact') and project['artifact'].get('revision') == revision)
    project.update(phase="ready" if ready else "outline", activeJobId=None, updatedAt=time.time())
    save(path, project)
    return project


async def run_legacy_job(identifier, request):
    def progress(stage, message):
        update_job(identifier, status="running", stage=stage, message=message)
    try:
        progress(0, "准备生成任务")
        if request.kind == "render":
            if not request.project.slides:
                raise ValueError("请先创建至少一页幻灯片。")
            template_id = request.templateId or request.project.templateId
            template_path = ROOT / "templates" / key(template_id) / "template.pptx" if template_id else None
            if template_path and not template_path.is_file():
                raise ValueError("公司模板不存在，请重新上传或选择模板。")
            folder = ROOT / "artifacts" / identifier
            folder.mkdir(exist_ok=True)
            illustrated, pictures, image_warnings = await image_service.illustrate_project(ROOT, request.project, progress)
            progress(1, "正在套用公司模板" if template_path else "正在排版幻灯片")
            warnings = await asyncio.to_thread(create_deck, illustrated, folder / "presentation.pptx", template_path, pictures)
            warnings = list(dict.fromkeys(image_warnings + warnings))
            progress(2, "LibreOffice 正在生成真实预览与 PDF")
            images = await render_pdf(folder / "presentation.pptx")
            warnings.extend(await asyncio.to_thread(audit_rendered_pdf, illustrated, folder / "presentation.pdf"))
            warnings = list(dict.fromkeys(warnings))
            base = f"{PREFIX}/artifacts/{identifier}"
            artifact = {"id": identifier, "pptxUrl": base + "/presentation.pptx", "pdfUrl": base + "/presentation.pdf", "previewUrls": [base + "/" + image for image in images], "warnings": warnings}
            save(folder / "metadata.json", artifact)
            result = {"slides": [slide.model_dump() for slide in illustrated.slides], "artifact": artifact, "reply": f"演示文件已生成，共 {len(illustrated.slides)} 页，可预览、播放或下载。"}
        else:
            async with LLM_LIMIT:
                try:
                    with telemetry.generation(request, identifier) as span:
                        context = span.get_span_context()
                        if context.is_valid:
                            update_job(identifier, traceId=f"{context.trace_id:032x}")
                        result = await asyncio.wait_for(agents.generate(request, progress), timeout=600)
                        if result.get("generationMeta"):
                            span.set_attribute("ppt.prompt.version", result["generationMeta"]["promptVersion"])
                            span.set_attribute("ppt.scenario", result["generationMeta"]["scenario"])
                finally:
                    await telemetry.flush()
        if read(job_path(identifier))["status"] == "cancelled":
            return
        completed_project = finish_project_job(identifier, result)
        if completed_project:
            result["project"] = completed_project
        update_job(identifier, status="completed", stage=3, message="生成完成", result=result)
    except asyncio.CancelledError:
        update_job(identifier, status="cancelled", message="任务已停止")
        finish_project_job(identifier)
        raise
    except Exception as exc:
        # Do not expose provider errors, request headers, tokens or filesystem paths.
        message = str(exc) if isinstance(exc, ValueError) else "生成服务调用失败，请检查模型配置或渲染器后重试。"
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "WEKNORA_API_KEY", "TAVILY_API_KEY", "SERPER_API_KEY", "PPT_WEB_MCP_HEADERS"):
            secret = os.getenv(name)
            if secret:
                message = message.replace(secret, "[redacted]")
        update_job(identifier, status="failed", message=message[:500], error=message[:500])
        finish_project_job(identifier)
    finally:
        TASKS.pop(identifier, None)


@app.post(PREFIX + "/jobs", status_code=202)
async def create_job(request: JobRequest):
    return enqueue_job(request)


def enqueue_job(request, *, parent=None, checkpoint=None):
    ensure_not_deleted(request.project.id)
    path = project_path(request.project.id)
    previous = read(path) if path.is_file() else None
    if previous and project_busy(previous):
        raise HTTPException(409, "该工作记录已有生成任务，请等待完成或先停止。")
    if len(TASKS) >= 12:
        raise HTTPException(429, "当前任务较多，请稍后重试。")
    if request.kind != "render" and not agents.health()["llmConfigured"]:
        raise HTTPException(503, "大模型未配置，请先配置服务端 API Key 和模型。")
    store = document_store()
    actual = store.read(request.project.id)['revision'] if store.exists(request.project.id) else 0
    if request.expectedRevision is not None and request.expectedRevision != actual:
        raise HTTPException(409, '文档版本已变化，请重新加载后发起任务。')
    request.expectedRevision = actual
    identifier = uuid4().hex
    job = {"id": identifier, "projectId": request.project.id, "previousPhase": (previous or {}).get('phase', request.project.phase), "status": "queued", "kind": request.kind, "stage": 0, "message": "任务排队中", "createdAt": time.time(), "baseRevision": actual, "recoverable": True}
    job['execution'] = checkpoint_execution(initial_execution(), checkpoint) if checkpoint else initial_execution()
    if parent:
        job['parentJobId'] = parent
    for directory in ('requests', 'checkpoints'):
        (ROOT / directory).mkdir(exist_ok=True)
    save(ROOT / 'requests' / f'{identifier}.json', request.model_dump())
    if checkpoint:
        save(ROOT / 'checkpoints' / f'{identifier}.json', checkpoint)
        job['checkpoint'] = {'stage': checkpoint.get('stage', 'resume'), 'summary': '已保留生成检查点'}
        completed = len(checkpoint.get('pages', []))
        total = len(checkpoint.get('plan', {}).get('pages', []))
        job['checkpoint'].update(completedPages=completed, totalPages=total, summary=f'已保留 {completed}/{total} 页' if total else '已保留生成检查点')
    save(job_path(identifier), job)
    project = metadata_from_client(request.project, previous)
    project.update(activeJobId=identifier, lastJobId=identifier, phase="generating", updatedAt=time.time())
    save(path, project)
    TASKS[identifier] = asyncio.create_task(run_job(identifier, request))
    TASKS[identifier].add_done_callback(lambda _: TASKS.pop(identifier, None))
    return job


@app.get(PREFIX + "/jobs/{identifier}")
async def get_job(identifier: str):
    return read(job_path(identifier))


@app.post(PREFIX + "/jobs/{identifier}/cancel")
async def cancel_job(identifier: str):
    job = read(job_path(identifier))
    if job["status"] in ("queued", "running"):
        job.update(status="cancelled", message="任务已停止")
        job['execution'] = terminal_execution(job.get('execution'), 'cancelled', job['message'])
        save(job_path(identifier), job)
        finish_project_job(identifier)
        task = TASKS.get(identifier)
        if task:
            task.cancel()
    return job


@app.get(PREFIX + "/artifacts/{identifier}/{filename}")
async def artifact_file(identifier: str, filename: str):
    if not re.fullmatch(r"presentation\.(pptx|pdf)|slide-[1-9][0-9]*\.png|source\.zip|export-report\.json|visual-review\.json", filename):
        raise HTTPException(404, "未找到文件。")
    folder = ROOT / "artifacts" / key(identifier)
    read(folder / "metadata.json")  # only expose completely rendered artifacts
    path = folder / filename
    if not path.is_file():
        raise HTTPException(404, "未找到文件。")
    return FileResponse(path, filename=filename if not filename.endswith(".png") else None)


@app.get(PREFIX + '/templates/{identifier}/surgical')
async def template_surgical_inspect(identifier: str):
    """Paragraph addresses a surgical edit can target in this template.

    Editing a real corporate deck in place is lossless; converting it to PPTD is
    not (add-in data, embedded workbooks and embedded fonts do not survive). This
    route exposes the same three-step loop the reference skill uses: prep here,
    edit below, verify inside ``apply``.
    """
    source = ROOT / 'templates' / key(identifier) / 'template.pptx'
    if not source.is_file():
        raise HTTPException(404, '未找到公司模板。')
    try:
        return await asyncio.to_thread(surgical.inspect, source)
    except surgical.SurgicalError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post(PREFIX + '/templates/{identifier}/surgical')
async def template_surgical_apply(identifier: str, body: dict = Body(...)):
    """Apply surgical operations to a copy of the template, keeping all else byte-identical."""
    source = ROOT / 'templates' / key(identifier) / 'template.pptx'
    if not source.is_file():
        raise HTTPException(404, '未找到公司模板。')
    artifact_id = uuid4().hex
    folder = ROOT / 'artifacts' / artifact_id
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / 'presentation.pptx'
    try:
        report = await asyncio.to_thread(surgical.apply, source, body.get('operations'), destination)
    except surgical.SurgicalError as exc:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(422, str(exc)) from exc
    save(folder / 'metadata.json', {'id': artifact_id, 'source': 'surgical', 'templateId': identifier,
                                    'operations': report['operations'], 'verification': report['verification']})
    return {**report, 'artifactId': artifact_id,
            'url': f'{PREFIX}/artifacts/{artifact_id}/presentation.pptx'}


@app.exception_handler(RevisionConflict)
async def revision_conflict(_request, exc):
    return JSONResponse(status_code=409, content={'detail': str(exc)})


def require_pptd(identifier):
    ensure_not_deleted(identifier)
    project = read(project_path(identifier))
    store = document_store()
    if not store.exists(identifier):
        raise HTTPException(404, '该项目还没有 PPTD 文档。')
    return store, project


def changed_document(identifier, state):
    project = read(project_path(identifier))
    project.update(artifact=None, phase='outline', updatedAt=time.time(), workflow='pptd-v2')
    save(project_path(identifier), project)
    return state


@app.get(PREFIX + '/projects/{identifier}/pptd')
@app.get(PREFIX + '/projects/{identifier}/pptd/document')
async def get_pptd(identifier: str):
    store, _ = require_pptd(identifier)
    return store.read(identifier)


@app.get(PREFIX + '/projects/{identifier}/pptd/view', response_class=HTMLResponse)
async def view_pptd(identifier: str, embed: bool = False):
    require_pptd(identifier)
    return HTMLResponse(workbench.workbench_html(f'{PREFIX}/projects/{identifier}/pptd', project_id=identifier, embed=embed), headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@app.get(PREFIX + '/projects/{identifier}/pptd/fonts')
async def pptd_fonts(identifier: str):
    store, _ = require_pptd(identifier)
    return await asyncio.to_thread(workbench.fonts_for_document, store.read(identifier))


@app.get(PREFIX + '/projects/{identifier}/pptd/capabilities')
async def pptd_capabilities(identifier: str):
    require_pptd(identifier)
    return workbench.capabilities()


@app.get(PREFIX + '/pptd-runtime/{resource:path}')
async def pptd_runtime(resource: str):
    try:
        path = workbench.safe_resource(workbench.RUNTIME_DIR, resource)
    except ValueError as exc:
        raise HTTPException(404, '未找到运行时资源。') from exc
    if not path.is_file():
        raise HTTPException(404, '未找到运行时资源。')
    return FileResponse(path, headers={'X-Content-Type-Options': 'nosniff'})


@app.get(PREFIX + '/pptd-fonts/{filename}')
async def pptd_font(filename: str):
    if not re.fullmatch(r'[a-f0-9]{64}\.(ttf|otf)', filename):
        raise HTTPException(404, '未找到字体。')
    path = workbench.safe_resource(workbench.FONT_CACHE, filename)
    if not path.is_file():
        raise HTTPException(404, '未找到字体。')
    return FileResponse(path, headers={'Cache-Control': 'public,max-age=31536000,immutable'})


@app.get(PREFIX + '/projects/{identifier}/pptd/assets/{resource:path}')
async def pptd_asset(identifier: str, resource: str):
    store, _ = require_pptd(identifier)
    try:
        path = store.resource_path(identifier, resource)
    except ValueError as exc:
        raise HTTPException(404, '未找到文档资源。') from exc
    if not path.is_file():
        raise HTTPException(404, '未找到文档资源。')
    return FileResponse(path, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@app.get(PREFIX + '/projects/{identifier}/pptd/source.zip')
async def pptd_source(identifier: str):
    from starlette.background import BackgroundTask
    store, _ = require_pptd(identifier)
    folder = ROOT / 'downloads'
    folder.mkdir(exist_ok=True)
    path = folder / f'{uuid4().hex}.zip'
    await asyncio.to_thread(store.bundle, identifier, path)
    return FileResponse(path, filename=f'{identifier}-pptd.zip', background=BackgroundTask(path.unlink, missing_ok=True))


@app.post(PREFIX + '/projects/{identifier}/pptd/edit')
async def edit_pptd(identifier: str, body: dict = Body(...)):
    store, project = require_pptd(identifier)
    if project_busy(project):
        raise HTTPException(409, '该文档正在生成，请停止任务后再编辑。')
    patch = dict(body)
    expected = patch.pop('expectedRevision', None)
    if type(expected) is not int:
        raise HTTPException(422, '编辑必须提供 expectedRevision。')
    try:
        return changed_document(identifier, store.edit(identifier, patch, expected))
    except RevisionConflict:
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get(PREFIX + '/projects/{identifier}/pptd/versions')
async def pptd_versions(identifier: str):
    store, _ = require_pptd(identifier)
    return store.read(identifier)['versions']


@app.post(PREFIX + '/projects/{identifier}/pptd/versions/{version_id}/restore')
async def pptd_restore_version(identifier: str, version_id: str, body: dict = Body(...)):
    store, project = require_pptd(identifier)
    if project_busy(project):
        raise HTTPException(409, '该文档正在生成，请停止任务后再恢复版本。')
    if type(body.get('expectedRevision')) is not int:
        raise HTTPException(422, '恢复必须提供 expectedRevision。')
    try:
        return changed_document(identifier, store.restore(identifier, key(version_id), body['expectedRevision']))
    except RevisionConflict:
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post(PREFIX + '/projects/{identifier}/pptd/migrate')
async def pptd_migrate(identifier: str):
    ensure_not_deleted(identifier)
    project = read(project_path(identifier))
    if project_busy(project):
        raise HTTPException(409, '项目正在运行任务，请先停止。')
    try:
        store = document_store()
        if store.exists(identifier):
            return store.read(identifier)
        return changed_document(identifier, store.migrate_legacy(identifier, project))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post(PREFIX + '/projects/{identifier}/pptd/export', status_code=202)
@app.post(PREFIX + '/projects/{identifier}/pptd/export-pdf', status_code=202)
async def pptd_export(identifier: str):
    _, project = require_pptd(identifier)
    return enqueue_job(JobRequest(kind='render', project=Project.model_validate(hydrate_project(project))))


@app.get(PREFIX + '/jobs/{identifier}/events')
async def job_events(identifier: str, request: Request):
    read(job_path(identifier))
    async def events():
        previous = None
        while not await request.is_disconnected():
            job = read(job_path(identifier))
            encoded = json.dumps(job, ensure_ascii=False)
            if encoded != previous:
                yield 'data: ' + encoded + '\n\n'
                previous = encoded
            else:
                yield ': heartbeat\n\n'
            if job['status'] not in ('queued', 'running'):
                break
            await asyncio.sleep(.5)
    return StreamingResponse(events(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'})


def restart_job(identifier, resume):
    old = read(job_path(identifier))
    if old['status'] not in ('failed', 'cancelled', 'interrupted'):
        raise HTTPException(409, '只有失败、停止或中断的任务可以重试。')
    path = ROOT / 'requests' / f'{key(identifier)}.json'
    if not path.is_file():
        raise HTTPException(409, '旧任务未保存请求，请从项目重新发起。')
    request = JobRequest.model_validate(read(path))
    ensure_not_deleted(request.project.id)
    current = read(project_path(request.project.id))
    checkpoint_path = ROOT / 'checkpoints' / path.name
    checkpoint = read(checkpoint_path) if resume and checkpoint_path.is_file() else None
    store = document_store()
    state = store.read(request.project.id) if store.exists(request.project.id) else None
    revision = state['revision'] if state else 0
    if checkpoint and state:
        checkpoint = visual_pipeline.reconcile_visual_checkpoint(checkpoint, state)
    base = checkpoint.get('_committedRevision', old.get('baseRevision', 0)) if checkpoint else old.get('baseRevision', 0)
    if resume and revision != base:
        raise HTTPException(409, '文档在任务中断后已变化，请发起新任务，避免旧检查点覆盖新内容。')
    # Generation context must remain unchanged for resume. Retry uses current metadata.
    if not resume:
        request.project = Project.model_validate(hydrate_project(current))
    request.expectedRevision = revision
    return enqueue_job(request, parent=identifier, checkpoint=checkpoint)


@app.post(PREFIX + '/jobs/{identifier}/retry', status_code=202)
async def retry_job(identifier: str):
    return restart_job(identifier, False)


@app.post(PREFIX + '/jobs/{identifier}/resume', status_code=202)
async def resume_job(identifier: str):
    return restart_job(identifier, True)


def assert_current_job(identifier, project_id):
    ensure_not_deleted(project_id)
    job = read(job_path(identifier))
    if job['status'] == 'cancelled':
        raise asyncio.CancelledError()
    project = read(project_path(project_id))
    if project.get('activeJobId') != identifier:
        raise ValueError('任务已被新的操作替代，未覆盖当前文档。')
    return project


def clean_error(exc):
    message = str(exc) if isinstance(exc, ValueError) else '生成服务调用失败，请检查模型配置或渲染器后重试。'
    for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'OPENAI_API_KEY', 'PPT_VISION_API_KEY', 'WEKNORA_API_KEY', 'TAVILY_API_KEY', 'SERPER_API_KEY', 'PPT_WEB_MCP_HEADERS'):
        secret = os.getenv(name)
        if secret:
            message = message.replace(secret, '[redacted]')
    return message[:1500]


async def run_job(identifier, request):
    if request.project.workflow != 'pptd-v2' and not document_store().exists(request.project.id):
        return await run_legacy_job(identifier, request)
    store = document_store()
    project_id = request.project.id
    cp_path = ROOT / 'checkpoints' / f'{identifier}.json'
    checkpoint = read(cp_path) if cp_path.is_file() else None
    def progress(stage, message):
        assert_current_job(identifier, project_id)
        update_job(identifier, status='running', stage=stage, message=message)
    def execution_event(event):
        assert_current_job(identifier, project_id)
        current = read(job_path(identifier))
        execution = record_event(current.get('execution'), event)
        stage = current.get('stage', 0)
        if event.get('phase') == 'visual':
            covered, total = event.get('covered', 0), event.get('total', 1)
            if isinstance(covered, int) and isinstance(total, int) and total > 0:
                stage = max(stage, 85 + min(12, int(12 * covered / total)))
        update_job(identifier, stage=stage, execution=execution, message=execution['events'][-1]['message'] if execution['events'] else current['message'])
    def checkpoint_save(value):
        previous = read(cp_path) if cp_path.is_file() else {}
        value = {**{k: v for k, v in previous.items() if k.startswith('_')}, **value}
        save(cp_path, value)
        completed = len(value.get('pages', []))
        total = len(value.get('plan', {}).get('pages', []))
        summary = f'已保存 {completed}/{total} 页' if total else '已保存生成检查点'
        stage = value.get('stage')
        position = {'context': 5, 'plan': 10, 'assets': 15, 'edit': 35, 'validated': 60, 'export': 65}.get(stage, 20 + int(40 * completed / max(1, total)))
        current = read(job_path(identifier))
        update_job(identifier, stage=max(current.get('stage', 0), position), checkpoint={'stage': stage, 'completedPages': completed, 'totalPages': total, 'summary': summary}, recoverable=True,
                   execution=checkpoint_execution(current.get('execution'), value))
    def generation_progress(stage, message):
        current = read(job_path(identifier))
        progress(max(current.get('stage', 0), {0: 10, 1: 20, 2: 40}.get(stage, stage)), message)
    async def export_progress(stage, message):
        progress(65 + int(stage * .20), message)
        execution_event({'phase': 'export', 'status': 'started', 'message': message})
    try:
        progress(0, '准备 PPTD 任务')
        existing = store.read(project_id) if store.exists(project_id) else None
        with telemetry.generation(request, identifier) as span:
            context = span.get_span_context()
            if context.is_valid:
                update_job(identifier, traceId=f'{context.trace_id:032x}')
            current_revision = existing['revision'] if existing else 0
            source_existing = checkpoint.get('_sourceExisting') if checkpoint and '_sourceExisting' in checkpoint else existing
            result = {'generationMeta': read(project_path(project_id)).get('generationMeta', {}), 'reply': ''}
            folder = ROOT / 'artifacts' / identifier
            render_attempts = 0
            seen_issues = set()
            while True:
                committed = checkpoint and checkpoint.get('_committedRevision')
                external = checkpoint and checkpoint.get('externalIssues')
                if committed and checkpoint.get('stage') == 'export' and pptd_exports.layout_only_issues(external):
                    # Resume old renderer checkpoints through protected layout
                    # patches; never rewrite saved text to fix its geometry.
                    checkpoint = {**checkpoint}
                    checkpoint.pop('externalIssues', None)
                    checkpoint_save(checkpoint)
                    external = None
                if committed and checkpoint.get('stage') == 'export' and not external and request.kind == 'outline':
                    # Older checkpoints could mark title-only pages complete.
                    # Recover their saved planned content in the generation
                    # stage; visual style repair must never invent body text.
                    from .planned_content import planned_content_issues
                    source = store.read(project_id)
                    if source['revision'] != committed:
                        raise RevisionConflict('待恢复正文的文档版本已变化，请重新发起任务。')
                    missing = planned_content_issues(source, checkpoint.get('plan') or {})
                    if missing:
                        checkpoint = {**checkpoint, 'externalIssues': missing, '_visualReview': None,
                            'pages': source['pages'], 'plan': {**checkpoint['plan'], 'document': source['document']}}
                        checkpoint_save(checkpoint)
                        external = missing
                        execution_event({'phase': 'validate', 'status': 'retrying',
                            'message': f'发现 {len(missing)} 页缺少计划正文，AI 正在按原大纲补回，保留其余页面。'})
                if committed and checkpoint.get('stage') == 'export' and not external:
                    if current_revision != committed:
                        raise RevisionConflict('待导出的文档版本已变化，请重新发起任务。')
                    result['generationMeta'] = checkpoint.get('_generationMeta', result['generationMeta'])
                elif request.kind == 'render':
                    if not existing:
                        raise ValueError('请先生成或迁移一份 PPTD 文档。')
                else:
                    async with LLM_LIMIT:
                        result = await asyncio.wait_for(pptd_generation.generate_pptd(request, generation_progress, existing=source_existing, checkpoint=checkpoint, save_checkpoint=checkpoint_save, data_root=ROOT, on_event=execution_event), timeout=agents.timeout_setting('PPTD_GENERATION_TIMEOUT_SECONDS', 1800))
                    assert_current_job(identifier, project_id)
                    assets = {item['path']: Path(item['sourcePath']) for item in result.get('assets', [])}
                    state = store.commit(project_id, result['document'], result['pages'], 'AI 生成' if request.kind == 'outline' else 'AI 修改', current_revision, assets=assets)
                    current_revision = state['revision']
                    checkpoint = {**result.get('checkpoint', {}), 'stage': 'export', '_committedRevision': current_revision, '_generationMeta': result.get('generationMeta', {}), '_sourceExisting': source_existing}
                    checkpoint.pop('externalIssues', None)
                    checkpoint_save(checkpoint)
                    project = read(project_path(project_id))
                    project.update(workflow='pptd-v2', artifact=None, title=state['document'].get('title', project['title']), generationMeta=result.get('generationMeta', {}))
                    save(project_path(project_id), project)
                try:
                    execution_event({'phase': 'export', 'status': 'started', 'message': '等待渲染器，准备检查排版与导出文件'})
                    async with LLM_LIMIT:
                        metadata = await visual_pipeline.export_reviewed(store, project_id, folder, request, export_progress,
                            checkpoint=checkpoint, save_checkpoint=checkpoint_save, on_event=execution_event,
                            assert_current=lambda: assert_current_job(identifier, project_id))
                    break
                except ValueError as exc:
                    issues = getattr(exc, 'issues', None)
                    if not issues or request.kind == 'render' or getattr(exc, 'generation_context', {}).get('phase') == 'visual':
                        raise
                    render_attempts += 1
                    mapped = [{**item, 'location': item.get('location') or item.get('page') or 'document'} for item in issues]
                    fingerprint = json.dumps(mapped, sort_keys=True)
                    checkpoint = {**(checkpoint or {}), 'externalIssues': mapped, '_sourceExisting': source_existing, '_visualReview': None}
                    checkpoint_save(checkpoint)
                    if fingerprint in seen_issues or render_attempts >= int(os.getenv('PPTD_RENDER_REPAIR_BUDGET', '4')):
                        raise ValueError('渲染检查仍未通过，已保留文档和检查点。' + str(exc)) from exc
                    seen_issues.add(fingerprint)
                    progress(60, f'浏览器/导出检查发现 {len(issues)} 项问题，正在第 {render_attempts} 轮修复')
                    execution_event({'phase': 'validate', 'status': 'retrying', 'attempt': render_attempts, 'message': f'排版与导出检查发现 {len(issues)} 项问题，正在修复对应页面'})
            project = assert_current_job(identifier, project_id)
            if metadata['revision'] != store.read(project_id)['revision']:
                raise RevisionConflict('导出期间文档已变化，旧文件未发布，请重新导出。')
            base = f'{PREFIX}/artifacts/{identifier}'
            warnings = [*metadata.get('warnings', []), *result.get('generationMeta', {}).get('research', {}).get('warnings', []), *result.get('generationMeta', {}).get('assetWarnings', [])]
            artifact = {'id': identifier, 'revision': metadata['revision'], 'pptdRevision': metadata['revision'], 'pptxUrl': base + '/' + metadata['pptx'], 'pdfUrl': base + '/' + metadata['pdf'], 'sourceUrl': base + '/' + metadata['source'], 'reportUrl': base + '/' + metadata['report'], 'previewUrls': [base + '/' + name for name in metadata['previews']], 'warnings': list(dict.fromkeys(warnings))}
            meta = result.get('generationMeta', {})
            meta['validation'] = {**meta.get('validation', {}), 'status': 'render-passed', 'renderRequired': False, 'revision': metadata['revision'], 'reportUrl': artifact['reportUrl'], 'visualReview': metadata['visualReview']}
            project.update(artifact=artifact, generationMeta=meta, phase='ready', activeJobId=None, contentGenerated=True, updatedAt=time.time())
            reply = f"演示文件已生成，共 {metadata['pageCount']} 页；整稿及全部单页已通过 AI 视觉审查，预览、可编辑 PPTX、PDF 和 PPTD 源文件已就绪。"
            project['messages'] = [*project.get('messages', []), {'id': uuid4().hex, 'role': 'assistant', 'text': reply}][-1000:]
            save(folder / 'metadata.json', artifact)
            save(project_path(project_id), project)
            execution = record_event(read(job_path(identifier)).get('execution'), {'phase': 'done', 'status': 'completed', 'message': f'共 {metadata["pageCount"]} 页，演示文件已就绪'})
            update_job(identifier, status='completed', stage=100, message='生成完成', recoverable=False, execution=execution, result={'artifact': artifact, 'reply': reply, 'project': hydrate_project(project)})
    except asyncio.CancelledError:
        if read(job_path(identifier))['status'] != 'cancelled':
            update_job(identifier, status='interrupted', message='任务中断，检查点已保留。', recoverable=True,
                       execution=terminal_execution(read(job_path(identifier)).get('execution'), 'interrupted', '任务中断，检查点已保留'))
        finish_project_job(identifier)
        raise
    except Exception as exc:
        if isinstance(exc, pptd_generation.GenerationIncomplete):
            checkpoint_save(exc.checkpoint)
        current = read(job_path(identifier))
        message, diagnostic, context = diagnose_error(exc, current.get('execution', {}).get('phase', 'context'))
        if diagnostic.get('issues') and cp_path.is_file():
            plan = read(cp_path).get('plan', {})
            paths = plan.get('document', {}).get('pages', [])
            for issue in diagnostic['issues']:
                path = issue.get('page') or next((p for p in paths if issue.get('location', '').startswith(p + '#') or issue.get('location') == p), None)
                if path in paths:
                    index = paths.index(path)
                    issue['pageIndex'] = index
                    issue['pageTitle'] = plan.get('pages', [])[index].get('title', '') if index < len(plan.get('pages', [])) else ''
        logging.getLogger(__name__).error('PPTD job %s failed: %s (%s)', identifier, diagnostic['code'], ', '.join(diagnostic['exceptionTypes']))
        update_job(identifier, status='failed', message=message, error=message, diagnostic=diagnostic, recoverable=True,
                   execution=terminal_execution(current.get('execution'), 'failed', message, context))
        finish_project_job(identifier)
    finally:
        await telemetry.flush()
        TASKS.pop(identifier, None)


@app.post(PREFIX + '/projects/{identifier}/pptd/import-template')
async def pptd_import_template(identifier: str, body: dict = Body(...)):
    """Analyze a company template into a reusable design reference.

    This follows the template workflow of the source skill: convert the upload,
    read its visual style, page archetypes and reusable components, then
    organize a *new* presentation. The template's own pages therefore never
    become the project document, and only the bounded digest travels with model
    requests; individual pages stay readable on demand by sourceId.
    """
    ensure_not_deleted(identifier)
    project = read(project_path(identifier))
    if project_busy(project):
        raise HTTPException(409, '项目正在运行任务，请先停止。')
    template_id = key(str(body.get('templateId', '')))
    source = ROOT / 'templates' / template_id / 'template.pptx'
    if not source.is_file():
        raise HTTPException(404, '未找到公司模板。')
    expected = body.get('expectedRevision', 0)
    if type(expected) is not int or expected < 0:
        raise HTTPException(422, '导入必须提供有效 expectedRevision。')
    try:
        report = await asyncio.to_thread(template_reference.build, source, ROOT, template_id)
        reference = await asyncio.to_thread(template_reference.load, ROOT, template_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not reference:
        raise HTTPException(422, '模板参考分析未生成，请重试。')
    # No other request may have started while Office conversion ran.
    project = read(project_path(identifier))
    ensure_not_deleted(identifier)
    if project_busy(project):
        raise HTTPException(409, '导入期间项目已开始生成，请完成后再导入。')
    warnings = [item.get('detail', str(item)) for item in report.get('unsupported', [])]
    project.update(templateId=template_id, workflow='pptd-v2', phase='outline', updatedAt=time.time(),
                   importWarnings=warnings, importReport=report, templateReference=reference['summary'])
    save(project_path(identifier), project)
    return {'project': hydrate_project(project), 'warnings': warnings, 'reference': reference['summary']}
