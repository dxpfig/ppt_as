const assert = require('node:assert/strict');
const { before, after, test } = require('node:test');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');

// Exercise the real viewer and runtime; only the saved-document API is a fixture.
const root = path.resolve(__dirname, '../..');
const projectId = 'viewer-interaction-fixture';
const pages = [0, 1, 2].map((index) => ({
  path: `pages/${index + 1}.page`,
  data: {
    pageType: 'content',
    elements: [0, 1].map((item) => ({
      elementId: `title-${item}`,
      elementType: 'text',
      bounds: [80, 80 + item * 100, 800, 60],
      content: { text: `Page ${index + 1}, item ${item + 1}`, fontSize: 32 },
    })),
    animations: index === 0 ? [0, 1].map((item) => ({
      elementId: `title-${item}`, effect: 'appear', trigger: 'onClick',
    })) : [],
  },
}));
// Page 2 carries the vector artwork a real company template imports: a custom
// freeform path, a preset with adjustments, and a native gradient fill.
pages[1].data.elements.push(
  { elementId: 'art-custom', elementType: 'shape', shapeName: 'custom', bounds: [80, 300, 60, 60],
    viewBox: [576, 512], path: 'M 361 1 C 344 -4 326 6 321 23 L 383 41 Z',
    fill: { type: 'solid', color: '#E7201A' }, border: { width: 1, color: '#333333', style: 'solid' } },
  { elementId: 'art-chevron', elementType: 'shape', shapeName: 'chevron', bounds: [200, 300, 160, 50],
    adjustments: [50000],
    fill: { type: 'gradient', gradientType: 'linear', angle: 90,
      stops: [{ position: 0, color: '#FE4444' }, { position: 1, color: '#83A0FF' }] } },
  { elementId: 'art-homePlate', elementType: 'shape', shapeName: 'homePlate', bounds: [400, 300, 160, 50],
    fill: { type: 'solid', color: '#1783FF' } },
);
// Page 3 carries the trailing empty paragraph an imported title placeholder
// uses to lift a centred title above its subtitle.
pages[2].data.elements.push({
  elementId: 'spaced-title', elementType: 'text', bounds: [80, 380, 800, 130],
  content: { align: ['left', 'middle'],
    text: '<p style="text-align:center;line-height:1.2"><span style="font-size:30.0px;color:#111111">\u6807\u9898</span></p>'
      + '<p style="text-align:center;line-height:1.2"></p>' },
});
let server, browser, origin;

before(async () => {
  const template = await fs.readFile(path.join(root, 'pptd_engine/web/index.html'), 'utf8');
  server = http.createServer(async (request, response) => {
    const url = new URL(request.url, 'http://localhost');
    const json = (value) => { response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify(value)); };
    if (url.pathname === '/api/document') return json({ revision: 1, document: { title: 'Viewer fixture', size: [960, 540] }, pages });
    if (url.pathname === '/api/capabilities') return json({});
    if (url.pathname === '/api/fonts') return json({ fonts: [], errors: [] });
    if (url.pathname.startsWith('/runtime/')) {
      const filename = path.basename(url.pathname);
      response.setHeader('Content-Type', filename.endsWith('.css') ? 'text/css' : 'text/javascript');
      response.end(await fs.readFile(path.join(root, 'packages/pptd-runtime/build', filename)));
      return;
    }
    response.setHeader('Content-Type', 'text/html');
    if (url.pathname === '/viewer' || url.pathname === '/standalone') {
      response.end(template.replaceAll('__PPTD_RUNTIME__', '/runtime').replace('__PPTD_CONFIG__', JSON.stringify({ apiBase: '/api', projectId, embed: url.pathname === '/viewer' })));
      return;
    }
    response.end(`<!doctype html><html><body style="margin:0"><iframe title="viewer" src="/viewer?mode=${url.searchParams.get('mode') === 'edit' ? 'edit' : 'preview'}" style="display:block;border:0;width:950px;height:280px"></iframe><script>window.viewerEvents=[];addEventListener('message',event=>viewerEvents.push(event.data));</script></body></html>`);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ executablePath: process.env.PPTD_CHROMIUM || process.env.CHROMIUM_PATH || '/snap/bin/chromium', headless: true, args: ['--no-sandbox'] });
});
after(async () => {
  await browser?.close();
  if (server) await new Promise((resolve) => server.close(resolve));
});

