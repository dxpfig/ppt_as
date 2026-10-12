import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const modules = process.env.PPTD_NODE_MODULES || '/home/figodxp/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules';
const { chromium } = require(process.env.PPTD_NODE_MODULES ? `${process.env.PPTD_NODE_MODULES}/playwright` : 'playwright');
let browser;
let source;
before(async () => {
  source = await readFile(new URL('../src/animations.js', import.meta.url), 'utf8').catch(() => 'export const createAnimationPlayer = undefined;');
  browser = await chromium.launch({ executablePath: process.env.PPTD_CHROMIUM || '/snap/bin/chromium', headless: true, args: ['--no-sandbox'] });
});
after(async () => { await browser?.close(); });
async function fixture(animations, styles = '') {
  const page = await browser.newPage();
  await page.setContent(`<div id="slide" style="position:relative;width:1000px;height:500px"><div data-element-id="a" style="position:absolute;left:100px;top:100px;width:100px;height:100px;background:rgb(0,0,255);${styles}">A</div><div data-element-id="b" style="position:absolute;left:300px;top:100px;width:100px;height:100px;background:rgb(0,255,0)">B</div></div>`);
  await page.evaluate(async ({ source, animations }) => {
    const { createAnimationPlayer } = await import(`data:text/javascript;base64,${btoa(unescape(encodeURIComponent(source)))}`);
    window.apiPresent = typeof createAnimationPlayer === 'function';
    if (!window.apiPresent) return;
    let now = 0, id = 0;
    const frames = new Map();
    window.tick = (time) => { now = time; const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(cb => cb(now)); };
    window.originalAnimations = JSON.stringify(animations);
    window.inputAnimations = animations;
    window.player = createAnimationPlayer(document.querySelector('#slide'), animations, { pageSize: [1000, 500], clock: { now: () => now, requestAnimationFrame: cb => { frames.set(++id, cb); return id; }, cancelAnimationFrame: id => frames.delete(id) } });
    window.read = (id = 'a') => { const node = document.querySelector(`[data-element-id="${id}"]`), s = getComputedStyle(node); return { opacity: Number(s.opacity), visibility: s.visibility, background: s.backgroundColor, transform: s.transform, clipPath: s.clipPath, status: player.status }; };
  }, { source, animations });
  assert.equal(await page.evaluate(() => window.apiPresent), true, 'animation player must be available');
  return page;
}

test('click groups respect delay, repeats, withPrevious start and afterPrevious end', async () => {
  const page = await fixture([
    { elementId: 'a', effect: 'fade-in', durationMs: 100, delayMs: 50, repeat: 2 },
    { elementId: 'b', effect: 'fade-in', trigger: 'withPrevious', durationMs: 200 },
    { elementId: 'a', effect: 'transparency', trigger: 'afterPrevious', amount: 0.2, durationMs: 100 },
    { elementId: 'b', effect: 'disappear' },
  ]);
  assert.equal((await page.evaluate(() => read())).visibility, 'hidden');
  await page.evaluate(() => { player.next(); tick(100); });
  assert.equal((await page.evaluate(() => read())).opacity, 0.5);
  assert.equal((await page.evaluate(() => read('b'))).opacity, 0.25);
  await page.evaluate(() => tick(300));
  assert.ok(Math.abs((await page.evaluate(() => read())).opacity - 0.6) < 0.001);
  await page.evaluate(() => tick(350));
  assert.equal((await page.evaluate(() => read())).opacity, 0.2);
  assert.equal((await page.evaluate(() => read('b'))).visibility, 'visible');
  await page.evaluate(() => player.next());
  assert.equal((await page.evaluate(() => read('b'))).visibility, 'hidden');
  assert.equal(await page.evaluate(() => JSON.stringify(inputAnimations) === originalAnimations), true);
  await page.close();
});

