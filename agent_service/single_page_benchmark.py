"""Repeatable benchmark runner for fixed single-page user-template cases."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import socket
import statistics
import subprocess
import sys
import time
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
PREFIX = '/api/studio'
TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}
ISSUE_GROUP = {
    'text-overlap': 'overlap',
    'text-clipped': 'clipped',
    'text-overflow': 'overflow',
    'text-contrast': 'contrast',
}
TRACE_STAGES = {
    'templateLoad': 'templateLoad',
    'planning': 'planning',
    'pageModel': 'pageModel',
    'compile': 'compile',
    'structureValidation': 'structureValidation',
    'hostTransform': 'hostTransform',
    'browserRender': 'browserRender',
    'browserLayout': 'browserLayout',
    'visionReview': 'visionReview',
    'repairModel': 'repairModel',
}
COMMON_BUDGETS = {
    'cachedTemplateLoad': ('templateLoad',),
    'planning': ('planning',),
    'pageModel': ('pageModel',),
    'compileAndStructureValidation': ('compile', 'structureValidation'),
    'hostTemplateTransform': ('hostTransform',),
    'browserRenderAndLayout': ('browserRender', 'browserLayout'),
    'singleVisionReview': ('visionReview',),
    'diagnosticRepairRound': ('repairModel',),
}
OPENER = build_opener(ProxyHandler({}))


class BenchmarkError(RuntimeError):
    pass


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise BenchmarkError(f'无法读取 JSON：{path}') from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f'JSON 顶层必须是对象：{path}')
    return value


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def load_plan(cases_path: Path, inputs_path: Path) -> tuple[dict, dict[str, dict]]:
    cases = _read_json(cases_path)
    inputs = _read_json(inputs_path)
    if cases.get('version') != 1 or cases.get('status') != 'mandatory':
        raise BenchmarkError('用例清单版本或强制状态不符合预期。')
    case_items = cases.get('cases')
    input_items = inputs.get('cases')
    if not isinstance(case_items, list) or not isinstance(input_items, dict):
        raise BenchmarkError('用例清单或输入夹具格式不正确。')
    ids = [item.get('id') for item in case_items if isinstance(item, dict)]
    if len(ids) != len(set(ids)):
        raise BenchmarkError('基准用例 ID 必须唯一。')
    for item in case_items:
        if not isinstance(item, dict):
            raise BenchmarkError('用例清单中的每个用例都必须是对象。')
        if (
            'includeInFullSuite' in item
            and not isinstance(item['includeInFullSuite'], bool)
        ):
            raise BenchmarkError('includeInFullSuite 必须是布尔值。')
    mandatory_ids = [
        item['id']
        for item in case_items
        if item.get('includeInFullSuite', True)
    ]
    if len(mandatory_ids) != 10:
        raise BenchmarkError('固定基准必须恰好包含 10 个全量用例。')
    if set(ids) != set(input_items):
        raise BenchmarkError('用例 ID 与输入夹具不一致。')
    for identifier, item in input_items.items():
        if not isinstance(item, dict):
            raise BenchmarkError(f'{identifier} 输入夹具不是对象。')
        for key in ('projectTitle', 'prompt', 'requiredText', 'forbiddenText', 'singleTitle'):
            if key not in item:
                raise BenchmarkError(f'{identifier} 缺少输入字段 {key}。')
    return cases, input_items


def selected_cases(cases: dict, requested: list[str] | None) -> list[dict]:
    available = {item['id']: item for item in cases['cases']}
    if not requested:
        return [
            item
            for item in cases['cases']
            if item.get('includeInFullSuite', True)
        ]
    identifiers = []
    for value in requested:
        identifiers.extend(part.strip() for part in value.split(',') if part.strip())
    unknown = sorted(set(identifiers) - set(available))
    if unknown:
        raise BenchmarkError('未知用例：' + ', '.join(unknown))
    return [available[identifier] for identifier in dict.fromkeys(identifiers)]


def _http_json(
    method: str,
    url: str,
    body: dict | None = None,
    timeout: float = 30,
) -> dict:
    data = (
        json.dumps(body, ensure_ascii=False).encode('utf-8')
        if body is not None
        else None
    )
    request = Request(
        url,
        data=data,
        method=method,
        headers={'Content-Type': 'application/json'} if data else {},
    )
    try:
        with OPENER.open(request, timeout=timeout) as response:
            value = json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')[:2000]
        raise BenchmarkError(f'{method} {url} 返回 {exc.code}：{detail}') from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise BenchmarkError(f'{method} {url} 失败：{exc}') from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f'{method} {url} 未返回 JSON 对象。')
    return value


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def _wait_health(base_url: str, process: subprocess.Popen, timeout: float = 90) -> dict:
    deadline = time.monotonic() + timeout
    last_error = ''
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BenchmarkError(f'基准服务提前退出，代码 {process.returncode}。')
        try:
            health = _http_json('GET', base_url + PREFIX + '/health', timeout=3)
            if health.get('status') == 'ok':
                return health
        except BenchmarkError as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise BenchmarkError('基准服务健康检查超时。' + (f' {last_error}' if last_error else ''))


class BenchmarkService:
    def __init__(self, session_root: Path, template: dict):
        self.session_root = session_root
        self.template = template
        self.data_root = session_root / 'studio'
        self.log_path = session_root / 'agent.log'
        self.process: subprocess.Popen | None = None
        self.base_url = ''
        self.health: dict = {}
        self.prewarm: dict = {}

    def _prepare_data_root(self) -> None:
        source_template = Path(self.template['path'])
        destination = (
            self.data_root
            / 'templates'
            / self.template['id']
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_template.parent, destination)

    def start(self, cases: dict) -> None:
        python = ROOT / '.venv' / 'bin' / 'python'
        if not python.is_file():
            raise BenchmarkError('缺少 .venv/bin/python。')
        if not (ROOT / '.env').is_file():
            raise BenchmarkError('缺少 .env。')
        self._prepare_data_root()
        self._prewarm_template()
        port = _free_port()
        self.base_url = f'http://127.0.0.1:{port}'
        environment = dict(os.environ)
        environment.update(
            {
                'STUDIO_DATA_DIR': str(self.data_root),
                'PPT_RENDER_CACHE': '0',
                'PPT_REVIEW_CACHE': '0',
                'PPT_BENCHMARK_TRACE': '1',
                'PPT_KIMI_VISUAL_REVIEW': '1',
                'PPT_VISION_MAX_TOKENS': '4096',
                'AGENTSCOPE_STUDIO_URL': '',
                'PYTHONUNBUFFERED': '1',
            }
        )
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open('ab')
        try:
            self.process = subprocess.Popen(
                [
                    str(python),
                    '-m',
                    'uvicorn',
                    'agent_service.app:app',
                    '--host',
                    '127.0.0.1',
                    '--port',
                    str(port),
                ],
                cwd=ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        finally:
            log.close()
        self.health = _wait_health(self.base_url, self.process)
        self._validate_health(cases)
        _write_json(self.session_root / 'environment.json', self.environment())

    def _prewarm_template(self) -> None:
        """Prepare reusable template conversion outside page-generation timing."""
        started = time.monotonic()
        template_path = (
            self.data_root
            / 'templates'
            / self.template['id']
            / 'template.pptx'
        )
        process = subprocess.run(
            [
                str(ROOT / '.venv' / 'bin' / 'python'),
                '-c',
                (
                    'from pathlib import Path; '
                    'from agent_service.template_cache import prepare; '
                    f'prepare(Path({str(self.data_root)!r}), '
                    f'Path({str(template_path)!r}), screenshots=False)'
                ),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        duration_ms = round((time.monotonic() - started) * 1000, 3)
        if process.returncode:
            detail = (process.stderr or process.stdout).strip()[-3000:]
            raise BenchmarkError(f'模板缓存预热失败：{detail}')
        self.prewarm = {
            'measuredSeparately': True,
            'durationMs': duration_ms,
        }

    def _validate_health(self, cases: dict) -> None:
        execution = cases['execution']
        if self.health.get('model') != execution['generationModel']:
            raise BenchmarkError(
                f"生成模型不匹配：需要 {execution['generationModel']}，"
                f"实际 {self.health.get('model')}。"
            )
        providers = {
            item.get('kind'): item
            for item in self.health.get('providers') or []
            if isinstance(item, dict)
        }
        vision = providers.get('vision') or {}
        if vision.get('model') != execution['visionModel']:
            raise BenchmarkError(
                f"视觉模型不匹配：需要 {execution['visionModel']}，"
                f"实际 {vision.get('model')}。"
            )
        if not self.health.get('rendererAvailable'):
            raise BenchmarkError('浏览器渲染器不可用。')

    def environment(self) -> dict:
        return {
            'baseUrl': self.base_url,
            'dataRoot': str(self.data_root),
            'pid': self.process.pid if self.process else None,
            'cache': {'render': False, 'review': False},
            'templatePrewarm': self.prewarm,
            'telemetry': {'enabled': False},
            'visionMaxTokens': 4096,
            'health': self.health,
            'template': self.template,
        }

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, 15)
        except ProcessLookupError:
            return
        try:
            self.process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.process.pid, 9)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=5)

    def __enter__(self) -> 'BenchmarkService':
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()


def _project(case: dict, input_case: dict, template_id: str) -> dict:
    return {
        'id': (
            f"bench-{case['id'].lower().replace('-', '')}-"
            f"{uuid4().hex[:12]}"
        ),
        'title': input_case['projectTitle'],
        'slides': [],
        'messages': [],
        'theme': 'forest',
        'phase': 'outline',
        'versions': [],
        'pageCount': 1,
        'audience': '管理层',
        'references': '',
        'knowledgeBaseIds': [],
        'imageSource': 'none',
        'activeJobId': None,
        'templateId': template_id,
        'artifact': None,
        'workflow': 'pptd-v2',
    }


def _request(case: dict, input_case: dict, template_id: str) -> tuple[dict, dict]:
    project = _project(case, input_case, template_id)
    return {
        'kind': 'kimi-slides',
        'project': project,
        'prompt': input_case['prompt'],
        'pageCount': 1,
        'audience': '管理层',
        'references': '',
        'templateId': template_id,
        'workflow': 'new',
        'designMode': 'template',
        'usage': 'presentation',
        'allowResearch': False,
        'speakerNotes': False,
        'animationRequested': False,
        'scenario': 'management-report',
        'attachments': [],
        'expectedRevision': 0,
        'outputMode': 'adaptive',
    }, project


def _wait_for_job(
    service: BenchmarkService,
    identifier: str,
    timeout_seconds: float,
    poll_path: Path,
) -> tuple[dict, list[dict], bool]:
    snapshots = []
    started = time.monotonic()
    timed_out = False
    while True:
        job = _http_json(
            'GET',
            f'{service.base_url}{PREFIX}/jobs/{identifier}',
        )
        execution = job.get('execution') or {}
        snapshots.append(
            {
                'elapsedMs': round((time.monotonic() - started) * 1000, 3),
                'status': job.get('status'),
                'stage': job.get('stage'),
                'message': job.get('message'),
                'phase': execution.get('phase'),
            }
        )
        if job.get('status') in TERMINAL:
            break
        if time.monotonic() - started >= timeout_seconds:
            timed_out = True
            try:
                _http_json(
                    'POST',
                    f'{service.base_url}{PREFIX}/jobs/{identifier}/cancel',
                )
            except BenchmarkError:
                pass
            cancel_deadline = time.monotonic() + 15
            while time.monotonic() < cancel_deadline:
                time.sleep(0.5)
                job = _http_json(
                    'GET',
                    f'{service.base_url}{PREFIX}/jobs/{identifier}',
                )
                if job.get('status') in TERMINAL:
                    break
            break
        time.sleep(0.75)
    poll_path.write_text(
        ''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in snapshots),
        encoding='utf-8',
    )
    return job, snapshots, timed_out


def _optional_json(
    service: BenchmarkService,
    url: str,
    path: Path,
) -> dict | None:
    try:
        value = _http_json('GET', url)
    except BenchmarkError:
        return None
    _write_json(path, value)
    return value


def _copy_workspace(
    workspace: Path,
    run_dir: Path,
    identifier: str,
    repeat: int,
) -> dict:
    result = {
        'workspace': str(workspace),
        'pages': [],
        'previews': [],
        'manifest': None,
        'trace': None,
    }
    if not workspace.is_dir():
        return result
    pages_dir = workspace / 'deck' / 'pages'
    target_pages = run_dir / 'artifacts' / 'pages'
    if pages_dir.is_dir():
        target_pages.mkdir(parents=True, exist_ok=True)
        for page in sorted(pages_dir.glob('*.page')):
            target = target_pages / page.name
            shutil.copy2(page, target)
            result['pages'].append(str(target))
    previews_dir = workspace / 'draft-previews'
    target_previews = run_dir / 'artifacts' / 'previews'
    if previews_dir.is_dir():
        target_previews.mkdir(parents=True, exist_ok=True)
        for preview in sorted(previews_dir.glob('*.png')):
            target = target_previews / preview.name
            shutil.copy2(preview, target)
            result['previews'].append(str(target))
    manifest = workspace / '.draft-preview.json'
    if manifest.is_file():
        target = run_dir / 'draft-preview.json'
        shutil.copy2(manifest, target)
        result['manifest'] = str(target)
    trace = (
        workspace.parents[2]
        / 'benchmark-traces'
        / f'{identifier}.json'
    )
    if trace.is_file():
        target = run_dir / 'timing-trace.json'
        shutil.copy2(trace, target)
        result['trace'] = str(target)
    if result['pages']:
        primary = Path(result['pages'][0])
        shutil.copy2(primary, run_dir / f'run-{repeat}.page')
    if result['previews']:
        primary = Path(result['previews'][0])
        shutil.copy2(primary, run_dir / f'run-{repeat}.png')
    return result


def _collect_text(value, result: list[str]) -> None:
    if isinstance(value, str):
        result.append(
            html.unescape(re.sub(r'<[^>]+>', '', value))
        )
    elif isinstance(value, list):
        for item in value:
            _collect_text(item, result)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {'text', 'title', 'label'}:
                _collect_text(item, result)
            elif isinstance(item, (dict, list)):
                _collect_text(item, result)


def document_text(document: dict) -> str:
    values = []
    for page in document.get('pages') or []:
        if isinstance(page, dict):
            _collect_text((page.get('data') or {}).get('elements'), values)
    return '\n'.join(dict.fromkeys(values))


def document_elements(document: dict) -> list[dict]:
    elements = []
    for page in document.get('pages') or []:
        if not isinstance(page, dict):
            continue
        page_data = page.get('data') or {}
        for element in page_data.get('elements') or []:
            if isinstance(element, dict):
                elements.append(element)
    return elements


def issue_counts(issues: list[dict]) -> dict[str, int]:
    counts = {'overlap': 0, 'clipped': 0, 'overflow': 0, 'contrast': 0, 'visual': 0}
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        code = str(issue.get('code') or '')
        group = ISSUE_GROUP.get(code)
        if group:
            counts[group] += 1
        elif code.startswith('visual-'):
            counts['visual'] += 1
    return counts


def stage_totals(trace: dict | None) -> dict:
    totals = {name: 0.0 for name in TRACE_STAGES}
    spans = (trace or {}).get('spans') or []
    for span in spans:
        if not isinstance(span, dict):
            continue
        stage = span.get('stage')
        if stage in totals:
            totals[stage] += float(span.get('durationMs') or 0)
    return {name: round(value, 3) for name, value in totals.items()}


def _timing(
    job: dict,
    trace: dict | None,
    total_ms: float,
) -> tuple[dict, dict]:
    stages = stage_totals(trace)
    node_timings = {}
    for node in (job.get('execution') or {}).get('nodes') or []:
        if not isinstance(node, dict):
            continue
        duration = node.get('durationSeconds')
        if isinstance(duration, (int, float)):
            node_timings[node.get('id')] = round(float(duration) * 1000, 3)
    timing = {
        'templateLoad': stages['templateLoad'],
        'planning': stages['planning'],
        'pageGeneration': stages['pageModel'],
        'compile': stages['compile'],
        'structureValidation': stages['structureValidation'],
        'hostTransform': stages['hostTransform'],
        'browserRender': stages['browserRender'],
        'browserLayout': stages['browserLayout'],
        'visionReview': stages['visionReview'],
        'repairModel': stages['repairModel'],
        'total': round(total_ms, 3),
    }
    return timing, node_timings


def _events(job: dict) -> list[dict]:
    execution = job.get('execution') or {}
    return [
        item for item in execution.get('events') or []
        if isinstance(item, dict)
    ]


def _trace_issue_sets(trace: dict | None, stage: str) -> list[dict]:
    return [
        item
        for item in (trace or {}).get('issueSets') or []
        if isinstance(item, dict) and item.get('stage') == stage
    ]


def _latest_issues(
    manifest: dict | None,
    trace: dict | None,
) -> list[dict]:
    manifest_issues = []
    for page in (manifest or {}).get('pages') or []:
        if isinstance(page, dict):
            manifest_issues.extend(
                issue for issue in page.get('issues') or []
                if isinstance(issue, dict)
            )
    if manifest_issues:
        return manifest_issues
    candidates = [
        *(_trace_issue_sets(trace, 'visionReview')[-1:]),
        *(_trace_issue_sets(trace, 'deckLayout')[-1:]),
    ]
    if not candidates:
        return []
    latest = max(candidates, key=lambda item: item.get('recordedAt') or 0)
    return [
        issue for issue in latest.get('issues') or []
        if isinstance(issue, dict)
    ]


def _first_gate_issues(trace: dict | None) -> list[dict]:
    sets = _trace_issue_sets(trace, 'pageGate')
    if not sets:
        return []
    return [
        issue for issue in sets[0].get('issues') or []
        if isinstance(issue, dict)
    ]


def _review_status(manifest: dict | None, job: dict) -> str:
    pages = (manifest or {}).get('pages') or []
    if pages and isinstance(pages[0], dict):
        status = pages[0].get('reviewStatus')
        if isinstance(status, str) and status:
            return status
    for event in reversed(_events(job)):
        if event.get('phase') == 'visual' and event.get('status') == 'completed':
            return 'passed' if not event.get('issueCount') else 'issues'
    return 'unavailable'


def _invalid_visual_responses(events: list[dict]) -> int:
    markers = ('结果结构无效', '返回不完整', 'JSON 转义格式')
    return sum(
        event.get('status') == 'retrying'
        and any(marker in str(event.get('message') or '') for marker in markers)
        for event in events
    )


def _content_checks(
    input_case: dict,
    document: dict | None,
    elements: list[dict],
) -> dict:
    text = document_text(document or {})
    required = {
        value: text.count(value)
        for value in input_case.get('requiredText') or []
    }
    forbidden = {
        value: text.count(value)
        for value in input_case.get('forbiddenText') or []
    }
    title = str(input_case.get('singleTitle') or '')
    normalized_title = re.sub(r'\s+', '', title)
    normalized_text = re.sub(r'\s+', '', text)
    element_ids = {
        element.get('elementId')
        for element in elements
        if element.get('elementId')
    }
    return {
        'requiredTextCounts': required,
        'forbiddenTextCounts': forbidden,
        'singleTitleCount': (
            normalized_text.count(normalized_title)
            if normalized_title
            else 0
        ),
        'elementIds': sorted(element_ids),
        'passed': (
            all(count >= 1 for count in required.values())
            and all(count == 0 for count in forbidden.values())
            and (
                not normalized_title
                or normalized_text.count(normalized_title) == 1
            )
        ),
    }


def _review_ids_valid(issues: list[dict], elements: list[dict]) -> tuple[bool, list[str]]:
    ids = {
        element.get('elementId')
        for element in elements
        if element.get('elementId')
    }
    missing = []
    for issue in issues:
        values = issue.get('elementIds')
        if not isinstance(values, list):
            values = [issue.get('elementId')]
        for value in values:
            if value and value not in ids and value not in missing:
                missing.append(str(value))
    return not missing, missing


def _quality_result(
    case: dict,
    input_case: dict,
    job: dict,
    document: dict | None,
    manifest: dict | None,
    trace: dict | None,
    timed_out: bool,
    total_ms: float,
    declared_content_elements: int | None = None,
) -> tuple[dict, dict, dict]:
    elements = document_elements(document or {})
    content_element_count = (
        declared_content_elements
        if isinstance(declared_content_elements, int)
        and declared_content_elements >= 0
        else len(elements)
    )
    latest_issues = _latest_issues(manifest, trace)
    first_issues = _first_gate_issues(trace)
    events = _events(job)
    review_status = _review_status(manifest, job)
    review_ids_ok, missing_ids = _review_ids_valid(latest_issues, elements)
    content = _content_checks(input_case, document, elements)
    repair_spans = [
        span
        for span in (trace or {}).get('spans') or []
        if isinstance(span, dict) and span.get('stage') == 'repairModel'
    ]
    repair_events = [
        event for event in events
        if event.get('status') == 'retrying'
    ]
    repair_rounds = len(repair_spans)
    page_count = (
        len((document or {}).get('pages') or [])
        if document
        else int((job.get('result') or {}).get('pageCount') or 0)
    )
    after_counts = issue_counts(latest_issues)
    first_counts = issue_counts(first_issues)
    quality_failures = []
    if timed_out:
        quality_failures.append('absolute-timeout')
    if job.get('status') != 'completed':
        quality_failures.append('job-not-completed')
    if page_count != 1:
        quality_failures.append('page-count')
    if repair_rounds or repair_events:
        quality_failures.append('repair-used')
    if any(after_counts.values()):
        quality_failures.append('final-issues')
    if review_status != 'passed':
        quality_failures.append('visual-review-not-passed')
    if not review_ids_ok:
        quality_failures.append('visual-missing-element')
    if not content['passed']:
        quality_failures.append('content-or-title')
    if content_element_count > int(case['hardElementLimit']):
        quality_failures.append('element-budget')
    if total_ms > float(case['firstPassHardMs']):
        quality_failures.append('first-pass-hard-time')
    first_pass = not quality_failures
    issues = {
        'beforeRepair': first_counts,
        'afterRepair': after_counts,
        'firstGateIssueCount': len(first_issues),
        'finalIssueCount': len(latest_issues),
        'missingReviewElementIds': missing_ids,
        'details': {
            'beforeRepair': first_issues,
            'afterRepair': latest_issues,
        },
    }
    counts = {
        'modelRequests': sum(
            event.get('phase') in {'context', 'plan', 'pages'}
            and event.get('status') == 'started'
            for event in events
        ),
        'visionRequests': sum(
            event.get('phase') in {'pages', 'visual'}
            and '视觉' in str(event.get('message') or '')
            and event.get('status') in {'started', 'completed'}
            for event in events
        ),
        'repairRounds': repair_rounds,
        'invalidReviewResponses': _invalid_visual_responses(events),
        'compiledElements': len(elements),
        'contentElements': content_element_count,
    }
    checks = {
        'content': content,
        'reviewStatus': review_status,
        'pageCount': page_count,
        'repairEventCount': len(repair_events),
        'templateRequested': True,
        'failures': quality_failures,
    }
    return (
        {
            'firstPass': first_pass,
            'finalStatus': 'passed' if first_pass else 'failed',
            'jobStatus': job.get('status'),
            'timedOut': timed_out,
            'reviewStatus': review_status,
            'issues': issues,
            'counts': counts,
            'checks': checks,
        },
        issues,
        counts,
    )


def _copy_job_artifacts(
    service: BenchmarkService,
    identifier: str,
    run_dir: Path,
    repeat: int,
) -> tuple[dict, dict | None, dict | None, dict]:
    job = _http_json(
        'GET',
        f'{service.base_url}{PREFIX}/jobs/{identifier}',
    )
    _write_json(run_dir / 'job.json', job)
    events_path = run_dir / 'events.jsonl'
    events_path.write_text(
        ''.join(
            json.dumps(event, ensure_ascii=False) + '\n'
            for event in _events(job)
        ),
        encoding='utf-8',
    )
    project_id = job.get('projectId')
    project = _optional_json(
        service,
        f'{service.base_url}{PREFIX}/projects/{project_id}',
        run_dir / 'project.json',
    )
    document = _optional_json(
        service,
        f'{service.base_url}{PREFIX}/projects/{project_id}/pptd/document',
        run_dir / 'document.json',
    )
    workspace = (
        service.data_root
        / 'kimi-workspace'
        / str(project_id)
        / identifier
    )
    workspace_result = _copy_workspace(
        workspace,
        run_dir,
        identifier,
        repeat,
    )
    return job, project, document, workspace_result


def run_case(
    service: BenchmarkService,
    cases: dict,
    case: dict,
    input_case: dict,
    repeat: int,
    session_root: Path,
) -> dict:
    run_id = uuid4().hex
    run_dir = session_root / case['id'] / f'run-{repeat}'
    if run_dir.exists():
        raise BenchmarkError(f'拒绝覆盖已有结果：{run_dir}')
    run_dir.mkdir(parents=True)
    request_body, project = _request(
        case,
        input_case,
        cases['template']['id'],
    )
    _write_json(run_dir / 'request.json', request_body)
    started_at = datetime.now().astimezone()
    started_monotonic = time.monotonic()
    identifier = ''
    timed_out = False
    try:
        initial = _http_json(
            'POST',
            f'{service.base_url}{PREFIX}/jobs',
            request_body,
        )
        identifier = str(initial['id'])
        job, _, timed_out = _wait_for_job(
            service,
            identifier,
            float(case['absoluteTimeoutMs']) / 1000,
            run_dir / 'poll.jsonl',
        )
    except BaseException:
        if identifier:
            try:
                _http_json(
                    'POST',
                    f'{service.base_url}{PREFIX}/jobs/{identifier}/cancel',
                )
            except BenchmarkError:
                pass
        raise
    final_total_ms = (time.monotonic() - started_monotonic) * 1000
    job, project, document, workspace = _copy_job_artifacts(
        service,
        identifier,
        run_dir,
        repeat,
    )
    trace_path = Path(workspace['trace']) if workspace.get('trace') else None
    trace = _read_json(trace_path) if trace_path else None
    manifest_path = (
        Path(workspace['manifest'])
        if workspace.get('manifest')
        else None
    )
    manifest = _read_json(manifest_path) if manifest_path else None
    generation_meta = (
        (project or {}).get('generationMeta') or {}
    )
    timing, node_timings = _timing(job, trace, final_total_ms)
    quality, issues, counts = _quality_result(
        case,
        input_case,
        job,
        document,
        manifest,
        trace,
        timed_out,
        final_total_ms,
        generation_meta.get('contentElements'),
    )
    result = {
        'runId': run_id,
        'caseId': case['id'],
        'repeat': repeat,
        'timestamp': started_at.isoformat(),
        'gitRevision': _git_revision(),
        'templateId': cases['template']['id'],
        'templateSha256': cases['template']['sha256'],
        'model': cases['execution']['generationModel'],
        'visionModel': cases['execution']['visionModel'],
        'cache': {'render': False, 'review': False},
        'jobId': identifier,
        'projectId': project['id'],
        'timingMs': timing,
        'jobStageMs': node_timings,
        'firstPass': quality['firstPass'],
        'finalStatus': quality['finalStatus'],
        'jobStatus': quality['jobStatus'],
        'timedOut': quality['timedOut'],
        'reviewStatus': quality['reviewStatus'],
        'issues': issues,
        'counts': counts,
        'checks': quality['checks'],
        'budgets': {
            'targetMs': case['firstPassTargetMs'],
            'hardMs': case['firstPassHardMs'],
            'absoluteTimeoutMs': case['absoluteTimeoutMs'],
            'targetMet': final_total_ms <= case['firstPassTargetMs'],
            'hardMet': final_total_ms <= case['firstPassHardMs'],
            'common': _budget_checks(timing, cases['commonBudgetsMs']),
        },
        'artifactPath': str(run_dir.resolve()),
        'workspace': workspace,
    }
    _write_json(run_dir / 'run.json', result)
    return result


def _budget_checks(timing: dict, budgets: dict) -> dict:
    checks = {}
    for budget_name, stages in COMMON_BUDGETS.items():
        budget = budgets.get(budget_name)
        if not isinstance(budget, (int, float)):
            continue
        value = sum(float(timing.get(stage) or 0) for stage in stages)
        checks[budget_name] = {
            'hardMs': budget,
            'actualMs': round(value, 3),
            'passed': value <= float(budget),
        }
    return checks


def _git_revision() -> str:
    try:
        revision = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        dirty = subprocess.check_output(
            ['git', 'status', '--porcelain'],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return revision + ('-dirty' if dirty else '')
    except (OSError, subprocess.CalledProcessError):
        return 'unknown'


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def summarize(results: list[dict]) -> dict:
    by_case = {}
    for result in results:
        by_case.setdefault(result['caseId'], []).append(result)
    case_summaries = {}
    for identifier, runs in sorted(by_case.items()):
        totals = [float(run['timingMs']['total']) for run in runs]
        stages = {
            stage: _median([
                float(run['timingMs'].get(stage) or 0)
                for run in runs
            ])
            for stage in TRACE_STAGES
        }
        case_summaries[identifier] = {
            'runs': len(runs),
            'firstPassCount': sum(bool(run['firstPass']) for run in runs),
            'firstPass': all(bool(run['firstPass']) for run in runs),
            'landedRuns': sum(bool(run['finalStatus'] == 'passed') for run in runs),
            'medianTotalMs': _median(totals),
            'maxTotalMs': round(max(totals), 3),
            'medianStagesMs': stages,
            'targetMs': runs[0]['budgets']['targetMs'],
            'hardMs': runs[0]['budgets']['hardMs'],
            'repairRounds': sum(int(run['counts']['repairRounds']) for run in runs),
        }
    return {
        'version': 1,
        'generatedAt': datetime.now().astimezone().isoformat(),
        'results': len(results),
        'allFirstPass': all(bool(run['firstPass']) for run in results),
        'cases': case_summaries,
    }


def summary_markdown(summary: dict) -> str:
    lines = [
        '# 单页模板基准汇总',
        '',
        f"- 生成时间：{summary['generatedAt']}",
        f"- 运行次数：{summary['results']}",
        f"- 全部首轮通过：{'是' if summary['allFirstPass'] else '否'}",
        '',
        '| 用例 | 首轮通过 | 中位总耗时 | 最大值 | 目标 | 硬上限 | 修复轮数 | 最大阶段 |',
        '|---|---:|---:|---:|---:|---:|---:|---|',
    ]
    for identifier, item in summary['cases'].items():
        stages = item['medianStagesMs']
        bottleneck = max(stages, key=lambda key: stages[key] or 0)
        lines.append(
            f"| {identifier} | {item['firstPassCount']}/{item['runs']} | "
            f"{_seconds(item['medianTotalMs'])} | {_seconds(item['maxTotalMs'])} | "
            f"{_seconds(item['targetMs'])} | {_seconds(item['hardMs'])} | "
            f"{item['repairRounds']} | {bottleneck} {_seconds(stages[bottleneck])} |"
        )
    lines.append('')
    return '\n'.join(lines)


def _seconds(value) -> str:
    if not isinstance(value, (int, float)):
        return '-'
    return f'{value / 1000:.1f}s'


def _prepare_session(
    output_root: Path,
    mode: str,
    case_ids: list[str],
) -> Path:
    local_now = datetime.now().astimezone()
    token = (
        local_now.strftime('%Y-%m-%dT%H-%M-%S')
        + f'-{mode}-{uuid4().hex[:8]}'
    )
    session = output_root / local_now.strftime('%Y-%m-%d') / token
    session.mkdir(parents=True)
    marker = {
        'mode': mode,
        'cases': case_ids,
        'createdAt': local_now.isoformat(),
    }
    _write_json(session / 'session.json', marker)
    return session


def execute(
    *,
    cases_path: Path,
    inputs_path: Path,
    output_root: Path,
    case_ids: list[str] | None,
    mode: str,
    dry_run: bool = False,
) -> int:
    cases, inputs = load_plan(cases_path, inputs_path)
    chosen = selected_cases(cases, case_ids)
    template = cases.get('template') or {}
    template_path = ROOT / str(template.get('path') or '')
    if not template_path.is_file():
        raise BenchmarkError(f'固定模板不存在：{template_path}')
    digest = _sha256(template_path)
    if digest != template.get('sha256'):
        raise BenchmarkError(
            f'模板 SHA-256 不匹配：需要 {template.get("sha256")}，实际 {digest}。'
        )
    repeats = int(
        cases['execution']['smokeRepeats']
        if mode == 'smoke'
        else cases['execution']['repeats']
    )
    selected_ids = [case['id'] for case in chosen]
    if dry_run:
        print(json.dumps(
            {
                'mode': mode,
                'repeats': repeats,
                'cases': selected_ids,
                'templateSha256': digest,
                'models': {
                    'generation': cases['execution']['generationModel'],
                    'vision': cases['execution']['visionModel'],
                },
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    session_root = _prepare_session(output_root, mode, selected_ids)
    results = []
    service = BenchmarkService(session_root, template)
    try:
        service.start(cases)
        for case in chosen:
            for repeat in range(1, repeats + 1):
                print(
                    f"[{case['id']}] run {repeat}/{repeats} 开始",
                    flush=True,
                )
                result = run_case(
                    service,
                    cases,
                    case,
                    inputs[case['id']],
                    repeat,
                    session_root,
                )
                results.append(result)
                print(
                    f"[{case['id']}] run {repeat}/{repeats} "
                    f"{'通过' if result['firstPass'] else '未通过'}，"
                    f"总耗时 {_seconds(result['timingMs']['total'])}；"
                    f"结果 {result['artifactPath']}",
                    flush=True,
                )
    finally:
        service.stop()
    summary = summarize(results)
    summary['mode'] = mode
    summary['session'] = str(session_root.resolve())
    _write_json(session_root / 'summary.json', summary)
    (session_root / 'summary.md').write_text(
        summary_markdown(summary),
        encoding='utf-8',
    )
    print(
        f"汇总：{session_root / 'summary.md'}；"
        f"首轮通过 {summary['cases']}",
        flush=True,
    )
    return 0 if summary['allFirstPass'] else 1