async function openViewer(mode = 'preview') {
  const page = await browser.newPage({ viewport: { width: 1366, height: 768 } });
  await page.goto(`${origin}/?mode=${mode}`);
  const frame = page.frames().find((item) => item.url().includes('/viewer'));
  await frame.waitForFunction(() => document.body.dataset.loaded === 'true');
  return { page, frame };
}
async function send(page, message) {
  await page.evaluate((data) => document.querySelector('iframe').contentWindow.postMessage(data, location.origin), { projectId, ...message });
}
async function assertPage(frame, index) {
  await frame.waitForFunction((value) => document.getElementById('slide').dataset.pageIndex === String(value) && document.getElementById('slide').dataset.ready === 'true', index, { timeout: 2000 });
}
async function geometry(frame) {
  return frame.evaluate(() => {
    const rect = document.getElementById('slide').getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, viewportWidth: innerWidth, viewportHeight: innerHeight, scrollWidth: document.documentElement.scrollWidth, scrollHeight: document.documentElement.scrollHeight };
  });
}
function assertFullSlide(box) {
  assert.ok(box.x >= -1 && box.y >= -1 && box.x + box.width <= box.viewportWidth + 1 && box.y + box.height <= box.viewportHeight + 1, `Full slide must fit the viewport: ${JSON.stringify(box)}`);
  assert.ok(Math.abs(box.width / box.height - 16 / 9) < 0.01);
  assert.ok(box.scrollWidth <= box.viewportWidth && box.scrollHeight <= box.viewportHeight);
}

test('embedded preview fits width and height, centers slides, and removes duplicate controls', async () => {
  const { page, frame } = await openViewer();
  try {
    assertFullSlide(await geometry(frame));
    assert.equal(await frame.locator('.toolbar').isVisible(), false);
    assert.equal(await frame.locator('#warnings').isVisible(), false);
    let box = await geometry(frame);
    assert.ok(Math.abs(box.x - (950 - box.width) / 2) < 2);
    assert.ok(Math.abs(box.y - (280 - box.height) / 2) < 2);
    await page.locator('iframe').evaluate((node) => { node.style.width = '300px'; node.style.height = '420px'; });
    await frame.waitForFunction(() => document.getElementById('slide').getBoundingClientRect().width < 300);
    box = await geometry(frame);
    assertFullSlide(box);
    assert.ok(Math.abs(box.y - (420 - box.height) / 2) < 2);
  } finally { await page.close(); }
});

test('preview wheel changes one page per gesture, preserves zoom and horizontal gestures, and reports selection', async () => {
  const { page, frame } = await openViewer();
  const wheel = (options) => frame.locator('#slide').dispatchEvent('wheel', { deltaY: 120, bubbles: true, cancelable: true, ...options });
  try {
    await wheel({ ctrlKey: true }); await wheel({ metaKey: true }); await wheel({ deltaX: 140, deltaY: 10 });
    assert.equal(await frame.locator('#slide').getAttribute('data-page-index'), '0');
    await wheel({ deltaY: 10 });
    assert.equal(await frame.locator('#slide').getAttribute('data-page-index'), '0');
    await wheel({ deltaY: 120 });
    await assertPage(frame, 1);
    await wheel({ deltaY: 120 }); await wheel({ deltaY: 120 });
    assert.equal(await frame.locator('#slide').getAttribute('data-page-index'), '1');
    await page.waitForFunction(() => viewerEvents.some((event) => event.type === 'pptd:page-selected' && event.page === 'pages/2.page'));
    await frame.waitForTimeout(400);
    await wheel({ deltaY: -120 });
    await assertPage(frame, 0);
  } finally { await page.close(); }
});

test('presentation honors animation steps before page advance, keyboard navigation, and exit', async () => {
  const { page, frame } = await openViewer();
  try {
    await page.locator('iframe').evaluate((node) => { node.style.width = '1366px'; node.style.height = '768px'; });
    await send(page, { type: 'pptd:presentation', active: true });
    await frame.waitForFunction(() => getComputedStyle(document.querySelector('[data-element-id="title-0"]')).visibility === 'hidden', null, { timeout: 2000 });
    assertFullSlide(await geometry(frame));
    assert.ok((await geometry(frame)).width > 1300, 'Presentation uses the available fullscreen area');
    await send(page, { type: 'pptd:navigate', direction: 1 });
    await frame.waitForFunction(() => getComputedStyle(document.querySelector('[data-element-id="title-0"]')).visibility === 'visible');
    assert.equal(await frame.locator('#slide').getAttribute('data-page-index'), '0');
    await frame.locator('#slide').click({ position: { x: 20, y: 20 } });
    await frame.waitForFunction(() => getComputedStyle(document.querySelector('[data-element-id="title-1"]')).visibility === 'visible');
    assert.equal(await frame.locator('#slide').getAttribute('data-page-index'), '0');
    await frame.locator('#slide').press('ArrowRight');
    await assertPage(frame, 1);
    await frame.locator('#slide').press('End'); await assertPage(frame, 2);
    await frame.locator('#slide').press('PageUp'); await assertPage(frame, 1);
    await frame.locator('#slide').press('Home'); await assertPage(frame, 0);
    await frame.locator('#slide').press('Escape');
    await page.waitForFunction(() => viewerEvents.some((event) => event.type === 'pptd:exit-presentation'));
    assert.equal(await frame.locator('[data-element-id="title-0"]').evaluate((node) => getComputedStyle(node).visibility), 'visible');
  } finally { await page.close(); }
});

