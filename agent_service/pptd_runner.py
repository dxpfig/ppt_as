"""Deterministic quick, edit and export job runner."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import logging
from pathlib import Path
import time
from uuid import uuid4

from . import (
    export_service as pptd_exports,
    model_gateway,
    pptd_generation,
    telemetry,
    visual_pipeline,
)
from .contracts import ArtifactRecord
from .execution_policy import quick_policy
from .job_execution import diagnose_error, record_event, terminal_execution
from .job_store import JobRuntime
from .layout_autofit import autofit_state
from .pptd_compat import normalize_custom_fonts, normalize_pages
from .pptd_store import RevisionConflict


EDITABLE_FALLBACK_ISSUES = {
    'text-overflow',
    'text-overlap',
    'text-clipped',
    'text-contrast',
    'font-unresolved',
    'native-unsupported',
}


def editable_fallback_ready(checkpoint):
    """Whether a complete deck may be published without the export repair pass."""
    checkpoint = checkpoint or {}
    plan = checkpoint.get('plan') or {}
    paths = (plan.get('document') or {}).get('pages') or []
    completed = {
        item.get('path')
        for item in checkpoint.get('pages') or []
        if isinstance(item, dict)
    }
    issues = checkpoint.get('externalIssues') or []
    return (
        bool(paths)
        and set(paths) <= completed
        and bool(issues)
        and all(
            isinstance(item, dict) and item.get('code') in EDITABLE_FALLBACK_ISSUES
            for item in issues
        )
    )


def normalize_document_for_export(store, project_id, expected_revision):
    """Persist deterministic compatibility fixes before rendering an old deck."""
    state = store.read(project_id)
    if state['revision'] != expected_revision:
        return None
    pages = deepcopy(state['pages'])
    changed = normalize_pages(pages)
    removed_fonts = normalize_custom_fonts(
        document := deepcopy(state['document']),
        pages,
    )
    if not any(changed.values()) and not removed_fonts:
        return None
    return store.commit(
        project_id,
        document,
        pages,
        '导出兼容性修复',
        expected_revision,
    )


def autofit_document_for_export(store, project_id, error, expected_revision):
    """Scale only measured overflow elements without calling the model."""
    issues = getattr(error, 'issues', None)
    if not isinstance(issues, list) or not issues or not pptd_exports.layout_only_issues(issues):
        return None
    layouts = getattr(error, 'layouts', None)
    if not isinstance(layouts, dict):
        layouts = {}
    state = store.read(project_id)
    if state['revision'] != expected_revision:
        return None
    pages = deepcopy(state['pages'])
    changed = autofit_state({'pages': pages}, layouts, issues)
    if not any(changed.values()):
        return None
    return store.commit(
        project_id,
        deepcopy(state['document']),
        pages,
        '导出排版自适应',
        expected_revision,
    )


async def run_pptd_job(
    identifier,
    request,
    *,
    root: Path,
    prefix: str,
    llm_limit,
    job_store,
    update_job,
    read_job,
    document_store,
    project_path,
    read_json,
    save_json,
    hydrate_project,
    assert_current,
    finish_job,
    remove_task,
):
    policy = quick_policy()
    store = document_store()
    project_id = request.project.id
    checkpoint_path = root / 'checkpoints' / f'{identifier}.json'
    checkpoint = read_json(checkpoint_path) if checkpoint_path.is_file() else None
    runtime = JobRuntime(
        job_store,
        identifier,
        project_id,
        lambda: assert_current(identifier, project_id),
        checkpoint_path,
    )
    progress = runtime.progress
    execution_event = runtime.event
    checkpoint_save = runtime.save_checkpoint

    def generation_progress(stage, message):
        current = read_job(identifier)
        progress(
            max(current.get('stage', 0), {0: 10, 1: 20, 2: 40}.get(stage, stage)),
            message,
        )

    async def export_progress(stage, message):
        current = read_job(identifier)
        progress(max(current.get('stage', 0), 65 + int(stage * 0.20)), message)
        execution_event({'phase': 'export', 'status': 'started', 'message': message})

    async def validation_progress(stage, message):
        current = read_job(identifier)
        progress(max(current.get('stage', 0), 55 + int(stage * 0.25)), message)
        execution_event({'phase': 'validate', 'status': 'started', 'message': message})

    def finish_editable_fallback(checkpoint_value):
        project = assert_current(identifier, project_id)
        state = store.read(project_id)
        issues = checkpoint_value.get('externalIssues') or []
        message = (
            f'可编辑稿已生成，共 {len(state["pages"])} 页；'
            f'导出前仍有 {len(issues)} 项排版或兼容性问题，自动修复所需模型暂不可用。'
            '内容与检查点均已保留。'
        )
        meta = project.get('generationMeta') or {}
        meta['validation'] = {
            'status': 'editable-with-export-issues',
            'renderRequired': True,
            'issueCount': len(issues),
            'issues': issues[:50],
            'summary': '可编辑 PPTD 已保存；导出验收未完成',
        }
        project.update(
            workflow='pptd-v2',
            phase='ready',
            activeJobId=None,
            contentGenerated=True,
            generationMeta=meta,
            updatedAt=time.time(),
        )
        project['messages'] = [
            *project.get('messages', []),
            {'id': uuid4().hex, 'role': 'assistant', 'text': message},
        ][-1000:]
        save_json(project_path(project_id), project)
        execution = record_event(
            read_job(identifier).get('execution'),
            {'phase': 'done', 'status': 'completed', 'message': message},
        )
        update_job(
            identifier,
            status='completed',
            stage=100,
            message=message,
            recoverable=False,
            execution=execution,
            result={
                'reply': message,
                'revision': state['revision'],
                'pageCount': len(state['pages']),
                'warnings': issues[:50],
                'project': hydrate_project(project),
            },
        )

    try:
        progress(0, '准备 PPTD 任务')
        existing = store.read(project_id) if store.exists(project_id) else None
        with telemetry.generation(request, identifier) as span:
            context = span.get_span_context()
            if context.is_valid:
                update_job(identifier, traceId=f'{context.trace_id:032x}')
            current_revision = existing['revision'] if existing else 0
            source_existing = (
                checkpoint.get('_sourceExisting')
                if checkpoint and '_sourceExisting' in checkpoint
                else existing
            )
            result = {
                'generationMeta': read_json(project_path(project_id)).get(
                    'generationMeta',
                    {},
                ),
                'reply': '',
            }
            folder = root / 'artifacts' / identifier
            render_attempts = 0
            autofit_attempts = 0
            seen_issues = set()
            while True:
                committed = checkpoint and checkpoint.get('_committedRevision')
                external = checkpoint and checkpoint.get('externalIssues')
                if committed and checkpoint.get('stage') == 'export' and external:
                    migrated = normalize_document_for_export(
                        store,
                        project_id,
                        current_revision,
                    )
                    if migrated:
                        current_revision = migrated['revision']
                        committed = current_revision
                        checkpoint = {
                            **checkpoint,
                            '_committedRevision': current_revision,
                        }
                        checkpoint.pop('externalIssues', None)
                        if request.kind != 'render':
                            checkpoint['_visualReview'] = None
                        checkpoint_save(checkpoint)
                        external = None
                        execution_event(
                            {
                                'phase': 'validate',
                                'status': 'retrying',
                                'message': (
                                    '已自动清理未引用字体和不兼容样式，'
                                    '正在重新执行排版检查。'
                                ),
                            }
                        )
                if (
                    committed
                    and checkpoint.get('stage') == 'export'
                    and pptd_exports.layout_only_issues(external)
                ):
                    checkpoint = {**checkpoint}
                    checkpoint.pop('externalIssues', None)
                    checkpoint_save(checkpoint)
                    external = None
                if (
                    committed
                    and checkpoint.get('stage') == 'export'
                    and not external
                    and request.kind == 'outline'
                ):
                    from .planned_content import planned_content_issues

                    source = store.read(project_id)
                    if source['revision'] != committed:
                        raise RevisionConflict(
                            '待恢复正文的文档版本已变化，请重新发起任务。'
                        )
                    missing = planned_content_issues(
                        source,
                        checkpoint.get('plan') or {},
                    )
                    if missing:
                        checkpoint = {
                            **checkpoint,
                            'externalIssues': missing,
                            '_visualReview': None,
                            'pages': source['pages'],
                            'plan': {
                                **checkpoint['plan'],
                                'document': source['document'],
                            },
                        }
                        checkpoint_save(checkpoint)
                        external = missing
                        execution_event(
                            {
                                'phase': 'validate',
                                'status': 'retrying',
                                'message': (
                                    f'发现 {len(missing)} 页缺少计划正文，'
                                    'AI 正在按原大纲补回，保留其余页面。'
                                ),
                            }
                        )
                if committed and checkpoint.get('stage') == 'export' and not external:
                    if current_revision != committed:
                        raise RevisionConflict(
                            '待导出的文档版本已变化，请重新发起任务。'
                        )
                    result['generationMeta'] = checkpoint.get(
                        '_generationMeta',
                        result['generationMeta'],
                    )
                elif request.kind == 'render':
                    if not existing:
                        raise ValueError('请先生成或迁移一份 PPTD 文档。')
                else:
                    try:
                        async with llm_limit:
                            result = await asyncio.wait_for(
                                pptd_generation.generate_pptd(
                                    request,
                                    generation_progress,
                                    existing=source_existing,
                                    checkpoint=checkpoint,
                                    save_checkpoint=checkpoint_save,
                                    data_root=root,
                                    on_event=execution_event,
                                ),
                                timeout=model_gateway.timeout_setting(
                                    'PPTD_GENERATION_TIMEOUT_SECONDS',
                                    1800,
                                ),
                            )
                    except Exception as exc:
                        _, diagnostic, _ = diagnose_error(
                            exc,
                            'validate',
                            resumable=checkpoint_path.is_file(),
                        )
                        if (
                            diagnostic.get('code')
                            in ('model_unavailable', 'model_timeout')
                            and editable_fallback_ready(checkpoint)
                        ):
                            finish_editable_fallback(checkpoint)
                            return
                        raise
                    assert_current(identifier, project_id)
                    normalize_pages(result['pages'])
                    normalize_custom_fonts(
                        result['document'],
                        result['pages'],
                    )
                    assets = {
                        item['path']: Path(item['sourcePath'])
                        for item in result.get('assets', [])
                    }
                    state = store.commit(
                        project_id,
                        result['document'],
                        result['pages'],
                        'AI 生成' if request.kind == 'outline' else 'AI 修改',
                        current_revision,
                        assets=assets,
                    )
                    current_revision = state['revision']
                    checkpoint = {
                        **result.get('checkpoint', {}),
                        'stage': 'export',
                        '_committedRevision': current_revision,
                        '_generationMeta': result.get('generationMeta', {}),
                        '_sourceExisting': source_existing,
                    }
                    checkpoint.pop('externalIssues', None)
                    checkpoint_save(checkpoint)
                    project = read_json(project_path(project_id))
                    project.update(
                        workflow='pptd-v2',
                        artifact=None,
                        title=state['document'].get('title', project['title']),
                        generationMeta=result.get('generationMeta', {}),
                    )
                    save_json(project_path(project_id), project)
                migrated = normalize_document_for_export(
                    store,
                    project_id,
                    current_revision,
                )
                if migrated:
                    current_revision = migrated['revision']
                    if checkpoint:
                        checkpoint = {
                            **checkpoint,
                            '_committedRevision': current_revision,
                        }
                        if request.kind != 'render':
                            checkpoint['_visualReview'] = None
                        checkpoint_save(checkpoint)
                    execution_event(
                        {
                            'phase': 'validate',
                            'status': 'retrying',
                            'message': (
                                '已自动转换旧文档中不兼容的换行、字体与透明样式，'
                                '正在重新检查导出。'
                            ),
                        }
                    )
                try:
                    visual_state = (checkpoint or {}).get('_visualReview') or {}
                    reviewed_revision = (checkpoint or {}).get('_committedRevision')
                    if request.kind == 'render':
                        visual_summary = (
                            visual_state
                            if visual_state.get('status') == 'passed'
                            and visual_state.get('sourceRevision') == reviewed_revision
                            else None
                        )
                        execution_event(
                            {
                                'phase': 'export',
                                'status': 'started',
                                'message': '正在生成 PPTX/PDF 文件',
                            }
                        )
                    else:
                        if not (
                            visual_state.get('status') == 'passed'
                            and visual_state.get('sourceRevision') == reviewed_revision
                        ):
                            execution_event(
                                {
                                    'phase': 'validate',
                                    'status': 'started',
                                    'message': '正在渲染最终稿并进行 AI 视觉审稿',
                                }
                            )
                            async with llm_limit:
                                reviewed = await visual_pipeline.export_reviewed(
                                    store,
                                    project_id,
                                    folder,
                                    request,
                                    validation_progress,
                                    checkpoint=checkpoint,
                                    save_checkpoint=checkpoint_save,
                                    on_event=execution_event,
                                    assert_current=lambda: assert_current(
                                        identifier,
                                        project_id,
                                    ),
                                    publish=True,
                                )
                            reviewed_revision = reviewed['revision']
                            checkpoint = (
                                read_json(checkpoint_path)
                                if checkpoint_path.is_file()
                                else (checkpoint or {})
                            )
                            checkpoint = {
                                **checkpoint,
                                'stage': 'export',
                                '_committedRevision': reviewed_revision,
                            }
                            checkpoint_save(checkpoint)
                            visual_summary = reviewed['visualReview']
                            metadata = reviewed
                            execution_event(
                                {
                                    'phase': 'export',
                                    'status': 'completed',
                                    'message': (
                                        f'已生成 {metadata["pageCount"]} 页 '
                                        'PPTX/PDF 文件'
                                    ),
                                }
                            )
                            break
                        visual_summary = visual_state
                        execution_event(
                            {
                                'phase': 'export',
                                'status': 'started',
                                'message': '视觉审稿通过，正在生成 PPTX/PDF 文件',
                            }
                        )
                    metadata = await pptd_exports.export_document(
                        store,
                        project_id,
                        folder,
                        export_progress,
                    )
                    if visual_summary:
                        metadata['visualReview'] = visual_summary
                    execution_event(
                        {
                            'phase': 'export',
                            'status': 'completed',
                            'message': f'已生成 {metadata["pageCount"]} 页 PPTX/PDF 文件',
                        }
                    )
                    break
                except ValueError as exc:
                    issues = getattr(exc, 'issues', None)
                    if (
                        issues
                        and autofit_attempts < policy.layout_autofit_budget
                        and getattr(exc, 'generation_context', {}).get('phase')
                        != 'visual'
                    ):
                        fitted = autofit_document_for_export(
                            store,
                            project_id,
                            exc,
                            current_revision,
                        )
                    else:
                        fitted = None
                    if fitted:
                        autofit_attempts += 1
                        current_revision = fitted['revision']
                        if checkpoint:
                            checkpoint = {
                                **checkpoint,
                                '_committedRevision': current_revision,
                            }
                            if request.kind != 'render':
                                checkpoint['_visualReview'] = None
                            checkpoint_save(checkpoint)
                        progress(
                            60,
                            f'浏览器实测发现 {len(issues)} 项文字溢出，正在按实际尺寸自动适配',
                        )
                        execution_event(
                            {
                                'phase': 'validate',
                                'status': 'retrying',
                                'attempt': autofit_attempts,
                                'message': (
                                    f'浏览器实测发现 {len(issues)} 项文字溢出，'
                                    '正在自动缩放对应文本框并重新检查。'
                                ),
                            }
                        )
                        continue
                    if (
                        not issues
                        or request.kind == 'render'
                        or getattr(exc, 'generation_context', {}).get('phase')
                        == 'visual'
                    ):
                        raise
                    render_attempts += 1
                    mapped = [
                        {
                            **item,
                            'location': item.get('location')
                            or item.get('page')
                            or 'document',
                        }
                        for item in issues
                    ]
                    fingerprint = json.dumps(mapped, sort_keys=True)
                    checkpoint = {
                        **(checkpoint or {}),
                        'externalIssues': mapped,
                        '_sourceExisting': source_existing,
                        '_visualReview': None,
                    }
                    checkpoint_save(checkpoint)
                    if (
                        fingerprint in seen_issues
                        or render_attempts >= policy.render_repair_budget
                    ):
                        raise ValueError(
                            '渲染检查仍未通过，已保留文档和检查点。' + str(exc)
                        ) from exc
                    seen_issues.add(fingerprint)
                    progress(
                        60,
                        f'浏览器/导出检查发现 {len(issues)} 项问题，'
                        f'正在第 {render_attempts} 轮修复',
                    )
                    execution_event(
                        {
                            'phase': 'validate',
                            'status': 'retrying',
                            'attempt': render_attempts,
                            'message': (
                                f'排版与导出检查发现 {len(issues)} 项问题，'
                                '正在修复对应页面'
                            ),
                        }
                    )
            project = assert_current(identifier, project_id)
            if metadata['revision'] != store.read(project_id)['revision']:
                raise RevisionConflict(
                    '导出期间文档已变化，旧文件未发布，请重新导出。'
                )
            base = f'{prefix}/artifacts/{identifier}'
            generation_meta = result.get('generationMeta', {})
            visual_review = metadata.get('visualReview')
            visual_warnings = [
                (
                    f"{item.get('pagePath', 'document')}: "
                    f"{item.get('message', '视觉样式提醒')}"
                )
                for item in (visual_review or {}).get('warnings', [])
                if isinstance(item, dict)
            ]
            warnings = [
                *metadata.get('warnings', []),
                *generation_meta.get('research', {}).get('warnings', []),
                *generation_meta.get('assetWarnings', []),
                *visual_warnings,
            ]
            artifact = ArtifactRecord(
                id=identifier,
                revision=metadata['revision'],
                pptdRevision=metadata['revision'],
                pptxUrl=base + '/' + metadata['pptx'],
                pdfUrl=base + '/' + metadata['pdf'],
                sourceUrl=base + '/' + metadata['source'],
                reportUrl=base + '/' + metadata['report'],
                previewUrls=[
                    base + '/' + name for name in metadata['previews']
                ],
                warnings=list(dict.fromkeys(warnings)),
            ).model_dump()
            meta = result.get('generationMeta', {})
            meta['validation'] = {
                **meta.get('validation', {}),
                'status': 'render-passed',
                'renderRequired': False,
                'revision': metadata['revision'],
                'reportUrl': artifact['reportUrl'],
                **(
                    {'visualReview': visual_review}
                    if visual_review
                    else {}
                ),
            }
            project.update(
                artifact=artifact,
                generationMeta=meta,
                phase='ready',
                activeJobId=None,
                contentGenerated=True,
                updatedAt=time.time(),
            )
            visual_warning_count = (
                int(visual_review.get('warningCount') or 0)
                if visual_review
                else 0
            )
            reply = (
                f"演示文件已生成，共 {metadata['pageCount']} 页；"
                + (
                    f'视觉审稿完成并保留 {visual_warning_count} 项非阻断样式提醒，'
                    if visual_warning_count
                    else '整稿及全部单页已通过 AI 视觉审查，'
                )
                + '预览、可编辑 PPTX、PDF 和 PPTD 源文件已就绪。'
                if visual_review
                else (
                    f"演示文件已生成，共 {metadata['pageCount']} 页；"
                    '本次为导出操作，未在导出阶段执行 AI 视觉审稿，'
                    '预览、PPTX、PDF 和 PPTD 源文件已就绪。'
                )
            )
            project['messages'] = [
                *project.get('messages', []),
                {'id': uuid4().hex, 'role': 'assistant', 'text': reply},
            ][-1000:]
            save_json(folder / 'metadata.json', artifact)
            save_json(project_path(project_id), project)
            execution = record_event(
                read_job(identifier).get('execution'),
                {
                    'phase': 'done',
                    'status': 'completed',
                    'message': f'共 {metadata["pageCount"]} 页，演示文件已就绪',
                },
            )
            update_job(
                identifier,
                status='completed',
                stage=100,
                message='生成完成',
                recoverable=False,
                execution=execution,
                result={
                    'artifact': artifact,
                    'reply': reply,
                    'project': hydrate_project(project),
                },
            )
    except asyncio.CancelledError:
        current = read_job(identifier)
        if current['status'] != 'cancelled':
            update_job(
                identifier,
                status='interrupted',
                message='任务中断，检查点已保留。',
                recoverable=True,
                execution=terminal_execution(
                    current.get('execution'),
                    'interrupted',
                    '任务中断，检查点已保留',
                ),
            )
        finish_job(identifier)
        raise
    except Exception as exc:
        if isinstance(exc, pptd_generation.GenerationIncomplete):
            checkpoint_save(exc.checkpoint)
        current = read_job(identifier)
        message, diagnostic, context = diagnose_error(
            exc,
            current.get('execution', {}).get('phase', 'context'),
            resumable=checkpoint_path.is_file(),
        )
        if diagnostic.get('issues') and checkpoint_path.is_file():
            plan = read_json(checkpoint_path).get('plan', {})
            paths = plan.get('document', {}).get('pages', [])
            for issue in diagnostic['issues']:
                path = issue.get('page') or next(
                    (
                        item
                        for item in paths
                        if issue.get('location', '').startswith(item + '#')
                        or issue.get('location') == item
                    ),
                    None,
                )
                if path in paths:
                    index = paths.index(path)
                    issue['pageIndex'] = index
                    issue['pageTitle'] = (
                        plan.get('pages', [])[index].get('title', '')
                        if index < len(plan.get('pages', []))
                        else ''
                    )
        logging.getLogger(__name__).error(
            'PPTD job %s failed: %s (%s)',
            identifier,
            diagnostic['code'],
            ', '.join(diagnostic['exceptionTypes']),
        )
        update_job(
            identifier,
            status='failed',
            message=message,
            error=message,
            diagnostic=diagnostic,
            recoverable=True,
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
