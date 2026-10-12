import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdtemp, rm } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { build } from 'esbuild';
import { exportIconIndex, latexToMathML, renderIcon, renderMathInElement } from '../src/math-icons.js';

test('KaTeX emits structured fractions, scripts, roots, matrices and integrals', () => {
  for (const [latex, tag] of [[String.raw`\frac{1}{3}`, 'mfrac'], [String.raw`\sqrt{x}`, 'msqrt'], ['x_1^2', 'msubsup'], [String.raw`\begin{pmatrix}a&b\\c&d\end{pmatrix}`, 'mtable'], [String.raw`\int_0^1 x`, 'msubsup']]) {
    assert.match(latexToMathML(latex), new RegExp(`<${tag}`));
  }
  assert.throws(() => latexToMathML(String.raw`\unknown{x}`), /math|KaTeX|Undefined/);
  assert.throws(() => latexToMathML(String.raw`\frac{x}`));
});

test('Font Awesome free catalog contains three styles and renders original paths', () => {
  const index = exportIconIndex();
  assert.ok(Object.keys(index).length > 2000);
  for (const name of ['fas:house', 'far:heart', 'fab:github']) {
    const svg = renderIcon({ iconName: name, fill: { type: 'solid', color: '$primary' } }, { colors: { primary: '#123456' } });
    assert.match(svg, /<svg/);
    assert.match(svg, /#123456/);
    assert.ok(svg.includes(index[name].paths[0]));
    assert.ok(svg.includes(name));
  }
  assert.throws(() => renderIcon({ iconName: 'fas:missing-icon' }), /Unknown.*icon/);
  assert.equal(typeof renderMathInElement, 'function');
});

test('icons preserve radial gradients and support image fills as path fills', () => {
  const svg = renderIcon({ iconName: 'fas:heart', fill: { type: 'gradient', gradientType: 'radial', stops: [{ position: 0, color: '#ffffff' }, { position: 1, color: '#ff0000' }] } });
  assert.match(svg, /<radialGradient/);
  const image = renderIcon({ iconName: 'fas:heart', fill: { type: 'image', src: 'media/a.png', fit: { mode: 'contain' }, opacity: .5 } });
  assert.match(image, /<pattern/);
  assert.match(image, /xMidYMid meet/);
  assert.match(image, /opacity="0.5"/);
  assert.match(image, /media\/a.png/);
});

test('real browser lays out inline and table formulas without inheriting bold/link styles or losing text', async () => {
  const require = createRequire(import.meta.url);
  const modules = process.env.PPTD_NODE_MODULES || '/home/figodxp/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules';
  const { chromium } = require(process.env.PPTD_NODE_MODULES ? path.join(process.env.PPTD_NODE_MODULES, 'playwright') : 'playwright');
  const temp = await mkdtemp(path.join(os.tmpdir(), 'pptd-math-'));
  let browser;
  try {
    const bundle = await build({ entryPoints: [new URL('../src/index.js', import.meta.url).pathname], bundle: true, format: 'iife', globalName: 'PPTDMath', write: false });
    browser = await chromium.launch({ executablePath: process.env.PPTD_CHROMIUM || '/snap/bin/chromium', headless: true, args: ['--no-sandbox'] });
    const page = await browser.newPage();
    await page.setContent(String.raw`<!doctype html><main id="root" style="font-size:24px;color:rgb(12,34,56)"><p><strong>中文前缀 \(\frac{a}{b}\) 后缀</strong> <a href="https://example.org">链接</a></p><p style="text-align:center">\(\sqrt{x_1^2}\)</p><table><tr><td>表格 \(\int_0^1 x dx\)</td></tr></table></main>`);
    await page.addStyleTag({ path: require.resolve('katex/dist/katex.min.css') });
    await page.addScriptTag({ content: bundle.outputFiles[0].text });
    const result = await page.evaluate(() => {
      const count = PPTDMath.renderMathInElement(document.getElementById('root'));
      const formula = document.querySelector('[data-pptd-math]');
      return { count, second: PPTDMath.renderMathInElement(document.getElementById('root')), text: document.querySelector('strong').textContent, link: document.querySelector('a').getAttribute('href'), fontSize: getComputedStyle(formula).fontSize, color: getComputedStyle(formula).color, weight: getComputedStyle(formula).fontWeight, height: formula.getBoundingClientRect().height, table: document.querySelectorAll('td .katex').length, fractions: document.querySelectorAll('mfrac').length, align: getComputedStyle(document.querySelectorAll('p')[1]).textAlign };
    });
    assert.equal(result.count, 3); assert.equal(result.second, 0);
    assert.match(result.text, /^中文前缀 /); assert.match(result.text, / 后缀$/);
    assert.equal(result.link, 'https://example.org');
    assert.equal(result.fontSize, '24px'); assert.equal(result.color, 'rgb(12, 34, 56)'); assert.equal(result.weight, '400');
    assert.ok(result.height > 24); assert.equal(result.table, 1); assert.equal(result.fractions, 1); assert.equal(result.align, 'center');
    const invalid = await page.evaluate(() => {
      const root = document.createElement('div'); root.textContent = String.raw`before \(x\) then \(\invalidcommand{x}\)`; document.body.append(root);
      const before = root.innerHTML;
      try { PPTDMath.renderMathInElement(root); return { threw: false }; } catch { return { threw: true, unchanged: root.innerHTML === before }; }
    });
    assert.deepEqual(invalid, { threw: true, unchanged: true });
    const routed = await page.evaluate(() => {
      const root = document.getElementById('root'); root.dataset.fontRouting = JSON.stringify({ latin: 'Arial', ea: 'Noto Sans CJK SC' });
      const beforeMath = document.querySelector('.katex').outerHTML;
      PPTDMath.routeFontScripts(root);
      return { ea: [...root.querySelectorAll('[data-pptd-font-script="ea"]')].map((n) => [n.textContent, n.style.fontFamily]), latin: [...root.querySelectorAll('[data-pptd-font-script="latin"]')].map((n) => [n.textContent, n.style.fontFamily]), unchangedMath: beforeMath === document.querySelector('.katex').outerHTML, link: root.querySelector('a').getAttribute('href') };
    });
    assert.ok(routed.ea.some(([text, family]) => text.includes('中文') && family.includes('Noto Sans CJK SC')));
    assert.ok(routed.latin.some(([, family]) => family.includes('Arial')));
    assert.equal(routed.unchangedMath, true); assert.equal(routed.link, 'https://example.org');
  } finally { if (browser) await browser.close(); await rm(temp, { recursive: true, force: true }); }
});
