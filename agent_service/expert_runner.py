"""Job runner for the agentic PPT expert."""
from __future__ import annotations

import asyncio
from pathlib import Path
import time
from uuid import uuid4

from . import kimi_agent, model_gateway, telemetry
from .job_execution import diagnose_error, record_event, terminal_execution
from .job_store import JobRuntime


async def run_kimi_job(
    identifier,
    request,
    *,
    root: Path,
    llm_limit,
    job_store,
    update_job,
    read_job,
    finish_job,
    assert_current,
    project_path,
    hydrate_project,
    save_json,
    remove_task,
):
    project_id = request.project.id
    current = read_job(identifier)
    kimi_agent.restore_workspace(
        root,
        project_id,
        identifier,
        current.get('parentJobId'),
    )
    recovery = kimi_agent.recovery_checkpoint(root, project_id, identifier)
    if recovery:
        update_job(identifier, checkpoint=recovery)
    runtime = JobRuntime(
        job_store,
        identifier,
        project_id,
        lambda: assert_current(identifier, project_id),
        root / 'checkpoints' / f'{identifier}.json',
    )

    try:
        runtime.progress(0, '准备 PPT 专家任务')
        async with llm_limit:
            result = await asyncio.wait_for(
                kimi_agent.run_kimi(
                    request,
                    data_root=root,
                    progress=runtime.progress,
                    on_event=runtime.event,
                    project_id=project_id,
                    job_id=identifier,
                ),
                timeout=model_gateway.timeout_setting(
                    'PPT_KIMI_TIMEOUT_SECONDS',
                    5400,
                ),
            )
        project = assert_current(identifier, project_id)
        project.update(
            workflow='pptd-v2',
            artifact=None,
            title=result['document'].get('title', project['title']),
            generationMeta=result.get('generationMeta', {}),
            phase='ready',
            activeJobId=None,
            contentGenerated=True,
            updatedAt=time.time(),
        )
        reply = result.get('reply', '') or '演示文稿已生成，可编辑预览并手动导出。'
        warnings = result.get('warnings') or []
        project['messages'] = [
            *project.get('messages', []),
            {'id': uuid4().hex, 'role': 'assistant', 'text': reply},
        ][-1000:]
        save_json(project_path(project_id), project)
        summary = f'共 {len(result["pages"])} 页，演示文件已就绪'
        if warnings:
            summary += f'；{len(warnings)} 项排版问题待确认'
        execution = record_event(
            read_job(identifier).get('execution'),
            {'phase': 'done', 'status': 'completed', 'message': summary},
        )
        update_job(
            identifier,
            status='completed',
            stage=100,
            message=summary,
            recoverable=False,
            execution=execution,
            result={
                'reply': reply,
                'revision': result['revision'],
                'pageCount': len(result['pages']),
                'warnings': warnings,
                'project': hydrate_project(project),
            },
        )
    except asyncio.CancelledError:
        current = read_job(identifier)
        if current['status'] != 'cancelled':
            recovery = kimi_agent.recovery_checkpoint(root, project_id, identifier)
            message = (
                f"任务中断，已保留 {recovery['summary']}，可继续任务。"
                if recovery
                else '任务中断，未生成可用文档。'
            )
            update_job(
                identifier,
                status='interrupted',
                message=message,
                recoverable=bool(recovery),
                checkpoint=recovery,
                execution=terminal_execution(
                    current.get('execution'),
                    'interrupted',
                    message,
                ),
            )
        finish_job(identifier)
        raise
    except Exception as exc:
        current = read_job(identifier)
        recovery = (
            kimi_agent.recovery_checkpoint(root, project_id, identifier)
            or current.get('checkpoint')
        )
        resumable = bool(
            isinstance(recovery, dict) and recovery.get('completedPages', 0) > 0
        )
        message, diagnostic, context = diagnose_error(
            exc,
            current.get('execution', {}).get('phase', 'context'),
            resumable=resumable,
        )
        update_job(
            identifier,
            status='failed',
            message=message,
            error=message,
            diagnostic=diagnostic,
            recoverable=resumable,
            checkpoint=recovery,
            execution=terminal_execution(
                current.get('execution'),
                'failed',
                message,
                context,
            ),
        )
        finish_job(identifier)
    finally:
        await telemetry.flush()
        remove_task(identifier)
