import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4
import time
import logging

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request, Body
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse, JSONResponse

from . import agents, images as image_service, telemetry, template_reference, kimi_agent, kimi_workspace, page_actions, orchestrator, model_gateway, provider_registry
from .models import ImageSearchRequest, JobRequest, Project
from .pptd_store import DocumentStore, RevisionConflict
from . import export_service as pptd_exports, pptd_generation, visual_pipeline
from .resource_manager import MODEL_LIMIT
from .job_execution import initial_execution, checkpoint_execution, terminal_execution
from .job_store import JobStore
from .job_stream import events as job_events_stream
from .legacy_runner import run_legacy_job as run_legacy_job_impl
from .page_action_runner import run_page_action_job as run_page_action_job_impl
from .expert_runner import run_kimi_job as run_kimi_job_impl
from .pptd_runner import (
    autofit_document_for_export,
    editable_fallback_ready,
    normalize_document_for_export,
    run_pptd_job as run_pptd_job_impl,
)
from pptd_engine import workbench
from .rendering import (
    MAX_UPLOAD,
    audit_rendered_pdf,
    create_deck,
    render_pdf,
    renderer_available,
    template_info,
    validate_package,
)

ROOT = Path(os.getenv("STUDIO_DATA_DIR", Path(__file__).resolve().parents[1] / "runtime" / "studio")).resolve()
for directory in ("projects", "templates", "jobs", "artifacts", "trash", "images", "requests", "checkpoints", "reference-files"):
    (ROOT / directory).mkdir(parents=True, exist_ok=True)
TASKS: dict[str, asyncio.Task] = {}
LLM_LIMIT = MODEL_LIMIT


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
    return job_store().path(key(identifier))


def job_store():
    return JobStore(ROOT)


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
    return job_store().update(key(identifier), **changes)


def kimi_recovery_checkpoint(job):
    if job.get('kind') != 'kimi-slides':
        return None
    return kimi_agent.recovery_checkpoint(ROOT, job.get('projectId'), job.get('id'))


@asynccontextmanager
async def lifespan(app):
    telemetry.setup()
    for path in (ROOT / "jobs").glob("*.json"):
        job = read(path)
        if job["status"] in ("queued", "running"):
            recovery = kimi_recovery_checkpoint(job)
            if recovery:
                message = f"服务重启，已保留 {recovery['summary']}，可继续任务。"
            else:
                message = "服务重启，任务已保留，可继续任务。"
            job.update(status="interrupted", message=message, error="任务因服务重启中断。", recoverable=(ROOT / "requests" / path.name).is_file())
            if recovery:
                job['checkpoint'] = recovery
            if job.get('execution'):
                job['execution'] = terminal_execution(job['execution'], 'interrupted', job['message'])
            save(path, job)
            finish_project_job(job["id"])
        elif not job.get('checkpoint'):
            recovery = kimi_recovery_checkpoint(job)
            if recovery:
                job['checkpoint'] = recovery
                job['recoverable'] = True
                save(path, job)
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
    return {"status": "ok", **model_gateway.health(), "rendererAvailable": await asyncio.to_thread(pptd_exports.renderer_available), "legacyRendererAvailable": await asyncio.to_thread(renderer_available), "workflow": "pptd-v2", "pptdAvailable": True, "images": image_service.capabilities(), "tracing": telemetry.health(), **provider_registry.snapshot()}


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


@app.get(PREFIX + "/reference-budget")
async def reference_budget():
    try:
        return await model_gateway.reference_budget()
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(503, "读取模型上下文预算超时，请稍后重试。") from exc


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


