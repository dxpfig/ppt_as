export { renderMathInElement, renderIcon, latexToMathML, splitMath } from './math-icons.js';
export { renderChart, renderChartSvg, buildChartOption, validateChart } from './charts.js';
export { createAnimationPlayer } from './animations.js';

/** Route literal rich text through the PPTD latin/ea families. Formula internals
 * and explicitly styled inline font families keep their own typography.
 */
export function routeFontScripts(root) {
	const doc = root.ownerDocument;
	const walker = doc.createTreeWalker(root, 4);
	const edits = [];
	const eastAsian =
		/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}\p{Script=Bopomofo}\u3000-\u303f\uff00-\uffef]/u;
	const extender = /[\p{Mark}\u200d\ufe00-\ufe0f]/u;
	while (walker.nextNode()) {
		const node = walker.currentNode;
		if (
			!node.data ||
			node.parentElement.closest('[data-pptd-math],[data-pptd-font-script],script,style')
		)
			continue;
		let owner = node.parentElement;
		while (owner && !owner.dataset.fontRouting) {
			if (owner.style.fontFamily) {
				owner = null;
				break;
			}
			owner = owner.parentElement;
		}
		if (!owner) continue;
		const fonts = JSON.parse(owner.dataset.fontRouting);
		const chunks = [];
		for (const char of node.data) {
			const previous = chunks.at(-1);
			const kind =
				extender.test(char) && previous
					? previous.kind
					: eastAsian.test(char)
						? 'ea'
						: 'latin';
			if (previous?.kind === kind) previous.text += char;
			else chunks.push({ kind, text: char });
		}
		const fragment = doc.createDocumentFragment();
		for (const { kind, text } of chunks) {
			const span = doc.createElement('span');
			span.dataset.pptdFontScript = kind;
			span.style.fontFamily = JSON.stringify(fonts[kind] || fonts.latin || fonts.ea);
			span.textContent = text;
			fragment.append(span);
		}
		edits.push([node, fragment]);
	}
	for (const [node, fragment] of edits) node.replaceWith(fragment);
	return edits.length;
}