test('first withPrevious is automatic, colors compose, reset and destroy restore DOM', async () => {
  const page = await fixture([
    { elementId: 'a', effect: 'fill-color', color: 'FF0000', durationMs: 100, trigger: 'withPrevious' },
    { elementId: 'a', effect: 'color-pulse', color: '00FF00', durationMs: 100, trigger: 'afterPrevious' },
    { elementId: 'a', effect: 'transparency', amount: 0.3, durationMs: 100, trigger: 'afterPrevious' },
  ], 'transform:rotate(15deg)');
  await page.evaluate(() => tick(100));
  assert.equal((await page.evaluate(() => read())).background, 'rgb(255, 0, 0)');
  await page.evaluate(() => tick(150));
  assert.equal((await page.evaluate(() => read())).background, 'rgb(0, 255, 0)');
  await page.evaluate(() => tick(300));
  assert.equal((await page.evaluate(() => read())).background, 'rgb(255, 0, 0)');
  assert.equal((await page.evaluate(() => read())).opacity, 0.3);
  await page.evaluate(() => { player.reset(); tick(1000); });
  assert.equal((await page.evaluate(() => read())).background, 'rgb(0, 0, 255)');
  assert.equal((await page.evaluate(() => read())).opacity, 1);
  assert.match((await page.evaluate(() => read())).transform, /0\.965926/);
  await page.evaluate(() => { player.play(); tick(1050); player.destroy(); tick(5000); });
  assert.equal((await page.evaluate(() => read())).background, 'rgb(0, 0, 255)');
  await page.close();
});

const effects = [
  ['appear', 0], ['fade-in', 500], ['fly-in', 500], ['zoom-in', 500], ['wipe-in', 500], ['float-in', 500], ['peek-in', 500], ['rise-in', 1000],
  ['pulse', 600], ['grow-shrink', 2000], ['spin', 2000], ['teeter', 1000], ['fill-color', 2000], ['transparency', 2000], ['color-pulse', 2000],
  ['disappear', 0], ['fade-out', 500], ['fly-out', 500], ['zoom-out', 500], ['wipe-out', 500], ['float-out', 500], ['motion-path', 2000],
];
for (const [effect, duration] of effects) test(`${effect}: visible behavior and specified default duration`, async () => {
  const page = await fixture([{ elementId: 'a', effect, color: 'FF0000', amount: 0.25, path: 'M 0 0 L 200 -100' }]);
  await page.evaluate(() => player.play());
  if (duration) {
    await page.evaluate(time => tick(time), duration / 2);
    const current = await page.evaluate(() => read());
    assert.equal(current.status.playing, true);
    if (['fade-in', 'fade-out', 'float-in', 'float-out'].includes(effect)) assert.equal(current.opacity, 0.5);
    if (effect === 'transparency') assert.equal(current.opacity, 0.625);
    if (effect === 'color-pulse') assert.equal(current.background, 'rgb(255, 0, 0)');
    if (['fly-in', 'fly-out', 'zoom-in', 'zoom-out', 'float-in', 'float-out', 'rise-in', 'peek-in', 'grow-shrink', 'spin', 'pulse', 'motion-path'].includes(effect)) assert.notEqual(current.transform, 'none');
    if (effect === 'wipe-in' || effect === 'wipe-out') assert.notEqual(current.clipPath, 'none');
    await page.evaluate(time => tick(time), duration);
  }
  const end = await page.evaluate(() => read());
  assert.equal(end.status.playing, false);
  assert.equal(end.status.completed, true);
  if (effect.endsWith('-out') || effect === 'disappear') assert.equal(end.visibility, 'hidden');
  if (effect === 'grow-shrink') assert.match(end.transform, /matrix\(1.5, 0, 0, 1.5,/);
  if (effect === 'motion-path') assert.match(end.transform, /200, -100\)/);
  if (effect === 'fill-color') assert.equal(end.background, 'rgb(255, 0, 0)');
  if (effect === 'color-pulse') assert.equal(end.background, 'rgb(0, 0, 255)');
  await page.close();
});