def project_summary(project, store=None):
    store = store or document_store()
    slide_count = store.summary(project["id"])["pageCount"] if store.exists(project["id"]) else len(project.get("slides", []))
    return {"id": project["id"], "title": project["title"], "slideCount": slide_count,
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


def reference_file_directory(identifier, attachment_id):
    return ROOT / "reference-files" / key(identifier) / key(attachment_id)


def reference_file_metadata(identifier, attachment_id):
    folder = reference_file_directory(identifier, attachment_id)
    metadata_path = folder / "metadata.json"
    if not metadata_path.is_file():
        raise HTTPException(404, "未找到参考资料。")
    return folder, read(metadata_path)


@app.get(PREFIX + "/projects/{identifier}/reference-files")
async def list_reference_files(identifier: str):
    ensure_not_deleted(identifier)
    folder = ROOT / "reference-files" / key(identifier)
    if not folder.is_dir():
        return []
    records = [read(path) for path in folder.glob("*/metadata.json")]
    return sorted(records, key=lambda item: item.get("createdAt", 0), reverse=True)


@app.post(PREFIX + "/projects/{identifier}/reference-files", status_code=201)
async def upload_reference_file(
    identifier: str,
    file: UploadFile = File(...),
    text: str = Form(...),
):
    ensure_not_deleted(identifier)
    name = Path(file.filename or "参考资料").name[:200]
    suffix = Path(name).suffix.lower()
    if suffix not in (".docx", ".txt", ".md", ".markdown", ".csv", ".json"):
        raise HTTPException(400, "参考资料支持 TXT、Markdown、CSV、JSON 和 DOCX 文件。")
    limit = int(os.getenv("PPT_REFERENCE_FILE_MAX_BYTES") or 50 * 1024 * 1024)
    data = await file.read(limit + 1)
    await file.close()
    if len(data) > limit:
        raise HTTPException(413, f"参考资料请控制在 {limit // 1024 // 1024} MB 以内。")
    extracted = text.replace("\x00", "").strip()
    if not extracted:
        raise HTTPException(400, "文件中没有可读取的文本内容。")
    attachment_id = uuid4().hex
    folder = reference_file_directory(identifier, attachment_id)
    folder.mkdir(parents=True)
    original = folder / ("original" + suffix)
    original.write_bytes(data)
    (folder / "extracted.txt").write_text(extracted, encoding="utf-8")
    base = f"{PREFIX}/projects/{key(identifier)}/reference-files/{attachment_id}"
    metadata = {
        "id": attachment_id,
        "name": name,
        "size": len(data),
        "extension": suffix,
        "characters": len(extracted),
        "createdAt": time.time(),
        "downloadUrl": base + "/download",
        "textUrl": base + "/text",
    }
    save(folder / "metadata.json", metadata)
    return metadata


@app.get(PREFIX + "/projects/{identifier}/reference-files/{attachment_id}/text")
async def reference_file_text(identifier: str, attachment_id: str):
    folder, metadata = reference_file_metadata(identifier, attachment_id)
    path = folder / "extracted.txt"
    if not path.is_file():
        raise HTTPException(404, "未找到参考资料正文。")
    return {**metadata, "text": path.read_text(encoding="utf-8")}


@app.get(PREFIX + "/projects/{identifier}/reference-files/{attachment_id}/download")
async def download_reference_file(identifier: str, attachment_id: str):
    folder, metadata = reference_file_metadata(identifier, attachment_id)
    path = folder / ("original" + metadata["extension"])
    if not path.is_file():
        raise HTTPException(404, "未找到参考资料原文件。")
    media_type = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if metadata["extension"] == ".docx"
        else "text/plain; charset=utf-8"
    )
    return FileResponse(path, media_type=media_type, filename=metadata["name"])


@app.delete(PREFIX + "/projects/{identifier}/reference-files/{attachment_id}")
async def delete_reference_file(identifier: str, attachment_id: str):
    folder, metadata = reference_file_metadata(identifier, attachment_id)
    shutil.rmtree(folder)
    return {"id": attachment_id, "deleted": True}


@app.get(PREFIX + "/projects")
async def list_projects():
    projects = [read(path) for path in (ROOT / "projects").glob("*.json")]
    store = document_store()
    return [project_summary(p, store) for p in sorted(projects, key=lambda p: p.get("updatedAt", 0), reverse=True)]


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
    return await run_legacy_job_impl(
        identifier,
        request,
        root=ROOT,
        prefix=PREFIX,
        llm_limit=LLM_LIMIT,
        update_job=update_job,
        read_job=lambda value: read(job_path(value)),
        finish_job=finish_project_job,
        save_json=save,
        remove_task=TASKS.pop,
        safe_id=key,
        create_deck=create_deck,
        render_pdf=render_pdf,
        audit_rendered_pdf=audit_rendered_pdf,
    )


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
    if request.kind != "render" and not model_gateway.health()["llmConfigured"]:
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
        recovery = kimi_recovery_checkpoint(job)
        message = "任务已停止"
        if recovery:
            message = f"任务已停止，已保留 {recovery['summary']}，可继续任务。"
            job['checkpoint'] = recovery
        job.update(status="cancelled", message=message)
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


@app.get(PREFIX + '/projects/{identifier}/pptd/thumbnail')
async def get_pptd_thumbnail(identifier: str, page: str, revision: int | None = None):
    """Return only the page needed by one navigation thumbnail."""
    store, _ = require_pptd(identifier)
    state = store.read(identifier)
    if revision is not None and revision != state['revision']:
        raise HTTPException(409, '页面版本已更新，请刷新预览。')
    selected = next((item for item in state['pages'] if item.get('path') == page), None)
    if selected is None:
        raise HTTPException(404, '页面不存在。')
    return JSONResponse(
        {
            'revision': state['revision'],
            'document': state['document'],
            'pages': [selected],
        },
        headers={'Cache-Control': 'private, max-age=300, immutable'},
    )


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


@app.get(PREFIX + '/projects/{identifier}/pptd/design-candidates/{candidate_id}/preview')
async def design_candidate_preview(identifier: str, candidate_id: str):
    folder, _ = page_actions.read_design_candidate(ROOT, key(identifier), key(candidate_id))
    preview = folder / 'preview.png'
    if not preview.is_file():
        raise HTTPException(404, '设计稿预览不存在。')
    return FileResponse(preview, media_type='image/png', headers={
        'Cache-Control': 'private, max-age=300',
        'X-Content-Type-Options': 'nosniff',
    })


