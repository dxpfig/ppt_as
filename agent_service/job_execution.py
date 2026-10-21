"""Public execution milestones; never store model reasoning or raw responses."""
from copy import deepcopy
import os
import time

from .orchestrator import STAGES

PHASES = list(STAGES)


def initial_execution():
    return {'phase': 'context', 'nodes': [{'id': key, 'label': label, 'status': 'pending'} for key, label in PHASES], 'pages': [], 'visualPages': [], 'events': []}


def record_event(execution, event, now=None):
    state = deepcopy(execution or initial_execution())
    now = time.time() if now is None else now
    phase = event.get('phase')
    # Older durable jobs can resume into the newly introduced review phase.
    if phase == 'visual' and not any(n['id'] == phase for n in state['nodes']):
        index = next((i for i, n in enumerate(state['nodes']) if n['id'] == 'done'), len(state['nodes']))
        state['nodes'].insert(index, {'id': 'visual', 'label': 'AI 视觉审稿', 'status': 'pending'})
    node = next((n for n in state['nodes'] if n['id'] == phase), None)
    if node is None:
        return state
    if state.get('phase') == 'export' and phase == 'validate' and event.get('status') == 'retrying':
        exporting = next(n for n in state['nodes'] if n['id'] == 'export')
        exporting.update(status='pending', summary='等待检查修复后重新导出')
    # Explicit allowlist: traces and reasoning must never enter the product feed.
    item = {k: event[k] for k in ('phase', 'status', 'message', 'pagePath', 'pageIndex', 'title', 'attempt', 'scope', 'elementId', 'issueCount', 'covered', 'total') if k in event}
    item['timestamp'] = now
    for key in ('message', 'title', 'pagePath', 'elementId'):
        if key in item:
            item[key] = redact(str(item[key]))[:1500]
    state['events'] = [*state['events'], item][-100:]
    state['phase'] = phase
    status = {'started': 'running', 'waiting': 'running', 'retrying': 'running', 'completed': 'completed', 'failed': 'failed', 'cancelled': 'cancelled'}.get(item.get('status'), 'running')
    if phase == 'visual':
        previous = state.get('visual', {})
        if item.get('status') == 'started' and item.get('attempt', 0) > previous.get('attempt', 0):
            previous = {}
            state['visualPages'] = []
        state['visual'] = {**previous, **{k: item[k] for k in ('scope', 'attempt', 'covered', 'total', 'issueCount') if k in item}, 'status': status}
    node['summary'] = item.get('message', '')
    node.setdefault('startedAt', now)
    # A single completed parallel page does not complete the whole page stage.
    subtask = item.get('pagePath') or (phase == 'visual' and item.get('scope'))
    node['status'] = 'running' if subtask and status == 'completed' else status
    if phase == 'visual':
        state['visual']['status'] = node['status']
    if node['status'] in ('completed', 'failed', 'cancelled'):
        node['endedAt'] = now
        if isinstance(node.get('startedAt'), (int, float)):
            node['durationSeconds'] = round(max(0, now - node['startedAt']), 3)
    else:
        node.pop('endedAt', None)
        node.pop('durationSeconds', None)
    if item.get('pagePath'):
        collection = state.setdefault('visualPages', []) if phase == 'visual' else state['pages']
        page = next((p for p in collection if p['path'] == item['pagePath']), None)
        if page is None:
            title = next((p.get('title', '') for p in state['pages'] if p['path'] == item['pagePath']), '')
            page = {'path': item['pagePath'], 'index': item.get('pageIndex', len(collection)), 'title': item.get('title', title)}
            collection.append(page)
        page.update(status=status, updatedAt=now, message=item.get('message', ''))
        for key in ('title', 'attempt', 'issueCount', 'elementId'):
            if key in item:
                page[key] = item[key]
        if 'pageIndex' in item:
            page['index'] = item['pageIndex']
        collection.sort(key=lambda p: p['index'])
    return state


