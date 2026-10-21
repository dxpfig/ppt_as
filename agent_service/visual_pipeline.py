"""Automatic visual gate. Candidate revisions stay private until the AI passes them."""
from __future__ import annotations

from copy import copy, deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from .pptd_store import DocumentStore, RevisionConflict, _serialize, safe_path
from .kimi_rules import SCENARIOS
from .visual_repair import semantic_payload
from .visual_completion import apply_completion, complete_content, needs_completion
from . import repair_coordinator
from .review_service import review_deck
from . import export_service as pptd_exports
from .page_work_queue import PageWorkQueue, PageWorkStatus
from .execution_policy import quick_policy
from .layout_autofit import autofit_state


async def repair_deck(*args, **kwargs):
    return await repair_coordinator.repair_deck(*args, **kwargs)


async def repair_deck_batched(request, state, issues, *, on_event=None, rendered_pages=None,
                              layout_measurements=None, validate_candidate=None, on_audit=None):
    return await repair_coordinator.repair_deck_batched(
        request,
        state,
        issues,
        on_event=on_event,
        rendered_pages=rendered_pages,
        layout_measurements=layout_measurements,
        validate_candidate=validate_candidate,
        on_audit=on_audit,
        repair_fn=repair_deck,
    )


class VisualReviewExhausted(ValueError):
    code = 'visual_review_exhausted'

    def __init__(self, issues):
        super().__init__('AI 视觉复审仍未通过，已保留候选稿与检查点，未发布未通过的文件。')
        self.issues = issues
        self.generation_context = {'phase': 'visual'}


class VisualIntegrityError(ValueError):
    code = 'visual_review_invalid'

    def __init__(self):
        super().__init__('视觉审稿候选稿的内容或语义发生变化，已保留原稿，未发布文件。')
        self.generation_context = {'phase': 'visual'}


def _source_sha256(state):
    source = {key: state[key] for key in ('document', 'pages')}
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _semantic_sha256(state, contract_version=None):
    return _source_sha256(semantic_payload(state, contract_version=contract_version))


def _assert_semantics(state, expected_hash):
    try:
        matches = _semantic_sha256(state) == expected_hash
    except (KeyError, TypeError, ValueError, AttributeError):
        matches = False
    if not matches:
        raise VisualIntegrityError()


def reconcile_visual_checkpoint(checkpoint, current_state):
    """Recognize exactly our verified commit if its final checkpoint write failed.

    A pending receipt is saved before source publication. It cannot excuse a
    concurrent edit, changed candidate, or semantic loss: both the exact bytes'
    canonical source hash and immutable original semantic hash must agree.
    Mismatches remain untouched for the caller's normal stale-revision guard.
    """
    cp = deepcopy(checkpoint or {})
    visual = cp.get('_visualReview')
    if not isinstance(visual, dict):
        return cp
    pending = visual.get('pendingVerified')
    if not isinstance(pending, dict):
        return cp
    expected, source = pending.get('expectedRevision'), visual.get('sourceRevision')
    if (type(expected) is not int or type(source) is not int or
            expected not in (source, source + 1) or current_state.get('revision') != expected):
        return cp
    try:
        proof_semantics = (pending.get('originalSemanticSha256') if visual.get('completionReceipts')
                           else pending.get('semanticSha256', visual.get('sourceSemanticSha256')))
        if (pending.get('sourceSha256') != _source_sha256(current_state) or
                pending.get('sourceSha256') != _source_sha256(visual.get('candidate') or {}) or
                proof_semantics != visual.get('sourceSemanticSha256') or
                pending.get('semanticSha256', visual.get('sourceSemanticSha256')) != _semantic_sha256(current_state, visual.get('semanticContractVersion', 1))):
            return cp
    except (KeyError, TypeError, ValueError, AttributeError):
        return cp
    cp['_committedRevision'] = expected
    visual['sourceRevision'] = expected
    visual['sourceSemanticSha256'] = _semantic_sha256(current_state)
    visual['semanticContractVersion'] = 3
    visual['completionReceipts'] = []  # The verified additions now belong to the authoritative source.
    return cp


