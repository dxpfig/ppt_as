"""Cancellable native PPTX + Chromium PDF export from one PPTD revision."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import inspect
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED

import pymupdf
from pptd_engine.workbench import ROOT as REPOSITORY_ROOT, WEB_DIR, RUNTIME_DIR, snapshot_server


EXPORT_LOCK = asyncio.Semaphore(1)


def layout_only_issues(issues):
    """Layout findings can be fixed without replacing content or document schema."""
    return bool(issues) and all(
        item.get('code') in {'text-overflow', 'text-overlap', 'text-clipped'}
        and (item.get('pagePath') or item.get('page')) for item in issues)


class ValidationError(ValueError):
    """Located content/format problems which the generation agent can repair."""
    def __init__(self, issues, *, previews=None, layouts=None):
        self.issues = issues
        self.previews = previews or {}  # Private actual pixels, never public diagnostic JSON.
        self.layouts = layouts or {}
        super().__init__('PPTD 内容校验失败：' + '; '.join(f"{item.get('location', item.get('page', 'document'))}: {item['message']}" for item in issues))


@asynccontextmanager
async def _snapshot_renderer(source, revision):
    manager = snapshot_server(source, revision)
    # Font resolution may download a declared custom font. Keep it off the
    # application event loop, including cancellation while the server starts.
    setup = asyncio.create_task(asyncio.to_thread(manager.__enter__))
    try:
        url = await asyncio.shield(setup)
    except asyncio.CancelledError:
        try:
            await setup
        except Exception:
            pass
        else:
            await asyncio.to_thread(manager.__exit__, None, None, None)
        raise
    try:
        yield url
    finally:
        await asyncio.to_thread(manager.__exit__, None, None, None)


async def _validated_process(output, *args, **kwargs):
    try:
        return await _run_process(*args, **kwargs)
    except ValueError:
        issues_file = output / 'validation-errors.json'
        if issues_file.is_file():
            evidence = json.loads(issues_file.read_text(encoding='utf-8'))
            issues = evidence.get('issues')
            if issues:
                previews = {}
                for item in evidence.get('previews', []):
                    name = item.get('file', '')
                    if Path(name).name != name or not name.endswith('.png'):
                        raise ValueError('Invalid layout preview path')
                    target = output / name
                    if target.is_file() and target.stat().st_size <= 15 * 1024 * 1024:
                        previews[item['page']] = target.read_bytes()
                raise ValidationError(issues, previews=previews, layouts=evidence.get('layouts')) from None
        raise


async def _progress(callback, stage, message):
    if callback is not None:
        result = callback(stage, message)
        if inspect.isawaitable(result):
            await result


def chromium_path():
    configured = os.getenv('PPTD_CHROMIUM')
    if configured:
        if not Path(configured).is_file():
            raise ValueError('PPTD_CHROMIUM does not name an existing browser')
        return configured
    for candidate in ('chromium', 'chromium-browser', 'google-chrome'):
        found = shutil.which(candidate)
        if found:
            return found
    return None  # Playwright's installed Chromium is also supported.


def renderer_available():
    return bool(shutil.which('node') and (RUNTIME_DIR / 'pptd-runtime.js').is_file() and (chromium_path() or any((Path.home() / '.cache/ms-playwright').glob('chromium-*'))))


async def _run_process(*args, timeout=300, env=None):
    process = await asyncio.create_subprocess_exec(*map(str, args), cwd=REPOSITORY_ROOT, env=env, start_new_session=True, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        if process.returncode:
            raise ValueError('PPTD 导出失败：' + (stderr or stdout).decode(errors='replace')[-5000:])
        return stdout.decode(errors='replace')
    finally:
        if process.returncode is None:
            # Chromium and font/math child processes must not survive cancellation.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()


def _merge_and_preview(folder, page_count):
    report = json.loads((folder / 'pdf-pages.json').read_text(encoding='utf-8'))
    if report.get('status') != 'passed' or len(report.get('pages', [])) != page_count:
        raise ValueError('PDF renderer returned an incomplete page set')
    merged = pymupdf.open()
    try:
        for item in report['pages']:
            name = item['file']
            if Path(name).name != name or not name.endswith('.pdf'):
                raise ValueError('Invalid rendered PDF page path')
            with pymupdf.open(folder / name) as page:
                if len(page) != 1:
                    raise ValueError('Each PPTD page must render to exactly one PDF page')
                merged.insert_pdf(page)
        merged.save(folder / 'presentation.pdf')
    finally:
        merged.close()
    names = []
    with pymupdf.open(folder / 'presentation.pdf') as document:
        for index, page in enumerate(document):
            name = f'slide-{index + 1}.png'
            page.get_pixmap(matrix=pymupdf.Matrix(1.2, 1.2), alpha=False).save(folder / name)
            names.append(name)
    for item in report['pages']:
        (folder / item['file']).unlink()
    (folder / 'pdf-pages.json').unlink()
    return report, names


async def export_document(store, project_id, folder, progress=None, *, allow_vector=True):
    """Return file metadata only after every artifact has been verified.

    The application publishes its own metadata.json after checking that the job
    is still current. Cancellation/failure leaves neither a final folder nor an
    artifact metadata file. A stable immutable snapshot is used even if the user
    edits the authoritative document while this task is running.
    """
    folder = Path(folder).resolve()
    if folder.exists():
        raise ValueError('Artifact output must be a new directory')
    folder.parent.mkdir(parents=True, exist_ok=True)
    async with EXPORT_LOCK:
        await _progress(progress, 10, '正在锁定当前 PPTD 文档版本')
        with tempfile.TemporaryDirectory(prefix='.pptd-export-', dir=folder.parent) as temporary:
            staging = Path(temporary)
            source_dir = staging / 'source'
            state = store.snapshot(project_id, source_dir)
            source = source_dir / 'document.pptd'
            artifact_dir = staging / 'artifact'
            artifact_dir.mkdir()
            browser_dir, native_dir = staging / 'browser', staging / 'native'
            browser_dir.mkdir(); native_dir.mkdir()
            issues, previews, layouts = [], {}, {}
            await _progress(progress, 25, f'正在检查实际排版并生成 PDF（文档版本 {state["revision"]}）')
            env = os.environ.copy()
            browser = chromium_path()
            if browser:
                env['PPTD_CHROMIUM'] = browser
            try:
                async with _snapshot_renderer(source, state['revision']) as url:
                    await _validated_process(browser_dir, 'node', WEB_DIR / 'export_pdf.cjs', url, browser_dir, env=env, timeout=360)
            except ValidationError as error:
                # Content failures can be repaired together with native issues.
                # Dependency, process and browser failures remain ordinary errors.
                issues.extend(error.issues)
                previews.update(error.previews)
                layouts.update(error.layouts)
            await _progress(progress, 60, '正在检查同一文档的原生 PPTX 导出')
            arguments = [sys.executable, '-m', 'agent_service.pptd_exports', '--native', source, native_dir]
            if allow_vector:
                arguments.append('--allow-vector-charts')
            try:
                await _validated_process(native_dir, *arguments)
            except ValidationError as error:
                issues.extend(error.issues)
            report_path = native_dir / 'export-report.json'
            report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.is_file() else {}
            for item in report.get('unsupported', []):
                location = item.get('location', 'document')
                page = next((p['path'] for p in state['pages'] if location.startswith(p['path'])), None)
                issues.append({'page': page, 'location': location, 'code': 'native-unsupported', 'severity': 'error', 'message': item.get('feature', 'element') + ': ' + item.get('detail', '')})
            if issues:
                unique = {json.dumps(item, ensure_ascii=False, sort_keys=True): item for item in issues}
                raise ValidationError(list(unique.values()), previews=previews, layouts=layouts)
            if not report or report.get('status') == 'failed':
                raise ValueError('原生导出没有返回完整的校验报告，未发布文件。')
            (native_dir / 'export.pptx').replace(artifact_dir / 'presentation.pptx')
            pdf_report, previews = _merge_and_preview(browser_dir, len(state['pages']))
            for name in ['presentation.pdf', *previews]:
                (browser_dir / name).replace(artifact_dir / name)
            with ZipFile(artifact_dir / 'source.zip', 'w', ZIP_DEFLATED) as archive:
                for path in sorted(source_dir.rglob('*')):
                    if path.is_file():
                        archive.write(path, path.relative_to(source_dir).as_posix())
            report.update(revision=state['revision'], projectId=project_id, output='presentation.pptx', source='source.zip', pdf=pdf_report, previews=previews)
            (artifact_dir / 'export-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            warnings = [item.get('detail', str(item)) for item in report.get('unsupported', [])]
            warnings.extend(report.get('fontSubstitutions', []))
            for page in pdf_report['pages']:
                warnings.extend(page.get('warnings', []))
            warnings = list(dict.fromkeys(warnings))
            await _progress(progress, 95, '文件与预览已校验，正在完成发布')
            result = {'pptx': 'presentation.pptx', 'pdf': 'presentation.pdf', 'source': 'source.zip', 'report': 'export-report.json', 'previews': previews, 'revision': state['revision'], 'pageCount': len(state['pages']), 'warnings': warnings}
            # No await after publication: cancellation always happens before it.
            os.replace(artifact_dir, folder)
            return result


def _native_cli():
    """A subprocess boundary makes expensive Office conversion cancellable."""
    from pptd_engine.pptx import Exporter
    from pptd_engine.document import load_yaml
    source, output = Path(sys.argv[2]), Path(sys.argv[3])
    exporter = None
    try:
        exporter = Exporter(source, output, allow_vector='--allow-vector-charts' in sys.argv)
        exporter.run()
    except Exception as error:
        output.mkdir(parents=True, exist_ok=True)
        if exporter is not None:
            # Font embedding happens after object conversion; retain all native
            # diagnostics even when that final step rejects a font.
            partial = {**exporter.report.data, 'status': 'failed', 'failure': str(error)}
            (output / 'export-report.json').write_text(json.dumps(partial, ensure_ascii=False, indent=2), encoding='utf-8')
        issues = []
        if isinstance(error, (ValueError, KeyError, TypeError, IndexError)):
            document = load_yaml(source)
            message = str(error)
            page = next((p for p in document['pages'] if p in message), None)
            if page:
                issues.append({'page': page, 'location': message.split(':', 1)[0], 'code': 'native-export', 'severity': 'error', 'message': message})
            elif isinstance(error, ValueError) and any(marker in message for marker in (
                ' missing glyphs:', 'not installed; declare customFonts',
                'Font family mismatch', 'Font does not permit editable embedding',
                'Bitmap-only embedding permission', 'Font source must use HTTPS',
            )):
                from pptd_engine.workbench import fonts_for_document
                pages = [{'path': path, 'data': load_yaml(source.parent / path)} for path in document['pages']]
                for issue in fonts_for_document({'document': document, 'pages': pages})['errors']:
                    for page in issue.get('pages') or document['pages']:
                        for element_id in issue.get('elementIds') or [None]:
                            location = issue.get('location') or (page + '#' + element_id if element_id else page)
                            issues.append({'page': page, **({'elementId': element_id} if element_id else {}),
                                           'location': location, 'code': 'font-unresolved', 'severity': 'error',
                                           'message': f"{issue.get('family', '字体')}: {issue.get('error', issue.get('message', ''))}"})
        if issues:
            (output / 'validation-errors.json').write_text(json.dumps({'issues': issues}, ensure_ascii=False), encoding='utf-8')
        # Without a located content issue, the failed process remains an
        # infrastructure/engine failure instead of consuming a model repair.
        raise


if __name__ == '__main__':
    if len(sys.argv) >= 4 and sys.argv[1] == '--native':
        _native_cli()
    else:
        raise SystemExit('Usage: python -m agent_service.pptd_exports --native DOCUMENT OUTPUT [--allow-vector-charts]')