def checkpoint_execution(execution, checkpoint):
    """Populate only facts persisted by the generator, including resumed pages."""
    state = deepcopy(execution or initial_execution())
    nodes = {n['id']: n for n in state['nodes']}
    for phase, key in (('context', 'research'), ('plan', 'plan'), ('assets', 'assets')):
        if key in checkpoint:
            nodes[phase]['status'] = 'completed'
    plan = checkpoint.get('plan') or {}
    paths = plan.get('document', {}).get('pages', [])
    plans = plan.get('pages', [])
    completed = {p['path'] for p in checkpoint.get('pages', [])}
    previous = {p['path']: p for p in state['pages']}
    for index, path in enumerate(paths):
        page = previous.setdefault(path, {'path': path, 'index': index, 'title': plans[index].get('title', '') if index < len(plans) else '', 'status': 'pending'})
        if path in completed and not checkpoint.get('externalIssues'):
            page['status'] = 'completed'
    state['pages'] = sorted(previous.values(), key=lambda p: p['index'])
    if paths and len(completed) == len(paths) and not checkpoint.get('externalIssues') and not checkpoint.get('drafts'):
        nodes['pages']['status'] = 'completed'
    if checkpoint.get('stage') in ('validated', 'export') and not checkpoint.get('externalIssues'):
        nodes['validate']['status'] = 'completed'
    return state


def terminal_execution(execution, status, message, context=None):
    context = context or {}
    state = execution or initial_execution()
    phase = context.get('phase') or state['phase']
    state = record_event(state, {**context, 'phase': phase, 'status': 'failed' if status == 'failed' else 'cancelled', 'message': message})
    for page in [*state['pages'], *state.get('visualPages', [])]:
        if page['status'] == 'running':
            page.update(status='cancelled', message='任务结束，已保留检查点')
    for node in state['nodes']:
        if node['status'] == 'running':
            node['status'] = 'cancelled'
    return state


def stage_metrics(execution):
    """Return only measured stage durations; never invent missing timings."""
    metrics = {}
    for node in (execution or {}).get('nodes', []):
        if not isinstance(node, dict):
            continue
        start, end = node.get('startedAt'), node.get('endedAt')
        if type(start) in (int, float) and type(end) in (int, float) and end >= start:
            metrics[node.get('id', '')] = {
                'status': node.get('status', ''),
                'seconds': round(end - start, 3),
            }
    return metrics


def redact(text):
    for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'OPENAI_API_KEY', 'PPT_VISION_API_KEY', 'WEKNORA_API_KEY', 'TAVILY_API_KEY', 'SERPER_API_KEY', 'PPT_WEB_MCP_HEADERS'):
        value = os.getenv(key)
        if value:
            text = text.replace(value, '[redacted]')
    return text


