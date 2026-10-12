/** Repeatable UI acceptance for the comprehensive advanced PPTD fixture.
 * Usage: node advanced_browser_probe.cjs [http://127.0.0.1:8893] [output-directory]
 */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const modules = process.env.PPTD_NODE_MODULES || '/home/figodxp/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules';
const { chromium } = require(process.env.PPTD_NODE_MODULES ? path.join(process.env.PPTD_NODE_MODULES, 'playwright') : 'playwright');

async function main() {
  const url = process.argv[2] || 'http://127.0.0.1:8893';
  const output = path.resolve(process.argv[3] || 'runtime/pptd-advanced/browser-all');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: process.env.PPTD_CHROMIUM || '/snap/bin/chromium', headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1100 }, deviceScaleFactor: 1 });
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => document.body.dataset.loaded === 'true', null, { timeout: 120000 });
    const source = await (await page.request.get(url + '/api/document')).json();
    const fonts = await page.evaluate(() => ({ fonts: window.pptdProbe.state.fonts, errors: window.pptdProbe.state.fontWarnings }));
    assert.deepEqual(fonts.errors, []); assert.ok(fonts.fonts.every(f => f.status === 'loaded'));
    const pages = [];
    for (let index = 0; index < source.pages.length; index += 1) {
      await page.locator('#page-select').selectOption(String(index));
      await page.waitForFunction(i => document.getElementById('slide').dataset.pageIndex === String(i) && document.getElementById('slide').dataset.ready === 'true', index);
      await page.evaluate(() => document.fonts.ready);
      const evidence = await page.locator('#slide').evaluate(node => ({ math: node.querySelectorAll('[data-pptd-math]').length, icons: node.querySelectorAll('[data-icon-name]').length, charts: node.querySelectorAll('[data-element-type="chart"] svg').length, errors: [...node.querySelectorAll('[data-unsupported],[data-math-error]')].map(n => n.textContent), text: node.innerText }));
      assert.deepEqual(evidence.errors, [], `page ${index + 1} has an unresolved element`);
      const data = source.pages[index].data;
      assert.equal(evidence.icons, data.elements.filter(e => e.elementType === 'icon').length);
      assert.equal(evidence.charts, data.elements.filter(e => e.elementType === 'chart').length);
      const expectedMath = (JSON.stringify(data.elements).match(/\\\\\(/g) || []).length;
      assert.equal(evidence.math, expectedMath);
      if (data.animations?.length) {
        const original = await page.locator('#slide').evaluate(n => [...n.children].map(e => e.getAttribute('style')));
        await page.locator('#animation-play').click();
        const groups = await page.evaluate(() => window.pptdProbe.animationPlayer.status.groupCount);
        for (let i = 0; i < groups + 1; i += 1) await page.locator('#animation-next').click();
        assert.equal(await page.evaluate(() => window.pptdProbe.animationPlayer.status.completed), true);
        await page.locator('#animation-reset').click();
        assert.deepEqual(await page.locator('#slide').evaluate(n => [...n.children].map(e => e.getAttribute('style'))), original);
      }
      const file = `slide-${String(index + 1).padStart(3, '0')}.png`;
      await page.locator('#slide').screenshot({ path: path.join(output, file) });
      pages.push({ index, source: source.pages[index].path, screenshot: file, math: evidence.math, icons: evidence.icons, charts: evidence.charts, animations: data.animations?.length || 0 });
    }
    const exports = {};
    for (const format of ['pptx', 'pdf']) {
      const endpoint = format === 'pdf' ? '/api/export-pdf' : '/api/export';
      const [response] = await Promise.all([page.waitForResponse(r => r.url().endsWith(endpoint) && r.request().method() === 'POST', { timeout: 180000 }), page.locator('#export-' + format).click()]);
      const result = await response.json();
      assert.equal(response.status(), 200, JSON.stringify(result));
      const binary = await page.request.get(url + result.url);
      assert.equal(binary.status(), 200);
      fs.writeFileSync(path.join(output, 'ui-export.' + format), await binary.body());
      const report = await page.request.get(url + result.reportUrl);
      assert.equal(report.status(), 200);
      fs.writeFileSync(path.join(output, 'ui-export-' + format + '-report.json'), await report.body());
      exports[format] = { url: result.url, reportUrl: result.reportUrl, bytes: (await binary.body()).length };
    }
    assert.deepEqual(await (await page.request.get(url + '/api/document')).json(), source);
    assert.deepEqual(errors, []);
    const report = { status: 'passed', browser: browser.version(), pages, fonts, exports, errors, sourceUnchanged: true, totals: pages.reduce((a, p) => ({ math: a.math + p.math, icons: a.icons + p.icons, charts: a.charts + p.charts, animations: a.animations + p.animations }), { math: 0, icons: 0, charts: 0, animations: 0 }) };
    fs.writeFileSync(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify({ status: report.status, pages: pages.length, totals: report.totals, exports, output }));
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });
