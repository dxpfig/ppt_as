/** Shared strict TeX parser and Font Awesome 7 Free source paths.
 * Browser callers must load katex/dist/katex.min.css (including its fonts).
 * CLI: node math-icons.js --icons, or pipe TeX into --mathml.
 */
import katex from 'katex';
import { fas } from '@fortawesome/free-solid-svg-icons';
import { far } from '@fortawesome/free-regular-svg-icons';
import { fab } from '@fortawesome/free-brands-svg-icons';

const escapeXml = (value) => String(value).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' })[c]);
const mathOptions = {
  throwOnError: true,
  trust: false,
  strict: (code) => code === 'unknownSymbol' || code === 'unicodeTextInMathMode' ? 'ignore' : 'error',
  maxExpand: 1000,
};

function validateLatex(latex) {
  if (typeof latex !== 'string' || !latex.trim()) throw new Error('Invalid math: an empty formula is not allowed');
  if (/<\/?[a-z][^>]*>/i.test(latex)) throw new Error('Invalid math: rich text tags are not allowed inside formulas');
}

export function latexToMathML(latex, options = {}) {
  validateLatex(latex);
  const output = katex.renderToString(latex, { ...mathOptions, displayMode: Boolean(options.displayMode), output: 'mathml' });
  return output.slice(output.indexOf('<math'), output.lastIndexOf('</math>') + 7);
}

export function splitMath(text) {
  const result = [];
  let cursor = 0;
  while (cursor < text.length) {
    const start = text.indexOf('\\(', cursor);
    const strayEnd = text.indexOf('\\)', cursor);
    if (strayEnd >= 0 && (start < 0 || strayEnd < start)) throw new Error('Unbalanced math delimiter \\)');
    if (start < 0) { result.push([false, text.slice(cursor)]); break; }
    if (start > cursor) result.push([false, text.slice(cursor, start)]);
    const end = text.indexOf('\\)', start + 2);
    if (end < 0 || text.indexOf('\\(', start + 2) >= 0 && text.indexOf('\\(', start + 2) < end) throw new Error('Unbalanced math delimiter \\(');
    result.push([true, text.slice(start + 2, end)]);
    cursor = end + 2;
  }
  return result;
}

/** Replace delimited formulas in text nodes, preserving surrounding DOM semantics.
 * All formulas validate before any DOM mutation. Repeated calls are idempotent.
 */
export function renderMathInElement(root, options = {}) {
  const doc = root.ownerDocument;
  if (!doc) throw new TypeError('renderMathInElement expects a DOM element');
  const walker = doc.createTreeWalker(root, 4);
  const edits = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (node.parentElement.closest('[data-pptd-math],script,style,textarea')) continue;
    if (!node.data.includes('\\(') && !node.data.includes('\\)')) continue;
    const parts = splitMath(node.data);
    const computed = doc.defaultView.getComputedStyle(node.parentElement);
    const fragment = doc.createDocumentFragment();
    for (const [isMath, text] of parts) {
      if (!isMath) { fragment.append(doc.createTextNode(text)); continue; }
      validateLatex(text);
      const span = doc.createElement('span');
      span.dataset.pptdMath = text;
      // Reset typography so bold, underline, superscript, font-family, and links
      // cannot alter the formula. Size and color are PPTD's only inherited styles.
      span.style.cssText = 'all:initial;display:inline-block;text-indent:0;letter-spacing:normal;text-decoration:none;vertical-align:baseline';
      span.style.fontSize = computed.fontSize;
      span.style.color = computed.color;
      katex.render(text, span, { ...mathOptions, displayMode: Boolean(options.displayMode), output: 'htmlAndMathml' });
      fragment.append(span);
    }
    edits.push([node, fragment, parts.filter(([math]) => math).length]);
  }
  for (const [node, fragment] of edits) node.replaceWith(fragment);
  return edits.reduce((sum, edit) => sum + edit[2], 0);
}

let iconIndex;
export function exportIconIndex() {
  if (iconIndex) return iconIndex;
  iconIndex = Object.create(null);
  for (const collection of [fas, far, fab]) {
    for (const definition of Object.values(collection)) {
      const [width, height, aliases, , path] = definition.icon;
      const record = { width, height, paths: Array.isArray(path) ? path : [path] };
      iconIndex[`${definition.prefix}:${definition.iconName}`] = record;
      for (const alias of aliases) if (typeof alias === 'string') iconIndex[`${definition.prefix}:${alias}`] = record;
    }
  }
  return iconIndex;
}

