/** Render the separately imported six-page PPTX through the same PPTD browser view. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const modules = process.env.PPTD_NODE_MODULES || '/home/figodxp/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules';
const { chromium } = require(process.env.PPTD_NODE_MODULES ? path.join(process.env.PPTD_NODE_MODULES, 'playwright') : 'playwright');
const root = path.resolve(__dirname, '../..');
const input = path.join(root, 'runtime/pptd-v2-spike/pptx-edited/imported/doc.pptd');
const output = path.join(root, 'runtime/pptd-v2-spike/import-browser');
const working = path.join(output, `run-${Date.now()}`, 'working');
const port = Number(process.env.PPTD_IMPORT_PORT || 8894);
const url = `http://127.0.0.1:${port}`;
fs.mkdirSync(output, { recursive: true });
(async () => {
  const log = fs.openSync(path.join(output, 'server.log'), 'w');
  const server = spawn(path.join(root, '.venv/bin/python'), [path.join(__dirname, 'server.py'), '--input', input, '--workspace', working, '--port', String(port)], { cwd: root, stdio: ['ignore', log, log] });
  let browser;
  try {
    for (let attempt = 0; attempt < 100; attempt++) {
      if (server.exitCode !== null) throw Error('Server exited; see server.log');
      try { const response = await fetch(url + '/api/identity'); if (response.ok && (await response.json()).workspace === working) break; } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.equal((await (await fetch(url + '/api/identity')).json()).workspace, working, 'Must use the newly started import workspace');
    browser = await chromium.launch({ executablePath: process.env.PPTD_CHROMIUM || '/snap/bin/chromium', headless: true, args: ['--no-sandbox'] });
    const page = await browser.newPage({ viewport: { width: 1450, height: 1000 }, deviceScaleFactor: 1 });
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => document.body.dataset.loaded === 'true');
    const document = await (await page.request.get(url + '/api/document')).json();
    assert.equal(document.pages.length, 6, "Independent import fixture contains six pages");
    const pages = [];
    for (let index = 0; index < document.pages.length; index++) {
      await page.locator('#page-select').selectOption(String(index));
      await page.waitForFunction(i => window.document.getElementById('slide').dataset.pageIndex === String(i) && window.document.getElementById('slide').dataset.ready === 'true', index);
      assert.equal(await page.locator('#slide [data-element-id]').count(), document.pages[index].data.elements.length);
      const unsupported = await page.locator('#slide [data-unsupported]').count();
      const warnings = await page.locator('#warning-list').innerText();
      await page.locator('#slide').screenshot({ path: path.join(output, `slide-${index+1}.png`) });
      pages.push({ page: index + 1, elements: document.pages[index].data.elements.length, unsupported, warnings });
    }
    assert.deepEqual(errors, []);
    const report = { status: 'rendered', scope: 'imported document loads and produces screenshots; no claim of visual fidelity', input, size: document.document.size, pages, errors };
    fs.writeFileSync(path.join(output, 'render-report.json'), JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify({ status: report.status, pages: pages.length, output }));
  } finally { if (browser) await browser.close(); server.kill('SIGTERM'); fs.closeSync(log); }
})().catch(error => { console.error(error); process.exitCode = 1; });