test('direction, easing, repeated pulse and curved cumulative motion preserve semantics', async () => {
  const page = await fixture([
    { elementId: 'a', effect: 'fly-in', direction: 'right', durationMs: 100, easing: 'ease-in' },
    { elementId: 'a', effect: 'pulse', durationMs: 100, repeat: 2, trigger: 'afterPrevious' },
    { elementId: 'a', effect: 'motion-path', path: 'M 0 0 Q 100 -100 200 0', durationMs: 100, trigger: 'afterPrevious' },
    { elementId: 'a', effect: 'motion-path', path: 'M 0 0 C 0 0 50 50 100 100', durationMs: 100, trigger: 'afterPrevious' },
  ]);
  await page.evaluate(() => { player.play(); tick(50); });
  assert.ok(Number((await page.evaluate(() => read())).transform.split(',')[4]) < -100);
  await page.evaluate(() => tick(300));
  assert.match((await page.evaluate(() => read())).transform, /matrix\(1, 0, 0, 1, 0, 0\)/);
  await page.evaluate(() => tick(350));
  assert.match((await page.evaluate(() => read())).transform, /100, -50\)/);
  await page.evaluate(() => tick(500));
  assert.match((await page.evaluate(() => read())).transform, /300, 100\)/);
  await page.close();
});

test('invalid paths and unsupported effects fail with an element location', async () => {
  const page = await fixture([]);
  const errors = await page.evaluate(async (source) => {
    const { createAnimationPlayer } = await import(`data:text/javascript;base64,${btoa(unescape(encodeURIComponent(source)))}`);
    return ['M 1 0 L 2 3', 'M 0 0 M 2 3', 'M 0 0 A 20 20 0 0 1 10 10'].map(path => {
      try { createAnimationPlayer(document.querySelector('#slide'), [{ elementId: 'a', effect: 'motion-path', path }], { pageSize: [1000, 500] }); return ''; } catch (error) { return error.message; }
    });
  }, source);
  assert.ok(errors.every(error => /animations\[0\].*a.*path/.test(error)));
  await page.close();
});

test('real browser RAF progresses without an injected clock and SVG fill restores on cancel', async () => {
  const page = await browser.newPage();
  await page.setContent('<div id="slide" style="width:1000px;height:500px"><div data-element-id="shape"><svg width="100" height="100"><path data-animation-fill d="M0 0 H100 V100 H0 Z" fill="#0000ff"/></svg></div></div>');
  await page.evaluate(async source => {
    const { createAnimationPlayer } = await import(`data:text/javascript;base64,${btoa(unescape(encodeURIComponent(source)))}`);
    window.player = createAnimationPlayer(document.querySelector('#slide'), [{ elementId: 'shape', effect: 'fill-color', color: 'FF0000', durationMs: 80, trigger: 'afterPrevious' }], { pageSize: [1000,500] });
  }, source);
  await page.waitForFunction(() => player.status.completed, { timeout: 2000 });
  assert.equal(await page.locator('path').evaluate(node => getComputedStyle(node).fill), 'rgb(255, 0, 0)');
  await page.evaluate(() => player.destroy());
  assert.equal(await page.locator('path').evaluate(node => getComputedStyle(node).fill), 'rgb(0, 0, 255)');
  await page.close();
});

test('teeter moves around authored rotation; instantaneous effects ignore explicit duration', async () => {
  const page = await fixture([{elementId:'a',effect:'teeter',durationMs:100},{elementId:'a',effect:'disappear',durationMs:5000,trigger:'afterPrevious'}], 'transform:rotate(15deg)');
  await page.evaluate(() => { player.play(); tick(12.5); });
  assert.match((await page.evaluate(() => read())).transform, /0\.939693/); // cos(20 degrees)
  await page.evaluate(() => tick(100));
  assert.equal((await page.evaluate(() => read())).visibility,'hidden');
  assert.equal((await page.evaluate(() => read())).status.completed,true);
  await page.close();
});