@app.post(PREFIX + '/projects/{identifier}/pptd/design-candidates/{candidate_id}/apply')
async def design_candidate_apply(identifier: str, candidate_id: str, body: dict = Body(...)):
    expected = body.get('expectedRevision')
    if type(expected) is not int:
        raise HTTPException(422, '应用设计稿必须提供 expectedRevision。')
    try:
        committed = page_actions.apply_design_candidate(
            document_store(), key(identifier), key(candidate_id), expected
        )
    except RevisionConflict:
        raise
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    project = read(project_path(identifier))
    project.update(
        workflow='pptd-v2',
        phase='ready',
        activeJobId=None,
        artifact=None,
        contentGenerated=True,
        updatedAt=time.time(),
    )
    project['messages'] = [
        *project.get('messages', []),
        {'id': uuid4().hex, 'role': 'assistant', 'text': '页面设计稿已应用。'},
    ][-1000:]
    save(project_path(identifier), project)
    return committed


@app.delete(PREFIX + '/projects/{identifier}/pptd/design-candidates/{candidate_id}')
async def design_candidate_discard(identifier: str, candidate_id: str):
    try:
        page_actions.discard_design_candidate(
            document_store(), key(identifier), key(candidate_id)
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'id': candidate_id, 'discarded': True}


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
    return StreamingResponse(
        job_events_stream(
            job_store(),
            key(identifier),
            request.is_disconnected,
        ),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'},
    )


@app.get(PREFIX + '/jobs/{identifier}/draft-previews/{filename}')
async def job_draft_preview(identifier: str, filename: str):
    if not re.fullmatch(r'page-[1-9][0-9]*\.png', filename):
        raise HTTPException(404, '未找到页面预览。')
    job = read(job_path(identifier))
    workspace = kimi_workspace.workspace(
        ROOT,
        key(job.get('projectId', '')),
        key(identifier),
    )
    path = workspace / 'draft-previews' / filename
    if not path.is_file():
        raise HTTPException(404, '页面预览尚未生成。')
    return FileResponse(
        path,
        media_type='image/png',
        headers={
            'Cache-Control': 'private, max-age=60',
            'X-Content-Type-Options': 'nosniff',
        },
    )


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


async def run_kimi_job(identifier, request):
    return await run_kimi_job_impl(
        identifier,
        request,
        root=ROOT,
        llm_limit=LLM_LIMIT,
        job_store=job_store(),
        update_job=update_job,
        read_job=lambda value: read(job_path(value)),
        finish_job=finish_project_job,
        assert_current=assert_current_job,
        project_path=project_path,
        hydrate_project=hydrate_project,
        save_json=save,
        remove_task=TASKS.pop,
    )


async def run_page_action_job(identifier, request):
    return await run_page_action_job_impl(
        identifier,
        request,
        root=ROOT,
        llm_limit=LLM_LIMIT,
        job_store=job_store(),
        update_job=update_job,
        read_job=lambda value: read(job_path(value)),
        finish_job=finish_project_job,
        assert_current=assert_current_job,
        store_factory=document_store,
        project_path=project_path,
        hydrate_project=hydrate_project,
        save_json=save,
        clean_error=clean_error,
        remove_task=TASKS.pop,
    )


def agentic_kimi_enabled():
    return os.getenv('PPT_KIMI_AGENTIC', '1').strip().lower() not in ('0', 'false', 'no')


async def run_job(identifier, request):
    return await orchestrator.dispatch(
        identifier,
        request,
        handlers={
            orchestrator.ExecutionRoute.PAGE_ACTION: run_page_action_job,
            orchestrator.ExecutionRoute.EXPERT: run_kimi_job,
            orchestrator.ExecutionRoute.LEGACY: run_legacy_job,
            orchestrator.ExecutionRoute.PPTD: run_pptd_job,
        },
        has_document=document_store().exists(request.project.id),
        expert_enabled=agentic_kimi_enabled(),
    )



async def run_pptd_job(identifier, request):
    return await run_pptd_job_impl(
        identifier,
        request,
        root=ROOT,
        prefix=PREFIX,
        llm_limit=LLM_LIMIT,
        job_store=job_store(),
        update_job=update_job,
        read_job=lambda value: read(job_path(value)),
        document_store=document_store,
        project_path=project_path,
        read_json=read,
        save_json=save,
        hydrate_project=hydrate_project,
        assert_current=assert_current_job,
        finish_job=finish_project_job,
        remove_task=TASKS.pop,
    )


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
    # Audits retain every location; the project-facing list only needs distinct
    # capability limits. The UI groups older saved projects that still contain
    # one warning per shape.
    warnings = list(dict.fromkeys(
        str(item.get('detail', item)).strip()
        for item in report.get('unsupported', [])
        if str(item.get('detail', item)).strip()
    ))
    project.update(templateId=template_id, workflow='pptd-v2', phase='outline', updatedAt=time.time(),
                   importWarnings=warnings, importReport=report, templateReference=reference['summary'])
    save(project_path(identifier), project)
    return {'project': hydrate_project(project), 'warnings': warnings, 'reference': reference['summary']}
