/** Render PPTD pages to print-sized PDFs for the `kimi-slides screenshot` CLI.
 *
 * This intentionally reuses the same Chromium print path as export_pdf.cjs but
 * skips the validation gate: screenshots are for the model to *see* pages, not
 * to block generation. Page PDFs are later rasterized to PNG/JPG by the caller.
 */
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PPTD_NODE_MODULES ? path.join(process.env.PPTD_NODE_MODULES, 'playwright') : 'playwright');

function parsePages(spec, count) {
  const all = Array.from({ length: count }, (_, i) => i + 1);
  if (!spec || spec === 'all') return all;
  const selected = new Set();
  for (const part of String(spec).split(',')) {
    const token = part.trim();
    if (!token) throw new Error('invalid page spec: ' + spec);
    if (token.includes('-')) {
      const [lo, hi] = token.split('-').map(Number);
      if (!(lo >= 1 && hi >= lo && hi <= count)) throw new Error('page out of range: ' + token);
      for (let n = lo; n <= hi; n += 1) selected.add(n);
    } else {
      const n = Number(token);
      if (!(n >= 1 && n <= count)) throw new Error('page out of range: ' + token);
      selected.add(n);
    }
  }
  return all.filter((n) => selected.has(n));
}

async function main() {
  const url = process.argv[2];
  const output = process.argv[3] && path.resolve(process.argv[3]);
  const spec = process.argv[4] || null;
  if (!url || !output) throw new Error('Usage: node screenshot.cjs URL output-directory [pages]');
  if (!/^http:\/\/127\.0\.0\.1:\d+\/?$/.test(url)) throw new Error('Screenshot source must be the isolated loopback snapshot');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: process.env.PPTD_CHROMIUM || undefined, headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1100 }, deviceScaleFactor: 1 });
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForFunction(() => document.body.dataset.loaded === 'true' || document.body.dataset.error, null, { timeout: 120000 });
    const document = await page.evaluate(() => {
      if (window.document.body.dataset.error) throw new Error(window.document.body.dataset.error);
      return { size: window.pptdWorkbench.state.document.size, count: window.pptdWorkbench.state.pages.length };
    });
    const [width, height] = document.size;
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
    for (const number of parsePages(spec, document.count)) {
      await page.evaluate((index) => {
        const probe = window.pptdWorkbench;
        probe.animation('reset'); probe.select(null); probe.state.pageIndex = index;
        return probe.render();
      }, number - 1);
      await page.evaluate(() => window.document.fonts.ready);
      const filename = `slide-${String(number).padStart(3, '0')}.pdf`;
      await page.pdf({ path: path.join(output, filename), width: `${width / 72}in`, height: `${height / 72}in`, margin: { top: '0px', right: '0px', bottom: '0px', left: '0px' }, preferCSSPageSize: true, printBackground: true });
      pages.push({ index: number, file: filename, widthPt: width, heightPt: height });
    }
    const report = { status: 'passed', title: document.title, pages };
    fs.writeFileSync(path.join(output, 'pdf-pages.json'), JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report));
  } finally {
    await browser.close();
  }
}

main().catch((error) => { console.error(error.stack || error.message); process.exitCode = 1; });
