"""Job runner for page repair, review and design operations."""
from __future__ import annotations

import asyncio
from pathlib import Path
import time
from uuid import uuid4

from . import model_gateway, page_actions, telemetry
from .job_store import JobRuntime


async def run_page_action_job(
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
    store_factory,
    project_path,
    hydrate_project,
    save_json,
    clean_error,
    remove_task,
):
    project_id = request.project.id
    page_path = request.selectedSlideId
    runtime = JobRuntime(
        job_store,
        identifier,
        project_id,
        lambda: assert_current(identifier, project_id),
        root / 'checkpoints' / f'{identifier}.json',
    )

    try:
        if not isinstance(page_path, str) or not page_path:
            raise ValueError('页面操作必须指定当前页面。')
        runtime.progress(5, '正在准备当前页面')
        async with llm_limit:
            report = await asyncio.wait_for(
                page_actions.run_page_action(
                    request.kind,
                    store_factory(),
                    project_id,
                    page_path,
                    request,
                    root,
                ),
                timeout=model_gateway.timeout_setting(
                    'PPTD_PAGE_ACTION_TIMEOUT_SECONDS',
                    1800,
                ),
            )
        project = assert_current(identifier, project_id)
        project.update(
            workflow='pptd-v2',
            phase='ready',
            activeJobId=None,
            contentGenerated=True,
            updatedAt=time.time(),
        )
        action = report['action']
        issue_count = len(report.get('issues') or [])
        if action == 'review':
            message = '当前页面视觉检视完成' + (
                f'，发现 {issue_count} 项问题' if issue_count else '，未发现问题'
            )
        elif action == 'repair':
            project['artifact'] = None
            message = '当前页面修复完成' + (
                f'，仍有 {issue_count} 项问题' if issue_count else ''
            )
        else:
            message = '页面设计稿已生成，可预览后应用' + (
                f'，仍有 {issue_count} 项问题' if issue_count else ''
            )
        project['messages'] = [
            *project.get('messages', []),
            {'id': uuid4().hex, 'role': 'assistant', 'text': message},
        ][-1000:]
        save_json(project_path(project_id), project)
        update_job(
            identifier,
            status='completed',
            stage=100,
            message=message,
            recoverable=False,
            result={
                'reply': message,
                'revision': report['revision'],
                'pageAction': report,
                'project': hydrate_project(project),
            },
        )
    except asyncio.CancelledError:
        current = read_job(identifier)
        if current['status'] != 'cancelled':
            update_job(
                identifier,
                status='interrupted',
                message='页面操作已中断，原稿未覆盖。',
                recoverable=False,
            )
        finish_job(identifier)
        raise
    except Exception as exc:
        message = clean_error(exc)
        update_job(
            identifier,
            status='failed',
            message=message,
            error=message,
            recoverable=False,
        )
        finish_job(identifier)
    finally:
        await telemetry.flush()
        remove_task(identifier)
