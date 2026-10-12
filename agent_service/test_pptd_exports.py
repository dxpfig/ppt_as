"""Production preview/export contracts; never route PDF through LibreOffice."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from agent_service.pptd_store import DocumentStore
from agent_service.pptd_exports import export_document, ValidationError, chromium_path
from pptd_engine.workbench import workbench_html, snapshot_server, fonts_for_document


def document_fixture():
    document = {'version': 'v2', 'title': '一致快照', 'size': [960, 540], 'theme': {'textStyles': {'body': {'fontFamily': 'Noto Sans CJK SC', 'fontSize': 28, 'color': '#17342D'}}}, 'pages': ['pages/a.page']}
    pages = [{'path': 'pages/a.page', 'data': {'pageType': 'content', 'background': {'type': 'solid', 'color': '#FFFFFF'}, 'elements': [{'elementId': 'a', 'elementType': 'text', 'bounds': [40, 40, 880, 200], 'content': {'text': r'<p>公式与原始快照 \(E=mc^2\)</p>', 'style': '$body'}}]}}]
    return document, pages


class WorkbenchTests(unittest.TestCase):
    def test_html_has_scoped_api_and_parent_bridge_without_experimental_paths(self):
        markup = workbench_html('/api/studio/projects/demo/pptd', project_id='demo', embed=True)
        self.assertIn('"apiBase": "/api/studio/projects/demo/pptd"', markup)
        self.assertIn('pptd:changed', markup)
        self.assertIn('event.source !== window.parent', markup)
        self.assertIn('event.origin !== window.location.origin', markup)
        self.assertNotIn('experiments/', markup)
        self.assertNotIn("fetch('/api/", markup)
        self.assertIn('/api/studio/pptd-runtime/katex.min.css', markup)
    def test_html_rejects_external_or_script_configuration(self):
        for path in ['https://evil.example/api', '//evil.example', '/api/../bad', '/a"<script>']:
            with self.assertRaises(ValueError): workbench_html(path)
    def test_font_coverage_errors_only_target_pages_with_missing_glyphs(self):
        class Font:
            path = Path('a' * 64 + '.ttf')
            def __init__(self, family): self.family = family
            def public(self): return {'family': self.family, 'weight': 400, 'style': 'normal'}
            def check_coverage(self, text):
                if self.family == 'Latin Only' and '中' in text:
                    raise ValueError('Latin Only missing glyphs: 中')
        def page(path, family, text):
            return {'path': path, 'data': {'elements': [{'elementId': 'title', 'elementType': 'text', 'content': {'text': text, 'fontFamily': family}}]}}
        state = {'document': {'customFonts': [{'family': 'Latin Only', 'src': 'https://example.test/font.ttf'}]}, 'pages': [
            page('pages/good.page', {'latin': 'Latin Only', 'ea': 'Chinese'}, 'AI 中'),
            page('pages/bad.page', 'Latin Only', 'AI 中'),
            page('pages/not-used.page', 'Chinese', '中'),
        ]}
        with patch('pptd_engine.workbench.FontResolver') as resolver:
            resolver.return_value.resolve.side_effect = lambda family, weight, italic: Font(family)
            result = fonts_for_document(state)
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(result['errors'][0]['pages'], ['pages/bad.page'])
        self.assertEqual(result['errors'][0]['elementIds'], ['title'])
        self.assertIn('Latin Only', [font['family'] for font in result['fonts']], 'valid font resource remains available for the pages it covers')

    def test_real_iframe_preview_mode_and_conflicted_save(self):
        from pptd_engine.workbench import ROOT
        with TemporaryDirectory() as folder:
            store = DocumentStore(Path(folder))
            document, pages = document_fixture()
            store.commit('demo', document, pages, 'fixture')
            embedded_html = Path(folder) / 'workbench.html'
            embedded_html.write_text(workbench_html('/api', '/runtime', project_id='demo', embed=True), encoding='utf-8')
            with snapshot_server(store.source_path('demo'), revision=1) as url:
                script = r"""
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({executablePath:process.env.PPTD_CHROMIUM,headless:true,args:['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport:{width:640,height:960}});
    await page.route('**/?mode=*', route => route.fulfill({contentType:'text/html',body:require('node:fs').readFileSync(process.argv[2],'utf8')}));
    await page.route('**/parent', route => route.fulfill({contentType:'text/html', body:'<!doctype html><script>window.messages=[];window.addEventListener("message",event=>messages.push(event.data));</script><iframe style="width:100%;height:900px;border:0" src="/?mode=preview"></iframe>'}));
    const edits=[];
    await page.route('**/api/edit', route => {
      edits.push(route.request().postDataJSON());
      return route.fulfill({status:409, contentType:'application/json', body:JSON.stringify({detail:'文档已更新（当前版本 2）；请重新加载后再保存。'})});
    });
    await page.goto(process.argv[1]+'/parent');
    let frame = page.frames().find(f => f.parentFrame());
    await frame.waitForSelector('body[data-loaded="true"]');
    assert.equal(await frame.locator('aside').isVisible(), false, 'preview hides the editing inspector');
    assert.equal(await frame.locator('#page-select').isVisible(), false, 'outer workspace owns preview navigation');
    assert.equal(await frame.locator('#animation-play').isVisible(), false, 'presentation playback replaces duplicate preview controls');
    await frame.waitForFunction(() => document.getElementById('slide').getBoundingClientRect().width <= document.querySelector('.stage-column').clientWidth + 1);
    assert.equal(await frame.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'embedded preview must fit without horizontal scrolling');
    await frame.locator('[data-element-id="a"]').click();
    assert.equal(await frame.evaluate(() => window.pptdWorkbench.state.selected), null, 'preview cannot select an editable element');
    await frame.evaluate(() => window.pptdWorkbench.select(null));
    await page.evaluate(() => { document.querySelector('iframe').src='/?mode=edit'; });
    frame = page.frames().find(f => f.parentFrame());
    await frame.waitForURL('**/?mode=edit');
    await frame.waitForSelector('body[data-loaded="true"]');
    assert.equal(await frame.locator('aside').isVisible(), true);
    await page.setViewportSize({width:1100,height:960});
    await frame.waitForFunction(() => document.getElementById('slide').getBoundingClientRect().width <= document.querySelector('.stage-column').clientWidth + 1);
    assert.equal(await frame.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'editor and scaled canvas must both fit');
    await frame.locator('[data-element-id="a"]').click();
    await frame.locator('#text-content').fill('尝试覆盖旧版本');
    await frame.evaluate(() => document.body.dataset.lastSaveVerified='true');
    await frame.locator('#save').click();
    await frame.waitForFunction(() => document.getElementById('status').textContent.includes('保存失败'));
    assert.equal(edits.length, 1);
    assert.equal(edits[0].expectedRevision, 1);
    assert.equal(await frame.evaluate(() => window.pptdWorkbench.state.revision), 1);
    assert.match(await frame.locator('#status').innerText(), /当前版本 2/);
    assert.equal(await page.evaluate(() => messages.filter(m => m.type==='pptd:changed').length), 0, 'failed save cannot notify parent of success');
    assert.notEqual(await frame.evaluate(() => document.body.dataset.lastSaveVerified), 'true');
    if(process.env.PPTD_WORKBENCH_SCREENSHOT) await page.screenshot({path:process.env.PPTD_WORKBENCH_SCREENSHOT,fullPage:true});
    console.log('preview/edit, responsive scaling and conflict iframe checks passed');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode=1;});
"""
                environment = {**os.environ, 'PPTD_CHROMIUM': chromium_path() or ''}
                result = subprocess.run(['node', '-e', script, url, str(embedded_html)], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = DocumentStore(self.root)
        document, pages = document_fixture()
        self.store.commit('demo', document, pages, '初稿')
        self.output = self.root / 'artifact'
        self.events = []
    async def asyncTearDown(self):
        self.tmp.cleanup()
    async def progress(self, stage, message):
        self.events.append((stage, message))
    async def test_failed_export_never_publishes_output_or_metadata(self):
        with patch('agent_service.pptd_exports._run_process', side_effect=ValueError('bad font')):
            with self.assertRaisesRegex(ValueError, 'bad font'):
                await export_document(self.store, 'demo', self.output, self.progress)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.store.read('demo')['revision'], 1)
    async def test_cancellation_discards_staging_and_publication(self):
        started = asyncio.Event()
        async def slow(*args, **kwargs):
            started.set()
            await asyncio.Future()
        with patch('agent_service.pptd_exports._run_process', side_effect=slow):
            task = asyncio.create_task(export_document(self.store, 'demo', self.output, self.progress))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.pptd-export-*')), [])
    async def test_actual_text_overflow_is_located_failure(self):
        state = self.store.read('demo')
        state['pages'][0]['data']['elements'][0]['bounds'] = [40, 40, 120, 15]
        self.store.commit('demo', state['document'], state['pages'], '溢出', 1)
        with self.assertRaises(ValidationError) as failed:
            await export_document(self.store, 'demo', self.output, self.progress)
        self.assertTrue(any(i['code'] == 'text-overflow' and i['page'] == 'pages/a.page' and i['elementId'] == 'a' for i in failed.exception.issues))
        overflow = next(i for i in failed.exception.issues if i['code'] == 'text-overflow')
        self.assertIn('layout', overflow, 'AI needs measured geometry, not only a prose error')
        self.assertEqual(overflow['layout']['canvasSize'], [960, 540])
        self.assertEqual(overflow['layout']['bounds'], [40, 40, 120, 15])
        self.assertGreater(overflow['layout']['measuredTextSize'][1], overflow['layout']['availableSize'][1])
        self.assertTrue(failed.exception.previews['pages/a.page'].startswith(b'\x89PNG'))
        self.assertEqual(failed.exception.layouts['pages/a.page']['elements'][0]['elementId'], 'a')
        self.assertFalse(self.output.exists())
    async def test_visible_text_intersection_blocks_export_with_both_element_locations(self):
        state = self.store.read('demo')
        state['pages'][0]['data']['elements'] = [
            {'elementId': 'heading', 'elementType': 'text', 'bounds': [40, 40, 450, 120], 'content': {'text': '重叠标题 ABC', 'style': '$body'}},
            {'elementId': 'body', 'elementType': 'text', 'bounds': [55, 45, 450, 120], 'content': {'text': '重叠正文 DEF', 'style': '$body'}},
        ]
        self.store.commit('demo', state['document'], state['pages'], '可见文字相互重叠', 1)
        with self.assertRaises(ValidationError) as failed:
            await export_document(self.store, 'demo', self.output, self.progress)
        overlaps = [issue for issue in failed.exception.issues if issue['code'] == 'text-overlap']
        self.assertTrue(overlaps)
        self.assertEqual(overlaps[0]['page'], 'pages/a.page')
        self.assertEqual(overlaps[0]['elementId'], 'body')
        self.assertIn('heading', overlaps[0]['message'])
        self.assertFalse(self.output.exists())

    async def test_overlapping_empty_textbox_space_and_background_do_not_block_export(self):
        state = self.store.read('demo')
        # Both text boxes cover the same canvas area, but their actual text sits
        # on different lines. A background is deliberately behind both boxes.
        state['pages'][0]['data']['elements'] = [
            {'elementId': 'card', 'elementType': 'shape', 'bounds': [20, 20, 900, 400], 'shapeName': 'rect', 'fill': {'type': 'solid', 'color': '#EDF2E6'}},
            {'elementId': 'heading', 'elementType': 'text', 'bounds': [40, 40, 850, 200], 'content': {'text': r'标题与公式 \(E=mc^2\)', 'style': '$body'}},
            {'elementId': 'body', 'elementType': 'text', 'bounds': [40, 125, 850, 200], 'content': {'text': '下方正文不重叠', 'style': '$body'}},
        ]
        self.store.commit('demo', state['document'], state['pages'], '合法图文叠层', 1)
        result = await export_document(self.store, 'demo', self.output, self.progress)
        self.assertEqual(result['pageCount'], 1)
        self.assertTrue((self.output / 'presentation.pdf').is_file())

    async def test_missing_font_has_structured_page_issue(self):
        state = self.store.read('demo')
        state['document']['theme']['textStyles']['body']['fontFamily'] = 'MissingFontPptdTest997'
        self.store.commit('demo', state['document'], state['pages'], '缺字体', 1)
        with self.assertRaises(ValidationError) as failed:
            await export_document(self.store, 'demo', self.output, self.progress)
        self.assertTrue(any(i['code'] == 'font-unresolved' and i['page'] == 'pages/a.page' for i in failed.exception.issues))
        self.assertFalse(self.output.exists())
    async def test_native_style_loss_is_a_repairable_error(self):
        state = self.store.read('demo')
        state['document']['theme']['textStyles']['body']['backgroundColor'] = '#FF0000'
        self.store.commit('demo', state['document'], state['pages'], '原生不支持的样式', 1)
        with self.assertRaises(ValidationError) as failed:
            await export_document(self.store, 'demo', self.output, self.progress)
        self.assertTrue(any(i['code'] == 'native-unsupported' and i['page'] == 'pages/a.page' and 'backgroundColor' in i['message'] for i in failed.exception.issues))
        self.assertFalse(self.output.exists())
    async def test_off_page_text_is_not_silently_clipped(self):
        state = self.store.read('demo')
        state['pages'][0]['data']['elements'][0]['bounds'] = [900, 40, 880, 200]
        self.store.commit('demo', state['document'], state['pages'], '页面外文字', 1)
        with self.assertRaises(ValidationError) as failed:
            await export_document(self.store, 'demo', self.output, self.progress)
        self.assertTrue(any(i['code'] == 'text-clipped' for i in failed.exception.issues))
        self.assertFalse(self.output.exists())
    async def test_font_and_native_style_failures_are_reported_together(self):
        state = self.store.read('demo')
        style = state['document']['theme']['textStyles']['body']
        style['fontFamily'] = 'MissingFontAggregateTest991'
        style['backgroundColor'] = '#FF0000'
        self.store.commit('demo', state['document'], state['pages'], '字体与原生样式同时失败', 1)
        with self.assertRaises(ValidationError) as failed:
            await export_document(self.store, 'demo', self.output, self.progress)
        codes = {issue['code'] for issue in failed.exception.issues}
        self.assertIn('font-unresolved', codes)
        self.assertIn('native-unsupported', codes)
        self.assertTrue(all(issue.get('page') == 'pages/a.page' for issue in failed.exception.issues))
        self.assertFalse(self.output.exists())

    async def test_layout_and_native_style_failures_are_reported_together(self):
        state = self.store.read('demo')
        state['pages'][0]['data']['elements'][0]['bounds'] = [40, 40, 120, 15]
        state['document']['theme']['textStyles']['body']['backgroundColor'] = '#FF0000'
        self.store.commit('demo', state['document'], state['pages'], '溢出与原生样式同时失败', 1)
        with self.assertRaises(ValidationError) as failed:
            await export_document(self.store, 'demo', self.output, self.progress)
        codes = {issue['code'] for issue in failed.exception.issues}
        self.assertIn('text-overflow', codes)
        self.assertIn('native-unsupported', codes)
        self.assertFalse(self.output.exists())

    async def test_native_environment_failure_cannot_reuse_browser_issues(self):
        browser_output = None
        calls = []
        @asynccontextmanager
        async def server(*args):
            yield 'http://127.0.0.1:12345'
        async def child(*args, **kwargs):
            nonlocal browser_output
            calls.append(args)
            if args[0] == 'node':
                browser_output = Path(args[3])
                (browser_output / 'validation-errors.json').write_text(json.dumps({'issues': [{'page': 'pages/a.page', 'location': 'pages/a.page#a', 'code': 'text-overflow', 'message': '浏览器内容溢出'}]}))
                raise ValueError('browser validation failed')
            native_output = Path(args[args.index('--native') + 2])
            self.assertNotEqual(native_output, browser_output)
            self.assertFalse((native_output / 'validation-errors.json').exists())
            raise ValueError('ModuleNotFoundError: missing native dependency')
        with patch('agent_service.pptd_exports._snapshot_renderer', server), patch('agent_service.pptd_exports._run_process', side_effect=child):
            with self.assertRaisesRegex(ValueError, 'missing native dependency') as failed:
                await export_document(self.store, 'demo', self.output, self.progress)
        self.assertNotIsInstance(failed.exception, ValidationError)
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.output.exists())

    async def test_real_pptx_pdf_preview_and_source_share_one_revision(self):
        async def progress(stage, message):
            self.events.append((stage, message))
            if stage == 25:
                self.store.edit('demo', {'pageIndex': 0, 'elementId': 'a', 'text': '生成期间的后续修改'}, 1)
        artifact = await export_document(self.store, 'demo', self.output, progress)
        self.assertEqual(artifact['revision'], 1)
        self.assertEqual(self.store.read('demo')['revision'], 2)
        self.assertEqual(artifact['previews'], ['slide-1.png'])
        self.assertEqual(artifact['pageCount'], 1)
        for name in ['presentation.pptx', 'presentation.pdf', 'slide-1.png', 'source.zip', 'export-report.json']:
            self.assertTrue((self.output / name).is_file(), name)
        self.assertFalse((self.output / 'metadata.json').exists())
        with zipfile.ZipFile(self.output / 'source.zip') as archive:
            page = archive.read('pages/a.page').decode()
            self.assertIn('公式与原始快照', page)
            self.assertNotIn('生成期间的后续修改', page)
        import pymupdf
        with pymupdf.open(self.output / 'presentation.pdf') as pdf:
            self.assertEqual(len(pdf), 1)
            self.assertIn('公式与原始快照', pdf[0].get_text())
            self.assertEqual(round(pdf[0].rect.width), 960)
        report = json.loads((self.output / 'export-report.json').read_text())
        self.assertEqual(report['revision'], 1)
        self.assertEqual(report['pdf']['pages'][0]['math'], 1)
        self.assertFalse(report['pdf']['rasterized'])
        self.assertEqual(report['ooxmlEvidence']['slides'][0]['nativeEquations'], 1)

if __name__ == '__main__': unittest.main()
