/** Print the actual PPTD browser layout; report located content failures. */
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PPTD_NODE_MODULES ? path.join(process.env.PPTD_NODE_MODULES, 'playwright') : 'playwright');

async function main() {
  const url = process.argv[2];
  const output = process.argv[3] && path.resolve(process.argv[3]);
  if (!url || !output) throw new Error('Usage: node export_pdf.cjs URL output-directory');
  if (!/^http:\/\/127\.0\.0\.1:\d+\/?$/.test(url)) throw new Error('PDF source must be the isolated loopback snapshot');
  fs.mkdirSync(output, { recursive: true });
  function reject(issues, previews = [], layouts = {}) {
    fs.writeFileSync(path.join(output, 'validation-errors.json'), JSON.stringify({ issues, previews, layouts }, null, 2));
    throw new Error('PPTD content validation failed: ' + issues.map(i => `${i.location}: ${i.message}`).join('; '));
  }
  const browser = await chromium.launch({ executablePath: process.env.PPTD_CHROMIUM || undefined, headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1100 }, deviceScaleFactor: 1 });
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForFunction(() => document.body.dataset.loaded === 'true' || document.body.dataset.error, null, { timeout: 120000 });
    const document = await page.evaluate(() => {
      if (window.document.body.dataset.error) throw new Error(window.document.body.dataset.error);
      const state = window.pptdWorkbench.state;
      return { title: state.document.title, size: state.document.size, count: state.pages.length, pagePaths: state.pages.map(p => p.path), fonts: state.fonts, fontErrors: state.fontWarnings, fontIssues: state.fontIssues };
    });
    if (document.fontErrors.length || document.fonts.some(font => font.status !== 'loaded')) {
      const detailed = document.fontIssues.length ? document.fontIssues : document.fontErrors.map(error => ({ error, pages: document.pagePaths }));
      reject(detailed.flatMap(issue => (issue.pages?.length ? issue.pages : document.pagePaths).flatMap(page => (issue.elementIds?.length ? issue.elementIds : [null]).map(elementId => ({ page, ...(elementId ? { elementId } : {}), location: issue.location || (elementId ? `${page}#${elementId}` : page), code: 'font-unresolved', severity: 'error', message: `${issue.family || '字体'}: ${issue.error || issue.message}` })))));
    }
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
    const pages = [], allIssues = [], previews = [], layouts = {};
    for (let index = 0; index < document.count; index += 1) {
      const evidence = await page.evaluate(async (i) => {
        const probe = window.pptdWorkbench;
        probe.animation('reset'); probe.select(null); probe.state.pageIndex = i;
        await probe.render(); await window.document.fonts.ready;
        const slide = window.document.getElementById('slide');
        const source = probe.state.pages[i].path;
        const issues = [];
        const fail = (node, code, message, measurements = {}) => {
          const elementId = node?.closest('[data-element-id]')?.dataset.elementId;
          const element = probe.state.pages[i].data.elements.find(e => e.elementId === elementId);
          issues.push({ page: source, elementId, location: elementId ? `${source}#${elementId}` : source, code, severity: 'error', message,
            layout: { canvasSize: probe.state.document.size, bounds: element?.bounds, ...measurements } });
        };
        for (const node of slide.querySelectorAll('[data-unsupported],[data-math-error]')) {
          fail(node, node.dataset.mathError ? 'math-render' : 'unsupported-element', node.dataset.mathError || node.textContent);
        }
        const textLayout = [];
        for (const rich of slide.querySelectorAll('.rich-text')) {
          const box = rich.parentElement;
          const css = getComputedStyle(box);
          const horizontal = parseFloat(css.paddingLeft) + parseFloat(css.paddingRight);
          const vertical = parseFloat(css.paddingTop) + parseFloat(css.paddingBottom);
          const roomW = box.clientWidth - horizontal, roomH = box.clientHeight - vertical;
          const host = box.closest('[data-element-id]');
          if (host?.dataset.elementType === 'text') textLayout.push({
            elementId: host.dataset.elementId,
            bounds: probe.state.pages[i].data.elements.find(e => e.elementId === host.dataset.elementId)?.bounds,
            availableSize: [roomW, roomH], measuredTextSize: [rich.scrollWidth, rich.scrollHeight],
            paddingSize: [horizontal, vertical], fontSize: getComputedStyle(rich).fontSize,
            lineHeight: getComputedStyle(rich).lineHeight,
          });
          if (rich.scrollWidth > roomW + 2 || rich.scrollHeight > roomH + 2) {
            fail(box, 'text-overflow', `文本实际尺寸 ${rich.scrollWidth}×${rich.scrollHeight}px 超出可用空间 ${roomW}×${roomH}px；请保留完整文字，扩大文本框、调整布局或字号。`,
              { availableSize: [roomW, roomH], measuredTextSize: [rich.scrollWidth, rich.scrollHeight], paddingSize: [horizontal, vertical] });
          }
        }
        // Element rectangles include deliberate whitespace and layered card
        // backgrounds. Compare visible text fragments from distinct text
        // elements instead; rotated text and math need visual-model review.
        const textElements = [];
        const scale = slide.getBoundingClientRect().width / slide.offsetWidth;
        for (const element of probe.state.pages[i].data.elements) {
          if (element.elementType !== 'text' || (element.rotation || 0) % 360 !== 0 || element.flip?.some(Boolean)) continue;
          const node = Array.from(slide.children).find(n => n.dataset.elementId === element.elementId);
          const rich = node?.querySelector('.rich-text');
          if (!rich || Number(getComputedStyle(node).opacity) === 0) continue;
          const fragments = [];
          const walker = window.document.createTreeWalker(rich, NodeFilter.SHOW_TEXT);
          while (walker.nextNode()) {
            const text = walker.currentNode;
            const parent = text.parentElement;
            if (!parent || parent.closest('[data-pptd-math], .katex, svg')) continue;
            const css = getComputedStyle(parent);
            if (css.visibility === 'hidden' || css.display === 'none' || Number(css.opacity) === 0 || css.color === 'rgba(0, 0, 0, 0)') continue;
            for (const match of text.textContent.matchAll(/\S+/g)) {
              const range = window.document.createRange();
              range.setStart(text, match.index);
              range.setEnd(text, match.index + match[0].length);
              for (const rect of range.getClientRects()) {
                if (rect.width > 0 && rect.height > 0) fragments.push(rect);
              }
            }
          }
          if (fragments.length) textElements.push({ node, elementId: element.elementId, fragments });
        }
        for (let current = 1; current < textElements.length; current += 1) {
          const right = textElements[current];
          for (let previous = 0; previous < current; previous += 1) {
            const left = textElements[previous];
            const intersects = left.fragments.some(a => right.fragments.some(b => {
              const width = Math.min(a.right, b.right) - Math.max(a.left, b.left);
              const height = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
              return width > 3 * scale && height > Math.min(a.height, b.height) * .35 &&
                width * height > Math.min(a.width * a.height, b.width * b.height) * .08;
            }));
            if (intersects) {
              fail(right.node, 'text-overlap', `可见文字与元素 ${left.elementId} 的文字发生明显重叠；请调整两个文本框的位置、宽度或排版，保留原始文字和数据。`);
              issues[issues.length - 1].relatedElementId = left.elementId;
            }
          }
        }
        const normalized = (value) => value.normalize('NFKC').replace(/\s/g, '');
        for (const element of probe.state.pages[i].data.elements) {
          const node = Array.from(slide.children).find(n => n.dataset.elementId === element.elementId);
          if (!node) { issues.push({ page: source, elementId: element.elementId, location: `${source}#${element.elementId}`, code: 'content-missing', severity: 'error', message: '元素未出现在实际渲染结果中' }); continue; }
          if (node.querySelector('.rich-text')) {
            const box = node.getBoundingClientRect(), canvas = slide.getBoundingClientRect();
            if (box.left < canvas.left - 1 || box.top < canvas.top - 1 || box.right > canvas.right + 1 || box.bottom > canvas.bottom + 1) fail(node, 'text-clipped', '文字或表格超出幻灯片边界，导出时会被裁切；请移动或缩小文本框。');
          }
          if ((element.elementType === 'text' || element.content?.text || element.elementType === 'table') && !node.dataset.unsupported) {
            const original = String(element.elementType === 'table' ? (element.rows || []).flat().map(cell => cell.text || '').join('') : (element.content?.text || '')).replace(/\\\([\s\S]*?\\\)/g, '');
            const parsed = new DOMParser().parseFromString(original, 'text/html');
            const expected = normalized(parsed.body.textContent || '');
            const visible = node.cloneNode(true);
            for (const math of visible.querySelectorAll('[data-pptd-math]')) math.remove();
            if (expected && !normalized(visible.textContent || '').includes(expected)) fail(node, 'content-missing', '原始文本未完整出现在渲染结果中，请移除不支持的富文本标签。');
          }
        }
        return { source, textLayout, math: slide.querySelectorAll('[data-pptd-math]').length, icons: slide.querySelectorAll('[data-icon-name]').length, charts: slide.querySelectorAll('[data-element-type="chart"] svg').length, warnings: [...probe.state.fontWarnings, ...probe.state.warnings], issues };
      }, index);
      if (evidence.issues.length) {
        layouts[evidence.source] = { canvasSize: document.size, elements: evidence.textLayout };
        allIssues.push(...evidence.issues);
        const file = `layout-${index + 1}.png`;
        await page.locator('#slide').screenshot({ path: path.join(output, file) });
        previews.push({ page: evidence.source, file });
        continue;
      }
      const filename = `slide-${String(index + 1).padStart(3, '0')}.pdf`;
      await page.pdf({ path: path.join(output, filename), width: `${width / 72}in`, height: `${height / 72}in`, margin: { top: '0px', right: '0px', bottom: '0px', left: '0px' }, preferCSSPageSize: true, printBackground: true, tagged: true });
      pages.push({ index, file: filename, widthPt: width, heightPt: height, ...evidence });
    }
    if (allIssues.length) reject(allIssues, previews, layouts);
    if (errors.length) throw new Error('Browser PDF failed: ' + errors.join('; '));
    const report = { status: 'passed', renderer: `Chromium ${browser.version()} vector print`, title: document.title, rasterized: false, fonts: document.fonts, pages, errors };
    fs.writeFileSync(path.join(output, 'pdf-pages.json'), JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report));
  } finally { await browser.close(); }
}
main().catch((error) => { console.error(error.stack || error.message); process.exitCode = 1; });
