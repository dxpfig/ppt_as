/** Print the real PPTD browser layout, preserving text and vectors in PDF.
 * Usage: node export_pdf.cjs http://127.0.0.1:8892 output-directory
 * Produces slide-001.pdf... and pdf-pages.json; the BFF merges the pages.
 */
const fs = require('node:fs');
const path = require('node:path');
const modules = process.env.PPTD_NODE_MODULES || '/home/figodxp/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules';
const { chromium } = require(process.env.PPTD_NODE_MODULES ? path.join(process.env.PPTD_NODE_MODULES, 'playwright') : 'playwright');

async function main() {
  const url = process.argv[2];
  const output = process.argv[3] && path.resolve(process.argv[3]);
  if (!url || !output) throw new Error('Usage: node export_pdf.cjs URL output-directory');
  if (!/^https?:\/\//.test(url)) throw new Error('PDF source must be an HTTP(S) PPTD preview URL');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: process.env.PPTD_CHROMIUM || '/snap/bin/chromium', headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1100 }, deviceScaleFactor: 1 });
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForFunction(() => document.body.dataset.loaded === 'true' || document.body.dataset.error, null, { timeout: 120000 });
    const document = await page.evaluate(() => {
      if (window.document.body.dataset.error) throw new Error(window.document.body.dataset.error);
      return { title: window.pptdProbe.state.document.title, size: window.pptdProbe.state.document.size, count: window.pptdProbe.state.pages.length, fonts: window.pptdProbe.state.fonts, fontErrors: window.pptdProbe.state.fontWarnings };
    });
    if (document.fontErrors.length || document.fonts.some(font => font.status !== 'loaded')) throw new Error('Cannot print PDF with unresolved fonts: ' + document.fontErrors.join('; '));
    const [width, height] = document.size;
    if (![width, height].every(v => Number.isFinite(v) && v > 0)) throw new Error('Invalid PPTD page dimensions');
    await page.addStyleTag({ content: `
      @page { size: ${width}pt ${height}pt; margin: 0; }
      @media print {
        html, body { margin: 0 !important; padding: 0 !important; width: ${width}pt !important; height: ${height}pt !important; overflow: hidden !important; background: white; }
        header, .toolbar, aside, details, .canvas-meta, #warnings { display: none !important; }
        main, .stage-column { display: contents !important; }
        #slide { position: absolute !important; left: 0 !important; top: 0 !important; margin: 0 !important; box-shadow: none !important; transform: scale(1.3333333333333333); transform-origin: top left; }
        #slide .element.selected { outline: none !important; }
        * { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }
      }
    ` });
    const pages = [];
    for (let index = 0; index < document.count; index += 1) {
      const evidence = await page.evaluate(async (i) => {
        const probe = window.pptdProbe;
        probe.animation('reset'); probe.select(null); probe.state.pageIndex = i;
        await probe.render(); await window.document.fonts.ready;
        const slide = window.document.getElementById('slide');
        const invalid = [...slide.querySelectorAll('[data-unsupported],[data-math-error]')].map(n => `${n.dataset.elementId}: ${n.dataset.mathError || n.textContent}`);
        if (invalid.length) throw new Error('Cannot print unresolved PPTD elements: ' + invalid.join('; '));
        return { source: probe.state.pages[i].path, math: slide.querySelectorAll('[data-pptd-math]').length, icons: slide.querySelectorAll('[data-icon-name]').length, charts: slide.querySelectorAll('[data-element-type="chart"] svg').length, warnings: [...probe.state.fontWarnings, ...probe.state.warnings] };
      }, index);
      const filename = `slide-${String(index + 1).padStart(3, '0')}.pdf`;
      await page.pdf({ path: path.join(output, filename), width: `${width / 72}in`, height: `${height / 72}in`, margin: { top: '0px', right: '0px', bottom: '0px', left: '0px' }, preferCSSPageSize: true, printBackground: true, tagged: true });
      pages.push({ index, file: filename, widthPt: width, heightPt: height, ...evidence });
    }
    if (errors.length) throw new Error('Browser PDF failed: ' + errors.join('; '));
    const report = { status: 'passed', renderer: `Chromium ${browser.version()} vector print`, title: document.title, rasterized: false, fonts: document.fonts, pages, errors };
    fs.writeFileSync(path.join(output, 'pdf-pages.json'), JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report));
  } finally { await browser.close(); }
}
main().catch((error) => { console.error(error.stack || error.message); process.exitCode = 1; });