function color(value, theme) {
  const seen = new Set();
  while (typeof value === 'string' && value.startsWith('$')) {
    if (seen.has(value)) throw new Error(`Cyclic theme color ${value}`);
    seen.add(value);
    value = theme.colors?.[value.slice(1)];
  }
  if (!/^#[\da-f]{6}([\da-f]{2})?$/i.test(value)) throw new Error(`Invalid icon color ${value}`);
  return value;
}

/** SVG markup retains iconName and independent path elements. Position and
 * rotation belong to the containing PPTD element, like other SVG previews.
 */
export function renderIcon(element, theme = {}, options = {}) {
  const icon = exportIconIndex()[element.iconName];
  if (!icon) throw new Error(`Unknown Font Awesome 7 Free icon: ${element.iconName}`);
  const fill = element.fill ?? { type: 'solid', color: '#000000' };
  let paint = 'none', defs = '';
  if (fill.type === 'solid') paint = color(fill.color, theme);
  else if (fill.type === 'gradient') {
    const stops = fill.stops ?? fill.colors?.map((value, i, arr) => ({ position: i / (arr.length - 1 || 1), color: value }));
    if (!Array.isArray(stops) || stops.length < 2 || stops.some((s) => !(s.position >= 0 && s.position <= 1))) throw new Error('Icon gradient requires at least two stops within [0, 1]');
    const id = `pptd-icon-${String(element.elementId ?? element.iconName).replace(/[^\w-]/g, '-')}`;
    const angle = Number(fill.angle ?? 0) * Math.PI / 180;
    const x = Math.cos(angle) / 2, y = Math.sin(angle) / 2;
    const radial = fill.gradientType === 'radial';
    const tag = radial ? 'radialGradient' : 'linearGradient';
    defs = `<defs><${tag} id="${id}" ${radial ? 'cx="0.5" cy="0.5" r="0.5"' : `x1="${0.5 - x}" y1="${0.5 - y}" x2="${0.5 + x}" y2="${0.5 + y}"`}>${stops.map((s) => `<stop offset="${Number(s.position)}" stop-color="${color(s.color, theme)}"/>`).join('')}</${tag}></defs>`;
    paint = `url(#${id})`;
  } else if (fill.type === 'image') {
    if (typeof fill.src !== 'string' || /^(?:javascript|data|file):/i.test(fill.src)) throw new Error('Invalid icon image-fill source');
    if (fill.crop && Object.values(fill.crop).some((v) => Number(v) !== 0) && !options.imageSize) throw new Error('Cropped icon image fills require a resolved image asset');
    const id = `pptd-icon-${String(element.elementId ?? element.iconName).replace(/[^\w-]/g, '-')}`;
    const mode = fill.fit?.mode ?? 'cover';
    const ratio = { fill: 'none', contain: 'xMidYMid meet', cover: 'xMidYMid slice' }[mode];
    if (!ratio) throw new Error('Invalid icon image-fill fit mode');
    const [targetWidth, targetHeight] = element.bounds?.slice(2) || [icon.width, icon.height];
    const [imageWidth, imageHeight] = options.imageSize || [targetWidth, targetHeight];
    const crop = { left: 0, top: 0, right: 0, bottom: 0, ...fill.crop };
    const cropWidth = imageWidth * (1 - crop.left - crop.right), cropHeight = imageHeight * (1 - crop.top - crop.bottom);
    if (!(cropWidth > 0 && cropHeight > 0)) throw new Error('Degenerate icon image crop');
    const image = options.imageSize
      ? `<svg width="${targetWidth}" height="${targetHeight}" viewBox="${imageWidth * crop.left} ${imageHeight * crop.top} ${cropWidth} ${cropHeight}" preserveAspectRatio="${ratio}"><image href="${escapeXml(fill.src)}" width="${imageWidth}" height="${imageHeight}"/></svg>`
      : `<image href="${escapeXml(fill.src)}" width="${targetWidth}" height="${targetHeight}" preserveAspectRatio="${ratio}"/>`;
    defs = `<defs><pattern id="${id}" width="1" height="1" patternContentUnits="userSpaceOnUse"><svg width="${icon.width}" height="${icon.height}" viewBox="0 0 ${targetWidth} ${targetHeight}" preserveAspectRatio="none" opacity="${Number(fill.opacity ?? 1)}">${image}</svg></pattern></defs>`;
    paint = `url(#${id})`;
  } else if (fill.type !== 'none') throw new Error(`Unsupported icon fill ${fill.type}`);
  const border = element.border;
  const stroke = border ? ` stroke="${color(border.color ?? '#000000', theme)}" stroke-width="${Number(border.width ?? 1)}" vector-effect="non-scaling-stroke"${border.style === 'dash' ? ' stroke-dasharray="6 4"' : border.style === 'dot' ? ' stroke-dasharray="1 3"' : ''}` : '';
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${icon.width} ${icon.height}" width="100%" height="100%" preserveAspectRatio="none" data-icon-name="${escapeXml(element.iconName)}" role="img" aria-label="${escapeXml(element.iconName)}">${defs}${icon.paths.map((path) => `<path d="${escapeXml(path)}" fill="${paint}"${stroke}/>`).join('')}</svg>`;
}

// Avoid importing Node builtins: this same module is bundled for the browser.
if (typeof process !== 'undefined' && process.argv?.[1]?.replaceAll('\\', '/').endsWith('/math-icons.js')) {
  const mode = process.argv[2];
  if (mode === '--icons') process.stdout.write(JSON.stringify(exportIconIndex()));
  else if (mode === '--mathml') {
    (async () => {
      let input = '';
      for await (const chunk of process.stdin) input += chunk;
      try { process.stdout.write(latexToMathML(input)); }
      catch (error) { process.stderr.write(error.message); process.exitCode = 1; }
    })();
  }
}