test('viewer rejects foreign messages and leaves editable controls and non-embedded export scale intact', async () => {
  const { page, frame } = await openViewer('edit');
  try {
    await send(page, { type: 'pptd:select-page', projectId: 'other-project', page: 'pages/3.page' });
    await frame.evaluate(() => window.dispatchEvent(new MessageEvent('message', { origin: 'https://untrusted.example', source: window.parent, data: { type: 'pptd:select-page', projectId: 'viewer-interaction-fixture', page: 'pages/3.page' } })));
    await frame.evaluate(() => window.dispatchEvent(new MessageEvent('message', { origin: location.origin, source: window, data: { type: 'pptd:select-page', projectId: 'viewer-interaction-fixture', page: 'pages/3.page' } })));
    assert.equal(await frame.locator('#slide').getAttribute('data-page-index'), '0');
    assert.equal(await frame.locator('aside').isVisible(), true);
    assert.equal(await frame.locator('.toolbar').isVisible(), true);
    await frame.locator('#text-content').dispatchEvent('wheel', { deltaY: 120, bubbles: true });
    assert.equal(await frame.locator('#slide').getAttribute('data-page-index'), '0');
    await page.locator('iframe').evaluate((node) => { node.style.width = '600px'; node.style.height = '480px'; });
    await frame.waitForTimeout(100);
    assertFullSlide(await geometry(frame));
    await page.goto(`${origin}/standalone?mode=preview`);
    await page.waitForFunction(() => document.body.dataset.loaded === 'true');
    const dimensions = await page.locator('#slide').boundingBox();
    assert.equal(dimensions.width, 960);
    assert.equal(dimensions.height, 540);
  } finally { await page.close(); }
});

test('custom freeform, preset and gradient shapes render as vectors, not placeholders', async () => {
  const { page, frame } = await openViewer();
  try {
    await send(page, { type: 'pptd:select-page', projectId, page: 'pages/2.page' });
    await assertPage(frame, 1);
    for (const id of ['art-custom', 'art-chevron', 'art-homePlate']) {
      const node = frame.locator(`[data-element-id="${id}"]`);
      assert.equal(await node.count(), 1);
      assert.equal(await node.getAttribute('data-unsupported'), null, `${id} must not be a placeholder`);
      assert.equal(await node.locator('svg path').count(), 1, `${id} must render one path`);
    }
    assert.ok((await frame.locator('[data-element-id="art-custom"] svg path').getAttribute('d')).startsWith('M 361 1'));
    assert.equal(await frame.locator('[data-element-id="art-chevron"] svg linearGradient').count(), 1);
    assert.equal(await frame.locator('[data-element-id="art-chevron"] svg stop').count(), 2);
    assert.equal(await frame.locator('#warnings').isVisible(), false);
  } finally { await page.close(); }
});

test('an empty paragraph keeps a full line box so a centred title keeps its spacing', async () => {
  const { page, frame } = await openViewer();
  try {
    await send(page, { type: 'pptd:select-page', projectId, page: 'pages/3.page' });
    await assertPage(frame, 2);
    // The preview scales the slide, so compare paragraph heights with each
    // other instead of against an absolute pixel count.
    const heights = await frame.locator('[data-element-id="spaced-title"] p').evaluateAll(
      (nodes) => nodes.map((node) => node.getBoundingClientRect().height));
    assert.equal(heights.length, 2);
    assert.ok(heights[1] > 0, 'the trailing empty paragraph must keep a line box');
    // The host sizes these trailing lines by writing the slot's span inside
    // them; this only guards that the renderer gives an empty paragraph a line
    // box at all instead of collapsing it to zero height.
    assert.ok(heights[1] > heights[0] * 0.4,
      `empty paragraph must occupy a line, got ${heights.join(' vs ')}`);
  } finally { await page.close(); }
});