def diagnose_error(exc, phase='context', *, resumable=True):
    """Explain a failure without promising a resume checkpoint that does not exist.

    ``resumable`` is False for routes that keep no per-page checkpoint (the
    agentic Kimi expert writes whole files), so the message must not tell the
    user to continue from one.
    """
    chain, seen = [], set()
    current = exc
    context = {}
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current)); chain.append(current)
        context = context or getattr(current, 'generation_context', {})
        current = current.__cause__ or current.__context__ or getattr(current, 'original_error', None)
    phase = context.get('phase', phase)
    types = [type(item).__name__ for item in chain]
    status_codes = {getattr(item, 'status_code', None) for item in chain}
    failure_kinds = {getattr(item, 'failure_kind', None) for item in chain}
    issues = next((getattr(item, 'issues') for item in chain if isinstance(getattr(item, 'issues', None), list) and item.issues), [])
    visual_errors = {
        'vision_unavailable': '视觉模型不可用或无法读取图片，AI 视觉审稿未完成',
        'visual_review_invalid': '视觉模型未返回完整有效的审稿结果，未通过视觉检查',
        'visual_review_exhausted': 'AI 自动修复及复审已达到本轮上限，仍有视觉问题，未交付未经审稿的文件',
        'visual_content_changed': 'AI 修复改变了受保护的文字或数据，已拒绝此修复结果',
        'visual_repair_failed': 'AI 视觉修复未通过安全校验，修复结果未应用；原稿和具体问题已保留',
    }
    visual_code = next((getattr(item, 'code', None) for item in chain if getattr(item, 'code', None) in visual_errors), None)
    if not visual_code and any(getattr(item, 'failure_kind', None) == 'visual-repair' for item in chain):
        visual_code = 'visual_repair_failed'
    budget_error = next((item for item in chain if getattr(item, 'failure_kind', None) == 'context-limit' and isinstance(getattr(item, 'budget', None), dict)), None)
    if budget_error is not None:
        code, message = 'model_context_limit', str(budget_error)
    elif 'context-count' in failure_kinds:
        code, message = 'model_context_unavailable', '模型分词服务不可用，无法确认上下文预算，已停止提交请求'
    elif visual_code:
        code, message = visual_code, visual_errors[visual_code]
    elif any(isinstance(item, TimeoutError) or 'Timeout' in type(item).__name__ for item in chain):
        code, message = ('render_timeout', '渲染导出超时') if phase == 'export' else ('model_timeout', '模型请求超时，未能在时限内返回完整结果')
    elif 401 in status_codes or 403 in status_codes or any('Authentication' in name or 'PermissionDenied' in name for name in types):
        code, message = 'model_auth', '模型服务拒绝了认证，请检查 API Key 与模型访问权限'
    elif 429 in status_codes or 'RateLimitError' in types:
        code, message = 'model_rate_limit', '模型服务请求受限，请稍后从检查点继续'
    elif any('Connection' in name or 'ConnectError' in name for name in types):
        code, message = 'model_connection', '无法连接模型服务，请检查模型服务地址和网络'
    elif any(code is not None and code >= 500 for code in status_codes):
        code, message = 'model_unavailable', '模型服务暂时不可用，请稍后从检查点继续'
    elif 'format' in failure_kinds:
        code, message = 'model_output_invalid', '模型返回内容未通过 JSON 或数据结构检查，本次结果未应用'
    elif 'output-limit' in failure_kinds:
        code, message = 'model_output_limit', '模型输出达到长度上限，未返回完整可用结果，本次结果未应用'
    elif 'provider' in failure_kinds:
        code, message = 'model_provider_error', '模型服务调用失败，本次结果未应用'
    elif any(isinstance(issue, dict) and issue.get('code') == 'PlannedContentMissing' for issue in issues):
        code, message = 'content_recovery_failed', '原规划正文恢复未通过完整性检查，现有页面已保留'
    elif issues:
        code, message = ('render_validation' if phase == 'export' else 'generation_validation'), f'自动修复后仍有 {len(issues)} 项排版或格式问题，请展开具体问题查看位置'
    elif isinstance(exc, ValueError):
        code, message = ('render_validation' if phase == 'export' else 'generation_validation'), redact(str(exc))[:1000]
    else:
        code, message = ('render_error', '渲染导出失败') if phase == 'export' else ('generation_error', '生成过程发生异常')
    page = f"第 {context['pageIndex'] + 1} 页：" if isinstance(context.get('pageIndex'), int) and not message.startswith('第 ') else ''
    if resumable:
        message = page + message + '。已保留检查点，可继续恢复；无需重新生成已完成页面。'
    else:
        message = page + message + '。可重新尝试该任务。'
    diagnostic = {'code': code, 'phase': phase, 'exceptionTypes': types, **{k: context[k] for k in ('pageIndex', 'pagePath', 'attempt') if k in context}}
    rejections = next((getattr(item, 'rejections') for item in chain if getattr(item, 'rejections', None)), [])
    if rejections:
        issues = list(issues)
        for rejection in rejections[-2:]:
            # Expose the validator's reason and location, never rejected values.
            operations = rejection.get('operations') or rejection.get('answer', {}).get('operations') or []
            reason = rejection.get('reason', '修复候选稿未通过校验')
            operation = next((op for op in operations if op.get('path') and op['path'] in reason), {})
            issues.append({'code': 'RepairCandidateRejected',
                **{key: operation[key] for key in ('pagePath', 'elementId') if operation.get(key)},
                'location': operation.get('path', 'visual-repair'),
                'message': f"第 {rejection.get('attempt', '?')} 次修复被拒绝：{reason}"})
    if budget_error is not None:
        diagnostic['budget'] = dict(budget_error.budget)
    if issues:
        diagnostic['issues'] = []
        for issue in issues[:200]:
            if not isinstance(issue, dict):
                continue
            public = {k: value if k == 'pageIndex' and isinstance(value, int) else redact(str(value))[:1200] for k, value in issue.items() if k in ('page', 'pageIndex', 'pageTitle', 'location', 'elementId', 'code', 'message')}
            if not public.get('page') and issue.get('pagePath'):
                public['page'] = redact(str(issue['pagePath']))[:1200]
            if not public.get('elementId') and isinstance(issue.get('elementIds'), list):
                public['elementId'] = redact(', '.join(str(value) for value in issue['elementIds']))[:1200]
            public.setdefault('location', public.get('page', 'document'))
            diagnostic['issues'].append(public)
    return redact(message)[:1500], diagnostic, context
