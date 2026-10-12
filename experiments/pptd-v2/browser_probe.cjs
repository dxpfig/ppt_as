/** Disposable end-to-end evidence: real browser edits must return to PPTD YAML. */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const runtimeModules = process.env.PPTD_NODE_MODULES || '/home/figodxp/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules';
const { chromium } = require(process.env.PPTD_NODE_MODULES ? path.join(process.env.PPTD_NODE_MODULES, 'playwright') : 'playwright');
const root = path.resolve(__dirname, '../..');
const source = path.join(__dirname, 'fixture');
const output = path.resolve(process.argv[2] || path.join(root, 'runtime/pptd-v2-spike/browser'));
const working = path.join(output, `run-${Date.now()}`, 'working');
const port = Number(process.env.PPTD_PROBE_PORT || 8891);
const url = `http://127.0.0.1:${port}`;
fs.mkdirSync(output, { recursive: true });

function hashes(folder, prefix = '') {
  return Object.fromEntries(fs.readdirSync(folder, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name)).flatMap(entry => {
    const file = path.join(folder, entry.name); const ref = prefix + entry.name;
    return entry.isDirectory() ? Object.entries(hashes(file, ref + '/')) : [[ref, crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')]];
  }));
}
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

(async () => {
  const initialHashes = hashes(source);
  const log = fs.openSync(path.join(output, 'server.log'), 'w');
  const server = spawn(path.join(root, '.venv/bin/python'), [path.join(__dirname, 'server.py'), '--input', path.join(source, 'probe.pptd'), '--workspace', working, '--port', String(port)], { cwd: root, stdio: ['ignore', log, log] });
  let browser;
  try {
    for (let attempt = 0; attempt < 100; attempt++) {
      if (server.exitCode !== null) throw new Error('Probe server exited; see server.log');
      try { if ((await fetch(url + '/api/document')).ok) break; } catch {}
      await delay(100);
    }
    // Verify we reached our newly created working copy, never reuse an unrelated server.
    assert.ok(fs.existsSync(path.join(working, 'probe.pptd')));
    browser = await chromium.launch({ executablePath: process.env.PPTD_CHROMIUM || '/snap/bin/chromium', headless: true, args: ['--no-sandbox'] });
    const page = await browser.newPage({ viewport: { width: 1450, height: 1000 }, deviceScaleFactor: 1 });
    const errors = []; const failedRequests = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('requestfailed', request => failedRequests.push({ url: request.url(), error: request.failure()?.errorText }));
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => document.body.dataset.loaded === 'true');
    assert.equal(await page.locator('#export-pptx').count(), 1);
    assert.equal(await page.locator('#export-pdf').count(), 1);
    const before = await (await page.request.get(url + '/api/document')).json();
    const expected = structuredClone(before);
    const fontResults = await page.evaluate(() => window.pptdProbe.state.fonts);
    assert.equal(await page.locator('[data-element-id="crop-image"]').getAttribute('data-image-loaded'), 'true');
    assert.equal(await page.locator('[data-element-id="crop-image"] image').getAttribute('width'), '800');
    const checks = ['two-page PPTD fixture loaded in real Chromium', 'local crop calibration image loaded'];
    async function saveTitle(pageIndex, text, x) {
      await page.locator('#page-select').selectOption(String(pageIndex));
      await page.waitForFunction(i => document.getElementById('slide').dataset.pageIndex === String(i) && document.getElementById('slide').dataset.ready === 'true', pageIndex);
      await page.locator('[data-element-id="title"]').click();
      await page.locator('#text-content').fill(text);
      if (x !== undefined) await page.locator('#bound-x').fill(String(x));
      const [response] = await Promise.all([page.waitForResponse(r => r.url().endsWith('/api/edit') && r.request().method() === 'POST'), page.locator('#save').click()]);
      const result = await response.json();
      assert.equal(response.status(), 200); assert.equal(result.verified, true);
      const title = expected.pages[pageIndex].data.elements.find(e => e.elementId === 'title');
      title.content.text = text; if (x !== undefined) title.bounds[0] = x;
      await page.locator('#clear-selection').click();
    }
    await saveTitle(0, 'PPTD v2：编辑后仍以源文件为准', 88);
    await saveTitle(1, '数据、公式与动画：保存后继续核对');
    checks.push('UI changed first title text/x and second title text; both writes verified by server');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => document.body.dataset.loaded === 'true');
    const after = await (await page.request.get(url + '/api/document')).json();
    assert.deepEqual(after, expected);
    checks.push('reload reads precisely the intended three field changes; all other document values retained');
    assert.deepEqual(after.pages[1].data.animations, before.pages[1].data.animations);
    assert.deepEqual(after.document.customFonts, before.document.customFonts);
    checks.push('animations/customFonts/formula/icon/table/chart/crop survive edit and reload');
    const geometry = await page.locator('[data-element-id="title"]').evaluate(el => ({ x: parseFloat(el.style.left), y: parseFloat(el.style.top), width: parseFloat(el.style.width), height: parseFloat(el.style.height) }));
    assert.deepEqual(geometry, { x: 88, y: 44, width: 840, height: 54 });
    checks.push('browser title geometry matches persisted PPTD bounds');
    await page.locator('#slide').screenshot({ path: path.join(output, 'slide-1.png') });
    await page.screenshot({ path: path.join(output, 'editor.png'), fullPage: true });
    await page.locator('#page-select').selectOption('1');
    await page.waitForFunction(() => document.getElementById('slide').dataset.pageIndex === '1' && document.getElementById('slide').dataset.ready === 'true');
    assert.ok(await page.locator('[data-element-id="chart-revenue"] svg path').count() >= 3);
    assert.equal(await page.locator('[data-element-id="formula"] .katex').count(), 1);
    assert.equal(await page.locator('[data-element-id="sample-icon"] svg[data-icon-name="fas:lightbulb"] path').count(), 1);
    assert.equal(await page.locator('[data-element-id="formula"][data-math-error]').count(), 0);
    checks.push('KaTeX formula, actual FA7 lightbulb path and ECharts SVG render in Chromium');
    const beforeAnimation = await page.locator('[data-element-id="title"]').getAttribute('style');
    await page.locator('#animation-play').click();
    await page.waitForFunction(() => window.pptdProbe.animationPlayer?.status.completed === true);
    assert.equal(await page.locator('[data-element-id="title"]').evaluate(n => getComputedStyle(n).opacity), '1');
    await page.locator('#animation-reset').click();
    assert.equal(await page.locator('[data-element-id="title"]').getAttribute('style'), beforeAnimation);
    assert.deepEqual((await (await page.request.get(url + '/api/document')).json()).pages[1].data.animations, before.pages[1].data.animations);
    checks.push('animation plays to completion and reset restores DOM without changing source declarations');
    const fontRequestUrls = await page.evaluate(() => performance.getEntriesByType('resource').filter(r => /\.(?:woff2?|ttf|otf)(?:$|\?)/i.test(r.name)).map(r => r.name));
    assert.ok(fontRequestUrls.length > 0);
    assert.ok(fontRequestUrls.every(value => value.startsWith(url + '/fonts/') || value.startsWith(url + '/runtime/fonts/')));
    checks.push('all browser font binaries are same-origin cached resources');
    await page.locator('#slide').screenshot({ path: path.join(output, 'slide-2.png') });
    const capabilities = await (await page.request.get(url + '/api/capabilities')).json();
    assert.match(capabilities.authority, /PPTD v2 YAML/);
    assert.deepEqual(Object.keys(capabilities.advanced).sort(), ['animations', 'charts', 'fonts', 'icons', 'math']);
    assert.deepEqual(hashes(source), initialHashes); assert.deepEqual(errors, []);
    checks.push('original fixture byte-identical; no browser JavaScript exceptions');
    const report = { status: 'passed', scope: 'fixture subset, not full PPTD compatibility or a finished WYSIWYG editor', browser: browser.version(), playwright: require(path.join(runtimeModules, 'playwright/package.json')).version, url, workingEntry: path.join(working, 'probe.pptd'), checks, geometry, fontResults, failedRequests, errors, capabilities, fixtureHashes: initialHashes };
    fs.writeFileSync(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify({ status: report.status, checks: checks.length, fontResults, workingEntry: report.workingEntry, report: path.join(output, 'browser-report.json') }));
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM'); fs.closeSync(log);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