class CandidateSource:
    """Exporter-compatible immutable view, retaining every original asset."""
    def __init__(self, source, state):
        self.source, self.state = source, deepcopy(state)

    def read(self, _project_id):
        return deepcopy(self.state)

    def snapshot(self, _project_id, destination):
        shutil.copytree(self.source, destination)
        for path, value in [('document.pptd', self.state['document']), *[(p['path'], p['data']) for p in self.state['pages']]]:
            target = safe_path(Path(destination), path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_serialize(value), encoding='utf-8')
        return self.read(_project_id)


_fingerprint = repair_coordinator.issue_fingerprint


async def export_reviewed(store, project_id, folder, request, progress=None, *,
                          checkpoint=None, save_checkpoint=None, on_event=None,
                          assert_current=None, publish=True):
    folder = Path(folder)
    cp = deepcopy(checkpoint or {})
    request = copy(request)
    project_meta = getattr(getattr(request, 'project', None), 'generationMeta', {}) or {}
    if request.scenario not in SCENARIOS:
        request.scenario = cp.get('selectedScenario') or cp.get('plan', {}).get('scenario') or project_meta.get('scenario', 'management-report')
    if request.scenario not in SCENARIOS:
        request.scenario = 'management-report'
    initial = store.read(project_id)
    cp = reconcile_visual_checkpoint(cp, initial)
    base_revision = initial['revision']
    prior = cp.get('_visualReview') or {}
    if prior and prior.get('sourceRevision') != base_revision:
        raise RevisionConflict('视觉审稿检查点的原始版本已变化，未覆盖当前文档。')
    visual = deepcopy(prior or {'sourceRevision': base_revision, 'repairs': 0, 'reports': [], 'fingerprints': []})
    # Lock original meaning before accepting a resumed candidate or invoking
    # any adapter. Older checkpoints gain this proof only after comparison with
    # the still-authoritative source at the same revision.
    original_semantics = _semantic_sha256(initial)
    if visual.get('sourceSemanticSha256', original_semantics) != original_semantics:
        version = visual.get('semanticContractVersion', 1)
        legacy_hash = _semantic_sha256(initial, version)
        if version not in (1, 2) or visual.get('sourceSemanticSha256') != legacy_hash:
            raise VisualIntegrityError()
    visual['sourceSemanticSha256'] = original_semantics
    visual['semanticContractVersion'] = 3
    completion_plan = deepcopy(cp.get('plan') or project_meta.get('plan') or {})
    def expected_semantics(receipts):
        expected = deepcopy(initial)
        try:
            for receipt in receipts:
                # Ordinary patches affect style only; replay just the additions
                # to reconstruct the exact allowed semantic extension.
                answer = {**receipt['answer'], 'operations': []}
                expected = apply_completion(expected, completion_plan, answer, receipt['issues'])
            return _semantic_sha256(expected)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise VisualIntegrityError() from exc
    accepted_semantics = expected_semantics(visual.get('completionReceipts', []))
    previous_publication = deepcopy(visual.get('pendingVerified') or {})
    state = {**initial, **deepcopy(visual.get('candidate') or {})}
    _assert_semantics(state, accepted_semantics)
    DocumentStore._validate(state['document'], state['pages'])
    changed = state['document'] != initial['document'] or state['pages'] != initial['pages']
    state['revision'] = base_revision + int(changed)
    page_queue = PageWorkQueue(state['document']['pages'], 1)
    for page_path in state['document']['pages']:
        page_queue.mark(page_path, PageWorkStatus.GENERATED)
    private = store.root / 'visual_candidates' / folder.name / uuid4().hex
    private.mkdir(parents=True)
    store.snapshot(project_id, private / 'source')
    # Two repair rounds are a hard cap; recovery never silently resets it.
    budget = 2

    phase = 'visual'

    def emit(status, message, **extra):
        if assert_current:
            assert_current()
        if on_event:
            on_event({'phase': phase, 'status': status, 'message': message, **extra})

    def review_event(event):
        return on_event(event) if on_event else None

    def persist():
        visual['candidate'] = {'document': deepcopy(state['document']), 'pages': deepcopy(state['pages'])}
        cp['_visualReview'] = deepcopy(visual)
        cp['_pageStates'] = page_queue.snapshot()
        cp['_committedRevision'] = base_revision
        cp['stage'] = 'export'
        if save_checkpoint:
            save_checkpoint(cp)

    def audit(record):
        from .job_execution import redact
        # Only structured candidate edits and validation evidence are retained;
        # no model reasoning, credentials or request messages enter this record.
        def scrub(value):
            if isinstance(value, str): return redact(value)
            if isinstance(value, list): return [scrub(item) for item in value]
            if isinstance(value, dict): return {key: scrub(item) for key, item in value.items()}
            return value
        safe_record = scrub(record)
        visual.setdefault('repairAudit', []).append(safe_record)
        target = private / 'repair-audit.json'
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(visual['repairAudit'], ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(target)
        persist()

    persist()
    verified_candidate = None
    autofit_attempts = 0
    while True:
        if assert_current:
            assert_current()
        if store.read(project_id)['revision'] != base_revision:
            raise RevisionConflict('视觉审稿期间文档已变化，未发布旧版本。')
        attempt = visual['repairs'] + 1
        candidate_folder = private / f'round-{attempt}-{uuid4().hex[:8]}'
        try:
            if verified_candidate and verified_candidate['hash'] == _source_sha256(state):
                candidate_folder, metadata = verified_candidate['folder'], verified_candidate['metadata']
                verified_candidate = None
            else:
                metadata = await pptd_exports.export_document(CandidateSource(private / 'source', state), project_id, candidate_folder, progress)
        except pptd_exports.ValidationError as error:
            # Recovered plan text is now authoritative. Its renderer findings
            # must use the same protected layout edits as export-only jobs;
            # sending it back to full-page generation can rewrite that text.
            if (not visual['repairs'] and request.kind != 'render' and not cp.get('_contentRecoveryPages')
                    and not pptd_exports.layout_only_issues(error.issues)):
                cp['_visualReview'] = None
                if save_checkpoint:
                    save_checkpoint(cp)
                raise
            issues = [{**issue, 'pagePath': issue.get('pagePath') or issue.get('page'),
                       'elementIds': issue.get('elementIds') or ([issue['elementId']] if issue.get('elementId') else [])}
                      for issue in error.issues]
            report = {'status': 'issues', 'issues': issues, 'source': 'renderer', 'coveredPages': []}
            rendered_pages = error.previews
            layout_measurements = error.layouts
            if (
                pptd_exports.layout_only_issues(issues)
                and autofit_attempts < quick_policy().layout_autofit_budget
            ):
                pages = deepcopy(state['pages'])
                changed = autofit_state(
                    {'pages': pages},
                    layout_measurements or {},
                    issues,
                )
                if any(changed.values()):
                    autofit_attempts += 1
                    state = {
                        **state,
                        'pages': pages,
                        'revision': base_revision + 1,
                    }
                    for issue in issues:
                        page_path = issue.get('pagePath')
                        if page_path in page_queue.states:
                            page_queue.mark(
                                page_path,
                                PageWorkStatus.REPAIRED,
                                '已完成确定性排版自适应',
                            )
                    persist()
                    emit(
                        'retrying',
                        '已对文本溢出执行确定性自适应，正在重新检查',
                        scope='repair',
                        attempt=autofit_attempts,
                        issueCount=len(issues),
                        total=len(state['pages']),
                    )
                    continue
        else:
            for page_path in state['document']['pages']:
                page_queue.mark(page_path, PageWorkStatus.RENDERED)
            rendered_pages = {p['path']: (candidate_folder / name).read_bytes()
                              for p, name in zip(state['pages'], metadata.get('previews', []))}
            exported_report = json.loads((candidate_folder / metadata['report']).read_text(encoding='utf-8'))
            layout_measurements = {p['source']: {'canvasSize': state['document']['size'], 'elements': p.get('textLayout', [])}
                                   for p in exported_report.get('pdf', {}).get('pages', [])}
            if on_event:
                on_event({'phase': 'export' if publish else 'validate', 'status': 'completed',
                          'message': '候选稿已通过渲染与导出检查，准备 AI 视觉审查'})
            emit('started', f'AI 正在第 {attempt} 轮整稿视觉审查', scope='deck', attempt=attempt,
                 covered=0, total=len(state['pages']))
            previous_review = next((r for r in reversed(visual['reports']) if r.get('source') != 'renderer'), None)
            report = await review_deck(deepcopy(state), candidate_folder, scenario=request.scenario,
                                       usage=request.usage, on_event=review_event, previous_review=previous_review,
                                       cache_dir=store.root / 'review-cache',
                                       concurrency=quick_policy().visual_review_concurrency)
            paths = state['document']['pages']
            if (report.get('status') not in ('passed', 'issues') or
                    report.get('coveredPages') != paths or not isinstance(report.get('issues'), list)):
                error = ValueError('AI 视觉审查未返回完整页面覆盖或有效报告，未发布文件。')
                error.code = 'visual_review_invalid'
                error.generation_context = {'phase': 'visual'}
                raise error
            issues = report['issues']
            if report['status'] != ('issues' if issues else 'passed'):
                raise ValueError('AI 视觉审查状态与问题列表不一致。')
        visual['reports'].append({**report, 'attempt': attempt})
        for page_path in report.get('coveredPages', []):
            page_queue.mark(page_path, PageWorkStatus.REVIEWED)
        visual['issues'] = issues
        persist()
        if not issues:
            for page_path in state['document']['pages']:
                page_queue.mark(page_path, PageWorkStatus.APPROVED)
            persist()
            break
        fingerprint = _fingerprint(issues)
        if repair_coordinator.stalled(visual['fingerprints'], issues):
            raise VisualReviewExhausted(issues)
        if visual['repairs'] >= budget:
            raise VisualReviewExhausted(issues)
        emit('retrying', f'AI 发现 {len(issues)} 项问题，正在自动修复并准备复审', scope='repair',
             attempt=attempt, issueCount=len(issues), total=len(state['pages']))
        # Charge the attempt before calling the model, including interrupted calls.
        visual['repairs'] += 1
        visual['fingerprints'].append(fingerprint)
        persist()
        async def validate_candidate(candidate):
            nonlocal verified_candidate
            if assert_current:
                assert_current()
            _assert_semantics(candidate, accepted_semantics)
            checked = {**state, **candidate, 'revision': base_revision + 1}
            output = private / f'checked-{uuid4().hex}'
            result = await pptd_exports.export_document(CandidateSource(private / 'source', checked), project_id, output, progress)
            verified_candidate = {'hash': _source_sha256(checked), 'folder': output, 'metadata': result}

        missing = [issue for issue in issues if needs_completion(issue)]
        if missing:
            # Complete source-bound content before style repair. The next loop
            # renders and reviews it with the same unchanged round budget.
            repaired, receipt = await complete_content(request, deepcopy(state), missing, completion_plan,
                on_event=review_event, on_audit=audit, rendered_pages=rendered_pages)
            receipts = [*visual.get('completionReceipts', []), receipt]
            next_semantics = expected_semantics(receipts)
            _assert_semantics(repaired, next_semantics)
            visual['completionReceipts'] = receipts
            accepted_semantics = next_semantics
        else:
            repaired = await repair_deck_batched(
                request, deepcopy(state), issues, on_event=review_event,
                rendered_pages=rendered_pages, layout_measurements=layout_measurements,
                validate_candidate=validate_candidate, on_audit=audit,
            )
        _assert_semantics(repaired, accepted_semantics)
        DocumentStore._validate(repaired['document'], repaired['pages'])
        if repaired['document'] == state['document'] and repaired['pages'] == state['pages']:
            raise VisualReviewExhausted(issues)
        state = {**state, 'document': repaired['document'], 'pages': repaired['pages'], 'revision': base_revision + 1}
        for page_path in {issue.get('pagePath') or issue.get('page') for issue in issues}:
            if page_path in page_queue.states:
                page_queue.mark(page_path, PageWorkStatus.REPAIRED)
        persist()

    if assert_current:
        assert_current()
    if store.read(project_id)['revision'] != base_revision:
        raise RevisionConflict('视觉审稿期间文档已变化，旧文件未发布。')
    _assert_semantics(state, accepted_semantics)
    summary = {'status': 'passed', 'rounds': len(visual['reports']),
               'reviewedPages': len(state['pages']), 'totalPages': len(state['pages']), 'issueCount': 0,
               'repairedIssueCount': sum(len(r.get('issues', [])) for r in visual['reports']),
               'summary': '整稿与全部单页已通过 AI 视觉复审', 'model': report.get('model', '')}
    for page_path in state['document']['pages']:
        page_queue.mark(page_path, PageWorkStatus.APPROVED)
    metadata['visualReview'] = summary
    report_path = candidate_folder / metadata['report']
    export_report = json.loads(report_path.read_text(encoding='utf-8'))
    export_report['visualReview'] = {**summary, 'reviews': visual['reports']}
    report_path.write_text(json.dumps(export_report, ensure_ascii=False, indent=2), encoding='utf-8')
    if not publish:
        if state['revision'] != base_revision:
            committed = store.commit(project_id, state['document'], state['pages'], 'AI 视觉审稿自动修复', base_revision)
            metadata['revision'] = committed['revision']
        visual.pop('pendingVerified', None)
        visual.update(status='passed', sourceRevision=metadata['revision'],
                      sourceSemanticSha256=accepted_semantics, completionReceipts=[])
        cp.update(stage='validated', _committedRevision=metadata['revision'], _visualReview=visual)
        if save_checkpoint:
            save_checkpoint(cp)
        emit('completed', summary['summary'], covered=len(state['pages']), total=len(state['pages']),
             issueCount=0, attempt=attempt)
        return {**metadata, 'visualReview': summary, 'revision': metadata['revision'],
                'pageCount': len(state['pages']), 'published': False}
    # Durable receipt precedes any publication. An interrupted final checkpoint
    # write can be reconciled only against this exact reviewed source and its
    # original semantic proof. Moving files first is safe: the download route
    # requires metadata.json, which app.run_job creates only after this returns.
    visual['pendingVerified'] = {
        'expectedRevision': state['revision'], 'sourceSha256': _source_sha256(state),
        'semanticSha256': accepted_semantics,
        'originalSemanticSha256': original_semantics,
        'artifactPath': str(folder.resolve()),
    }
    persist()
    folder.parent.mkdir(parents=True, exist_ok=True)
    if folder.exists():
        # Same-job direct recovery may encounter files moved before a failed
        # commit/checkpoint. Retain them privately, never overwrite a published
        # artifact or an unrelated output directory.
        if (previous_publication.get('artifactPath') != str(folder.resolve()) or
                (folder / 'metadata.json').exists()):
            raise ValueError('导出目标目录已存在，未覆盖已有文件。')
        os.replace(folder, private / 'previous-unpublished-artifact')
    os.replace(candidate_folder, folder)
    # No await between the final revision guard, commit and checkpoint update.
    if state['revision'] != base_revision:
        committed = store.commit(project_id, state['document'], state['pages'], 'AI 视觉审稿自动修复', base_revision)
        metadata['revision'] = committed['revision']
    visual.update(status='passed', sourceRevision=metadata['revision'],
                  sourceSemanticSha256=accepted_semantics, completionReceipts=[])
    cp.update(stage='export', _committedRevision=metadata['revision'], _visualReview=visual)
    if save_checkpoint:
        save_checkpoint(cp)
    emit('completed', summary['summary'], covered=len(state['pages']), total=len(state['pages']),
         issueCount=0, attempt=attempt)
    return metadata
